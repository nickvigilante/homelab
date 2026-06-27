# kube-prometheus-stack (metrics & alerting)

Metrics, dashboards, and email alerting for the homelab (#76):
Prometheus scrapes cluster/node/kube-state metrics,
Grafana visualizes them behind Authentik OIDC at `https://grafana.vigihome.net`,
Prometheus is exposed at `https://prometheus.vigihome.net`,
Alertmanager is exposed at `https://alertmanager.vigihome.net`,
and Alertmanager emails alerts via the Forward Email relay.

This is sub-project **A — metrics only**.
Logs (Loki) are #116; per-app ServiceMonitors and long-term storage are future work.
Prometheus and Alertmanager are exposed behind Authentik forward-auth (#137, #117).

Chart: `prometheus-community/kube-prometheus-stack` v`85.3.3`
(appVersion `v0.90.1`). Release name `kps`, namespace `monitoring`
(shared with Uptime Kuma).

## Architecture

- **Scheduling:** Prometheus, Alertmanager, Grafana, and kube-state-metrics
  pin to **gandalf** (amd64, RAM headroom) via `nodeSelector`.
  node-exporter runs as a **DaemonSet** on all three nodes
  (gandalf amd64 + frodo/samwise arm64; broad toleration so it schedules
  everywhere) to collect host metrics from the Pis too.
- **Storage:** Prometheus TSDB and Alertmanager use disposable `local-path`
  (node-local, gandalf-pinned). Grafana persists to a pre-created hostPath PV
  at `/opt/grafana` so restic backs up dashboards. See "What we don't back up".
- **Auth:** Grafana native OIDC via Authentik (blueprint
  `../authentik/blueprints/applications/grafana.yaml`) plus a local-admin
  fallback, so Grafana stays reachable to diagnose outages when Authentik
  itself is down.
- **Flux observability:** `kube-state-metrics` runs the upstream Flux
  CRS (Custom Resource State) config so HelmRelease / Kustomization /
  GitRepository status conditions surface as `gotk_resource_info{ready=...}`.
  Without this the Flux dashboards in `../flux-monitoring/dashboards/` and
  the `FluxReconciliationFailure` alert have nothing to evaluate against
  (the controller PodMonitor only emits runtime metrics, not Ready state).
  See the inline comment in `values.yaml` for the trimmed vs upstream diff.

## Prereqs

- cert-manager + reflector running; `monitoring` is already in the
  `vigihome-tls` reflection list (`../cert-manager/certificate.yaml`), so the
  Grafana Ingress cert needs no extra plumbing.
- Pi-hole `*.vigihome.net` wildcard resolves `grafana.vigihome.net` to gandalf.
- `helm` on the laptop/gandalf with kubectl context = homelab, chart repo
  registered:
  ```sh
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
  helm repo update prometheus-community
  ```
- Bitwarden item **`Homelab Grafana`** with fields:
  - `oidc-client-secret` — `openssl rand -hex 64`
  - `admin-user` — e.g. `admin-local` (the local-admin fallback)
  - `admin-password` — a strong password

## One-time install

Run from the repo root with kubectl context = homelab. The stack's
prerequisites (secrets, PV, blueprint, reflected SMTP) must exist **before**
`helm install`, because the Grafana and Alertmanager pods mount them on boot.

1. **Provision the Grafana host dir.** Handled idempotently by
   `ansible/provision-gandalf.yml` (task `Ensure Grafana hostPath dir exists with Grafana-uid ownership` plus the systemd-tmpfiles drop-in that
   re-creates it at every boot if missing). On a fresh gandalf, run the
   playbook before `helm install`:

   ```sh
   # laptop
   ansible-playbook -i ansible/inventory.yml ansible/provision-gandalf.yml
   ```

   The chart's `init-chown-data` initContainer is **intentionally disabled**
   in `values.yaml` because it CrashLoopBackOffs on every pod restart after
   Grafana creates its 0700-mode report-export subdirs (the initContainer
   drops `DAC_OVERRIDE`,
   so root can't traverse them).
   The Ansible task plus tmpfiles drop-in fully replace its role -- do not re-enable.

   Emergency one-liner if Ansible isn't reachable:
   `sudo mkdir -p /opt/grafana && sudo chown 472:472 /opt/grafana && sudo chmod 0755 /opt/grafana`.

2. **The `grafana-secrets` Secret is managed by ESO** via
   `external-secret.yaml` (#161). On a fresh cluster, ensure the
   BWS bootstrap (#135 Tasks 4-7) has populated `bws-access-token` and
   the three secrets exist in the `homelab` BWS project: `grafana-admin-user`,
   `grafana-admin-password`, `grafana-oidc-client-secret`. ESO syncs the
   in-cluster Secret automatically when Flux reconciles
   `clusters/gandalf/kps.yaml`.

3. **Apply the Grafana PV/PVC:**

   ```sh
   kubectl apply -f k8s/kube-prometheus-stack/pv-pvc.yaml
   kubectl -n monitoring get pvc grafana-data   # expect: Bound
   ```

4. **Reflect the SMTP relay into `monitoring`** (so Alertmanager can mount it):

   ```sh
   kubectl -n auth annotate secret smtp-relay --overwrite \
     reflector.v1.k8s.emberstack.com/reflection-auto-namespaces=backup,monitoring
   kubectl -n monitoring get secret smtp-relay   # appears within seconds
   ```

5. **Land the Authentik Grafana OIDC blueprint.** `authentik-oidc-secrets`
   is now managed by ESO (#161) via `k8s/authentik/external-secret.yaml`,
   so adding Grafana to the OIDC clients means: (a) populate
   `grafana-oidc-client-secret` in BWS (already done as part of #161;
   `./scripts/bws-migrate.sh` handles the case), and (b) reference its
   UUID from the `authentik-oidc-secrets` ExternalSecret's `data:` block.
   ESO syncs the in-cluster Secret on the next reconcile.

   Re-render the blueprints ConfigMap, upgrade Authentik (pinned to the
   installed chart version), and trigger discovery (boot can race the
   ConfigMap mount):

   ```sh
   kubectl -n auth create configmap authentik-blueprints \
     $(find k8s/authentik/blueprints -name '*.yaml' -printf '--from-file=%f=%p ') \
     --dry-run=client -o yaml | kubectl apply -f -
   CHART_VER="$(helm list -n auth -f '^authentik$' -o json | jq -r '.[0].chart' | sed 's/^authentik-//')"
   helm -n auth upgrade authentik authentik/authentik --version "$CHART_VER" -f k8s/authentik/values.yaml
   kubectl -n auth rollout status deploy/authentik-worker
   kubectl -n auth exec deploy/authentik-worker -- \
     ak shell -c "from authentik.blueprints.v1.tasks import blueprints_discovery; blueprints_discovery.send()"
   ```

6. **Install the stack** (CRDs + operator + workloads):

   ```sh
   helm install kps prometheus-community/kube-prometheus-stack --version 85.3.3 \
     -n monitoring -f k8s/kube-prometheus-stack/values.yaml
   kubectl -n monitoring rollout status statefulset/prometheus-kps-kube-prometheus-stack-prometheus --timeout=300s
   ```

   The operator installs CRDs then creates the StatefulSets; allow a few
   minutes. If the exact Service/StatefulSet names differ by release, confirm
   with `kubectl -n monitoring get svc,sts` and adjust the targets below.

7. **No Homepage-widget step.** Grafana appears on Homepage as a **plain
   link tile**, not a live widget. The widget needs `GET /api/admin/stats`,
   which Grafana *server admin* gates, and Grafana OSS can't scope that to a
   service account — handing the Homepage pod a server-admin user just for a
   counts tile fails the least-privilege bar. Closed in #155; rationale in
   `../homepage/README.md` "Service widgets".

## Verify

```sh
# node-exporter on all 3 nodes
kubectl -n monitoring get ds -l app.kubernetes.io/name=prometheus-node-exporter   # DESIRED=3 READY=3
# Prometheus targets UP
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 &
curl -s 'http://localhost:9090/api/v1/targets?state=active' \
  | python3 -c "import sys,json; t=json.load(sys.stdin)['data']['activeTargets']; print(f\"{sum(1 for x in t if x['health']=='up')}/{len(t)} up\")"
kill %1
# Grafana cert + login
curl -sv https://grafana.vigihome.net 2>&1 | grep -E "issuer|HTTP/"   # Let's Encrypt, HTTP 200/302
```

In a browser: `https://grafana.vigihome.net` → "Sign in with Authentik" logs
in a `homelab-users` member as Admin; the local `admin-user`/`admin-password`
on `/login` also works (Authentik-independent). The bundled k8s dashboards
populate with live data. A Watchdog email arrives at
`vigihome-admin@vigiemail.com` within a few minutes.

## Day-to-day ops

- **Change config:** edit `values.yaml`, then
  `helm upgrade kps prometheus-community/kube-prometheus-stack -n monitoring --version 85.3.3 -f k8s/kube-prometheus-stack/values.yaml`.
  Always pin `--version` (drift discipline).

- **Prometheus is at `https://prometheus.vigihome.net`;
  Alertmanager is at `https://alertmanager.vigihome.net`.**
  Both are gated by Authentik forward-auth (#137, #117)
  via the `monitoring-forward-auth@kubernetescrd` Middleware in the `monitoring` namespace.
  The un-gated `/outpost.goauthentik.io` handshake route lives in the `auth` namespace
  (`k8s/authentik/ingress-forward-auth-outpost.yaml`).

  **SPOF / break-glass:** these UIs have no native login,
  so there is no Bitwarden fallback credential (no native auth surface).
  When Authentik is down, the only path in bypasses Traefik and Authentik entirely:

  ```sh
  kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090
  kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-alertmanager 9093:9093
  ```

  These are multi-port Services; if a `port-forward svc/...` lands on the wrong backend,
  use the pod-direct form instead,
  e.g. `kubectl -n monitoring port-forward pod/prometheus-kps-kube-prometheus-stack-prometheus-0 9090:9090`.

- **Alert routing:** Alertmanager → Forward Email over **STARTTLS on 587**
  (Alertmanager can't do implicit TLS/465 the way the restic job does).
  Credentials come from the reflected `smtp-relay` Secret — the password via a
  mounted file, the username inline (it's the `noreply@` alias, not a secret).

- **Upgrade the chart:** bump the `--version` and re-run `helm upgrade`; read
  the chart's upgrade notes (CRD changes sometimes need a manual
  `kubectl apply` of the new CRDs first).

## Troubleshooting

- **"Prometheus only has 1 target" via `kubectl port-forward svc/...`.** Don't
  trust that path — on this multi-port Service (9090 prometheus + 8080
  reloader) `port-forward svc/...` can resolve to the wrong backend and return
  a minimal/misleading config. Verify via the **pod**
  (`kubectl -n monitoring port-forward pod/prometheus-kps-kube-prometheus-stack-prometheus-0 9099:9090`)
  or **in-cluster DNS** (a throwaway `curlimages/curl` pod hitting
  `http://kps-kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090`).
  Grafana uses the in-cluster path, which works correctly.
- **First-install config lag.** On a fresh install the kubelet secret mount can
  lag, so Prometheus may briefly boot on a minimal config before the operator's
  full config propagates and reloads. It self-heals; `kubectl rollout restart`
  the Prometheus StatefulSet if it persists.
- **`<component>Down` alerts for controller-manager / scheduler / proxy.** k3s
  embeds these with metrics bound to localhost, so they're not scrapeable —
  they're disabled in `values.yaml` (along with `kubeEtcd`, since k3s here uses
  sqlite). Re-enable only if you expose those endpoints.

## What we don't back up

Only Grafana's `/opt/grafana` is in restic (tag `grafana`, wired in
`../backup/backup-cronjob.yaml`). The **Prometheus TSDB and Alertmanager data
are deliberately excluded** — they live on disposable `local-path`, are large
and churny, and are fully reconstructable (metrics re-scrape; silences are
transient). Restoring the stack = re-run the install runbook; only dashboards
need the restic restore.
