# Forward-Auth Primitive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
Stand up a domain-level Authentik forward-auth primitive and use it to expose the Prometheus and Alertmanager admin UIs behind SSO, with real `externalUrl`s.

**Architecture:**
One Authentik proxy provider in `forward_domain` mode is assigned to the pre-existing embedded outpost and gated on the `homelab-users` group.
At the edge, a per-namespace Traefik `forwardAuth` middleware calls the embedded outpost; each protected host carries that middleware on its app route and an un-gated `/outpost.goauthentik.io` route co-located in the `auth` namespace.
Prometheus and Alertmanager are the first consumers, wired via the kube-prometheus-stack `values.yaml`.

**Tech Stack:**
Authentik 2026.2.2 (blueprints config-as-code), Traefik (k3s built-in, CRD `Middleware`), kube-prometheus-stack chart 85.3.3, Flux (raw-manifest reconcile), kubectl/helm.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-26-forward-auth-design.md`. Issue #137.
- Secrets never enter the repo (public repo; betterleaks pre-commit + CI). No secret material is introduced by this plan.
- No `Co-Authored-By` trailer in commits or PR bodies.
- Markdown docs use semantic line breaks (one clause per line) and write "and"/"&", never `+`, in prose. Formatted by mdformat, not prettier.
- Always pin `helm upgrade --version`. The kps release is chart `85.3.3` (`helm list -n monitoring`).
- `allowCrossNamespace` stays OFF — no change to `system/traefik-helmchartconfig.yaml` or any Ansible-owned Traefik config.
- Authentik service `authentik-server.auth` listens on port **80** (targetPort 9000). The `forwardAuth` address and the outpost Ingress backend use the **Service port 80**, never `9000` (nothing listens on Service port 9000).
- Feature branch already checked out: `feat/137-forward-auth`. Branch → PR → squash-merge; never push to main.
- All commands run on the **laptop** with kubectl context = homelab, unless labelled otherwise.

______________________________________________________________________

### Task 1: Authentik forward-auth provider, outpost assignment, application, group gate

**Files:**

- Create: `k8s/authentik/blueprints/applications/forward-auth.yaml`
- Modify: `k8s/authentik/blueprints/README.md` (document the new blueprint)

**Interfaces:**

- Produces: an Authentik proxy provider named `forward-auth` (mode `forward_domain`), an application slug `forward-auth` gated on `homelab-users`, and the embedded outpost (`authentik Embedded Outpost`) carrying that provider. Later tasks rely on the embedded outpost answering `GET /outpost.goauthentik.io/auth/traefik`.

- [ ] **Step 1: Write the blueprint**

Create `k8s/authentik/blueprints/applications/forward-auth.yaml` with exactly:

```yaml
version: 1
metadata:
  name: "homelab - application - forward-auth"
# Domain-level forward-auth proxy provider (#137). Assigned to the embedded
# outpost; gated on homelab-users. external_host is the authentik URL itself
# (domain mode redirects there); cookie_domain shares the SSO cookie across
# every *.vigihome.net host. Implicit-consent authz flow so proxied requests
# aren't prompted for consent each time. The providers list on the embedded
# outpost is authoritative -- any future proxy provider must be added here too.
entries:
  - id: forward-auth-provider
    model: authentik_providers_proxy.proxyprovider
    state: present
    identifiers:
      name: forward-auth
    attrs:
      mode: forward_domain
      external_host: https://authentik.vigihome.net
      cookie_domain: vigihome.net
      authorization_flow: !Find [authentik_flows.flow, [slug, default-provider-authorization-implicit-consent]]
      invalidation_flow: !Find [authentik_flows.flow, [slug, default-provider-invalidation-flow]]
      property_mappings:
        - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-openid]]
        - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-email]]
        - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-profile]]
  - id: forward-auth-app
    model: authentik_core.application
    state: present
    identifiers:
      slug: forward-auth
    attrs:
      name: Forward Auth (vigihome)
      provider: !KeyOf forward-auth-provider
      policy_engine_mode: any
  - model: authentik_policies.policybinding
    state: present
    identifiers:
      target: !KeyOf forward-auth-app
      group: !Find [authentik_core.group, [name, homelab-users]]
      order: 0
    attrs:
      enabled: true
      negate: false
  - model: authentik_outposts.outpost
    state: present
    identifiers:
      name: authentik Embedded Outpost
    attrs:
      providers:
        - !KeyOf forward-auth-provider
