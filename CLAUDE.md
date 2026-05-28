# CLAUDE.md

Guidance for agents and future-you working in this repo.

## What this repo is

The k3s manifest + host-config repo for the home lab running on
`gandalf` (192.168.50.135, Ubuntu Server 26.04, single-node k3s control
plane + worker, Traefik default ingress). Apply pattern is **manual**:
`kubectl apply -f …` for raw manifests, `helm install/upgrade -f values.yaml` for charted services. No GitOps controller — the repo is
the source of truth, but applying changes is a deliberate human step.

Sibling repo for IaC (Tailscale ACLs, GitHub branch protection, OAuth
secrets, etc.) lives at `~/git/nickvigilante/infrastructure/`, public
on GitHub, OpenTofu-managed. Reach for it when state is "outside the
cluster."

## Service directory convention

Each service under `k8s/<service>/` follows the same layout:

| File                    | Purpose                                                                                                                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `namespace.yaml`        | The k8s Namespace                                                                                                                                                                                |
| `pv-pvc.yaml`           | Pre-created PV (hostPath, gandalf-pinned) + PVC for any persistent data the chart can't manage with its own dynamic provisioning                                                                 |
| `values.yaml`           | Helm values (when chart-managed) — Ingress lives inside the chart's `ingress` key, not as a separate file                                                                                        |
| `deployment.yaml`       | Raw `Deployment` + `Service` (when raw-managed)                                                                                                                                                  |
| `secret.example.yaml`   | **Template only** — documents which keys must live in the real Secret. Never applied; the real Secret is created via `kubectl create secret generic` sourced from a Bitwarden item               |
| `ingress-vigihome.yaml` | Raw `Ingress` at `https://<service>.vigihome.net`, **only for raw-managed services** (chart-managed services keep the Ingress in `values.yaml`). See `k8s/syncthing/` for the canonical example. |
| `README.md`             | One-time setup runbook + day-to-day ops notes                                                                                                                                                    |

When adding a new service, mirror this layout. See `k8s/authentik/`
and `k8s/coder/` for chart-managed examples; `k8s/syncthing/` for the
single raw-managed exception.

### Chart vs raw: which to pick

**Chart-first when an official or widely-used chart exists.** Raw
manifests when only community-maintained charts exist *and* the app
is a single Deployment.

Reasoning: the consistency win from chart-managed services
(uniform `helm upgrade` ergonomics, uniform Ingress placement in
`values.yaml`, atomic rollback) is worth a lot in a one-operator
repo. Community charts for single-Deployment apps add abstraction
that costs more than it saves — overriding chart defaults (e.g.
NodePort vs `hostNetwork` conflicts on the Syncthing chart),
managing chart-version drift, and "fighting the chart" when its
opinions misalign. Reference precedent: Syncthing kept raw
(2026-05-17) because only community charts exist and it's one
Deployment.

## TLS / HTTPS — the `*.vigihome.net` stack

Every internal service moves onto HTTPS at `*.vigihome.net` (cleanly
separated from the public `nickvigilante.com`). The supporting infra
runs in three namespaces:

| Namespace      | What                                                                                                                                                       | PR            |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `cert-manager` | cert-manager controller + LE staging/prod ClusterIssuers using native Cloudflare DNS-01 + the wildcard `Certificate` for `vigihome.net` + `*.vigihome.net` | #23, #24, #25 |
| `reflector`    | emberstack/reflector — mirrors annotated Secrets across namespaces                                                                                         | #26           |
| `homepage`     | First end-to-end exercise of the stack at the apex                                                                                                         | #27           |

**The flow:** cert-manager issues `vigihome-tls` (ECDSA P-256, 90d /
30d-renew) into `cert-manager/vigihome-tls`. Reflector reads the
`secretTemplate` annotations on `k8s/cert-manager/certificate.yaml`
and copies the Secret into every namespace listed in
`reflection-auto-namespaces`. Consumer Ingresses reference
`secretName: vigihome-tls` in their TLS block.

**Migrating a service onto HTTPS:**

1. Add the consumer namespace to `reflection-auto-namespaces` in
   `k8s/cert-manager/certificate.yaml` (comma-separated). `kubectl apply` it. Reflector mirrors `vigihome-tls` into the namespace in
   < 5s.
