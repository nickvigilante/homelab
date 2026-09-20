# In-cluster Grafana and Kubernetes MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Coder Agents chats and Claude Code in a Coder workspace read-only access to the gandalf cluster and its Grafana through two in-cluster MCP servers, with a laptop break-glass path for when Coder is down.

**Architecture:** Two Flux `HelmRelease`s (`kubernetes-mcp-server` and `grafana-mcp` charts) in a new `claude-mcp` namespace, each with a fixed ClusterIP, gated by a default-deny NetworkPolicy that admits only the `coder` namespace.
Both are registered in Coder Agents through the infrastructure repo with no auth, and Claude Code entries plus a `kubectl port-forward` helper come from the dotfiles repo.

**Tech Stack:** Flux 2 (`HelmRelease`, OCI and HTTP `HelmRepository`), Helm charts, External Secrets Operator with Bitwarden Secrets Manager, Kubernetes NetworkPolicy, OpenTofu with the `coder/coderd` provider, chezmoi, bash.

**Spec:** `docs/superpowers/specs/2026-09-19-claude-mcp-in-cluster-design.md` (homelab repo).

## Global Constraints

- Chart pins: `kubernetes-mcp-server` chart `0.1.0` from `oci://ghcr.io/containers/charts`, `grafana-mcp` chart `0.24.0` from `https://grafana-community.github.io/helm-charts`.
- Image pins: `quay.io/containers/kubernetes_mcp_server:v0.0.67`, `docker.io/grafana/mcp-grafana:1.5.1`.
  Never `latest`.
- Namespace `claude-mcp`.
  Services `kubernetes-mcp` (port 8080, fixed ClusterIP `10.43.0.201`) and `grafana-mcp` (port 8000, fixed ClusterIP `10.43.0.200`).
  ServiceAccount `claude-mcp`, bound to the built-in `view` ClusterRole.
- Registered URLs: `http://kubernetes-mcp.claude-mcp.svc.cluster.local:8080/mcp` and `http://grafana-mcp.claude-mcp.svc.cluster.local:8000/mcp`.
- Kubernetes server: `read_only = true`, `toolsets = ["core"]`, `stateless = true`, Secrets in `denied_resources`.
- Grafana server: `disableWrite: true` and `--allowed-hosts` set explicitly.
- Chart defaults that must be overridden: Kubernetes chart `ingress.enabled: false`; Grafana chart top-level `automountServiceAccountToken: false` and `--transport=streamable-http` (the image entrypoint defaults to `sse`).
- Access gate: NetworkPolicy default-deny ingress plus one allow rule for the `coder` namespace.
  No Traefik hostname, no Cloudflare, no caller-authentication token.
- Break-glass: `kubectl port-forward` with the admin kubeconfig `~/.kube/homelab.yaml`, local ports equal to container ports (8080 and 8000).
- Secrets: nothing secret is committed.
  The Grafana Viewer token lives in the Bitwarden vault item `Homelab Grafana` (field `mcp-sa-token`) and in the BWS `homelab` project, and reaches the cluster only through an `ExternalSecret` on the `bitwarden` ClusterSecretStore.
  BWS UUIDs carry `# gitleaks:allow`.
- Repo conventions:
  - Work in a git worktree under `.worktrees/<branch>`, never on `main`.
  - Conventional commits ending with the trailer `Assisted-by: AI`, and PR bodies ending with `---` then `🤖 Built with AI assistance.`
  - The homelab repo forbids `Co-Authored-By` trailers.
  - Markdown is one sentence per line, formatted by mdformat, so run pre-commit twice on Markdown.
  - Pass `SKIP=yamlfmt` locally only where `yamlfmt` is not installed, because CI runs it.
  - Never name a specific AI model, vendor, or product in any commit or PR.

______________________________________________________________________

## File Structure

| Repo           | File                                                                   | Responsibility                                                                                           |
| -------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| dotfiles       | `home/dot_config/dotfiles/Brewfile.tmpl`                               | Declare `kubernetes-cli` so `kubectl` exists on the laptop                                               |
| dotfiles       | `home/dot_local/bin/executable_mcp-breakglass`                         | Start, stop, and report the two port-forwards                                                            |
| dotfiles       | `home/.chezmoiignore`                                                  | Deploy the helper only where the admin kubeconfig is deployed                                            |
| dotfiles       | `home/run_onchange_after_09-register-claude-mcp.sh.tmpl`               | Register the two servers with Claude Code (localhost URLs on the laptop, in-cluster URLs in a workspace) |
| homelab        | `sources/kubernetes-mcp-server.yaml`, `sources/grafana-community.yaml` | The two chart repositories                                                                               |
| homelab        | `k8s/claude-mcp/namespace.yaml`                                        | The namespace                                                                                            |
| homelab        | `k8s/claude-mcp/helmrelease.yaml`                                      | Both HelmReleases as two YAML documents                                                                  |
| homelab        | `k8s/claude-mcp/external-secret.yaml`                                  | Sync the Grafana token from BWS                                                                          |
| homelab        | `k8s/claude-mcp/netpol-default-deny.yaml`, `netpol-allow-coder.yaml`   | The access gate                                                                                          |
| homelab        | `k8s/claude-mcp/kustomization.yaml`, `README.md`                       | Wiring and runbook                                                                                       |
| homelab        | `clusters/gandalf/claude-mcp.yaml`                                     | The Flux Kustomization                                                                                   |
| homelab        | `k8s/coder/helmrelease.yaml`                                           | Two more `/32` entries in `CODER_MCP_ALLOWED_PRIVATE_CIDRS`                                              |
| infrastructure | `coder/mcp_servers.tf`, `coder/README.md`                              | Register both servers in Coder Agents                                                                    |

Task order: 0 and 1 can start immediately and in parallel, 2 to 5 build the homelab change, 6 ships and verifies it in the cluster, 7 and 8 register and wire the clients, and 9 accepts the whole thing.

______________________________________________________________________

### Task 0: Grafana Viewer token (manual, user)

This task needs a person in the Grafana and Bitwarden web UIs.
An agent must stop here and hand the steps to the user, and must never ask for or print the token.

**Produces:** a Bitwarden Secrets Manager secret in the `homelab` project, whose UUID is exported as `BWS_SECRET_UUID` for Task 4.

- [ ] **Step 1: Create the service account**

In `https://grafana.vigihome.net`: Administration, Users and access, Service accounts, Add service account.
Name it `claude-mcp` and set the role to `Viewer`.

- [ ] **Step 2: Create its token**

On that service account choose Add service account token, name it `mcp`, and generate it with no expiry.
Copy the token once, since Grafana never shows it again.

- [ ] **Step 3: Store it in the vault**

In the Bitwarden password manager, open the item `Homelab Grafana` and add a hidden custom field named `mcp-sa-token` holding the token.
This is the disaster-recovery mirror.

- [ ] **Step 4: Store the cluster-readable copy**

In Bitwarden Secrets Manager, open the project `homelab` and create a secret named `grafana-mcp-sa-token` with the same token.
Use the secret's menu to copy its ID (a UUID), then in your shell run:

```bash
export BWS_SECRET_UUID='<the copied UUID>'
```

- [ ] **Step 5: Verify**

Confirm in the web UI that the secret sits in the `homelab` project, because `flux-eso` only has Read access to that project.
Expected: the secret is listed under `homelab`, and `echo "$BWS_SECRET_UUID"` prints a UUID.

______________________________________________________________________

### Task 1: Declare `kubectl` in the dotfiles Brewfile

`kubectl` is not on the laptop, and the break-glass helper, the `kubectl kustomize` render checks in Tasks 2 to 5, and the live checks in Task 6 all need it.
Installs go through the Brewfile, never a bare `brew install`.

**Files:**

- Modify: `~/git/nickvigilante/dotfiles/home/dot_config/dotfiles/Brewfile.tmpl` (personal-only formulae block)

**Produces:** `kubectl` on the personal laptop after `chezmoi apply`.

- [ ] **Step 1: Create the worktree**

```bash
cd ~/git/nickvigilante/dotfiles
git fetch origin
git worktree add .worktrees/feat/kubectl-brewfile -b feat/kubectl-brewfile origin/main
cd .worktrees/feat/kubectl-brewfile
```

- [ ] **Step 2: Write the failing check**

```bash
chezmoi execute-template < home/dot_config/dotfiles/Brewfile.tmpl | grep -c 'brew "kubernetes-cli"'
```

Expected: `0`, and grep exits 1.

- [ ] **Step 3: Add the formula**

In the `{{ if eq .profile "personal" -}}` block, directly after the `brew "opentofu"` line, add:

