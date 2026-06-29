# Kubernetes MCP — gated cluster access for Claude — Design

**Issue:** #186 (Claude MCP access — Kubernetes).
The Grafana MCP is a separate sub-project (#184).

**Goal:** Give Claude **read-only-by-default** access to the gandalf k3s cluster
— with an explicit, narrowly-scoped **opt-in write** path — through the
self-hosted `containers/kubernetes-mcp-server`, reached over Tailscale and
authenticating as a dedicated least-privilege `claude-mcp` identity whose
permissions are Flux-managed RBAC.

**Non-goals (this sub-project):**

- Cluster-admin or broad write access — writes are scoped per-namespace/verb.
- Standing write access — the default mode is read-only; writing is a
  deliberate, separate posture.
- Public exposure of the API server — reachability is tailnet-only.
- The MCP reading Secrets — excluded by the read role.
- A custom-built MCP server — use the maintained OSS server; revisit only if its
  read/write switch proves too coarse.

## Why this server

`containers/kubernetes-mcp-server` (Go, native client-go) is a single static
binary with **no host dependencies** (no kubectl/helm/Node to install or patch),
talks to the API directly (no shell-out), and ships a true **`--read-only`** flag
plus a separate **`--disable-destructive`** tier — which maps cleanly onto the
phased gating below. Flux159's TypeScript server was the alternative, but its
"non-destructive" mode still permits create/update and it requires `kubectl` +
`helm` on the host, a larger surface for a self-hoster.

## Architecture

`kubernetes-mcp-server` runs **locally on the laptop** as a stdio MCP server,
registered with Claude Code. It loads a kubeconfig whose `server:` points at the
API server's **tailnet** address, and issues Kubernetes API calls directly.

```
Claude Code  ──stdio──▶  kubernetes-mcp-server (laptop)  ──HTTPS over Tailscale──▶  gandalf k3s API :6443
```

The MCP authenticates **as the identity in that kubeconfig** — a dedicated
`claude-mcp` ServiceAccount, never the admin credential in `~/.kube/homelab.yaml`.

## Defense in depth — a write succeeds only if every layer allows it

1. **Network** — Tailscale ACL limits `:6443` to the connecting device; the API
   server has no public exposure.
2. **Identity** — a dedicated `claude-mcp` ServiceAccount, distinct from admin.
3. **Authorization (RBAC, Flux-managed)** —
   - *Phase 1 (read):* bind `claude-mcp` to the built-in **`view`** ClusterRole,
     which **omits Secrets** (and roles/rolebindings) by default — so cluster
     secrets can never enter model context. An explicit custom read role is an
     option if we want to be belt-and-suspenders (#186).
   - *Phase 2 (write):* a **separate, narrow** Role granting only chosen
     verbs/resources/namespaces (e.g. `patch`/`update` on `deployments` and
     `delete` on `pods` in `apps`), explicitly **not** `kube-system`, bound to a
     second identity.
4. **MCP layer** — run with **`--read-only`** by default. Writing requires both
   flipping the flag *and* the write RBAC, so a stray write tool with no RBAC
   behind it simply fails.

The clean expression of "writes only after proper authentication": **two
kubeconfigs** — a read-only one loaded by default, and a write-capable one
loaded only when ops are intended. The MCP cannot mutate anything until the
write identity is the one in play.

## Identity and RBAC manifests (in this repo)

Following the service-directory convention, a new `k8s/claude-mcp/` directory
holds the declarative, non-secret RBAC, wired into the gandalf Flux Kustomization:

- `serviceaccount.yaml` — `claude-mcp` ServiceAccount.
- `read-clusterrolebinding.yaml` — binds `claude-mcp` to the built-in `view`
  ClusterRole (Phase 1).
- `write-role.yaml` + `write-rolebinding.yaml` — the Phase-2 scoped write Role
  (added later, in its own gated PR; namespaces/verbs per #186).

These are safe to commit (RBAC is declarative config, the same class as the rest
of `k8s/`). No tokens appear in the repo.

## Networking

- Reached over **Tailscale**; the kubeconfig `server:` is gandalf's tailnet
  endpoint (exact address verified on the real machine — #186).
- A Tailscale ACL restricts `:6443` to the connecting device. No ingress, no
  Cloudflare, no public surface.

## Secrets

- The `claude-mcp` token(s) live **only in Bitwarden** (vault item TBD per #186,
  alongside the existing `Homelab Kubeconfig`), rendered into 0600 kubeconfig
  file(s) by a chezmoi `bitwarden` template at `chezmoi apply` time — the same
  mechanism as `~/.kube/homelab.yaml`. Nothing is committed.
- Token style — long-lived bound ServiceAccount token vs short-lived
  `kubectl create token` with refresh — is an open decision (#186); short-lived
  is more secure but needs a refresh path.

## Privacy boundary

The server and credentials stay on the private network, but **tool results
become context sent to Claude** (the model). The `view` role keeps Secrets out
of scope. Note that `view` still allows `pods/log`, so **application log
contents** can reach model context — acceptable for ops, but worth narrowing the
log permission if any workload logs sensitive payloads (#186).

## Implementation surface

- **This repo (homelab):** this spec; the `k8s/claude-mcp/` RBAC manifests +
  Flux Kustomization wiring; a short `README` for the directory.
- **dotfiles:** install `kubernetes-mcp-server` (chezmoi Brewfile); the
  Bitwarden-templated read-only (later write) kubeconfig(s); register the stdio
  MCP server in the chezmoi-managed Claude config with `--read-only` and
  `--kubeconfig`.

## Staged rollout

1. **Phase-1 RBAC** — commit the `claude-mcp` ServiceAccount + `view` binding via
   Flux; reconcile; verify with `kubectl auth can-i --as=system:serviceaccount:<ns>:claude-mcp`
   (can list pods, **cannot** get secrets, **cannot** write).
2. **Read kubeconfig** — mint the read token → Bitwarden; render the 0600
   read-only kubeconfig via chezmoi.
3. **Install + register** — add `kubernetes-mcp-server` to the Brewfile; register
   the stdio MCP with `--read-only` + the read kubeconfig; `chezmoi apply`.
4. **Verify read** — from Claude, list pods/deployments/events over Tailscale;
   confirm secret reads and any write are refused.
5. **Phase-2 writes (later, separate gated PR)** — scoped write Role + second
   identity + write kubeconfig; enable writes only on intent; verify an
   in-scope write works and an out-of-scope write (e.g. anything in
   `kube-system`) is denied.

## Acceptance criteria

- Claude can list/describe workloads and read pod logs over Tailscale.
- Claude **cannot** read Secrets and **cannot** perform writes in the default
  (read-only) posture.
- The API server has no public exposure; access is tailnet-only.
- All tokens are sourced from Bitwarden and present in no committed file;
  betterleaks pre-commit and CI are clean.
- (Phase 2) An in-scope write succeeds and an out-of-scope write is denied by
  RBAC.

## Things deliberately not done

- **Cluster-admin / broad write.** Excluded by design; writes are per-verb,
  per-namespace, opt-in.
- **Standing write access.** Read-only is the default posture; the write
  kubeconfig is loaded only when ops are intended.
- **Public API exposure.** Tailnet-only; no ingress or tunnel.
- **Custom MCP build.** Use the OSS server; reconsider only if its read/write
  granularity proves insufficient.
- **Helm writes via the MCP.** Helm releases remain GitOps-managed via Flux, not
  mutated through Claude.
