# homelab-monitoring

Homelab-specific Prometheus alert rules and the "Homelab Health" Grafana
dashboard, reconciled by the `homelab-monitoring` Flux Kustomization
(`clusters/gandalf/homelab-monitoring.yaml`).

This is the homelab's own observability layer,
distinct from the bundled kube-prometheus-stack default rules
(which cover disk, node-down, PV pressure, and crashloops)
and from `k8s/flux-monitoring/` (which covers Flux reconciliation).

## Alerts (`prometheusrule.yaml`, group `homelab`)

- **ResticBackupStale** — the nightly restic CronJob has not succeeded in >25h.
  One rule on `kube_cronjob_status_last_successful_time` catches both a failed
  run and a missed run. Triage: `kubectl -n backup get jobs`.
- **ContainerOOMKilled** — a container was OOMKilled and restarted in the last
  10m. Triage: raise the container's memory limit (see #176 for precedent).
- **CertificateExpiringSoon** — a cert-manager certificate is \<14d from expiry,
  i.e. automatic renewal (which runs at 30d) has been failing. Triage:
  `kubectl -n cert-manager get certificate,order,challenge`.

All three are `severity: warning` and route to email via the existing
Alertmanager receiver.

## Dashboard (`dashboards/homelab-health.yaml`)

A single "Homelab Health" pane: node CPU/memory/disk, PV usage, backup
freshness, certificate expiry, OOM kills, pod restarts, and Flux readiness.
Loaded by the Grafana sidecar via the `grafana_dashboard: "1"` label.

## Adding an alert or panel

Add rules to the `homelab` group in `prometheusrule.yaml`; validate locally
with `promtool check rules`. Add panels to the dashboard JSON; validate the
JSON parses before committing. Flux reconciles both on the next interval (1h)
or on demand: `flux reconcile kustomization homelab-monitoring`.

## Prerequisite

`CertificateExpiringSoon` and the cert-expiry panels need cert-manager's
ServiceMonitor enabled (`prometheus.servicemonitor.enabled: true` in
`k8s/cert-manager/values.yaml`, applied via `helm upgrade`). Without it those
queries return no data; the rest of the dashboard and the other two alerts
work regardless.