```ruby
brew "kubernetes-cli" # kubectl — reach the homelab cluster and run the MCP break-glass port-forwards
```

- [ ] **Step 4: Run the check to verify it passes**

```bash
chezmoi execute-template < home/dot_config/dotfiles/Brewfile.tmpl | grep -c 'brew "kubernetes-cli"'
```

Expected: `1`.

- [ ] **Step 5: Commit and open the PR**

```bash
git add home/dot_config/dotfiles/Brewfile.tmpl
git commit -m "feat(packages): declare kubectl on the personal profile" -m "The MCP break-glass helper and the homelab manifest checks need kubectl, which was not declared anywhere." -m "Assisted-by: AI"
git push -u origin feat/kubectl-brewfile
```

Open the PR with `gh pr create` and a body that ends with `---` then `🤖 Built with AI assistance.`
After it merges, run `cd ~/git/nickvigilante/dotfiles && git pull --ff-only && chezmoi apply`.

- [ ] **Step 6: Verify the install**

```bash
kubectl version --client
KUBECONFIG=~/.kube/homelab.yaml kubectl get nodes
```

Expected: a client version prints, and three nodes are listed (gandalf, frodo, samwise) while on the tailnet.

______________________________________________________________________

### Task 2: Namespace, chart sources, and the Flux Kustomization

**Files:**

- Create: `k8s/claude-mcp/namespace.yaml`
- Create: `k8s/claude-mcp/kustomization.yaml`
- Create: `sources/kubernetes-mcp-server.yaml`
- Create: `sources/grafana-community.yaml`
- Create: `clusters/gandalf/claude-mcp.yaml`

**Produces:** a `k8s/claude-mcp` kustomize directory that Tasks 3 to 5 append resources to, and a Flux Kustomization named `claude-mcp` that reconciles it.

- [ ] **Step 1: Create the worktree**

```bash
cd ~/git/nickvigilante/homelab
git fetch origin
git worktree add .worktrees/feat/claude-mcp-cluster -b feat/claude-mcp-cluster origin/main
cd .worktrees/feat/claude-mcp-cluster
```

- [ ] **Step 2: Write the failing check**

```bash
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s/claude-mcp
```

Expected: FAIL with `no such file or directory` because the directory does not exist yet.

- [ ] **Step 3: Create the namespace**

`k8s/claude-mcp/namespace.yaml`:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: claude-mcp
```

- [ ] **Step 4: Create the two chart sources**

`sources/kubernetes-mcp-server.yaml`:

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: kubernetes-mcp-server
  namespace: flux-system
spec:
  type: oci
  interval: 1h
  url: oci://ghcr.io/containers/charts
```

`sources/grafana-community.yaml`:

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: grafana-community
  namespace: flux-system
spec:
  interval: 1h
  url: https://grafana-community.github.io/helm-charts
```

- [ ] **Step 5: Create the kustomization**

`k8s/claude-mcp/kustomization.yaml`:

```yaml
# In-cluster MCP servers for Coder Agents and Claude Code (#184, #186).
# Both servers are Flux HelmReleases; the namespace, NetworkPolicies, and
# ExternalSecret are the pieces no chart provides.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - ../../sources/kubernetes-mcp-server.yaml
  - ../../sources/grafana-community.yaml
```

- [ ] **Step 6: Create the Flux Kustomization**

`clusters/gandalf/claude-mcp.yaml`:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: claude-mcp
  namespace: flux-system
spec:
  interval: 10m
  path: ./k8s/claude-mcp
  # Flux owns the whole directory, so pruning is safe.
  prune: true
  wait: true
  timeout: 5m
  sourceRef:
    kind: GitRepository
    name: flux-system
  # The ExternalSecret needs its ClusterSecretStore, which the
  # infrastructure Kustomization provides.
  dependsOn:
    - name: infrastructure
```

- [ ] **Step 7: Run the check to verify it passes**

```bash
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s/claude-mcp | grep -E '^kind:|^  name:'
```

Expected: one `Namespace` named `claude-mcp` and two `HelmRepository` objects named `kubernetes-mcp-server` and `grafana-community`.

- [ ] **Step 8: Commit**

```bash
SKIP=yamlfmt pre-commit run --files k8s/claude-mcp/namespace.yaml k8s/claude-mcp/kustomization.yaml sources/kubernetes-mcp-server.yaml sources/grafana-community.yaml clusters/gandalf/claude-mcp.yaml
git add k8s/claude-mcp sources clusters/gandalf/claude-mcp.yaml
git commit -m "feat(claude-mcp): add the namespace, chart sources, and Flux Kustomization" -m "Refs #184, #186" -m "Assisted-by: AI"
```

______________________________________________________________________

### Task 3: Kubernetes MCP HelmRelease

**Files:**

- Create: `k8s/claude-mcp/helmrelease.yaml` (first document)
- Modify: `k8s/claude-mcp/kustomization.yaml`

**Consumes:** the `claude-mcp` namespace and the `kubernetes-mcp-server` `HelmRepository` from Task 2.
**Produces:** a `HelmRelease` named `kubernetes-mcp`, a Service `kubernetes-mcp` on port 8080 with ClusterIP `10.43.0.201`, and a ServiceAccount `claude-mcp` bound to `view`.

- [ ] **Step 1: Write the failing render check**

Save this as `$TMPDIR/check-k8s.sh` (it is not committed):

```bash
#!/usr/bin/env bash
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
export DOCKER_CONFIG="$(mktemp -d)"   # anonymous OCI pull, ignoring any Docker Desktop credential helper
values="$TMPDIR/kubernetes-mcp-values.yaml"
uv run --quiet --with pyyaml python3 - <<'PY' > "$values"
import sys, yaml
for d in yaml.safe_load_all(open("k8s/claude-mcp/helmrelease.yaml")):
    if d and d["metadata"]["name"] == "kubernetes-mcp":
        yaml.safe_dump(d["spec"]["values"], sys.stdout)
PY
OUT="$TMPDIR/kubernetes-mcp-rendered.yaml"
helm template kubernetes-mcp oci://ghcr.io/containers/charts/kubernetes-mcp-server --version 0.1.0 \
  -n claude-mcp -f "$values" > "$OUT" || { echo "FAIL helm template"; exit 1; }

FAIL=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; FAIL=1; fi; }
check "no Ingress is rendered"            '! grep -q "^kind: Ingress" "$OUT"'
check "image is pinned to v0.0.67"        'grep -q "quay.io/containers/kubernetes_mcp_server:v0.0.67" "$OUT"'
check "server is read-only"               'grep -q "read_only = true" "$OUT"'
check "only the core toolset"             'grep -q "toolsets = \[\"core\"\]" "$OUT"'
check "Secrets are denied"                'grep -q "kind = \"Secret\"" "$OUT"'
check "runs statelessly"                  'grep -q "stateless = true" "$OUT"'
check "ServiceAccount is claude-mcp"      'grep -q "serviceAccountName: claude-mcp" "$OUT"'
check "binds the built-in view role"      'awk "/^kind: ClusterRoleBinding/{f=1} f" "$OUT" | grep -q "name: view"'
check "pinned to gandalf"                 'grep -q "kubernetes.io/hostname: gandalf" "$OUT"'
check "service is kubernetes-mcp:8080"    'awk "/^kind: Service\$/{f=1} f" "$OUT" | grep -q "port: 8080"'
check "runs as non-root"                  'grep -q "runAsNonRoot: true" "$OUT"'
check "read-only root filesystem"         'grep -q "readOnlyRootFilesystem: true" "$OUT"'
check "RuntimeDefault seccomp profile"    'grep -q "type: RuntimeDefault" "$OUT"'
exit $FAIL
```

- [ ] **Step 2: Run it to verify it fails**

```bash
bash "$TMPDIR/check-k8s.sh"
```

Expected: FAIL, with a Python error because `k8s/claude-mcp/helmrelease.yaml` does not exist.

- [ ] **Step 3: Write the HelmRelease**

`k8s/claude-mcp/helmrelease.yaml`:

