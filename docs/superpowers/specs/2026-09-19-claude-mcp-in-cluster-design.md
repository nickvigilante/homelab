# Grafana and Kubernetes MCP in the cluster — read-only access for Coder and Claude Code — Design

**Issues:** #184 (Claude MCP access — Grafana) and #186 (Claude MCP access — Kubernetes).
**Supersedes:** the laptop-stdio architecture in [the Grafana MCP design](2026-06-29-grafana-mcp-design.md) and [the Kubernetes MCP design](2026-06-29-kubernetes-mcp-design.md).
Their identity, least-privilege, and privacy reasoning still stands, and this spec reuses it.

**Goal:** Give Claude read-only, conversational access to the homelab's cluster state and observability data from the two places work now happens: Coder Agents chats and Claude Code inside a Coder workspace.
Both are served by one in-cluster deployment of the self-hosted `containers/kubernetes-mcp-server` and `grafana/mcp-grafana`, each installed from its published Helm chart under Flux, with no credentials rendered onto any laptop or workspace.
A break-glass path from the laptop keeps the same tools reachable when Coder is down.

**Non-goals (this sub-project):**

- Any write path.
  Both servers run read-only, and a write-capable Kubernetes instance is a separate, later, gated design.
- Public or tailnet exposure of the MCP endpoints.
  There is no Traefik hostname, no Cloudflare, and no new ingress.
- Helm operations, Flux or cert-manager custom resources, and node or metrics-API reads through the Kubernetes MCP.
  The built-in `view` role does not cover them, so they are a follow-up.