2. **Chart-managed service:** modify `values.yaml`'s `ingress` block
   to set the vigihome host with
   `traefik.ingress.kubernetes.io/router.entrypoints: websecure` and
   `tls.secretName: vigihome-tls`. Each chart's Ingress shape varies
   slightly — singular `host` (Coder) vs `hosts[]` (Authentik,
   Uptime Kuma, Jellyfin), flat `tls.{enable, secretName}` (Coder)
   vs `tls[].{hosts, secretName}` (others). Render with `helm template ... --show-only templates/ingress.yaml` before applying
   to verify the chart consumes the values as expected.
   **Raw-managed service:** add `k8s/<service>/ingress-vigihome.yaml`
   with the same Traefik annotation + TLS block. See
   `k8s/syncthing/ingress-vigihome.yaml` for the canonical example.
3. DNS: nothing per-service. Pi-hole's
   `address=/vigihome.net/192.168.50.135` and
   `address=/vigihome.net/100.92.2.25` wildcard directives in
   `dns.dnsmasq_lines` (managed via the Pi-hole admin UI, see
   `k8s/pihole/values.yaml`'s big DNS comment) resolve any new
   `*.vigihome.net` host to gandalf automatically on both LAN and
   tailnet.
4. Apply: `helm upgrade` for chart-managed, `kubectl apply -f ingress-vigihome.yaml` for raw. For chart-managed services that
   previously had both a raw `ingress-vigihome.yaml` *and* a chart
   Ingress on the legacy `*.home` host, the chart's Ingress takes
   over the vigihome host on `helm upgrade`; the orphaned raw
   resource needs `kubectl delete ingress <name>` afterward.
5. Test in a real browser. Expect a trusted cert from
   `O=Let's Encrypt, CN=E7` (ECDSA intermediate).
6. If any legacy Pi-hole local DNS record (`<service>.home`) is left
   over, retire it via the Pi-hole admin UI.

**OIDC clients gotcha:** Authentik derives token `iss` claims from
the request `Host` header — not a single configured external URL.
When migrating Authentik to a new HTTPS host, every OIDC client
must update its issuer URL in lockstep. The original Authentik
cutover landed as a 3-PR sequence (D1: add HTTPS Ingress → D2: flip
each client's `OIDC_ISSUER_URL` → D3: drop legacy HTTP Ingress; PRs
#28 / #30 / #31). Subsequent client migrations to the chart's
`values.yaml` Ingress (Coder #46, etc.) didn't trigger the same
`iss` flip because the *Authentik* host stayed put — only the
client's own external URL changed. Two related but distinct
gotchas to remember:

- **Authentik `iss` flip:** every downstream OIDC client must
  bounce when Authentik's external host changes.
- **OIDC redirect URI allowlist:** when a *client*'s external URL
  changes (e.g. Coder → `coder.vigihome.net`), Authentik's provider
  redirect URI list must include the new callback URL **byte-for-
  byte**. Strict mode rejects `http` vs `https` and trailing-slash
  mismatches.

**Lint:** the kubeconform CI filter at `.github/workflows/lint.yml`
covers `namespace.yaml`, `pv-pvc.yaml`, `secret.example.yaml`,
`deployment.yaml`, `*-job.yaml`, `*-cronjob.yaml`, `clusterissuer-*.yaml`,
`certificate.yaml`, `ingress-*.yaml`. When a new service uses a
filename outside that set, add it to the filter rather than letting
it silently bypass validation.

## Secrets discipline

- **Secrets never enter the repo.** The repo is public; betterleaks
  (the maintained gitleaks successor) runs as a pre-commit hook via the
  pre-commit framework (`.pre-commit-config.yaml`), and again in CI.
- Source of truth for every secret is Bitwarden. Items are named
  `Homelab <Service>` (e.g., `Homelab Restic Repository`, `Homelab Authentik`, `Homelab Coder`).
- k8s Secrets are created via `kubectl create secret generic` invocations
  that read values from Bitwarden CLI at apply time. Each service's
  README walks through its specific Secret keys.
- `secret.example.yaml` files document the *shape* of each Secret
  (keys and their roles) but use `REPLACE_WITH_*` placeholders. Don't apply
  them — they exist for documentation only.
- Storj S3 access keys live ONLY in `/etc/rclone/rclone.conf` (root:root
  0600\) and `~/.homelab-opentofu.env`. The cluster gets them via
  `kubectl create secret` from sourced env vars.

## DNS pattern (recurring gotcha)

Pi-hole serves custom records to the LAN and tailnet via two
mechanisms in `/opt/pihole/etc-pihole/pihole.toml` (managed by the
Pi-hole admin UI, persisted on the PV, captured by the nightly
restic backup — but **not** in this repo):

- **`misc.dnsmasq_lines` wildcard** — the canonical source of
  truth for `*.vigihome.net`. Two lines (order matters for
  sequential resolvers; LAN first so on-LAN clients don't pay
  tailnet overhead):

  ```
  address=/vigihome.net/192.168.50.135
  address=/vigihome.net/100.92.2.25
  ```

  Any new `*.vigihome.net` host resolves automatically — no
  per-service record needed. See `k8s/pihole/values.yaml`'s big
  DNS comment for the full rationale.

- **`dns.hosts` per-record list** — for one-off A records that
  don't fit a pattern (e.g. `pi.hole` itself, hardware on the LAN
  without its own DNS like a printer or NAS). Avoid for
  `*.vigihome.net` hosts — the wildcard above covers those.

The legacy `*.home` namespace (`jellyfin.home`, `uptime.home`,
`coder.home`, etc.) has been **fully retired**. Any leftover
`dns.hosts` entries for `*.home` are dead and can be cleaned up.

**CoreDNS inside the cluster does not see Pi-hole's records** — it
uses upstream DNS but is configured separately. So inside a pod,
`*.vigihome.net` hostnames may NXDOMAIN or resolve to gandalf's
external IPs (defeating the purpose). Always use cluster-internal
service DNS for pod-to-pod traffic:
`<svc>.<namespace>.svc.cluster.local`. Examples:

- `jellyfin.media.svc.cluster.local:8096`
- `pihole-web.networking.svc.cluster.local`
- `uptime-kuma.monitoring.svc.cluster.local:3001`

## Backup wiring

The nightly restic CronJob at `k8s/backup/backup-cronjob.yaml` mounts
persistent dirs from gandalf via hostPath and pushes encrypted
snapshots to Storj. **Any new persistent dir under `/opt/<service>/`
needs adding to that CronJob** — pattern: new `volume`, `volumeMount`, and
`restic backup --tag <service>` block. Repo password lives in
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
- **Authentik UI-only configuration state**. The recovery flow, the
  Coder/Outline OIDC providers + apps + group bindings, the groups, and
  the `email` scope-mapping override are now captured as Blueprints in
  `k8s/authentik/blueprints/` (#104) — a from-scratch rebuild
  reconstructs them from the `authentik-blueprints` ConfigMap without a
  postgres restore. Remaining DB-only state (lost with the DB if
  `AUTHENTIK_SECRET_KEY` is gone): users, group *memberships*, sessions,
  and event history. The postgres PVC is still in the nightly restic
  backup. See `k8s/authentik/README.md` and `blueprints/README.md`.
- **Prometheus TSDB and Alertmanager data** (#76). Both live on disposable
  `local-path` and are deliberately excluded — large, churny, and
  reconstructable (metrics re-scrape; silences are transient). Only Grafana's
  `/opt/grafana` (dashboards and settings) is in restic, tag `grafana`. See
  `k8s/kube-prometheus-stack/README.md`.

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

## Markdown / doc style

- **Semantic line breaks** — break source lines at sentence/clause
  boundaries, not arbitrary column wraps. mdformat (`--wrap keep`)
  preserves them, and clause-intact lines never get a stray `+`/`-`/`N)`
  at a line-start that a CommonMark formatter would misread as a list item.
- **Write "and"/"&", never `+`, to mean "and" in prose** — more accessible
  to screen readers, and avoids the line-start ambiguity above.
- Markdown is formatted by **mdformat** (see `.pre-commit-config.yaml`), not
  prettier — prettier rewrites embedded code blocks and mangles
  snake_case-near-emphasis in these identifier-heavy docs.

## Tracking open work

Open work — known gaps, deferred features, audit follow-ups — lives
as **GitHub issues** on this repo. Markdown docs (audit doc, READMEs,
specs) reference work by issue number (e.g. `#62`) rather than
describing the work inline. The issue list is the source of truth for
follow-up status.

**Disclosure screen before filing.** This repo is public. Before
opening an issue, ask: *would this be the first public mention of a
known-but-unpatched weakness?* If yes, route privately (Todoist, a
private notes repo, or resolve before public mention). If no — the
common case, because the audit doc already documents posture openly —
file on GitHub.

**What does NOT belong as an issue:**

- "Things deliberately not done" (accepted risks). Issues imply
  intent to resolve; these are decisions. Document in the relevant
  doc's "Things deliberately not done" section instead.
- Daily-flow tasks not tied to this repo. Those live in Todoist.
- In-conversation context or implementation details. Those live in
  the relevant spec or plan doc.

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