```yaml
# Read-only Kubernetes MCP for Coder Agents and Claude Code (#186).
# Reached only from the coder namespace (netpol-allow-coder.yaml). It runs as
# its own ServiceAccount, bound to the built-in `view` ClusterRole, which
# omits Secrets.
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: kubernetes-mcp
  namespace: claude-mcp
spec:
  releaseName: kubernetes-mcp
  targetNamespace: claude-mcp
  interval: 30m
  chart:
    spec:
      chart: kubernetes-mcp-server
      version: "0.1.0"
      sourceRef:
        kind: HelmRepository
        name: kubernetes-mcp-server
        namespace: flux-system
  # The chart's Service template has no clusterIP field. Coder's SSRF guard
  # allowlists exact /32 addresses, so pin the address with a patch.
  postRenderers:
    - kustomize:
        patches:
          - target:
              kind: Service
              name: kubernetes-mcp
            patch: |
              - op: add
                path: /spec/clusterIP
                value: 10.43.0.201
  values:
    fullnameOverride: kubernetes-mcp
    # The chart enables an Ingress by default. This server is cluster-internal.
    ingress:
      enabled: false
    # The chart's image.version defaults to `latest`.
    image:
      version: v0.0.67
    nodeSelector:
      kubernetes.io/hostname: gandalf
    serviceAccount:
      name: claude-mcp
    rbac:
      extraClusterRoleBindings:
        - name: view
          roleRef:
            name: view
            external: true
    config:
      port: "{{ .Values.service.port }}"
      read_only: true
      stateless: true
      toolsets:
        - core
      denied_resources:
        - group: ""
          version: v1
          kind: Secret
```

- [ ] **Step 4: Run the render check to verify it passes**

```bash
bash "$TMPDIR/check-k8s.sh"
```

Expected: thirteen `ok` lines and no `FAIL`.

- [ ] **Step 5: Verify the ClusterIP patch applies**

```bash
mkdir -p "$TMPDIR/patchtest" && cp "$TMPDIR/kubernetes-mcp-rendered.yaml" "$TMPDIR/patchtest/rendered.yaml"
cat > "$TMPDIR/patchtest/kustomization.yaml" <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - rendered.yaml
patches:
  - target:
      kind: Service
      name: kubernetes-mcp
    patch: |
      - op: add
        path: /spec/clusterIP
        value: 10.43.0.201
EOF
kubectl kustomize "$TMPDIR/patchtest" | grep -n 'clusterIP'
```

Expected: `clusterIP: 10.43.0.201`.

- [ ] **Step 6: Wire it into the kustomization**

In `k8s/claude-mcp/kustomization.yaml`, add `- helmrelease.yaml` to `resources`, after `namespace.yaml`.

- [ ] **Step 7: Verify the kustomization still builds**

```bash
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s/claude-mcp | grep -E '^kind: HelmRelease'
```

Expected: `kind: HelmRelease` appears once.

- [ ] **Step 8: Commit**

```bash
SKIP=yamlfmt pre-commit run --files k8s/claude-mcp/helmrelease.yaml k8s/claude-mcp/kustomization.yaml
git add k8s/claude-mcp
git commit -m "feat(claude-mcp): deploy the read-only Kubernetes MCP server" -m "Install the containers/kubernetes-mcp-server chart under Flux, bound to the built-in view role, with read_only on and Secrets denied. The chart's Ingress is disabled and its Service gets a fixed ClusterIP through a post-renderer." -m "Refs #186" -m "Assisted-by: AI"
```

______________________________________________________________________

### Task 4: Grafana MCP HelmRelease and ExternalSecret

**Files:**

- Modify: `k8s/claude-mcp/helmrelease.yaml` (append a second document)
- Create: `k8s/claude-mcp/external-secret.yaml`
- Modify: `k8s/claude-mcp/kustomization.yaml`

**Consumes:** the `grafana-community` `HelmRepository` from Task 2, and `$BWS_SECRET_UUID` from Task 0.
**Produces:** a `HelmRelease` named `grafana-mcp`, a Service `grafana-mcp` on port 8000 with ClusterIP `10.43.0.200`, and a Secret `grafana-mcp-token` with key `token`.

- [ ] **Step 1: Write the failing render check**

Save this as `$TMPDIR/check-grafana.sh` (not committed):

```bash
#!/usr/bin/env bash
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
values="$TMPDIR/grafana-mcp-values.yaml"
uv run --quiet --with pyyaml python3 - <<'PY' > "$values"
import sys, yaml
for d in yaml.safe_load_all(open("k8s/claude-mcp/helmrelease.yaml")):
    if d and d["metadata"]["name"] == "grafana-mcp":
        yaml.safe_dump(d["spec"]["values"], sys.stdout)
PY
OUT="$TMPDIR/grafana-mcp-rendered.yaml"
helm template grafana-mcp grafana-mcp --repo https://grafana-community.github.io/helm-charts --version 0.24.0 \
  -n claude-mcp -f "$values" > "$OUT" || { echo "FAIL helm template"; exit 1; }

FAIL=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; FAIL=1; fi; }
check "image is pinned to 1.5.1"          'grep -q "docker.io/grafana/mcp-grafana:1.5.1" "$OUT"'
check "writes are disabled"               'grep -q -- "--disable-write" "$OUT"'
check "streamable-http transport"         'grep -q -- "--transport=streamable-http" "$OUT"'
check "allowed hosts include the FQDN"    'grep -q -- "--allowed-hosts=grafana-mcp.claude-mcp.svc.cluster.local:8000" "$OUT"'
check "allowed hosts include localhost"   'grep -q "localhost:8000,127.0.0.1:8000" "$OUT"'
check "token comes from the Secret"       'grep -A4 "GRAFANA_SERVICE_ACCOUNT_TOKEN" "$OUT" | grep -q "name: grafana-mcp-token"'
check "token key is token"                'grep -A5 "GRAFANA_SERVICE_ACCOUNT_TOKEN" "$OUT" | grep -q "key: token"'
check "points at the kps Grafana service" 'grep -q "http://kps-grafana.monitoring.svc.cluster.local" "$OUT"'
check "no ServiceAccount token mounted"   '[ "$(grep -c "automountServiceAccountToken: false" "$OUT")" -ge 2 ]'
check "fixed ClusterIP"                   'grep -q "clusterIP: 10.43.0.200" "$OUT"'
check "service port 8000"                 'grep -q "port: 8000" "$OUT"'
check "pinned to gandalf"                 'grep -q "kubernetes.io/hostname: gandalf" "$OUT"'
check "TCP probes on the MCP port"        '[ "$(grep -c "port: mcp-http" "$OUT")" -ge 2 ]'
check "runs as non-root"                  'grep -q "runAsNonRoot: true" "$OUT"'
check "read-only root filesystem"         'grep -q "readOnlyRootFilesystem: true" "$OUT"'
check "RuntimeDefault seccomp profile"    'grep -q "type: RuntimeDefault" "$OUT"'
exit $FAIL
```

- [ ] **Step 2: Run it to verify it fails**

```bash
bash "$TMPDIR/check-grafana.sh"
```

Expected: FAIL, because the `grafana-mcp` document is not in `helmrelease.yaml` yet, so the extracted values are empty and the render lacks every setting.

- [ ] **Step 3: Append the HelmRelease document**

Append to the end of `k8s/claude-mcp/helmrelease.yaml`, after a `---` separator:

```yaml
---
# Read-only Grafana MCP for Coder Agents and Claude Code (#184).
# Reached only from the coder namespace (netpol-allow-coder.yaml). It calls
# Grafana in-cluster with a Viewer service-account token, so it bypasses
# Traefik's Authentik forward-auth and the token is the only credential.
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: grafana-mcp
  namespace: claude-mcp
spec:
  releaseName: grafana-mcp
  targetNamespace: claude-mcp
  interval: 30m
  chart:
    spec:
      chart: grafana-mcp
      version: "0.24.0"
      sourceRef:
        kind: HelmRepository
        name: grafana-community
        namespace: flux-system
  values:
    fullnameOverride: grafana-mcp
    nodeSelector:
      kubernetes.io/hostname: gandalf
    # The chart mounts a ServiceAccount token at pod level by default, and this
    # server never talks to the Kubernetes API.
    automountServiceAccountToken: false
    service:
      clusterIP: 10.43.0.200
    grafana:
      url: http://kps-grafana.monitoring.svc.cluster.local
      apiKeySecret:
        name: grafana-mcp-token
        key: token
    disableWrite: true
    # The image entrypoint defaults to `--transport sse`, and the chart appends
    # extraArgs after it, so the later flag wins. Host validation is on for
    # every route, so list every name a caller can use: the in-cluster names,
    # the fixed ClusterIP, and loopback for the break-glass port-forward.
    extraArgs:
      - --transport=streamable-http
      - --allowed-hosts=grafana-mcp.claude-mcp.svc.cluster.local:8000,grafana-mcp.claude-mcp.svc:8000,10.43.0.200:8000,localhost:8000,127.0.0.1:8000
    # The chart ships no probes. Host validation would reject an HTTP probe.
    readinessProbe:
      tcpSocket:
        port: mcp-http
    livenessProbe:
      tcpSocket:
        port: mcp-http
    resources:
      requests:
        cpu: 50m
        memory: 64Mi
      limits:
        memory: 256Mi
    # The chart's pod securityContext has no seccomp profile. Overriding it
    # replaces the whole map, so its other defaults are repeated here.
    securityContext:
      fsGroup: 1000
      runAsNonRoot: true
      runAsUser: 1000
      runAsGroup: 1000
      seccompProfile:
        type: RuntimeDefault
```

