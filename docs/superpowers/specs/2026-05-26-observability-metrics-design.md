# Observability (A): Metrics Stack — Design

**Issue:** #76 (observability stack). This spec covers **sub-project A — metrics only**.
Logs (Loki) are sub-project B, tracked separately as #116. Smaller deferred
enhancements are tracked as #117.

**Goal:** Deploy a metrics + alerting stack on the k3s homelab — Prometheus
scraping cluster/node metrics, Grafana for dashboards (behind Authentik OIDC),
and Alertmanager emailing alerts — so the lab has the "request rates, latency,
resource utilization, fire-on-failure" tier that Uptime Kuma's up/down checks
don't provide.

**Non-goals (this sub-project):** log aggregation/Loki (#116); per-app
ServiceMonitors, long-term/remote storage, Prometheus HA, and forward-auth
exposure of Prometheus/Alertmanager (#117).

## Architecture

`kube-prometheus-stack` (community Helm chart, version-pinned), Helm-managed in
the existing **`monitoring`** namespace (alongside Uptime Kuma). New repo
directory `k8s/kube-prometheus-stack/` follows the chart-managed service layout
(`values.yaml` + `README.md` + a pre-created hostPath PV for Grafana).

Chart-first per `CLAUDE.md`: kube-prometheus-stack is the de-facto standard,
multi-arch, and bundles Prometheus + Grafana + Alertmanager + node-exporter +
kube-state-metrics + curated k8s dashboards and alert rules, all pre-wired.

**Scheduling:** Prometheus, Grafana, Alertmanager, and kube-state-metrics pin to
**gandalf** (amd64, 8 CPU / 31 GB, ~24% used) via `nodeSelector`. node-exporter
runs as a **DaemonSet** across all three nodes (gandalf amd64 + frodo/samwise
arm64; multi-arch images, tolerations so it schedules everywhere) to collect
host metrics from the Pis too.

## Components & data flow

| Component          | Role                                                                                                  | Placement            |
| ------------------ | ----------------------------------------------------------------------------------------------------- | -------------------- |
| Prometheus         | Scrape & store metrics (kube API, node-exporter, kube-state-metrics, the stack's own ServiceMonitors) | gandalf              |
| Alertmanager       | Route firing alerts → email                                                                           | gandalf              |
| Grafana            | Dashboards; Prometheus pre-wired as default datasource                                                | gandalf              |
| node-exporter      | Per-node host metrics (CPU/mem/disk/net)                                                              | DaemonSet, all nodes |
| kube-state-metrics | Kubernetes object state (deployments, pods, PVCs…)                                                    | gandalf              |

Flow: node-exporter + kube-state-metrics + kube components → scraped by
Prometheus → visualized in Grafana / evaluated against alert rules →
Alertmanager → email.

## Storage

- **Prometheus TSDB:** `local-path` volumeClaimTemplate, gandalf-pinned,
  **20 Gi, 15-day retention**. **Deliberately not backed up** — large, churny,
  and reconstructable; documented in the README's "what we don't back up".
- **Alertmanager:** `local-path`, ~2 Gi, gandalf-pinned. Not backed up
  (silences are transient).
- **Grafana:** pre-created **hostPath PV at `/opt/grafana`** (gandalf,
  `storageClass: manual`, consumed via `persistence.existingClaim`), so the
  nightly restic CronJob captures user-added dashboards/settings. `/opt/grafana`
  is added to `k8s/backup/backup-cronjob.yaml` (new hostPath volume + mount +
  `restic backup --tag grafana` block).

local-path is node-local and `WaitForFirstConsumer`; pinning the pods to gandalf
keeps each volume co-located on gandalf.

## Auth (Grafana)

Native **OIDC via Authentik** plus a **local-admin fallback** (SPOF discipline:
Grafana must stay reachable to diagnose outages when Authentik itself is down).

- **New Authentik blueprint** `k8s/authentik/blueprints/applications/grafana.yaml`
  — OAuth2 provider `grafana` (confidential, redirect
  `https://grafana.vigihome.net/login/generic_oauth`, openid/email/profile
  mappings, implicit-consent flow), application `slug: grafana`
  (`meta_launch_url: https://grafana.vigihome.net`), PolicyBinding →
  `homelab-users`. `client_secret` injected via `!Env`
  (`AUTHENTIK_OIDC_GRAFANA_SECRET`) from `authentik-oidc-secrets`, exactly like
  Coder/Outline (#104).
- **One secret, two consumers:** generate a client secret, store in Bitwarden
  `Homelab Grafana` (field `oidc-client-secret`, matching the per-app
  convention). It feeds Authentik (via `authentik-oidc-secrets`) **and** Grafana
  (via a `grafana-oidc` k8s Secret created from Bitwarden at deploy time).
- **Grafana config:** `auth.generic_oauth` pointed at `authentik.vigihome.net`
  endpoints; `role_attribute_path` maps the Authentik `homelab-users` group →
  Grafana Admin/Editor. `admin_user`/`admin_password` set from the `grafana-oidc`
  Secret (Bitwarden `Homelab Grafana`) for the local-admin fallback.

## Networking

- **Grafana:** Ingress at `https://grafana.vigihome.net` (Traefik `websecure`,
  `tls.secretName: vigihome-tls`). The `monitoring` namespace is already in
  `cert-manager/certificate.yaml`'s reflection list — no cert plumbing needed.
  DNS resolves via the existing Pi-hole `*.vigihome.net` wildcard.
- **Prometheus + Alertmanager:** **ClusterIP only, no Ingress.** They have no
  authentication of their own (open admin APIs — TSDB delete, silence
  create/read), so they get no standing network-exposed surface. Access for
  occasional debugging is via `kubectl port-forward`; routine querying/silencing
  happens through authenticated Grafana (Explore + Alertmanager view). Exposing
  them later (behind Authentik forward-auth) is tracked in #117.

## Alerting

Alertmanager → **email via the Forward Email SMTP relay** already used by the
restic backup job.

- Add `monitoring` to the `reflection-auto-namespaces` annotation on the
  `auth/smtp-relay` Secret (currently `backup`); reflector mirrors it into
  `monitoring`.
- Wire SMTP via `smtp_auth_password_file` (mount the reflected `smtp-relay`
  Secret) so no credential lands in `values.yaml`. Sender = the relay username;
  receiver = `vigihome-admin@vigiemail.com` (same inbox as backup alerts).
- Keep the chart's **bundled default alert rules** (node down, disk/inode
  pressure, `KubePodCrashLooping`, target down, etc.) and the **Watchdog**
  always-firing heartbeat (so "no alerts at all" is itself detectable).

## Homepage integration

Add Grafana to Homepage as a **live widget** (not a link tile), per the
widgets-preferred convention: `widget: {type: grafana, url: <in-cluster Grafana svc address>, ...}` with a Grafana service-account token in
`homepage-secrets` (key `grafana-token`, sourced from Bitwarden). The widget
targets the direct in-cluster address, not the Authentik-gated Ingress.

## Staged rollout

The implementation plan will sequence these so each stage is independently
verifiable:

1. **Core stack** — install kube-prometheus-stack with Grafana Ingress
   disabled and Alertmanager receivers as no-op; verify all pods Running,
   node-exporter on all 3 nodes, Prometheus targets UP.
2. **Grafana access** — Grafana hostPath PV + Ingress + OIDC blueprint +
   `grafana-oidc` Secret; verify `grafana.vigihome.net` serves a trusted cert,
   OIDC login works for a `homelab-users` member, and the local-admin fallback
   works.
3. **Alert routing** — reflect `smtp-relay` into `monitoring`, wire the email
   receiver; verify a synthetic alert (or the Watchdog) delivers to the inbox.
4. **Integration** — add `/opt/grafana` to the restic CronJob; add the Grafana
   Homepage widget.

## Acceptance criteria

- All kube-prometheus-stack pods Running; node-exporter DaemonSet Ready on
  gandalf + frodo + samwise.
- Prometheus "Targets" page shows the cluster/node/kube-state targets UP.
- `https://grafana.vigihome.net` loads with a Let's Encrypt cert; the bundled
  k8s dashboards populate with live data.
- A `homelab-users` member logs into Grafana via Authentik; the local-admin
  account also works (Authentik-independent).
- A test alert (or Watchdog) is delivered to `vigihome-admin@vigiemail.com`.
- Prometheus/Alertmanager are reachable only via `kubectl port-forward` (no
  Ingress).
- `/opt/grafana` appears in the restic backup; Grafana shows as a live widget
  on Homepage.
- `helm`/`kubeconform`/`yamllint` CI clean.
