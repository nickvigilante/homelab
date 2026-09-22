# Loki log aggregation — pod, node and event logs in Grafana — Design

**Issue:** #116 (Observability sub-project B: Loki and log aggregation, wired into Grafana).
**Builds on:** [the observability metrics design](2026-05-26-observability-metrics-design.md) (sub-project A), whose Grafana this reuses.

**Goal:** Keep 30 days of searchable logs from every pod, every node's systemd journal, and Kubernetes events, queryable from the existing Grafana.
The immediate motivation is post-incident debugging: on 2026-09-20 and 2026-09-21 the evidence for two separate problems (Grafana's startup log and the Flux controller logs) had already been lost to container log rotation before anyone looked, and Kubernetes events expire after about an hour.

**Non-goals (this sub-project):**

- Alerting on log patterns.
  Loki's single-binary mode includes the ruler, so this is a later configuration change once a month of data shows which patterns matter (follow-up issue).
- Longer retention for security or audit streams.
  Per-stream retention can add it later without changing this design; it belongs with #59 and #126 (follow-up issue).
- Object storage.
  Chunks live on local disk; a move to Storj S3 is the path if retention must grow or logs must survive a rebuild.
- Log dashboards.
  Grafana Explore is the query surface for v1.
- Any ingress, UI, or tailnet exposure of Loki.

## Architecture

```
every node (gandalf, frodo, samwise)
  alloy (DaemonSet) ──┬─ /var/log/pods/**           → job="pods"
                      └─ /var/log/journal, /run/log/journal → job="journal"
gandalf
  alloy-events (Deployment, 1 replica) ── k8s API events → job="events"

          all three push over in-cluster HTTP
                         │
                         ▼
  loki (Monolithic StatefulSet, gandalf) ── local-path PVC, 20Gi, 30d retention
                         ▲
  kps-grafana ── datasource uid `loki` (sidecar-loaded ConfigMap)
```

Everything runs in the `monitoring` namespace, beside Prometheus and Grafana.

### Components

| Component      | Chart and source                                                       | Shape                                      | Purpose                                |
| -------------- | ---------------------------------------------------------------------- | ------------------------------------------ | -------------------------------------- |
| `loki`         | `loki` from `grafana-community` (18.13.4, Loki 3.7.8 as of 2026-09-21) | Monolithic StatefulSet, 1 replica, gandalf | Log store and query API on port 3100   |
| `alloy`        | `alloy` from `grafana` (1.12.1, Alloy v1.19.2 as of 2026-09-21)        | DaemonSet, all nodes                       | Pod-log and journald collection        |
| `alloy-events` | same `alloy` chart, second release                                     | Deployment, 1 replica, gandalf             | Kubernetes event collection            |
| datasource     | raw ConfigMap labeled `grafana_datasource: "1"`                        | —                                          | Registers Loki in the existing Grafana |

The `loki` chart moved to the `grafana-community` repository, which already backs `grafana-mcp`; the copy in the `grafana` repository stopped at 7.3.0 in August 2026.
Alloy is only published in the `grafana` repository, which becomes a new source.
Pin the newest versions available when the plan is executed and record them in the HelmReleases.

## Deployment method

Flux, following the service directory convention.

- `k8s/loki/helmrelease.yaml` holds the three `HelmRelease`s, with values and the Alloy configuration inline, as `k8s/claude-mcp/` does with two.
- `k8s/loki/datasource.yaml` is the Grafana datasource ConfigMap.
- `k8s/loki/rbac.yaml` is the raw least-privilege RBAC for both Alloy releases.
- `k8s/loki/kustomization.yaml` lists both, plus `../../sources/grafana-community-loki.yaml` and the new `../../sources/grafana.yaml`.
  A second name for the same repository keeps two pruning Kustomizations from owning one object.
- `k8s/loki/README.md` is the runbook.
- `clusters/gandalf/loki.yaml` is a new Flux `Kustomization` with `prune: true` (Flux owns the whole directory), `wait: true`, and `dependsOn: [{ name: kps }]`, because the Grafana it registers with belongs to kps.

`.github/workflows/lint.yml`'s kubeconform filename filter gains `datasource.yaml` and `rbac.yaml`, so the ConfigMap and RBAC manifest are validated rather than silently skipped.

## Collection (Alloy)

### Pod logs

`discovery.kubernetes` finds pods scheduled on the local node (field selector on `spec.nodeName`), and `loki.source.file` tails their files under `/var/log/pods`, mounted read-only from the host.
Reading files, rather than streaming through the API server with `loki.source.kubernetes`, keeps log collection off the control plane.
Read positions live on a hostPath (/var/lib/alloy) so a restart resumes rather than re-reading.

### Node journald

`loki.source.journal` reads the systemd journal.
Both `/var/log/journal` (persistent, as on Ubuntu) and `/run/log/journal` (volatile, often the Pi default) are mounted read-only, so the source works whichever the node uses.
`max_age` is 12h, so a first start does not replay days of history.

### Kubernetes events

`loki.source.kubernetes_events` watches events cluster-wide.
It runs in the separate single-replica `alloy-events` release because a DaemonSet would ingest every event once per node.
The Alloy chart's default ClusterRole grants far more than needed, including Secrets cluster-wide, and its template breaks when either rule list is empty, so both Alloy releases set rbac.create: false and use a raw least-privilege rbac.yaml: pods for alloy, events for alloy-events.

### Labels

Index labels stay low-cardinality: `job` (`pods`, `journal` or `events`), `namespace`, `container`, `app`, and `node`, plus `unit` and `level` for journald.
`app` comes from `app.kubernetes.io/name`, falling back to the `app` label.
The pod name is attached as structured metadata, not an index label, because Coder workspace pods get a new name on every rebuild and would otherwise create a new stream each time.
It stays filterable, for example `{namespace="coder"} | pod="coder-65b7b8695b-wg87d"`.

No streams are dropped in v1; filtering waits for measured volume (follow-up issue).

## Storage and retention

- Loki runs `deploymentMode: Monolithic` with one replica, `auth_enabled: false`, replication factor 1, a TSDB index on schema v13, and filesystem object storage.
  "Monolithic" is the chart's current name for what older versions called SingleBinary.
- The chart's gateway, chunks and results caches, bundled MinIO, canary, test pod, and read/write/backend replicas are disabled.
- The chart's rules sidecar is disabled: it reads ConfigMaps and Secrets cluster-wide and only exists to load ruler rules.
- Data lives on a `local-path` PVC of 20Gi on gandalf, the same pattern as the Prometheus TSDB.
- The compactor enforces retention: `retention_enabled: true`, `retention_period: 720h`, with a filesystem delete-request store.
- Loki data is **deliberately not backed up** by restic, for the same reasons as the Prometheus TSDB: it is large, churny, and worthless after a rebuild.

### Sizing

Container logs currently on disk total about 150 MiB, a snapshot capped by rotation rather than a daily rate.
Extrapolating from Grafana's rotation interval gives an estimate of 100–500 MB/day raw, and Loki compresses log text roughly tenfold, so 30 days should occupy about 0.3–1.5 GB.
20Gi leaves a wide margin for that estimate being wrong, and one week after rollout the real ingestion rate is checked against it.

## Resources

| Workload       | Requests        | Limits       |
| -------------- | --------------- | ------------ |
| `loki`         | 100m CPU, 512Mi | 1Gi memory   |
| `alloy`        | 50m CPU, 64Mi   | 256Mi memory |
| `alloy-events` | 25m CPU, 64Mi   | 128Mi memory |

`alloy`'s limit fits samwise (3.7 GiB total), and `loki` fits comfortably on gandalf (30 GiB, about 23 GiB available on 2026-09-21).

## Grafana integration

`datasource.yaml` registers a Loki datasource with uid `loki`, URL `http://loki.monitoring.svc.cluster.local:3100`, and `isDefault: false`.
kps-grafana's datasource sidecar already watches the `monitoring` namespace for this label, so kube-prometheus-stack needs no change.
The in-cluster Grafana MCP server gains Loki query tools automatically once the datasource exists.

## Self-monitoring and failure modes

The Loki and Alloy charts' ServiceMonitors are enabled so Prometheus scrapes their ingestion and error metrics.
No new alert rules are added; existing ones cover the failure modes.

| Failure              | Effect                                                                        | Existing coverage                   |
| -------------------- | ----------------------------------------------------------------------------- | ----------------------------------- |
| PVC fills            | Loki rejects writes; Alloy buffers, then drops                                | kps `KubePersistentVolumeFillingUp` |
| Loki down            | Alloy retries with backoff; a long outage leaves a gap of up to about an hour | `KubePodNotReady`                   |
| Alloy down on a node | Pod logs wait on the node until rotation; Alloy resumes from saved positions  | `KubeDaemonSetRolloutStuck`         |
| gandalf lost         | All stored logs lost                                                          | Accepted, as for Prometheus         |

## Verification

1. `flux get helmreleases -n monitoring` shows `loki`, `alloy` and `alloy-events` Ready.
2. Restart Grafana, then `{job="pods", namespace="monitoring", container="grafana"}` returns its startup lines.
3. `{job="journal", node="samwise"}` returns `k3s-agent` entries, proving the volatile-journal mount.
4. `{job="events"}` shows the events from that Grafana restart, each exactly once.
5. Loki's `/config` endpoint shows `retention_period: 720h` and compactor retention enabled.
6. One week later, PVC usage and `loki_distributor_bytes_received_total` are compared against the sizing estimate.

## Documentation changes

- New `k8s/loki/README.md`: setup, example LogQL queries, troubleshooting, and the no-backup decision.
- `k8s/kube-prometheus-stack/README.md`: the "Logs (Loki) are #116" line points to `k8s/loki/`.
- `CLAUDE.md`: Loki joins Prometheus under "What we don't back up".
- `k8s/claude-mcp/README.md`: the log-rotation caveat points agents to Loki for history.

## Acceptance criteria

- All three releases are Flux-managed, pinned, and Ready.
- Pod logs, journald from all three nodes, and Kubernetes events are queryable in Grafana Explore through the `loki` datasource.
- Retention is 30 days, enforced by the compactor.
- CI validates every new manifest, including `datasource.yaml`.
- The follow-up issues below are filed, and the PR closes #116.

## Follow-up issues

1. Loki ruler: alert on log patterns through the existing Alertmanager.
2. Longer per-stream retention for security and audit streams (links #59 and #126).
3. Drop or sample noisy streams once a week of volume data exists.

## Things deliberately not done

- **No restic backup of Loki data** — see "Storage and retention".
- **No `k8s-monitoring` umbrella chart.** It generates Alloy configuration but is built around metrics that overlap kube-prometheus-stack, and it is the kind of bundled wrapper this repo avoids.
- **No `loki-stack` chart.** It is deprecated and based on Promtail, which is itself deprecated in favor of Alloy.