- [ ] **Step 4: Run the render check to verify it passes**

```bash
bash "$TMPDIR/check-grafana.sh"
```

Expected: sixteen `ok` lines and no `FAIL`.

- [ ] **Step 5: Create the ExternalSecret**

`k8s/claude-mcp/external-secret.yaml`:

```yaml
# The Grafana Viewer service-account token for mcp-grafana, sourced from BWS.
# Vault item `Homelab Grafana`, field `mcp-sa-token`, is the DR mirror.
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: grafana-mcp-token
  namespace: claude-mcp
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: bitwarden
    kind: ClusterSecretStore
  target:
    name: grafana-mcp-token
    creationPolicy: Owner
  data:
    - secretKey: token
      remoteRef:
        key: BWS_SECRET_UUID # gitleaks:allow
```

Then substitute the real UUID from Task 0:

```bash
test -n "$BWS_SECRET_UUID" || { echo "run Task 0 first"; exit 1; }
sed -i '' "s/key: BWS_SECRET_UUID/key: $BWS_SECRET_UUID/" k8s/claude-mcp/external-secret.yaml
grep -n 'key:' k8s/claude-mcp/external-secret.yaml
```

Expected: the `key:` line holds a UUID, and the literal `BWS_SECRET_UUID` no longer appears.

- [ ] **Step 6: Wire both into the kustomization**

In `k8s/claude-mcp/kustomization.yaml`, add `- external-secret.yaml` to `resources`, after `helmrelease.yaml`.

- [ ] **Step 7: Verify the build and that no secret value is present**

```bash
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s/claude-mcp | grep -E '^kind: (HelmRelease|ExternalSecret)' | sort | uniq -c
```

Expected: two `HelmRelease` and one `ExternalSecret`.

- [ ] **Step 8: Commit**

```bash
SKIP=yamlfmt pre-commit run --files k8s/claude-mcp/helmrelease.yaml k8s/claude-mcp/external-secret.yaml k8s/claude-mcp/kustomization.yaml
git add k8s/claude-mcp
git commit -m "feat(claude-mcp): deploy the read-only Grafana MCP server" -m "Install the grafana-mcp chart under Flux with writes disabled, streamable-http, an explicit Host allowlist, and TCP probes. The Viewer token reaches it through an ExternalSecret from BWS." -m "Refs #184" -m "Assisted-by: AI"
```

The pre-commit betterleaks hook must pass, since the BWS UUID carries `# gitleaks:allow`.

______________________________________________________________________

### Task 5: NetworkPolicies, Coder allowlist, and README

**Files:**

- Create: `k8s/claude-mcp/netpol-default-deny.yaml`
- Create: `k8s/claude-mcp/netpol-allow-coder.yaml`
- Create: `k8s/claude-mcp/README.md`
- Modify: `k8s/claude-mcp/kustomization.yaml`
- Modify: `k8s/coder/helmrelease.yaml` (the `CODER_MCP_ALLOWED_PRIVATE_CIDRS` env var)

**Produces:** the access gate, and the Coder allowlist that lets Coder's MCP client reach `10.43.0.200` and `10.43.0.201`.

- [ ] **Step 1: Write the failing check**

```bash
grep -n 'CODER_MCP_ALLOWED_PRIVATE_CIDRS' -A 1 k8s/coder/helmrelease.yaml | grep -c '10.43.0.200/32'
```

Expected: `0`, and grep exits 1.

- [ ] **Step 2: Create the default-deny policy**

`k8s/claude-mcp/netpol-default-deny.yaml`:

```yaml
# Nothing reaches the MCP servers unless a policy below allows it.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: claude-mcp
spec:
  podSelector: {}
  policyTypes:
    - Ingress
```

- [ ] **Step 3: Create the allow policy**

`k8s/claude-mcp/netpol-allow-coder.yaml`:

```yaml
# The coder namespace holds the Coder server (Coder Agents' MCP client) and
# every workspace pod (Claude Code). Ports are the containers' ports:
# 8080 for kubernetes-mcp and 8000 for grafana-mcp.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-coder-namespace
  namespace: claude-mcp
spec:
  podSelector: {}
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: coder
      ports:
        - protocol: TCP
          port: 8080
        - protocol: TCP
          port: 8000
```

- [ ] **Step 4: Add both to the kustomization**

In `k8s/claude-mcp/kustomization.yaml`, add `- netpol-default-deny.yaml` and `- netpol-allow-coder.yaml` to `resources`, after `external-secret.yaml`.

- [ ] **Step 5: Extend the Coder allowlist**

In `k8s/coder/helmrelease.yaml`, change the value of `CODER_MCP_ALLOWED_PRIVATE_CIDRS` from `192.168.50.135/32,100.92.2.25/32` to:

```yaml
          value: 192.168.50.135/32,100.92.2.25/32,10.43.0.200/32,10.43.0.201/32
```

Add these lines to the end of that variable's comment block, above the `- name:` line:

```yaml
        # The last two are the fixed ClusterIPs of the in-cluster Grafana and
        # Kubernetes MCP servers (k8s/claude-mcp/), reached over plain HTTP by
        # service DNS name. Only exact /32s are listed so the rest of the
        # service CIDR stays blocked.
```

- [ ] **Step 6: Write the README**

`k8s/claude-mcp/README.md`:

```markdown
# claude-mcp

Read-only MCP servers for the homelab, reached by Coder Agents chats and by Claude Code in a Coder workspace.
Design: `docs/superpowers/specs/2026-09-19-claude-mcp-in-cluster-design.md`.
Tracking issues: #184 and #186.

## Layout

- `namespace.yaml` is the `claude-mcp` namespace.
- `helmrelease.yaml` holds two Flux `HelmRelease`s: `kubernetes-mcp` (the `containers/kubernetes-mcp-server` chart) and `grafana-mcp` (the `grafana-community` chart).
- `external-secret.yaml` syncs the Grafana Viewer token from BWS into `grafana-mcp-token`.
- `netpol-default-deny.yaml` and `netpol-allow-coder.yaml` admit only the `coder` namespace.
- `kustomization.yaml` also pulls in `../../sources/kubernetes-mcp-server.yaml` and `../../sources/grafana-community.yaml`.

Reconciled by the `claude-mcp` Flux Kustomization (`clusters/gandalf/claude-mcp.yaml`).

## Endpoints

| Server | URL | Fixed ClusterIP |
| --- | --- | --- |
| Kubernetes | `http://kubernetes-mcp.claude-mcp.svc.cluster.local:8080/mcp` | `10.43.0.201` |
| Grafana | `http://grafana-mcp.claude-mcp.svc.cluster.local:8000/mcp` | `10.43.0.200` |

Both addresses are `/32` entries in `CODER_MCP_ALLOWED_PRIVATE_CIDRS` (`k8s/coder/helmrelease.yaml`), because Coder's SSRF guard blocks private destinations otherwise.
They come from the low static band of the service CIDR, which Kubernetes keeps clear of dynamic allocation.

## Access and privilege

- Any pod in the `coder` namespace can call both servers, so a workspace has read access to the cluster (minus Secrets) and to Grafana.
  That is fine while one person holds a Coder account, and it must be revisited before adding collaborators.
- The Kubernetes server runs as ServiceAccount `claude-mcp` bound to the built-in `view` ClusterRole, with `read_only = true`, the `core` toolset only, and Secrets in `denied_resources`.
  `view` still allows `pods/log`, so log contents can reach model context.
- The Grafana server uses a Viewer service account and `--disable-write`.

## One-time setup

1. In Grafana, create a Viewer service account `claude-mcp` and a token.
2. Store the token in the vault item `Homelab Grafana` (field `mcp-sa-token`) and in the BWS `homelab` project as `grafana-mcp-sa-token`.
3. Put that secret's UUID in `external-secret.yaml`.

## Break-glass when Coder is down

From the laptop, run `mcp-breakglass up` (from the dotfiles repo).
It port-forwards both Services with the admin kubeconfig over Tailscale, on localhost ports 8080 and 8000.
Stop it with `mcp-breakglass down`.

## Rotating the Grafana token