- Loki or log-query tooling, which is still sub-project B (#116).
- Building the Coder workspace template.
  The workspace-side registration only takes effect once a workspace exists.

## Why the design changed

The two earlier specs assumed Claude Code on a laptop launching each MCP server as a stdio process with a Bitwarden-rendered token.
That no longer fits how the homelab is used.

- Coder Agents runs its MCP client inside the Coder server pod and only connects to remote `streamable_http` or `sse` servers, never stdio.
- Claude Code in a Coder workspace runs in a pod in the `coder` namespace and has neither the laptop's kubeconfig nor its chezmoi-rendered tokens.
- Both servers support HTTP transport and in-cluster operation.
  The Kubernetes server can use its pod's ServiceAccount, so no kubeconfig or token needs to be stored at all.

## Architecture

```
Coder server pod ──┐                        ┌─▶ kubernetes-mcp-server ──▶ k8s API (SA `claude-mcp` → `view`)
                   ├─ HTTP over ClusterIP ──┤
workspace pods ────┘  (NetworkPolicy:       └─▶ mcp-grafana ──▶ kps-grafana svc (Viewer SA token)
                       `coder` namespace only)

laptop (break-glass) ── kubectl port-forward (admin kubeconfig, over Tailscale) ──▶ the same two Services
```

Everything new lives in a `claude-mcp` namespace, so its network policy and RBAC are easy to audit in one place.

### Components

| Component               | Role                                                             | Placement                                   |
| ----------------------- | ---------------------------------------------------------------- | ------------------------------------------- |
| `kubernetes-mcp-server` | Read-only cluster tools over Streamable HTTP at `/mcp`           | HelmRelease in `claude-mcp`, port 8080      |
| `mcp-grafana`           | Read-only Grafana and Prometheus-through-Grafana tools at `/mcp` | HelmRelease in `claude-mcp`, port 8000      |
| `claude-mcp` SA         | The Kubernetes server's in-cluster identity, bound to `view`     | `claude-mcp` namespace                      |
| Viewer service account  | The identity `mcp-grafana` uses against Grafana                  | Grafana, token in BWS                       |
| NetworkPolicies         | Default-deny ingress, then admit only the `coder` namespace      | `claude-mcp` namespace                      |
| Coder registrations     | Two `coderd_agents_mcp_server` resources with no auth            | infrastructure repo, `coder/mcp_servers.tf` |
| Break-glass helper      | Starts and stops the two port-forwards                           | dotfiles                                    |

## Deployment method

The repo's rule is chart-first when an official or widely-used chart exists, and both servers have one.
Each server is a Flux `HelmRelease` in the `claude-mcp` namespace, and only the pieces no chart provides stay as raw manifests: the namespace, the NetworkPolicies, and the ExternalSecret.

| Server                  | Chart                                                                                                         | Source                                                                     |
| ----------------------- | ------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `kubernetes-mcp-server` | `kubernetes-mcp-server` from `oci://ghcr.io/containers/charts` (the project's own chart)                      | new `sources/kubernetes-mcp-server.yaml`, a `HelmRepository` of type `oci` |
| `mcp-grafana`           | `grafana-mcp` from `https://grafana-community.github.io/helm-charts` (moved there from `grafana/helm-charts`) | new `sources/grafana-community.yaml`                                       |

- **Pinning.**
  `chart.spec.version` is pinned for both, as the repo's Helm pin discipline requires: `0.1.0` for the Kubernetes chart and `0.24.0` for the Grafana chart.
  The Kubernetes chart's `image.version` defaults to `latest`, so it is set explicitly to `v0.0.67`, and the Grafana chart's tag follows its `appVersion` of `1.5.1`.
- **Chart defaults to override.**
  The Kubernetes chart enables an Ingress by default, so its values set `ingress.enabled: false`.
  The Grafana chart mounts a ServiceAccount token by default at pod level, so its values set `automountServiceAccountToken: false`.
  The Grafana image's entrypoint defaults to the `sse` transport and the chart appends `extraArgs` after it, so `--transport=streamable-http` is passed explicitly and the later flag wins.
- **Fixed cluster IP.**
  The Grafana chart exposes `service.clusterIP`.
  The Kubernetes chart's Service template has no such field, so its HelmRelease carries a small `postRenderers` kustomize patch that sets `spec.clusterIP`.
- **Names.**
  The release names and `fullnameOverride` are set so the Services are `grafana-mcp` and `kubernetes-mcp`, matching the in-cluster URLs registered in Coder, and the Kubernetes chart's `serviceAccount.name` is `claude-mcp`.
- **RBAC.**
  The Kubernetes chart creates the ServiceAccount and binds it to the built-in `view` ClusterRole through `rbac.extraClusterRoleBindings` with `roleRef.external: true`, so no RBAC manifest is written by hand.
- **Placement.**
  Both set `nodeSelector: kubernetes.io/hostname: gandalf`, following the repo's rule that pods stay on gandalf unless they are meant for the Pis.
- **Flux wiring.**
  `clusters/gandalf/claude-mcp.yaml` sets `prune: true` because Flux owns the whole directory, `wait: true`, and `dependsOn: infrastructure` so the ExternalSecret never reconciles before its store is ready.

## Identity and least privilege

- **Kubernetes:** a `claude-mcp` ServiceAccount bound by a ClusterRoleBinding to the built-in `view` ClusterRole.
  `view` omits Secrets, roles, and role bindings, so cluster secrets never enter model context.
- **Kubernetes server switches:** `read_only = true`, the `core` toolset only, and Secrets listed in `denied_resources`.
  A write needs the server flag flipped and RBAC that does not exist, and a Secret read is blocked by both RBAC and the server.
- **Grafana:** a dedicated Viewer-role service account named `claude-mcp`, so the token cannot create or modify dashboards, alerts, or users even if a tool were mis-invoked.
- **Grafana server switches:** `--disable-write` and the default toolset list, which leaves the `admin` category off.
- **Blast radius:** any process that can reach an endpoint gets exactly this read access and nothing more.

## Networking and access gate

- Each server has a ClusterIP Service with a **fixed** cluster IP from the static band at the low end of the service CIDR, which Kubernetes keeps clear of dynamic allocation: `10.43.0.200` for `mcp-grafana` and `10.43.0.201` for `kubernetes-mcp-server`.
  Both are confirmed unused with `kubectl get svc -A` at implementation.
- Coder's SSRF guard blocks private destinations for MCP traffic unless allowlisted, so both IPs are added as `/32` entries to `CODER_MCP_ALLOWED_PRIVATE_CIDRS` in `k8s/coder/helmrelease.yaml`.
  This is the same mechanism as the two existing gandalf entries, and it avoids opening the whole service CIDR.
  Changing it restarts the Coder pod, and running workspaces are unaffected.
- The `claude-mcp` namespace gets a default-deny ingress NetworkPolicy plus one allow rule for the `coder` namespace, which holds both the Coder server and workspace pods.
- Registered URLs use the in-cluster DNS names, for example `http://grafana-mcp.claude-mcp.svc.cluster.local:8000/mcp`.
  The name resolves to the allowlisted IP, and the same name goes into `mcp-grafana`'s `--allowed-hosts`.
- Egress is left unrestricted, because the servers only need the API server and Grafana and a policy adds little here.

### Host validation

`mcp-grafana` rejects any request whose `Host` header is not in `--allowed-hosts` with a 403, which is also what blocks browser DNS rebinding.
The list is set explicitly to the in-cluster names and ClusterIP with port 8000, plus `localhost:8000` and `127.0.0.1:8000` for the break-glass port-forward.
The Grafana chart ships no probes, so its HelmRelease sets TCP probes on the MCP port, which the Host check does not affect.
The Kubernetes server's `Host` handling is not documented, so it is verified at first contact (see Verification).

## Secrets

- Bitwarden is the source of truth, in the repo's two surfaces.
  The Grafana Viewer token is a new field, `mcp-sa-token`, on the `Homelab Grafana` vault item, which is the disaster-recovery mirror.
  Its cluster-readable copy lives in the BWS `homelab` project and is synced into the `claude-mcp` namespace by an `ExternalSecret` against the `bitwarden` ClusterSecretStore, referencing the BWS UUID with `# gitleaks:allow`, exactly like `grafana-secrets` today.
- It reaches `mcp-grafana` as `GRAFANA_SERVICE_ACCOUNT_TOKEN`, injected from the synced Secret through whichever env mechanism the chart provides.
- The Kubernetes server needs no stored credential, because it uses its pod ServiceAccount.
- In-cluster access goes straight to the `kps-grafana` Service, so it bypasses Traefik's Authentik forward-auth, and the token is the only credential.
- Neither server is given caller authentication.
  The NetworkPolicy is the access gate, and `mcp-grafana` will log a startup "security error" for binding a non-loopback address without `--server-auth-token`.
  That log line is accepted noise here, and adding a token later would mean carrying it in BWS, Coder, and workspace config.
- Nothing secret is committed, and betterleaks stays clean.

## Workload hardening

Both charts default to a read-only root filesystem, and the HelmRelease values add the rest where the chart does not: non-root, all capabilities dropped, the `RuntimeDefault` seccomp profile, and explicit resource requests and limits.
Only the Kubernetes server mounts a ServiceAccount token, and the Grafana chart's `automountServiceAccountToken` stays `false`.
Images are pinned to released versions and never `latest`: `quay.io/containers/kubernetes_mcp_server` at `v0.0.67` and `grafana/mcp-grafana` at `1.5.1`, or newer releases when the plan is written.

## Break-glass: Coder is down

Coder sits downstream of Authentik, and Coder being down is exactly when Claude's help on the cluster is most useful, so the tools must not depend on Coder.

- A small dotfiles helper (`mcp-breakglass up` and `down`) runs `kubectl port-forward` to both Services using the admin kubeconfig over Tailscale.
- The local ports match the container ports, `8080` and `8000`, so `mcp-grafana`'s default loopback host allowlist matches and no extra host config is needed.
- The laptop's Claude Code config lists two `http://localhost:…/mcp` entries that do nothing until the port-forwards are up.
- No new credential is involved.
  The admin kubeconfig is what makes this break-glass, because it is not available inside a workspace.
- It covers Coder being down.
  If gandalf itself is down the MCP servers are down too, and plain `kubectl` and SSH remain.

## Privacy boundary and constraints

- **Tool results become model context.**
  The `view` role keeps Secrets out, but it still allows `pods/log`, so application log contents can reach the model.
  Worth narrowing if any workload logs sensitive payloads.
  Grafana results are metric values, dashboard metadata, and query results.
- **A workspace is cluster-read access.**
  Any process in any workspace can call these endpoints, so a workspace gets read access to the cluster (minus Secrets) and to Grafana.
  That is acceptable while one person holds a Coder account, but Coder's OIDC currently accepts any email domain.
  Before adding collaborators, narrow the NetworkPolicy to specific pods or reconsider this design.
- **Coder Agents.**
  Both servers are registered `default_off`, so a chat opts in explicitly.

## Implementation surface

- **homelab:**
  - this spec and its implementation plan;
  - `k8s/claude-mcp/` with `namespace.yaml`, one `helmrelease.yaml` holding both HelmReleases, `netpol-*.yaml`, `external-secret.yaml`, `kustomization.yaml`, and a short `README.md`;
  - the two new `sources/*.yaml` files;
  - `clusters/gandalf/claude-mcp.yaml`, following `coder.yaml`;
  - the two `/32` additions in `k8s/coder/helmrelease.yaml`;
- **infrastructure:** two `coderd_agents_mcp_server` resources in `coder/mcp_servers.tf`, with `auth_type = "none"`, `default_off`, and `streamable_http`, applied locally through `coder/tofu.sh`.
- **dotfiles:** the break-glass helper, the laptop's Claude Code entries, and the workspace's Claude Code entries pointing at the in-cluster URLs.

## Staged rollout

1. **Grafana token (manual).**
   Create the `claude-mcp` Viewer service account in Grafana and store its token in the BWS `homelab` project.
   It must exist before the ExternalSecret syncs.
2. **Cluster manifests.**
   The homelab PR with `k8s/claude-mcp/`, the two chart sources, the Flux Kustomization, and the CIDR change.
   Every filename is already covered by the CI kubeconform filter, so the filter is not touched.
3. **Verify in-cluster** (see Verification).
4. **Register in Coder.**
   The infrastructure PR, applied locally, then turn each server on in a chat.
5. **Workspace and laptop.**
   The dotfiles PR, then verify from a workspace and from the laptop with the port-forwards up.
6. **Bookkeeping.**
   Mark the two earlier specs as superseded and update issues #184 and #186.

## Verification

Things the docs do not settle, checked in step 3 before anything is registered in Coder:

- The Kubernetes server accepts the in-cluster `Host` header, and the exact `denied_resources` TOML for the pinned version loads without error.
- k3s actually enforces NetworkPolicy here.
- `kubectl port-forward` still reaches the pods under the default-deny policy.
- Coder Agents' MCP client accepts a plain `http://` URL.
- The Grafana Service is `kps-grafana` on port 80 in `monitoring`, as the `kps` release name implies.
- The chosen ClusterIPs are unused.
- The `postRenderers` patch produces the intended fixed `clusterIP` when Flux reconciles it.
- Kubelet's TCP and HTTP probes still pass under the default-deny NetworkPolicy, and if they do not, an ingress rule for the node is added.

## Repo checklist notes

- **Backup wiring:** none, because the servers hold no persistent data.
- **SPOF impact:** no new Authentik dependency, since the endpoints are not behind SSO.
  Coder being down is the case the break-glass path covers.
- **Follow-ups** (a write-capable Kubernetes instance, and read access to Flux and cert-manager resources) are filed as GitHub issues and referenced here by number once they exist.

## Acceptance criteria

- From a Coder Agents chat and from Claude Code in a workspace, Claude can list pods, deployments, and events, read pod logs, and run a PromQL query that returns live homelab data.
- Reading a Secret is refused, and so is any write to the cluster or to Grafana.
- `kubectl auth can-i` as the `claude-mcp` ServiceAccount answers yes to listing pods cluster-wide and no to getting secrets and to any write.
- A pod outside the `coder` namespace cannot reach either endpoint.
- With the Coder deployment scaled to zero, the break-glass port-forward gives the laptop the same read-only tools.
- Neither server is exposed through Traefik, Cloudflare, or the tailnet, and no secret appears in any committed file, with betterleaks pre-commit and CI clean.

## Things deliberately not done

- **Writes.**
  A write-capable Kubernetes instance would be a separate design with its own ServiceAccount, NetworkPolicy, and Coder registration.
- **Wider read access.**
  A small extra read-only ClusterRole for Flux and cert-manager custom resources, nodes, and metrics is a natural follow-up, since `view` does not aggregate them.
- **Caller authentication.**
  Left off in favor of the network gate, for the reasons above.
- **Hand-written Deployments.**
  Both servers have published charts, so the repo's chart-first rule applies, and the cost is chart bumps to track and a `postRenderers` patch for one field.
- **Exposure through Traefik.**
  Reaching these from any tailnet device would need an authenticated edge that Traefik cannot provide natively.
