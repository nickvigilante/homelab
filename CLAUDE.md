# CLAUDE.md

Guidance for agents and future-you working in this repo.

## What this repo is

The k3s manifest + host-config repo for the home lab running on
`gandalf` (192.168.50.135, Ubuntu Server 26.04, single-node k3s control
plane + worker, Traefik default ingress). Apply pattern is **manual**:
`kubectl apply -f …` for raw manifests, `helm install/upgrade -f
values.yaml` for charted services. No GitOps controller — the repo is
the source of truth, but applying changes is a deliberate human step.

Sibling repo for IaC (Tailscale ACLs, GitHub branch protection, OAuth
secrets, etc.) lives at `~/git/nickvigilante/infrastructure/`, public
on GitHub, OpenTofu-managed. Reach for it when state is "outside the
cluster."

## Service directory convention

Each service under `k8s/<service>/` follows the same layout:

| File | Purpose |
|------|---------|
| `namespace.yaml` | The k8s Namespace |
| `pv-pvc.yaml` | Pre-created PV (hostPath, gandalf-pinned) + PVC for any persistent data the chart can't manage with its own dynamic provisioning |
| `values.yaml` | Helm values, or raw manifests when a chart doesn't fit |
| `secret.example.yaml` | **Template only** — documents which keys must live in the real Secret. Never applied; the real Secret is created via `kubectl create secret generic` sourced from a Bitwarden item |
| `ingress-vigihome.yaml` | Raw Ingress at `https://<service>.vigihome.net` once the service migrates onto HTTPS. See "TLS / HTTPS" below. |
| `README.md` | One-time setup runbook + day-to-day ops notes |

When adding a new service, mirror this layout. See `k8s/authentik/`
and `k8s/coder/` for the most complete examples.

## TLS / HTTPS — the `*.vigihome.net` stack

Every internal service moves onto HTTPS at `*.vigihome.net` (cleanly
separated from the public `nickvigilante.com`). The supporting infra
runs in three namespaces:

| Namespace | What | PR |
|-----------|------|----|
| `cert-manager` | cert-manager controller + LE staging/prod ClusterIssuers using native Cloudflare DNS-01 + the wildcard `Certificate` for `vigihome.net` + `*.vigihome.net` | #23, #24, #25 |
| `reflector` | emberstack/reflector — mirrors annotated Secrets across namespaces | #26 |
| `homepage` | First end-to-end exercise of the stack at the apex | #27 |

**The flow:** cert-manager issues `vigihome-tls` (ECDSA P-256, 90d /
30d-renew) into `cert-manager/vigihome-tls`. Reflector reads the
`secretTemplate` annotations on `k8s/cert-manager/certificate.yaml`
and copies the Secret into every namespace listed in
`reflection-auto-namespaces`. Consumer Ingresses reference
`secretName: vigihome-tls` in their TLS block.

**Migrating a service onto HTTPS:**

1. Add the consumer namespace to `reflection-auto-namespaces` in
   `k8s/cert-manager/certificate.yaml` (comma-separated). `kubectl
   apply` it. Reflector mirrors `vigihome-tls` into the namespace in
   < 5s.
2. Add `k8s/<service>/ingress-vigihome.yaml` — a raw `Ingress` at
   `<service>.vigihome.net` with `traefik.ingress.kubernetes.io/router.entrypoints: websecure`
   and `tls.secretName: vigihome-tls`. See `k8s/authentik/ingress-vigihome.yaml`
   for the canonical example.
3. Add the Pi-hole local DNS record `<service>.vigihome.net →
   192.168.50.135` via the Pi-hole admin UI (Settings → Local DNS
   Records). v6 manages local records in `pihole.toml`
   non-declaratively.
4. Test in a real browser. Expect a trusted cert from
   `O=Let's Encrypt, CN=E7` (ECDSA intermediate).
5. After the service is verified working over HTTPS, drop the chart's
   legacy HTTP Ingress + matching `*.home` Pi-hole record in a
   follow-up PR.