Create a new token on the `claude-mcp` service account, update the vault field and the BWS secret, then run `flux reconcile externalsecret -n claude-mcp grafana-mcp-token` and restart the `grafana-mcp` Deployment.
```

- [ ] **Step 7: Run the checks to verify they pass**

```bash
grep -n 'CODER_MCP_ALLOWED_PRIVATE_CIDRS' -A 1 k8s/coder/helmrelease.yaml | grep -c '10.43.0.200/32,10.43.0.201/32'
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s/claude-mcp | grep -c '^kind: NetworkPolicy'
```

Expected: `1`, then `2`.

- [ ] **Step 8: Commit**

```bash
SKIP=yamlfmt pre-commit run --files k8s/claude-mcp/netpol-default-deny.yaml k8s/claude-mcp/netpol-allow-coder.yaml k8s/claude-mcp/README.md k8s/claude-mcp/kustomization.yaml k8s/coder/helmrelease.yaml
SKIP=yamlfmt pre-commit run --files k8s/claude-mcp/README.md
git add k8s/claude-mcp k8s/coder/helmrelease.yaml
git commit -m "feat(claude-mcp): gate the MCP servers to the coder namespace" -m "Default-deny ingress plus one allow rule for the coder namespace, the two fixed ClusterIPs added to Coder's MCP allowlist, and a README for the directory." -m "Refs #184, #186" -m "Assisted-by: AI"
```

______________________________________________________________________

### Task 6: Ship the manifests and verify them in the cluster

This task needs `kubectl` (Task 1) and the tailnet.
Run every `kubectl` command with `export KUBECONFIG=~/.kube/homelab.yaml`.

**Files:** none new.

- [ ] **Step 1: Check cluster facts the plan assumed**

```bash
export KUBECONFIG=~/.kube/homelab.yaml
kubectl -n monitoring get svc kps-grafana -o jsonpath='{.spec.ports[0].port}{"\n"}'
kubectl get svc -A -o jsonpath='{range .items[*]}{.spec.clusterIP}{"\n"}{end}' | grep -E '^10\.43\.0\.(200|201)$' || echo "both IPs free"
kubectl -n kube-system get pods -o name | head -3
```

Expected: `80`, then `both IPs free`.
If `kps-grafana` does not exist, find the real name with `kubectl -n monitoring get svc | grep -i grafana` and fix `grafana.url` in `helmrelease.yaml`.
If either IP is taken, pick two free addresses in `10.43.0.100` to `10.43.0.250`, and update the four places they appear: both HelmRelease blocks, the `--allowed-hosts` line, and the Coder allowlist.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin feat/claude-mcp-cluster
```

Open the PR with `gh pr create --repo nickvigilante/homelab` using the repo's template.
The body must include a Summary, the "Before merge" checklist with backup wiring marked not applicable, and a Test plan, and it must end with `---` then `🤖 Built with AI assistance.`
Wait for CI (`kubeconform`, `yamlfmt`, `betterleaks`) to pass, then ask the user to merge it.
Do not ask while CI is red.

- [ ] **Step 3: Reconcile**

```bash
git -C ~/git/nickvigilante/homelab pull --ff-only
kubectl -n flux-system annotate gitrepository/flux-system reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite
sleep 60
kubectl -n flux-system get kustomization claude-mcp
kubectl -n claude-mcp get helmrelease,externalsecret,pods,svc
```

Expected: the Kustomization is `Ready=True`, both HelmReleases are `Ready=True`, the ExternalSecret shows `SecretSynced`, both pods are `Running 1/1`, and the two Services show the fixed ClusterIPs.
Reconciliation can take up to ten minutes without the annotation.

- [ ] **Step 4: If the pods are not Ready, check the probes under default-deny**

```bash
kubectl -n claude-mcp describe pod -l app.kubernetes.io/name=grafana-mcp | grep -A3 -i 'probe failed\|Unhealthy'
kubectl -n claude-mcp describe pod -l app.kubernetes.io/name=kubernetes-mcp-server | grep -A3 -i 'probe failed\|Unhealthy'
```

If kubelet's probes are blocked by the default-deny policy, find the node's pod-network gateway address and allow it:

```bash
kubectl get node gandalf -o jsonpath='{.spec.podCIDR}{"\n"}'
```

Add an `ipBlock` entry with that CIDR to the `from:` list of `netpol-allow-coder.yaml`, commit it as `fix(claude-mcp): admit kubelet probes under default-deny`, and repeat Step 3.
If the pods are Ready, skip this step.

- [ ] **Step 5: RBAC checks**

```bash
SA=system:serviceaccount:claude-mcp:claude-mcp
kubectl auth can-i list pods --all-namespaces --as=$SA
kubectl auth can-i get secrets --all-namespaces --as=$SA
kubectl auth can-i create deployments -n default --as=$SA
kubectl auth can-i delete pods -n kube-system --as=$SA
```

Expected: `yes`, `no`, `no`, `no`.

- [ ] **Step 6: Reachability from the coder namespace**

```bash
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
for target in "kubernetes-mcp.claude-mcp.svc.cluster.local:8080" "grafana-mcp.claude-mcp.svc.cluster.local:8000"; do
  kubectl -n coder run mcp-probe --rm -i --restart=Never --image=curlimages/curl:8.10.1 \
    --env="INIT=$INIT" --env="TARGET=$target" --command -- \
    sh -c 'curl -s -m 10 -o /dev/null -w "$TARGET %{http_code}\n" -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "$INIT" "http://$TARGET/mcp"'
done
```

Expected: both lines end in `200`.

- [ ] **Step 7: The gate blocks other namespaces**

```bash
kubectl -n default run mcp-probe --rm -i --restart=Never --image=curlimages/curl:8.10.1 --command -- \
  sh -c "curl -s -m 5 -o /dev/null -w '%{http_code}\n' http://kubernetes-mcp.claude-mcp.svc.cluster.local:8080/healthz || true"
```

Expected: `000` (a timeout).
If it prints `200`, NetworkPolicy is not being enforced on this cluster.
Stop and report, because the design depends on it.

- [ ] **Step 8: Host header handling**

```bash
kubectl -n coder run mcp-probe --rm -i --restart=Never --image=curlimages/curl:8.10.1 --command -- \
  sh -c "curl -s -m 10 -o /dev/null -w 'grafana bad Host: %{http_code}\n' -H 'Host: evil.example:8000' -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{}' http://grafana-mcp.claude-mcp.svc.cluster.local:8000/mcp"
```

Expected: `403` for the Grafana server.
The Kubernetes server's behavior is not documented, and Step 6 already showed it accepts the in-cluster name, which is the only name callers use, so record the result of a bad `Host` against it in the PR discussion without failing on it.

- [ ] **Step 9: The port-forward path works under default-deny**

```bash
kubectl -n claude-mcp port-forward svc/grafana-mcp 8000:8000 >/dev/null 2>&1 &
PF=$!
sleep 3
curl -s -m 10 -o /dev/null -w 'via port-forward: %{http_code}\n' -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' http://localhost:8000/mcp
kill $PF
```

Expected: `via port-forward: 200`.
If it fails, the break-glass design needs an ingress rule for the API server's source, so stop and report.

- [ ] **Step 10: Nothing secret is logged**

```bash
kubectl -n claude-mcp logs deploy/grafana-mcp | head -20
```

Expected: startup lines only, with no token value.
A "security error" line about binding a non-loopback address without `--server-auth-token` is expected and accepted, as the spec explains.

______________________________________________________________________

### Task 7: Register both servers in Coder Agents

**Files:**

- Modify: `~/git/nickvigilante/infrastructure/coder/mcp_servers.tf`
- Modify: `~/git/nickvigilante/infrastructure/coder/README.md`

**Consumes:** the running servers from Task 6.

- [ ] **Step 1: Create the worktree**

```bash
cd ~/git/nickvigilante/infrastructure
git fetch origin && git pull --ff-only
git worktree add .worktrees/feat/coder-cluster-mcp -b feat/coder-cluster-mcp origin/main
cd .worktrees/feat/coder-cluster-mcp/coder
set -a && source ~/.homelab-opentofu.env && set +a
tofu init -input=false >/dev/null
```

- [ ] **Step 2: Write the failing check**

```bash
./tofu.sh plan -no-color 2>&1 | grep -E '^Plan:|No changes'
```

The wrapper finds the operator token in Keychain and the `Homelab-IaC` project by default.

Expected: `No changes. Your infrastructure matches the configuration.`

- [ ] **Step 3: Add the two servers**

Append to `coder/mcp_servers.tf`:

