# loki

Log aggregation for the homelab (#116): Loki stores 30 days of logs, and Grafana Alloy collects them.
Design: `docs/superpowers/specs/2026-09-21-loki-log-aggregation-design.md`.

## Layout

- `helmrelease.yaml` holds three Flux `HelmRelease`s: `loki` (Monolithic, on gandalf), `alloy` (a DaemonSet on every node), and `alloy-events` (one replica on gandalf).
- `rbac.yaml` is least-privilege RBAC for both Alloy releases, which set `rbac.create: false` because the chart's default ClusterRole reads Secrets cluster-wide.
- `datasource.yaml` registers Loki in Grafana through kps-grafana's datasource sidecar.
- `kustomization.yaml` also pulls in `../../sources/grafana-community-loki.yaml` and `../../sources/grafana.yaml`.

Reconciled by the `loki` Flux Kustomization (`clusters/gandalf/loki.yaml`).

## What is collected

| `job`     | Source                          | Extra labels       |
| --------- | ------------------------------- | ------------------ |
| `pods`    | `/var/log/pods` on every node   | `container`, `app` |
| `journal` | systemd journal on every node   | `unit`, `level`    |
| `events`  | Kubernetes events, cluster-wide | —                  |

Every stream also has `namespace` where it applies and `node`.
The pod name is structured metadata, not a label, so filter on it after the selector.

## Querying

Use Grafana → Explore → Loki.

```logql
{job="pods", namespace="monitoring", container="grafana"}
{namespace="coder"} | pod="coder-65b7b8695b-wg87d"
{job="journal", node="gandalf", unit="k3s.service"} |= "error"
{job="events"} |= "Warning"
```

## Retention and storage

The compactor deletes data older than 30 days (`retention_period: 720h`).
Data lives on a 20Gi `local-path` PVC on gandalf.
It is deliberately not backed up by restic: like the Prometheus TSDB, it is large, churny, and worthless after a rebuild.

## Troubleshooting

- **No logs from one node.** Check that node's `alloy` pod log for `permission denied`; Alloy runs as root with all capabilities dropped, which reads root-owned files but nothing owned by other users.
- **No journald from a Pi.** The Pi may keep its journal somewhere other than `/var/log/journal` or `/run/log/journal`; check `journalctl --header` on the node.
- **Loki rejects writes.** The PVC is probably full; `KubePersistentVolumeFillingUp` fires first. Resize the PVC or lower retention.
