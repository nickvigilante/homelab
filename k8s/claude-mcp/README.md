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

| Server     | URL                                                           | Fixed ClusterIP |
| ---------- | ------------------------------------------------------------- | --------------- |
| Kubernetes | `http://kubernetes-mcp.claude-mcp.svc.cluster.local:8080/mcp` | `10.43.0.201`   |
| Grafana    | `http://grafana-mcp.claude-mcp.svc.cluster.local:8000/mcp`    | `10.43.0.200`   |

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