```hcl
# Kubernetes -- read-only view of the gandalf cluster, in-cluster.
#
# Served by k8s/claude-mcp in the homelab repo (#186). It is reachable only from
# the coder namespace, which is the access gate, so there is no auth here. The
# server runs as a `view`-bound ServiceAccount with read_only on and Secrets
# denied. Coder's SSRF guard allows the address through
# CODER_MCP_ALLOWED_PRIVATE_CIDRS (10.43.0.201/32).
resource "coderd_agents_mcp_server" "kubernetes" {
  display_name = "Kubernetes"
  slug         = "kubernetes"
  description  = "Read-only view of the gandalf cluster: workloads, events, and pod logs."
  url          = "http://kubernetes-mcp.claude-mcp.svc.cluster.local:8080/mcp"

  auth_type    = "none"
  availability = "default_off"
  enabled      = true
  transport    = "streamable_http"
}

# Grafana -- read-only dashboards and PromQL, in-cluster.
#
# Served by k8s/claude-mcp in the homelab repo (#184). Same access gate as the
# Kubernetes server. It reaches Grafana with a Viewer service-account token
# held in the cluster, not by Coder (10.43.0.200/32 is allowlisted).
resource "coderd_agents_mcp_server" "grafana" {
  display_name = "Grafana"
  slug         = "grafana"
  description  = "Read-only Grafana dashboards and Prometheus queries."
  url          = "http://grafana-mcp.claude-mcp.svc.cluster.local:8000/mcp"

  auth_type    = "none"
  availability = "default_off"
  enabled      = true
  transport    = "streamable_http"
}
```

- [ ] **Step 4: Add rows to the README table**

In `coder/README.md`, add these rows to the "What's managed" table and re-pad the table columns:

```markdown
| Kubernetes     | `http://kubernetes-mcp.claude-mcp.svc.cluster.local:8080/mcp` | None, gated by NetworkPolicy | Read-only cluster view, reachable only from the `coder` namespace.       |
| Grafana        | `http://grafana-mcp.claude-mcp.svc.cluster.local:8000/mcp`    | None, gated by NetworkPolicy | Read-only dashboards and PromQL, reachable only from the `coder` namespace. |
```

- [ ] **Step 5: Validate and plan**

```bash
tofu fmt -check && tofu validate
./tofu.sh plan -no-color 2>&1 | grep -E '^Plan:|will be created|Error'
```

Expected: the configuration is valid, then `Plan: 2 to add, 0 to change, 0 to destroy.` with `kubernetes` and `grafana` each listed as "will be created".
If validation rejects `auth_type = "none"`, read the provider schema in `.terraform` and use the accepted value for an unauthenticated server.

- [ ] **Step 6: Commit and open the PR**

```bash
cd ..
SKIP=yamlfmt pre-commit run --files coder/mcp_servers.tf coder/README.md
SKIP=yamlfmt pre-commit run --files coder/README.md
git add coder
git commit -m "feat(coder): register the in-cluster Kubernetes and Grafana MCP servers" -m "Both are read-only, reached over plain HTTP by service DNS name, and gated by a NetworkPolicy in the homelab repo, so they use no auth." -m "Refs nickvigilante/homelab#184, nickvigilante/homelab#186" -m "Assisted-by: AI"
git push -u origin feat/coder-cluster-mcp
```

Open the PR with `gh pr create --repo nickvigilante/infrastructure` and a body ending with `---` then `🤖 Built with AI assistance.`

- [ ] **Step 7: The user applies**

An agent must not run `tofu apply -auto-approve`.
The user runs, from the worktree's `coder/` directory:

```bash
set -a && source ~/.homelab-opentofu.env && set +a
./tofu.sh apply
```

Expected: they review the plan, type `yes`, and the result is `Apply complete! Resources: 2 added, 0 changed, 0 destroyed.`
Then verify with `./tofu.sh plan -no-color`, which must report no changes.

______________________________________________________________________

### Task 8: Break-glass helper and Claude Code registration (dotfiles)

**Files:**

- Create: `home/dot_local/bin/executable_mcp-breakglass`
- Create: `home/run_onchange_after_09-register-claude-mcp.sh.tmpl`
- Modify: `home/.chezmoiignore`
- Create: `$TMPDIR/breakglass-test.sh` and `$TMPDIR/register-test.sh` (throwaway test harnesses, not committed)

**Consumes:** `kubectl` from Task 1 and the Services from Task 6.

- [ ] **Step 1: Create the worktree**

```bash
cd ~/git/nickvigilante/dotfiles
git fetch origin && git pull --ff-only
git worktree add .worktrees/feat/mcp-breakglass -b feat/mcp-breakglass origin/main
cd .worktrees/feat/mcp-breakglass
```

- [ ] **Step 2: Write the failing helper tests**

Save as `$TMPDIR/breakglass-test.sh`:

```bash
#!/usr/bin/env bash
# Stub-based tests for mcp-breakglass. No cluster or credentials involved.
set -uo pipefail
SCRIPT="$1"
T="$(mktemp -d)"; STUBS="$T/stubs"; mkdir -p "$STUBS"
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
P1="$(free_port)"; P2="$(free_port)"
: > "$T/kubeconfig"

# A kubectl stub whose port-forward opens a listener on the local port, like the real one.
cat > "$STUBS/kubectl" <<'EOF'
#!/usr/bin/env bash
[ "${STUB_KUBECTL_FAIL:-0}" = 1 ] && { echo "forward failed" >&2; exit 1; }
for a in "$@"; do case "$a" in *:*) [[ "$a" =~ ^[0-9]+:[0-9]+$ ]] && lp="${a%%:*}";; esac; done
exec python3 -c "
import socket, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', $lp)); s.listen(); time.sleep(600)"
EOF
chmod +x "$STUBS/kubectl"

pass=0; fail=0
ok()  { pass=$((pass+1)); echo "  ok    $1"; }
bad() { fail=$((fail+1)); echo "  FAIL  $1"; }
run() { env -i PATH="$STUBS:/usr/bin:/bin:/opt/homebrew/bin" HOME="$T" STUB_KUBECTL_FAIL="${STUB_KUBECTL_FAIL:-0}" \
  MCP_BREAKGLASS_KUBECONFIG="${KC:-$T/kubeconfig}" MCP_BREAKGLASS_STATE_DIR="$T/state" \
  MCP_BREAKGLASS_FORWARDS="svc-a:$P1 svc-b:$P2" "$@" ; }
listening() { python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',$1))==0 else 1)"; }

echo "up starts both forwards"
OUT="$(run bash "$SCRIPT" up 2>&1)"; CODE=$?
[ "$CODE" = 0 ] && ok "exit 0" || { bad "exit $CODE"; echo "$OUT" | sed 's/^/        | /'; }
listening "$P1" && ok "port $P1 is open" || bad "port $P1 is closed"
listening "$P2" && ok "port $P2 is open" || bad "port $P2 is closed"
case "$OUT" in *"http://localhost:$P1/mcp"*) ok "prints the MCP URL";; *) bad "no MCP URL printed";; esac

echo "up is idempotent"
OUT="$(run bash "$SCRIPT" up 2>&1)"; CODE=$?
[ "$CODE" = 0 ] && ok "second up exits 0" || bad "second up exit $CODE"
case "$OUT" in *"already forwarded"*) ok "reports already forwarded";; *) bad "did not report already forwarded";; esac

echo "status reports running"
OUT="$(run bash "$SCRIPT" status 2>&1)"
case "$OUT" in *"svc-a"*running*) ok "status lists svc-a as running";; *) bad "status output: $OUT";; esac

echo "down stops both forwards"
run bash "$SCRIPT" down >/dev/null 2>&1; sleep 1
listening "$P1" && bad "port $P1 still open" || ok "port $P1 closed"
listening "$P2" && bad "port $P2 still open" || ok "port $P2 closed"

echo "a missing kubeconfig is refused"
OUT="$(KC="$T/nope" run bash "$SCRIPT" up 2>&1)"; CODE=$?
[ "$CODE" != 0 ] && ok "non-zero exit" || bad "exit 0"
case "$OUT" in *"kubeconfig not found"*) ok "says why";; *) bad "no reason given: $OUT";; esac

echo "a missing kubectl is refused"
OUT="$(env -i PATH="/usr/bin:/bin" HOME="$T" MCP_BREAKGLASS_KUBECONFIG="$T/kubeconfig" MCP_BREAKGLASS_STATE_DIR="$T/state" bash "$SCRIPT" up 2>&1)"; CODE=$?
[ "$CODE" != 0 ] && ok "non-zero exit" || bad "exit 0"
case "$OUT" in *"kubectl is not installed"*) ok "says why";; *) bad "no reason given: $OUT";; esac

