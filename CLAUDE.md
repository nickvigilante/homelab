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
| `README.md` | One-time setup runbook + day-to-day ops notes |

When adding a new service, mirror this layout. See `k8s/authentik/`
and `k8s/coder/` for the most complete examples.

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

Pi-hole serves custom records for `*.home` hostnames (`jellyfin.home`,
`uptime.home`, `authentik.home`, `coder.home`, etc.) to the LAN and
tailnet. **CoreDNS inside the cluster does not see these records** —
it uses upstream DNS but is configured separately.

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
