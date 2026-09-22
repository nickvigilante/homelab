# loki

Log aggregation for the homelab (#116): Loki stores 30 days of logs, and Grafana Alloy collects them.
Design: `docs/superpowers/specs/2026-09-21-loki-log-aggregation-design.md`.

## Layout

- `helmrelease.yaml` holds three Flux `HelmRelease`s: `loki` (Monolithic, on gandalf), `alloy` (a DaemonSet on every node), and `alloy-events` (one replica on gandalf).
- `rbac.yaml` is least-privilege RBAC for both Alloy releases, which set `rbac.create: false` because the chart's default ClusterRole reads Secrets cluster-wide.
- `datasource.yaml` registers Loki in Grafana through kps-grafana's datasource sidecar.
- `netpol-loki.yaml` restricts Loki's API to the `monitoring` namespace, since Loki runs without auth.
- `kustomization.yaml` also pulls in `../../sources/grafana-community-loki.yaml` and `../../sources/grafana.yaml`.

Reconciled by the `loki` Flux Kustomization (`clusters/gandalf/loki.yaml`).

## What is collected

| `job`     | Source                          | Labels besides `job`                              |
| --------- | ------------------------------- | ------------------------------------------------- |
| `pods`    | `/var/log/pods` on every node   | `namespace`, `container`, `app`, `stream`, `node` |
| `journal` | systemd journal on every node   | `unit`, `level`, `node`                           |
| `events`  | Kubernetes events, cluster-wide | none                                              |

`stream` is `stdout` or `stderr`, from Alloy's `stage.cri`.
Loki also adds its own `service_name` label to every stream.
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

The compactor deletes data older than 30 days (`retention_period: 720h`), and that retention window is the real bound on disk usage, not the PVC size.
The 20Gi `local-path` PVC on gandalf is a request, not a cap: k3s's local-path provisioner reports gandalf's whole filesystem (about 437 GiB) in kubelet volume stats, so Loki can grow past 20Gi and `KubePersistentVolumeFillingUp` only fires once the whole disk nears full.
Resizing the PVC does nothing to change this.
It is deliberately not backed up by restic: like the Prometheus TSDB, it is large, churny, and worthless after a rebuild.

## Troubleshooting

- **No logs from one node.** Check that node's `alloy` pod log for `permission denied`; Alloy runs as root with all capabilities dropped, which reads root-owned files but nothing owned by other users.
- **No journald from a Pi.** The Pi may keep its journal somewhere other than `/var/log/journal` or `/run/log/journal`; check `journalctl --header` on the node.
- **Loki rejects writes.** Kubelet volume stats report gandalf's whole filesystem, not what Loki has actually used, so check real disk usage with `du -sh` on the PV directory under `/var/lib/rancher/k3s/storage/`.
  Lower retention or filter noisy streams if it's actually full.
- **Backfilled lines rejected after a restart or outage.** On Alloy's first start, or after an outage of about an hour or more, backfilled lines can be rejected as "entry too far behind" or rate-limited.
  Only the history from that gap is affected; steady-state ingestion is fine.