echo "a port already in use is refused"
python3 -c "
import socket, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', $P1)); s.listen(); time.sleep(30)" &
BLOCKER=$!; sleep 1
OUT="$(run bash "$SCRIPT" up 2>&1)"; CODE=$?
kill $BLOCKER 2>/dev/null
[ "$CODE" != 0 ] && ok "non-zero exit" || bad "exit 0"
case "$OUT" in *"already in use"*) ok "says why";; *) bad "no reason given: $OUT";; esac
run bash "$SCRIPT" down >/dev/null 2>&1

echo "a failing port-forward is reported and cleaned up"
OUT="$(STUB_KUBECTL_FAIL=1 run bash "$SCRIPT" up 2>&1)"; CODE=$?
[ "$CODE" != 0 ] && ok "non-zero exit" || bad "exit 0"
case "$OUT" in *"did not come up"*) ok "says which forward failed";; *) bad "no reason given: $OUT";; esac

echo; echo "passed: $pass   failed: $fail"; rm -rf "$T"; [ "$fail" = 0 ]
```

- [ ] **Step 3: Run it to verify it fails**

```bash
bash "$TMPDIR/breakglass-test.sh" "$PWD/home/dot_local/bin/executable_mcp-breakglass"
```

Expected: FAIL, because the script does not exist yet (`No such file or directory` and failing checks).

- [ ] **Step 4: Write the helper**

`home/dot_local/bin/executable_mcp-breakglass`:

```bash
#!/usr/bin/env bash
# Break-glass access to the homelab's read-only MCP servers when Coder is down.
#
#   mcp-breakglass up      port-forward both in-cluster servers to localhost
#   mcp-breakglass down    stop the port-forwards
#   mcp-breakglass status  show which forwards are running
#
# It uses the admin kubeconfig over Tailscale, so it needs no other credential.
# The local ports match the containers' ports (8080 and 8000), which is what
# mcp-grafana's default loopback Host allowlist expects.
set -euo pipefail

NAMESPACE="${MCP_BREAKGLASS_NAMESPACE:-claude-mcp}"
KUBECONFIG_FILE="${MCP_BREAKGLASS_KUBECONFIG:-$HOME/.kube/homelab.yaml}"
STATE_DIR="${MCP_BREAKGLASS_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/mcp-breakglass}"
FORWARDS="${MCP_BREAKGLASS_FORWARDS:-kubernetes-mcp:8080 grafana-mcp:8000}"

die() {
  printf 'mcp-breakglass: %s\n' "$*" >&2
  exit 1
}

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
is_running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

wait_for_port() {
  local i
  for i in $(seq 1 20); do
    port_open "$1" && return 0
    sleep 0.5
  done
  return 1
}

stop_one() {
  local pidfile="$1"
  if is_running "$pidfile"; then kill "$(cat "$pidfile")" 2>/dev/null || true; fi
  rm -f "$pidfile"
}

up() {
  command -v kubectl >/dev/null 2>&1 || die "kubectl is not installed"
  [ -r "$KUBECONFIG_FILE" ] || die "kubeconfig not found: $KUBECONFIG_FILE"
  mkdir -p "$STATE_DIR"
  local pair svc port pidfile
  for pair in $FORWARDS; do
    svc="${pair%%:*}"
    port="${pair##*:}"
    pidfile="$STATE_DIR/$svc.pid"
    if is_running "$pidfile"; then
      echo "$svc already forwarded on localhost:$port"
      continue
    fi
    port_open "$port" && die "localhost:$port is already in use"
    nohup kubectl --kubeconfig "$KUBECONFIG_FILE" -n "$NAMESPACE" \
      port-forward "svc/$svc" "$port:$port" >"$STATE_DIR/$svc.log" 2>&1 &
    echo $! >"$pidfile"
    disown
    if ! wait_for_port "$port"; then
      stop_one "$pidfile"
      die "$svc did not come up; see $STATE_DIR/$svc.log"
    fi
    echo "$svc -> http://localhost:$port/mcp"
  done
}

down() {
  local pair svc
  for pair in $FORWARDS; do
    svc="${pair%%:*}"
    stop_one "$STATE_DIR/$svc.pid"
    echo "$svc stopped"
  done
}

status() {
  local pair svc port
  for pair in $FORWARDS; do
    svc="${pair%%:*}"
    port="${pair##*:}"
    if is_running "$STATE_DIR/$svc.pid"; then
      echo "$svc running on localhost:$port"
    else
      echo "$svc not running"
    fi
  done
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *) die "usage: mcp-breakglass up|down|status" ;;
esac
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
bash "$TMPDIR/breakglass-test.sh" "$PWD/home/dot_local/bin/executable_mcp-breakglass"
shellcheck home/dot_local/bin/executable_mcp-breakglass
shfmt -d -i 2 home/dot_local/bin/executable_mcp-breakglass
```

Expected: every line `ok` with `failed: 0`, and shellcheck and shfmt print nothing.
If shfmt reports a diff, apply it with `shfmt -w -i 2` and rerun the tests.

- [ ] **Step 6: Gate the helper like the admin kubeconfig**

In `home/.chezmoiignore`, inside the existing block that ignores `.kube/homelab.yaml`, add the helper on the next line:

```
{{ if or (ne .profile "personal") (eq .machine "server") (env "DOTFILES_IMAGE_BUILD") -}}
.kube/homelab.yaml
.local/bin/mcp-breakglass
{{ end -}}
```

- [ ] **Step 7: Write the failing registration test**

Save as `$TMPDIR/register-test.sh`:

```bash
#!/usr/bin/env bash
# Renders the registration script under three environments and checks the output.
set -uo pipefail
TPL="$1"
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "  ok    $1"; }
bad() { fail=$((fail+1)); echo "  FAIL  $1"; }

echo "laptop: localhost URLs"
OUT="$(env -u CODER -u DOTFILES_IMAGE_BUILD chezmoi execute-template < "$TPL")"
case "$OUT" in *"http://localhost:8080/mcp"*"http://localhost:8000/mcp"*) ok "uses the break-glass ports";; *) bad "no localhost URLs: $OUT";; esac
case "$OUT" in *"svc.cluster.local"*) bad "leaked an in-cluster URL";; *) ok "no in-cluster URL";; esac

echo "workspace: in-cluster URLs"
OUT="$(env -u DOTFILES_IMAGE_BUILD CODER=true chezmoi execute-template < "$TPL")"
case "$OUT" in *"http://kubernetes-mcp.claude-mcp.svc.cluster.local:8080/mcp"*"http://grafana-mcp.claude-mcp.svc.cluster.local:8000/mcp"*) ok "uses the service DNS names";; *) bad "no in-cluster URLs: $OUT";; esac

echo "image build: nothing rendered"
OUT="$(env -u CODER DOTFILES_IMAGE_BUILD=1 chezmoi execute-template < "$TPL")"
[ -z "$(printf '%s' "$OUT" | tr -d '[:space:]')" ] && ok "empty script" || bad "rendered content during an image build: $OUT"

echo "the rendered script runs claude mcp add for both servers"
STUBS="$(mktemp -d)"; LOG="$STUBS/calls.log"
printf '#!/bin/sh\necho "claude $*" >> "%s"\n' "$LOG" > "$STUBS/claude"; chmod +x "$STUBS/claude"
env -u CODER -u DOTFILES_IMAGE_BUILD chezmoi execute-template < "$TPL" > "$STUBS/script.sh"
PATH="$STUBS:$PATH" sh "$STUBS/script.sh" >/dev/null 2>&1
grep -q 'claude mcp add --transport http --scope user homelab-kubernetes http://localhost:8080/mcp' "$LOG" && ok "adds homelab-kubernetes" || bad "missing homelab-kubernetes add: $(cat "$LOG")"
grep -q 'claude mcp add --transport http --scope user homelab-grafana http://localhost:8000/mcp' "$LOG" && ok "adds homelab-grafana" || bad "missing homelab-grafana add"
grep -c 'claude mcp remove' "$LOG" | grep -q '^2$' && ok "removes each first, so reruns are idempotent" || bad "remove calls: $(grep -c 'claude mcp remove' "$LOG")"

echo "without the claude CLI the script exits 0"
NOCLAUDE="$(mktemp -d)"
env -i PATH="$NOCLAUDE:/usr/bin:/bin" sh "$STUBS/script.sh" >/dev/null 2>&1 && ok "exit 0" || bad "non-zero without claude"