```

- [ ] **Step 2: Render and apply the blueprints ConfigMap, roll the worker, trigger discovery**

Run (laptop, per `k8s/authentik/blueprints/README.md`):

```bash
cd ~/git/nickvigilante/homelab
kubectl -n auth create configmap authentik-blueprints \
  $(find k8s/authentik/blueprints -name '*.yaml' -printf '--from-file=%f=%p ') \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n auth rollout restart deploy/authentik-worker
kubectl -n auth rollout status deploy/authentik-worker
kubectl -n auth exec deploy/authentik-worker -- \
  ak shell -c "from authentik.blueprints.v1.tasks import blueprints_discovery; blueprints_discovery.send()"
```

- [ ] **Step 3: Verify the blueprint applied successfully**

Run:

```bash
POD=$(kubectl get pod -n auth -l app.kubernetes.io/component=server -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n auth "$POD" -- sh -c 'curl -s -H "Authorization: Bearer $AUTHENTIK_BOOTSTRAP_TOKEN" \
  "http://localhost:9000/api/v3/managed/blueprints/" | python3 -c "import sys,json; [print(b[\"name\"], b[\"status\"]) for b in json.load(sys.stdin)[\"results\"] if \"forward-auth\" in b[\"path\"]]"'
```

Expected: a line ending in `successful`.

- [ ] **Step 4: Verify the provider exists and the embedded outpost carries it**

Run:

```bash
POD=$(kubectl get pod -n auth -l app.kubernetes.io/component=server -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n auth "$POD" -- sh -c 'curl -s -H "Authorization: Bearer $AUTHENTIK_BOOTSTRAP_TOKEN" \
  "http://localhost:9000/api/v3/outposts/instances/" | python3 -c "import sys,json; [print(o[\"name\"], \"providers=\", o[\"providers\"]) for o in json.load(sys.stdin)[\"results\"]]"'
```

Expected: `authentik Embedded Outpost providers= [<one integer id>]` (non-empty list).
If the list is empty, the managed-outpost assignment did not stick — re-run Step 2's discovery trigger; if still empty, STOP and investigate before proceeding (the gate cannot work without the provider on the outpost).

- [ ] **Step 5: Document the blueprint in the blueprints README**

In `k8s/authentik/blueprints/README.md`, add `forward-auth.yaml` to the inventory of application blueprints with a one-line description: the domain-level forward-auth proxy provider gated on `homelab-users`, assigned to the embedded outpost (#137). Match the existing list's wording and semantic-line-break style.

- [ ] **Step 6: Commit**

```bash
git add k8s/authentik/blueprints/applications/forward-auth.yaml k8s/authentik/blueprints/README.md
git commit -m "Add domain-level forward-auth proxy provider blueprint (#137)"
```

______________________________________________________________________

### Task 2: Un-gated outpost-path Ingress in the auth namespace

**Files:**

- Create: `k8s/authentik/ingress-forward-auth-outpost.yaml`
- Modify: `k8s/authentik/kustomization.yaml` (add the Ingress to Flux-reconciled resources)

**Interfaces:**

- Consumes: the `authentik-server` Service (port 80) and the embedded outpost from Task 1.

- Produces: routes `Host(prometheus.vigihome.net|alertmanager.vigihome.net) && PathPrefix(/outpost.goauthentik.io/)` → the outpost. Task 4's gated app routes rely on this path being reachable un-gated for the login handshake.

- [ ] **Step 1: Write the Ingress**

Create `k8s/authentik/ingress-forward-auth-outpost.yaml`:

```yaml
# Un-gated outpost-path routes for every forward-auth-protected host (#137).
# Co-located in `auth` (same namespace as authentik-server) so the backend is
# an in-namespace Service and Traefik's allowCrossNamespace can stay OFF.
# Traefik aggregates Ingresses cluster-wide by host+path, so these compose with
# the gated app routes (which live in the app's namespace) for the same hosts.
# These routes carry NO forwardAuth middleware -- the login handshake must not
# gate itself. Backend port is the Service port (80 -> targetPort 9000); nothing
# listens on Service port 9000. Add a new host's rule here as services onboard.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: forward-auth-outpost
  namespace: auth
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
spec:
  ingressClassName: traefik
  rules:
    - host: prometheus.vigihome.net
      http:
        paths:
          - path: /outpost.goauthentik.io
            pathType: Prefix
            backend:
              service:
                name: authentik-server
                port:
                  number: 80
    - host: alertmanager.vigihome.net
      http:
        paths:
          - path: /outpost.goauthentik.io
            pathType: Prefix
            backend:
              service:
                name: authentik-server
                port:
                  number: 80
  tls:
    - hosts:
        - prometheus.vigihome.net
        - alertmanager.vigihome.net
      secretName: vigihome-tls
```

- [ ] **Step 2: Add the Ingress to the auth Flux kustomization**

Edit `k8s/authentik/kustomization.yaml` so `resources` reads:

```yaml
resources:
  - external-secret.yaml
  - ingress-forward-auth-outpost.yaml
```

- [ ] **Step 3: Validate the manifest locally (matches CI kubeconform)**

Run:

```bash
kubectl apply --dry-run=client -f k8s/authentik/ingress-forward-auth-outpost.yaml
```

Expected: `ingress.networking.k8s.io/forward-auth-outpost created (dry run)` with no schema error.

- [ ] **Step 4: Reconcile and verify the route serves the outpost**

Run:

```bash
flux reconcile kustomization auth --with-source
kubectl -n auth get ingress forward-auth-outpost
# From gandalf (on the LAN, where *.vigihome.net resolves to Traefik):
curl -sS -o /dev/null -w '%{http_code}\n' https://prometheus.vigihome.net/outpost.goauthentik.io/ping
```

Expected: the Ingress lists both hosts; the curl returns `204` (the outpost ping endpoint), confirming `/outpost.goauthentik.io` reaches the embedded outpost and is **not** gated.
(If run from the laptop and `*.vigihome.net` resolves via the tailnet, the same `204` applies. A `404` means the route isn't composing — recheck the host spelling and that Task 1's outpost has the provider.)

- [ ] **Step 5: Commit**

```bash
git add k8s/authentik/ingress-forward-auth-outpost.yaml k8s/authentik/kustomization.yaml
git commit -m "Route /outpost.goauthentik.io to embedded outpost for forward-auth hosts (#137)"
```

______________________________________________________________________

### Task 3: forwardAuth Middleware in the monitoring namespace

**Files:**

- Create: `k8s/kube-prometheus-stack/middleware-forward-auth.yaml`
- Modify: `k8s/kube-prometheus-stack/kustomization.yaml` (add the Middleware to Flux-reconciled resources)

**Interfaces:**

- Consumes: the embedded outpost endpoint at `http://authentik-server.auth.svc.cluster.local/outpost.goauthentik.io/auth/traefik` (Service port 80).

- Produces: a Traefik Middleware referenceable from monitoring-namespace Ingresses as `monitoring-forward-auth@kubernetescrd`. Task 4 attaches it.

- [ ] **Step 1: Write the Middleware**

Create `k8s/kube-prometheus-stack/middleware-forward-auth.yaml`:

```yaml
# Per-namespace forwardAuth middleware copy (#137). allowCrossNamespace stays
# OFF, so each consuming namespace holds its own identical copy pointing at the
# embedded outpost by FQDN. Address uses the authentik-server Service port (80
# -> targetPort 9000); there is no Service port 9000. trustForwardHeader lets
# the outpost read the original host/proto from Traefik. authResponseHeaders are
# copied onto the upstream request AFTER the websecure-default strip-auth-headers
# middleware blanks any client-supplied copies -- so the upstream only ever sees
# Authentik-asserted identity (Prometheus/Alertmanager ignore them; this is for
# future header-trusting consumers).
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: forward-auth
  namespace: monitoring
spec:
  forwardAuth:
    address: http://authentik-server.auth.svc.cluster.local/outpost.goauthentik.io/auth/traefik
    trustForwardHeader: true
    authResponseHeaders:
      - X-authentik-username
      - X-authentik-groups
      - X-authentik-entitlements
      - X-authentik-email
      - X-authentik-name
      - X-authentik-uid
      - X-authentik-jwt
      - X-authentik-meta-jwks
      - X-authentik-meta-outpost
      - X-authentik-meta-provider
      - X-authentik-meta-app
      - X-authentik-meta-version
```

- [ ] **Step 2: Add the Middleware to the kps Flux kustomization**

Edit `k8s/kube-prometheus-stack/kustomization.yaml` so `resources` reads:

```yaml
resources:
  - external-secret.yaml
  - middleware-forward-auth.yaml
```

- [ ] **Step 3: Validate locally**

Run:

```bash
kubectl apply --dry-run=client -f k8s/kube-prometheus-stack/middleware-forward-auth.yaml
```

Expected: `middleware.traefik.io/forward-auth created (dry run)` with no schema error.

- [ ] **Step 4: Reconcile and verify the object exists**

Run:

```bash
flux reconcile kustomization kps --with-source
kubectl -n monitoring get middleware forward-auth
```

Expected: the Middleware `forward-auth` is listed in `monitoring`.

- [ ] **Step 5: Commit**

```bash
git add k8s/kube-prometheus-stack/middleware-forward-auth.yaml k8s/kube-prometheus-stack/kustomization.yaml
git commit -m "Add forwardAuth middleware to monitoring namespace (#137)"
```

______________________________________________________________________

### Task 4: Expose Prometheus and Alertmanager behind forward-auth, fix externalUrl

**Files:**

- Modify: `k8s/kube-prometheus-stack/values.yaml` (Prometheus Ingress + externalUrl; Alertmanager Ingress + externalUrl)
- Modify: `k8s/kube-prometheus-stack/README.md` (document the exposure, SPOF posture, port-forward fallback)

**Interfaces:**

- Consumes: the Middleware from Task 3 (`monitoring-forward-auth@kubernetescrd`) and the outpost routes from Task 2.

- [ ] **Step 1: Add the Prometheus Ingress and externalUrl**

In `k8s/kube-prometheus-stack/values.yaml`, under `prometheus.prometheusSpec`, add (alongside the existing `retention` etc.):

```yaml
    # Real reachable URL now that forward-auth gates the UI (#137). Replaces the
    # in-cluster Service-DNS default that leaked into links.
    externalUrl: https://prometheus.vigihome.net
```

And under the top-level `prometheus:` key, add an `ingress` block as a sibling of `prometheusSpec`:

```yaml
  # Gated by Authentik forward-auth via the monitoring-namespace middleware
  # (#137). The un-gated /outpost.goauthentik.io handshake route lives in the
  # auth namespace (k8s/authentik/ingress-forward-auth-outpost.yaml).
  ingress:
    enabled: true
    ingressClassName: traefik
    annotations:
      traefik.ingress.kubernetes.io/router.entrypoints: websecure
      traefik.ingress.kubernetes.io/router.middlewares: monitoring-forward-auth@kubernetescrd
    hosts:
      - prometheus.vigihome.net
    paths:
      - /
    pathType: Prefix
    tls:
      - hosts: ["prometheus.vigihome.net"]
        secretName: vigihome-tls
```

- [ ] **Step 2: Add the Alertmanager Ingress and fix its externalUrl**

In `k8s/kube-prometheus-stack/values.yaml`, change the existing Alertmanager `externalUrl` line from:

```yaml
    externalUrl: http://localhost:9093
```

to:

```yaml
    externalUrl: https://alertmanager.vigihome.net
```

and update the comment above it to note the URL is now a real forward-auth-gated host (drop the port-forward-placeholder rationale).
Then add an `ingress` block under the top-level `alertmanager:` key as a sibling of `alertmanagerSpec`:

```yaml
  # Gated by Authentik forward-auth (#137); handshake route in the auth ns.
  ingress:
    enabled: true
    ingressClassName: traefik
    annotations:
      traefik.ingress.kubernetes.io/router.entrypoints: websecure
      traefik.ingress.kubernetes.io/router.middlewares: monitoring-forward-auth@kubernetescrd
    hosts:
      - alertmanager.vigihome.net
    paths:
      - /
    pathType: Prefix
    tls:
      - hosts: ["alertmanager.vigihome.net"]
        secretName: vigihome-tls
```

- [ ] **Step 3: Render the Ingresses to confirm the chart consumes the values**

Run:

```bash
cd ~/git/nickvigilante/homelab
helm template kps prometheus-community/kube-prometheus-stack --version 85.3.3 \
  -n monitoring -f k8s/kube-prometheus-stack/values.yaml \
  --show-only templates/prometheus/ingress.yaml \
  --show-only templates/alertmanager/ingress.yaml 2>/dev/null | \
  grep -E 'host:|middlewares|secretName|kind: Ingress'
```

Expected: both `prometheus.vigihome.net` and `alertmanager.vigihome.net` appear, each with the `monitoring-forward-auth@kubernetescrd` middleware annotation and `secretName: vigihome-tls`.
(If `--show-only` rejects two paths in one invocation, run it once per template.)

- [ ] **Step 4: Apply with a pinned helm upgrade**

Run:

```bash
helm repo update prometheus-community
helm upgrade kps prometheus-community/kube-prometheus-stack --version 85.3.3 \
  -n monitoring -f k8s/kube-prometheus-stack/values.yaml
kubectl -n monitoring rollout status statefulset/prometheus-kps-prometheus
kubectl -n monitoring rollout status statefulset/alertmanager-kps-alertmanager
```

Expected: `helm upgrade` reports `REVISION: 8` (one past the current 7); both StatefulSets roll out.

- [ ] **Step 5: Verify the gate (unauthenticated → redirect)**

Run (from a host where `*.vigihome.net` resolves to Traefik):

```bash
curl -sS -o /dev/null -w '%{http_code} -> %{redirect_url}\n' https://prometheus.vigihome.net/
curl -sS -o /dev/null -w '%{http_code} -> %{redirect_url}\n' https://alertmanager.vigihome.net/
```

Expected: each returns `302 -> https://authentik.vigihome.net/...` (the Authentik login), proving the UI is gated and the unauthenticated request never reaches the app.

- [ ] **Step 6: Verify the fallback path still works**

Run:

```bash
kubectl -n monitoring port-forward svc/kps-prometheus 9090:9090 >/dev/null 2>&1 &
sleep 2; curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:9090/-/ready; kill %1
```

Expected: `200` — `kubectl port-forward` reaches Prometheus directly, bypassing Traefik/Authentik (the documented break-glass path).

- [ ] **Step 7: Update the kps README**

In `k8s/kube-prometheus-stack/README.md`:

- Document that Prometheus and Alertmanager are exposed at `https://prometheus.vigihome.net` and `https://alertmanager.vigihome.net`, gated by Authentik forward-auth (#137), with the handshake route in the `auth` namespace and the middleware in `monitoring`.
- Add a SPOF note: these UIs have no native login, so the break-glass fallback when Authentik is down is `kubectl -n monitoring port-forward svc/kps-prometheus 9090:9090` (and `svc/kps-alertmanager 9093:9093`), which bypasses Traefik and Authentik. No Bitwarden fallback credential applies (no native auth surface).
- Update any text that referenced the old `externalUrl: http://localhost:9093` placeholder.

Use semantic line breaks and "and"/"&" per the doc-style constraint.

- [ ] **Step 8: Commit**

```bash
git add k8s/kube-prometheus-stack/values.yaml k8s/kube-prometheus-stack/README.md
git commit -m "Expose Prometheus and Alertmanager behind forward-auth, fix externalUrl (#117, #137)"
```

______________________________________________________________________

### Task 5: End-to-end acceptance and finish the branch

**Files:** none (verification and handoff only).

- [ ] **Step 1: Run the full acceptance matrix**

With a browser (or an authenticated session) confirm each spec acceptance criterion:

1. Unauthenticated `https://prometheus.vigihome.net` → 302 to Authentik login (already checked in Task 4 Step 5).
2. Log in as a `homelab-users` member → Prometheus UI loads; repeat for Alertmanager.
3. Authenticated user **not** in `homelab-users` → 403 from Authentik. If no non-member test user exists, note this as manually unverifiable and record the policy binding (Task 1) as the enforcing control.
4. `kubectl port-forward` to each Service still serves the UI (already checked in Task 4 Step 6).
5. Inspect an Alertmanager notification (or the Alertmanager UI's configured `externalUrl`) — links now point at `https://alertmanager.vigihome.net` and resolve.
6. A request carrying a forged `X-authentik-username` header does not bypass the gate:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -H 'X-authentik-username: attacker' https://prometheus.vigihome.net/
```

Expected: still `302` (forged header does not authenticate; strip-then-forwardAuth ordering holds).

- [ ] **Step 2: Confirm CI lint locally**

Run:

```bash
cd ~/git/nickvigilante/homelab
pre-commit run --all-files 2>&1 | tail -20
```

Expected: betterleaks, yamlfmt, mdformat, and kubeconform all pass. Fix any finding before opening the PR.

- [ ] **Step 3: Finish the branch**

Announce: "I'm using the finishing-a-development-branch skill to complete this work."
REQUIRED SUB-SKILL: superpowers:finishing-a-development-branch — open the PR against `main` (no `Co-Authored-By` trailer), referencing #137 and #117, with the acceptance results in the body.

- [ ] **Step 4: Update memory after merge**

Once merged and applied, update the homelab roadmap memory: mark #137 done, record the forward-auth primitive (domain-level, embedded outpost, per-namespace middleware, `allowCrossNamespace` off) and the Prometheus/Alertmanager exposure, and re-point "next up" to the user's choice. Add a feedback memory capturing the two reusable gotchas: the Service-port-80 (not 9000) address, and the two-route handshake topology with the outpost route co-located in `auth` to keep cross-namespace trust off.
