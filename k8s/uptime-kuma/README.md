# uptime-kuma

Self-hosted status board / monitor. Watches HTTP/TCP/Ping endpoints and
receives Push heartbeats from CronJobs (e.g. restic) so a silently broken
backup makes noise.

## Layout

- `namespace.yaml` — `monitoring` namespace
- `pv-pvc.yaml` — PV `uptime-kuma-data` → hostPath `/opt/uptime-kuma/data` (pinned to gandalf), matching PVC
- `values.yaml` — Helm values for `uptime-kuma/uptime-kuma`

## Install

```bash
helm repo add uptime-kuma https://dirsigler.github.io/uptime-kuma-helm
helm repo update uptime-kuma

kubectl apply -f namespace.yaml -f pv-pvc.yaml

helm install uptime-kuma uptime-kuma/uptime-kuma \
  -n monitoring --version 4.1.0 -f values.yaml
```

No Pi-hole DNS edits needed — `uptime-kuma.vigihome.net` is resolved by
the wildcard `address=/vigihome.net/...` directive in Pi-hole's
`misc.dnsmasq_lines` (see `k8s/pihole/values.yaml`).

## First-run setup

Open https://uptime-kuma.vigihome.net/ — Uptime Kuma will prompt for:

1. **Database type:** pick **SQLite**. Embedded MariaDB exists for users who
   outgrow SQLite; you're not anywhere near that with a handful of monitors,
   and the SQLite file at `/opt/uptime-kuma/data/kuma.db` is already covered
   by the restic backup CronJob.
2. **Admin user:** save the credentials to Bitwarden as
   `Homelab Uptime Kuma`.

## Pitfalls worth knowing

### Monitor URLs must use cluster-internal DNS, not `*.home`

Uptime Kuma's pod uses CoreDNS as its resolver. CoreDNS forwards external
queries to the node's upstream DNS — which is _not_ Pi-hole. So
`jellyfin.home` and friends are `NXDOMAIN` from inside the pod.

Use cluster Service DNS instead. This is also the _better_ monitor URL
because it tests the actual pod rather than the ingress chain, and it
keeps working while Pi-hole is broken (otherwise: chicken-and-egg).

| Service          | Use this URL                                            |
| ---------------- | ------------------------------------------------------- |
| Jellyfin         | `http://jellyfin.media.svc.cluster.local:8096/health`   |
| Pi-hole          | `http://pihole-web.networking.svc.cluster.local/admin/` |
| Uptime Kuma self | `http://uptime-kuma.monitoring.svc.cluster.local:3001/` |

`/health` for Jellyfin (not `/`) — the root path 302s to relative `web/`
which axios handles oddly under `UPTIME_KUMA_BEHIND_PROXY=1`. `/health`
returns a clean 200 with no redirects.

### Push monitor heartbeat interval ≠ probe interval

For HTTP/TCP monitors, "Heartbeat Interval" is how often Uptime Kuma
**probes**. For Push monitors, it's the **stale threshold** — how long
without a push before the monitor flips DOWN. Default is 60s, which makes
no sense for a daily backup. Set:

| Monitor         | Heartbeat Interval                   |
| --------------- | ------------------------------------ |
| `restic-backup` | **90000s** (25h — daily + 1h drift)  |
| `restic-forget` | **691200s** (8d — weekly + 1d drift) |

### CronJob pings via cluster DNS, not `uptime.home`

The CronJob secret stores the Push URL with the in-cluster service name,
because — see above — `uptime.home` doesn't resolve from inside a pod.
The user-facing token in the Uptime Kuma UI ends in `.../api/push/<token>`;
just swap the host part to `uptime-kuma.monitoring.svc.cluster.local:3001`
when you build the secret. See `../backup/README.md` step 5 for the
exact `kubectl create secret` invocation.

## Operations

Helm upgrade:

```bash
helm -n monitoring upgrade uptime-kuma uptime-kuma/uptime-kuma -f values.yaml
kubectl -n monitoring rollout status deployment/uptime-kuma
```

The DB lives in the PV (`/opt/uptime-kuma/data/kuma.db`) and is backed up
nightly by the restic CronJob under tag `uptime-kuma-data`. To restore,
shut down the deployment, restore the file, bring it back up:

```bash
kubectl -n monitoring scale deployment/uptime-kuma --replicas=0
# restore /opt/uptime-kuma/data/kuma.db from restic
kubectl -n monitoring scale deployment/uptime-kuma --replicas=1
```