echo; echo "passed: $pass   failed: $fail"; rm -rf "$STUBS" "$NOCLAUDE"; [ "$fail" = 0 ]
```

- [ ] **Step 8: Run it to verify it fails**

```bash
bash "$TMPDIR/register-test.sh" "$PWD/home/run_onchange_after_09-register-claude-mcp.sh.tmpl"
```

Expected: FAIL, because the template does not exist yet.

- [ ] **Step 9: Write the registration script**

`home/run_onchange_after_09-register-claude-mcp.sh.tmpl`:

```bash
{{- /*
Registers the homelab's read-only MCP servers with Claude Code.

  - In a Coder workspace (the agent sets CODER), use the in-cluster service
    names, which the NetworkPolicy admits.
  - On a personal laptop, use localhost, which only answers while
    `mcp-breakglass up` is running.
  - During an image build, or on any other machine, render nothing so chezmoi
    skips the script.

~/.claude.json is machine-local state and is never tracked, so this script is
how the entries reproduce. Editing it re-runs the registration.
*/ -}}
{{- $inWorkspace := ne (env "CODER") "" -}}
{{- $laptop := and (eq .profile "personal") (ne .machine "server") (eq (env "DOTFILES_IMAGE_BUILD") "") -}}
{{- if and (eq (env "DOTFILES_IMAGE_BUILD") "") (or $inWorkspace $laptop) -}}
#!/bin/sh
# run_onchange_after_09-register-claude-mcp: register the homelab MCP servers.
set -eu

if ! command -v claude >/dev/null 2>&1; then
  echo "==> claude CLI not found; skipping MCP registration" >&2
  exit 0
fi

add() {
  claude mcp remove --scope user "$1" >/dev/null 2>&1 || true
  claude mcp add --transport http --scope user "$1" "$2"
}

{{ if $inWorkspace -}}
add homelab-kubernetes http://kubernetes-mcp.claude-mcp.svc.cluster.local:8080/mcp
add homelab-grafana http://grafana-mcp.claude-mcp.svc.cluster.local:8000/mcp
{{- else -}}
add homelab-kubernetes http://localhost:8080/mcp
add homelab-grafana http://localhost:8000/mcp
{{- end }}
{{ end -}}
```

- [ ] **Step 10: Run the registration test to verify it passes**

```bash
bash "$TMPDIR/register-test.sh" "$PWD/home/run_onchange_after_09-register-claude-mcp.sh.tmpl"
```

Expected: every line `ok` and `failed: 0`.
If the image-build case fails, check that a script which renders only whitespace is treated as empty by chezmoi, and that the guard `eq (env "DOTFILES_IMAGE_BUILD") ""` is on the outer `if`.

- [ ] **Step 11: Run the repo hooks**

```bash
pre-commit run --files home/dot_local/bin/executable_mcp-breakglass home/.chezmoiignore home/run_onchange_after_09-register-claude-mcp.sh.tmpl
```

Expected: all hooks pass.

- [ ] **Step 12: Commit and open the PR**

```bash
git add home
git commit -m "feat(claude): add the MCP break-glass helper and register the homelab servers" -m "mcp-breakglass port-forwards the in-cluster Kubernetes and Grafana MCP servers to localhost so Claude can reach them when Coder is down. A run_onchange script registers both with Claude Code, using localhost on the laptop and the in-cluster service names inside a Coder workspace." -m "Refs nickvigilante/homelab#184, nickvigilante/homelab#186" -m "Assisted-by: AI"
git push -u origin feat/mcp-breakglass
```

Open the PR with `gh pr create` and a body ending with `---` then `🤖 Built with AI assistance.`
After it merges: `cd ~/git/nickvigilante/dotfiles && git pull --ff-only && chezmoi apply`.

______________________________________________________________________

### Task 9: Acceptance and bookkeeping

**Files:**

- Modify: `docs/superpowers/specs/2026-06-29-grafana-mcp-design.md` and `docs/superpowers/specs/2026-06-29-kubernetes-mcp-design.md` (homelab repo)

- [ ] **Step 1: Acceptance from a Coder Agents chat (user)**

In a Coder Agents chat, turn on Kubernetes and Grafana, then ask each of these and check the answer:

- "List the pods in the `claude-mcp` namespace." Expected: both MCP pods are listed.

- "Show me the last 20 log lines of the `grafana-mcp` pod." Expected: startup lines, and no token.

- "What is the CPU usage of the `coder` namespace over the last hour?" Expected: a real PromQL result.

- "Read the contents of any Secret in the `coder` namespace." Expected: refused.

- "Delete the `grafana-mcp` pod." Expected: refused, and the pod is still running afterward.

- "Create a Grafana dashboard called test." Expected: refused, and no dashboard exists afterward.

- [ ] **Step 2: Acceptance from a Coder workspace (user)**

In a workspace, confirm the entries were registered, then repeat the first and third prompt through Claude Code:

```bash
echo "$CODER"
claude mcp list
```

Expected: `true`, and `homelab-kubernetes` and `homelab-grafana` appear with the in-cluster URLs.
If they are missing, run `chezmoi apply` in the workspace and check that `DOTFILES_IMAGE_BUILD` is unset.

- [ ] **Step 3: Break-glass acceptance (user, laptop)**

```bash
mcp-breakglass up
claude mcp list
mcp-breakglass status
mcp-breakglass down
```

Expected: both forwards start on `localhost:8080` and `localhost:8000`, the two `homelab-*` entries appear with localhost URLs and connect while the forwards are up, and `down` stops them.
The full "Coder is down" proof (scaling the Coder deployment to zero) interrupts the user's own session and is optional.
Only do it if the user asks, with `kubectl -n coder scale deploy coder --replicas=0`, and restore it immediately with `--replicas=1`.

- [ ] **Step 4: Mark the old specs superseded**

In each of the two old specs, add this line after the opening paragraph (the `**Issue:**` line and the sentence below it), with a blank line before it:

```markdown
**Status:** superseded by [the in-cluster design](2026-09-19-claude-mcp-in-cluster-design.md); the laptop-stdio architecture below is not built.
```

Then commit on a fresh branch:

```bash
cd ~/git/nickvigilante/homelab
git fetch origin
git worktree add .worktrees/docs/claude-mcp-supersede -b docs/claude-mcp-supersede origin/main
cd .worktrees/docs/claude-mcp-supersede
```

Make the two edits, then:

```bash
pre-commit run --files docs/superpowers/specs/2026-06-29-grafana-mcp-design.md docs/superpowers/specs/2026-06-29-kubernetes-mcp-design.md
git add docs
git commit -m "docs(claude-mcp): mark the laptop-stdio MCP specs superseded" -m "Refs #184, #186" -m "Assisted-by: AI"
```

- [ ] **Step 5: File the follow-up issues**

Per the repo's tracking rule, follow-ups are issues, not inline prose.
Neither describes an unpatched weakness, so the disclosure screen passes.

```bash
gh issue create --repo nickvigilante/homelab --title "Claude MCP: read-only access to Flux and cert-manager resources, nodes, and metrics" --body "The built-in view ClusterRole does not cover Flux or cert-manager custom resources, nodes, or the metrics API, so the Kubernetes MCP server cannot show HelmRelease or Certificate status. Add a small read-only ClusterRole bound to the claude-mcp ServiceAccount. See docs/superpowers/specs/2026-09-19-claude-mcp-in-cluster-design.md."
gh issue create --repo nickvigilante/homelab --title "Claude MCP: write-capable Kubernetes instance" --body "The in-cluster Kubernetes MCP server is read-only. A gated write path would be a separate instance with its own ServiceAccount, RBAC, NetworkPolicy, and Coder registration. See docs/superpowers/specs/2026-09-19-claude-mcp-in-cluster-design.md."
```

- [ ] **Step 6: Close the loop on the tracking issues**

After Steps 1 to 3 pass:

```bash
gh issue close 184 --repo nickvigilante/homelab --comment "Done in the in-cluster design: mcp-grafana runs in the claude-mcp namespace and is registered in Coder Agents. Read-only, verified from a Coder chat, a workspace, and the break-glass path."
gh issue close 186 --repo nickvigilante/homelab --comment "Done in the in-cluster design: kubernetes-mcp-server runs read-only in the claude-mcp namespace as a view-bound ServiceAccount, with Secrets denied. Writes are tracked in a follow-up issue."
```

- [ ] **Step 7: Clean up**

Remove the merged worktrees (`feat/claude-mcp-cluster`, `docs/claude-mcp-in-cluster`, `docs/claude-mcp-supersede`, and their siblings in the infrastructure and dotfiles repos) with `git worktree remove` and `git branch -D`, checking first that each PR is merged and each tree is clean.
