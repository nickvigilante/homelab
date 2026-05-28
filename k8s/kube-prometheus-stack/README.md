# kube-prometheus-stack (metrics & alerting)

Metrics, dashboards, and email alerting for the homelab (#76):
Prometheus scrapes cluster/node/kube-state metrics,
Grafana visualizes them behind Authentik OIDC at `https://grafana.vigihome.net`,
and Alertmanager emails alerts via the Forward Email relay.

This is sub-project **A — metrics only**.
Logs (Loki) are #116; per-app ServiceMonitors, long-term storage, HA, and
forward-auth exposure of Prometheus/Alertmanager are #117.

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
  - `homepage-user` / `homepage-password` — added in step 7 for the widget

## One-time install

Run from the repo root with kubectl context = homelab. The stack's
prerequisites (secrets, PV, blueprint, reflected SMTP) must exist **before**
`helm install`, because the Grafana and Alertmanager pods mount them on boot.

1. **Pre-create the Grafana host dir** (gandalf; Grafana runs as uid 472):

   ```sh
   # gandalf
   sudo mkdir -p /opt/grafana && sudo chown 472:472 /opt/grafana
   ```

2. **Create the `grafana-secrets` Secret** from Bitwarden (laptop):

   ```sh
   export BW_SESSION="$(bw unlock --raw)"; bw sync
   ITEM=$(bw get item 'Homelab Grafana')
   kubectl -n monitoring create secret generic grafana-secrets \
     --from-literal=admin-user="$(echo "$ITEM" | jq -r '.fields[]|select(.name=="admin-user")|.value')" \
     --from-literal=admin-password="$(echo "$ITEM" | jq -r '.fields[]|select(.name=="admin-password")|.value')" \
     --from-literal=oidc-client-secret="$(echo "$ITEM" | jq -r '.fields[]|select(.name=="oidc-client-secret")|.value')"
   unset BW_SESSION ITEM
   ```

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

5. **Land the Authentik Grafana OIDC blueprint.** Re-create
   `authentik-oidc-secrets` with the new grafana key, re-render the blueprints
   ConfigMap, upgrade Authentik (pinned to the installed chart version), and
   trigger discovery (boot can race the ConfigMap mount):

   ```sh
   export BW_SESSION="$(bw unlock --raw)"; bw sync
   ITEM_C=$(bw get item 'Homelab Coder'); ITEM_O=$(bw get item 'Homelab Outline'); ITEM_G=$(bw get item 'Homelab Grafana')
   kubectl -n auth create secret generic authentik-oidc-secrets \
     --from-literal=oidc-coder-client-secret="$(echo "$ITEM_C" | jq -r '.fields[]|select(.name=="oidc-client-secret")|.value')" \
     --from-literal=oidc-outline-client-secret="$(echo "$ITEM_O" | jq -r '.fields[]|select(.name=="oidc-client-secret")|.value')" \
     --from-literal=oidc-grafana-client-secret="$(echo "$ITEM_G" | jq -r '.fields[]|select(.name=="oidc-client-secret")|.value')" \
     --dry-run=client -o yaml | kubectl apply -f -
   unset BW_SESSION ITEM_C ITEM_O ITEM_G
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

7. **Add the Homepage widget creds.** Create a dedicated **Viewer**-role
   Grafana login (Grafana UI → Administration → Users → add `homepage`), store
   it in Bitwarden `Homelab Grafana` (`homepage-user` / `homepage-password`),
   then re-create `homepage-secrets` with all keys (so nothing is dropped):

   ```sh
   export BW_SESSION="$(bw unlock --raw)"; bw sync
   ITEM_G=$(bw get item 'Homelab Grafana')
   kubectl -n homepage create secret generic homepage-secrets \
     --from-literal=octoprint-api-key="$(bw get item 'Homelab OctoPrint' | jq -r '.fields[]|select(.name=="API key")|.value')" \
     --from-literal=grafana-user="$(echo "$ITEM_G" | jq -r '.fields[]|select(.name=="homepage-user")|.value')" \
     --from-literal=grafana-password="$(echo "$ITEM_G" | jq -r '.fields[]|select(.name=="homepage-password")|.value')" \
     --dry-run=client -o yaml | kubectl apply -f -
   unset BW_SESSION ITEM_G
   helm upgrade homepage jameswynn/homepage -n homepage --version 2.1.0 -f k8s/homepage/values.yaml
   ```

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
- **Prometheus / Alertmanager have no Ingress — by design.** They ship no
  authentication of their own (open admin APIs: TSDB delete, silence
  create/read), so they get no standing network-exposed surface. Reach them
  for debugging via port-forward:
  ```sh
  kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090
  kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-alertmanager 9093:9093
  ```
  Routine querying and silencing happen through authenticated Grafana
  (Explore + the Alertmanager view). Exposing them behind Authentik
  forward-auth is tracked in #117.
- **Alert routing:** Alertmanager → Forward Email over **STARTTLS on 587**
  (Alertmanager can't do implicit TLS/465 the way the restic job does).
  Credentials come from the reflected `smtp-relay` Secret — the password via a
  mounted file, the username inline (it's the `noreply@` alias, not a secret).
- **Upgrade the chart:** bump the `--version` and re-run `helm upgrade`; read
  the chart's upgrade notes (CRD changes sometimes need a manual
  `kubectl apply` of the new CRDs first).

## What we don't back up

Only Grafana's `/opt/grafana` is in restic (tag `grafana`, wired in
`../backup/backup-cronjob.yaml`). The **Prometheus TSDB and Alertmanager data
are deliberately excluded** — they live on disposable `local-path`, are large
and churny, and are fully reconstructable (metrics re-scrape; silences are
transient). Restoring the stack = re-run the install runbook; only dashboards
need the restic restore.
