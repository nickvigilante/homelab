# Homelab alert rules and health dashboard (#138)

**Status:** approved 2026-06-27.
**Issue:** [#138](https://github.com/nickvigilante/homelab/issues/138).

## Goal

Add the homelab-specific Prometheus alert rules that the bundled
kube-prometheus-stack default rules do not cover,
plus one at-a-glance "Homelab Health" Grafana dashboard.
Everything is declarative config reconciled by Flux.
Per-service application dashboards are explicitly out of scope —
they need exporters or ServiceMonitors that do not exist yet,
and are deferred to [#117](https://github.com/nickvigilante/homelab/issues/117).

## Background and what already exists

The kube-prometheus-stack chart (v85.3.3) installs its default rule set,
because `defaultRules` is not overridden in
`k8s/kube-prometheus-stack/values.yaml`.
That default set already covers the infrastructure basics:
node-exporter disk and memory pressure,
`KubePersistentVolumeFillingUp`,
`KubeNodeNotReady`,
`KubePodCrashLooping`,
and Prometheus and Alertmanager self-monitoring.
Re-implementing those would be duplication, so this work does not touch them.

Alertmanager already routes every firing alert to email
through the Forward Email relay (single `email` receiver),
and that path is proven end-to-end (#154).
New rules inherit it automatically — no Alertmanager change is needed.

What is genuinely missing is the homelab-specific signal:
the nightly restic backup failing,
a cert-manager certificate drifting toward expiry because DNS-01 renewal broke,
and containers being OOMKilled (the #176 Grafana failure mode).

Scrape coverage today is the bundled exporters only —
node-exporter, kube-state-metrics, kubelet/cadvisor, and the stack's own components —
plus the Flux PodMonitor in `k8s/flux-monitoring/`.
No application exposes custom metrics yet,
and cert-manager's metrics endpoint is not scraped.

## Scope

In scope:

- Three new alert rules in a `homelab` PrometheusRule group.
- Enabling cert-manager's ServiceMonitor so the cert-expiry rule has data.
- One "Homelab Health" overview dashboard built on already-scraped metrics.

Out of scope (deferred to #117):

- Per-service application dashboards (Jellyfin, Coder, Outline, Pi-hole, Syncthing).
- Standing up application exporters or per-app ServiceMonitors.
- Blackbox/Storj reachability probing.
- Long-term metric storage and Prometheus HA.

## Components

### 1. PrometheusRule — group `homelab`, namespace `monitoring`

Three rules, each labelled `severity: warning`,
all flowing to the existing email receiver:

| Alert                     | Signal                                                                                                  | Fires when                                                                                                                                                                                     |
| ------------------------- | ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ResticBackupStale`       | `kube_cronjob_status_last_successful_time{cronjob="restic-backup", namespace="backup"}`                 | No successful nightly backup in >25h. A single rule on last-success-time catches both ran-and-failed and did-not-run-at-all.                                                                   |
| `ContainerOOMKilled`      | `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` correlated with a recent restart | A container was OOMKilled recently, scoped by namespace/pod/container so it reflects a fresh kill rather than a stale last-reason gauge.                                                       |
| `CertificateExpiringSoon` | `certmanager_certificate_expiration_timestamp_seconds - time()`                                         | Any cert is under 14 days from expiry. Renewal runs at 30 days before expiry, so \<14d means DNS-01 renewal has been failing for ~16+ days — a real safety net for a revoked Cloudflare token. |

Disk, PV, node-down, and crashloop alerts are intentionally absent —
the bundled default rules own them.

Implementation note for the plan:
the exact metric name `kube_cronjob_status_last_successful_time`
must be confirmed present in this chart's kube-state-metrics version before relying on it.
The OOMKilled expression must avoid the sticky-last-reason gauge trap —
correlate with `changes(kube_pod_container_status_restarts_total[...])`
so it reflects a recent kill, not a long-past one.

### 2. cert-manager ServiceMonitor (prerequisite for the cert alert)

cert-manager is helm-managed via `k8s/cert-manager/values.yaml` with no metrics scraping today.
Enable it by setting `prometheus.servicemonitor.enabled: true`
and applying a **pinned** `helm upgrade` on the cert-manager release.
The chart already exposes a metrics service on port 9402;
this just adds the ServiceMonitor.
Prometheus discovers all ServiceMonitors
(`serviceMonitorSelectorNilUsesHelmValues: false`),
so certs start scraping immediately and
`certmanager_certificate_expiration_timestamp_seconds` becomes available.

This is the only piece that touches a second release.
It is low-risk — additive only, no change to issuance behaviour.

### 3. "Homelab Health" Grafana dashboard

One dashboard ConfigMap, label `grafana_dashboard: "1"`, namespace `monitoring`,
picked up by the Grafana sidecar (the same mechanism the Flux dashboards use).
Built entirely on metrics already scraped:

- **Cluster** — per-node up / CPU / memory / root-disk for gandalf, frodo, samwise (node-exporter).
- **Storage** — PV used-percentage per PVC (kubelet volume stats).
- **Backups and Certs** — "hours since last restic backup" stat (from the same metric the alert uses), and a cert-expiry-days table (from cert-manager).
- **Reliability** — top pod restarts, OOMKills in the last 24h, and Flux not-ready count (`gotk_resource_info{ready="False"}`).

The dashboard is a single pane of homelab health,
distinct from the bundled per-namespace Kubernetes dashboards and the Flux dashboards.

### 4. Layout and Flux wiring

Mirrors the existing `k8s/flux-monitoring/` precedent — a dedicated directory with its own Flux Kustomization:

```
k8s/homelab-monitoring/
  prometheusrule.yaml
  dashboards/homelab-health.yaml
  kustomization.yaml
  README.md
clusters/gandalf/homelab-monitoring.yaml
k8s/cert-manager/values.yaml          # servicemonitor flip, applied via helm upgrade
```

The Flux Kustomization reconciles `k8s/homelab-monitoring/` into the cluster.
`prometheusrule.yaml` is already covered by the kubeconform lint filter;
the dashboard ConfigMap follows the flux-monitoring dashboards
(plain ConfigMaps, outside the kubeconform filter, validated server-side by Flux).

## Data flow

1. kube-state-metrics, node-exporter, and cert-manager expose metrics.
2. Prometheus scrapes them (cert-manager newly, via the enabled ServiceMonitor).
3. The `homelab` PrometheusRule group evaluates the three rules.
4. A firing rule reaches Alertmanager, which emails through the Forward Email relay.
5. Grafana's sidecar loads the dashboard ConfigMap; the dashboard queries the same Prometheus datasource.

## Error handling and edge cases

- **Backup never succeeded:** if `kube_cronjob_status_last_successful_time` is absent (a brand-new CronJob), the rule's vector is empty and does not fire. The restic CronJob has run for weeks, so the series is present; an absent-metric companion rule is YAGNI for now.
- **Sticky OOM gauge:** `kube_pod_container_status_last_terminated_reason` stays set until the next termination, so the rule correlates with a recent restart to avoid alerting on a long-resolved kill.
- **Cert alert noise:** a single warning tier at 14 days is enough; renewal at 30 days gives a 16-day buffer before it can fire. A critical \<3d tier is a possible later refinement, not part of this work.

## Testing and verification

No forced alert firing — the email path is already proven (#154), and firing real alerts spams the inbox. Instead:

- Confirm all three rules load without error in Prometheus → Status → Rules (green, no evaluation errors).
- Spot-check each expression's current value in the Prometheus UI returns a sane result (backup age in hours, cert days-remaining, OOM count).
- Confirm the "Homelab Health" dashboard appears in Grafana and its panels render data.
- kubeconform lint passes on the new PrometheusRule.

## Things deliberately not done

- No per-service application dashboards or exporters (→ #117).
- No critical-severity cert tier (single warning tier suffices for now).
- No Storj/backup blackbox reachability probe (the backup-success metric is a sufficient proxy).
- No Alertmanager routing changes (the existing email receiver is inherited).