**OIDC clients gotcha:** Authentik derives token `iss` claims from
the request `Host` header — not a single configured external URL.
When migrating Authentik to its new HTTPS host, every OIDC client
(Coder today; future Jellyfin) must update its issuer URL in
lockstep. The Authentik cutover therefore lands as a 3-PR sequence:
D1 (add HTTPS Ingress) → D2 (flip each client's `OIDC_ISSUER_URL`)
→ D3 (drop chart's HTTP Ingress + Pi-hole record). Don't merge them
out of order. See PRs #28 / #30 / #31 for the template.

**Lint:** the kubeconform CI filter at `.github/workflows/lint.yml`
covers `namespace.yaml`, `pv-pvc.yaml`, `secret.example.yaml`,
`deployment.yaml`, `*-job.yaml`, `*-cronjob.yaml`, `clusterissuer-*.yaml`,
`certificate.yaml`, `ingress-*.yaml`. When a new service uses a
filename outside that set, add it to the filter rather than letting
it silently bypass validation.

## Secrets discipline

- **Secrets never enter the repo.** The repo is public; gitleaks runs
  as a pre-commit hook.
- Source of truth for every secret is Bitwarden. Items are named
  `Homelab <Service>` (e.g., `Homelab Restic Repository`, `Homelab
  Authentik`, `Homelab Coder`).
- k8s Secrets are created via `kubectl create secret generic` invocations
  that read values from Bitwarden CLI at apply time. Each service's
  README walks through its specific Secret keys.
- `secret.example.yaml` files document the *shape* of each Secret (keys
  + their roles) but use `REPLACE_WITH_*` placeholders. Don't apply
  them — they exist for documentation only.
- Storj S3 access keys live ONLY in `/etc/rclone/rclone.conf` (root:root
  0600) and `~/.homelab-opentofu.env`. The cluster gets them via
  `kubectl create secret` from sourced env vars.

## DNS pattern (recurring gotcha)

Pi-hole serves custom records to the LAN and tailnet in two
hostname namespaces today, mid-migration:

- `*.vigihome.net` — the new target. Browser-trusted HTTPS, single
  wildcard cert (see "TLS / HTTPS" above). Live as of 2026-05-16 for
  `vigihome.net` (Homepage) and `authentik.vigihome.net`. Each Phase
  3 PR adds another service.
- `*.home` (`jellyfin.home`, `uptime.home`, `coder.home`, etc.) —
  legacy plain-HTTP names. Stay live for services not yet cut over;
  retire each one in a tiny PR after its `*.vigihome.net` Ingress is
  verified.

**CoreDNS inside the cluster does not see these records** — it uses
upstream DNS but is configured separately.

So: inside a pod, `*.home` hostnames will NXDOMAIN. Always use
cluster-internal service DNS for pod-to-pod traffic:
`<svc>.<namespace>.svc.cluster.local`. Examples:

- `jellyfin.media.svc.cluster.local:8096`
- `pihole-web.networking.svc.cluster.local`
- `uptime-kuma.monitoring.svc.cluster.local:3001`

Custom DNS records for the host network live in
`/opt/pihole/etc-pihole/pihole.toml` under `dns.hosts`. Editing them
requires a Pi-hole pod rollout to pick up.

## Backup wiring

The nightly restic CronJob at `k8s/backup/backup-cronjob.yaml` mounts
persistent dirs from gandalf via hostPath and pushes encrypted
snapshots to Storj. **Any new persistent dir under `/opt/<service>/`
needs adding to that CronJob** — pattern: new `volume` + `volumeMount`
+ `restic backup --tag <service>` block. Repo password lives in
Bitwarden item `Homelab Restic Repository` — losing it loses every
snapshot.

Heartbeats: the CronJob pings Uptime Kuma push monitors on success
and on failure (via `trap ERR`). Push URLs live in Secret
`backup/uptime-kuma-push-urls`.

### What we don't back up (and why it's fine)

- **The k3s state DB** (`/var/lib/rancher/k3s/server/db/state.db`).
  SQLite mode has no online snapshot — `k3s etcd-snapshot save` is
  etcd-only, and a raw file copy while k3s is mid-write would be
  inconsistent. Recovery instead: reinstall k3s, re-apply manifests
  from this repo, re-`kubectl create secret` from Bitwarden, let
  `local-path` re-provision dynamic PVCs from restored hostPaths.
- **Coder workspace home directories**. Considered ephemeral; rebuild
  via the workspace template from git + dotfiles inside the workspace.
- **Authentik UI-only configuration state**. Property mappings,
  application/provider bindings etc. live inside the `authentik`
  postgres DB which *is* backed up — but a from-scratch rebuild
  without the postgres restore (e.g. lost `AUTHENTIK_SECRET_KEY`)
  means redoing UI clicks. See `k8s/authentik/README.md`.

## SPOF discipline (Authentik is a SPOF)

Authentik fronts SSO for downstream services. When Authentik is down,
every service integrated via OIDC / forward-auth loses its login flow.
**Every service put behind Authentik must keep a local-fallback
credential** so it can be reached when Authentik is broken:

- Jellyfin: native admin account stays in Bitwarden
- Pi-hole admin: native password stays in Bitwarden (item `Pi-Hole`)
- Uptime Kuma: native admin in Bitwarden
- Coder: `coder users create admin-local --password=...` (see `k8s/coder/README.md` step 8)

When wiring a new downstream integration, verify the fallback works
*before* declaring the integration done.

## Host-level changes

Anything host-level on gandalf (apt, systemd, files in `/etc/`,
sysctl, etc.) belongs in `ansible/provision-gandalf.yml`. It's
idempotent and meant to be re-run after pulling repo changes that
touch `system/`. Don't ad-hoc edit gandalf — if it matters enough to
remember, capture it in the playbook.

For Pi workers, the analogous playbook is `ansible/provision-pi.yml`.
The Tailscale auth keys it needs are minted via
`ansible/bin/mint-tailscale-authkey.sh` (OAuth, on-demand, short-lived).

## Pull requests + commits

- Feature branches → PRs → squash-merge to `main`. Never push to main
  directly (branch protection enforces this).
- **No `Co-Authored-By` trailer in commit messages or PR bodies.**
- Commit subjects: imperative mood, ≤ 70 chars. Body for "why," not "what."
- PR template at `.github/pull_request_template.md` lists the
  before-merge checklist (secrets, backup wiring, SPOF impact).

## Things deliberately not done

- No GitOps controller (Argo CD, Flux). Apply is manual on purpose —
  one operator, low blast radius, easier to reason about.
- No bundled chart wrappers / Kustomize layers. Helm values + raw
  YAML, applied top-level.
- No CI test suite for manifests beyond `.github/workflows/lint.yml`
  (kubeconform + yamllint).
- k3s itself is not under IaC. `provision-gandalf.yml` captures
  *some* host state, but the k3s install command lives in shell
  history. A from-scratch rebuild means re-running `curl … | sh -`
  and re-importing all PVs. Accepted trade-off for now.
