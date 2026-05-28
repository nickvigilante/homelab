# Observability Metrics Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy kube-prometheus-stack (Prometheus + Grafana + Alertmanager + node-exporter + kube-state-metrics) on the k3s homelab, with Grafana behind Authentik OIDC at `grafana.vigihome.net` and Alertmanager emailing via the Forward Email relay.

**Architecture:** One version-pinned Helm chart in the existing `monitoring` namespace; heavy pods pinned to gandalf, node-exporter as a DaemonSet on all nodes. Prometheus/Alertmanager TSDB on `local-path` (not backed up); Grafana on a hostPath PV at `/opt/grafana` (backed up by restic). Grafana auth via a new Authentik OIDC blueprint (mirrors the #104 Coder/Outline pattern) plus a local-admin fallback.

**Tech Stack:** kube-prometheus-stack `85.3.3` (appVersion `v0.90.1`), Helm, k3s, Authentik Blueprints, Bitwarden CLI, Traefik Ingress + reflected `vigihome-tls`, Forward Email SMTP.

**Spec:** `docs/superpowers/specs/2026-05-26-observability-metrics-design.md`

**Conventions for this plan:**

- Branch `observability-metrics` already exists (spec committed there). Do all work on it.
- Helm is run from `gandalf` or the laptop with kubectl context = homelab. Tag each live command with its host.
- "Verify" for infra = a `kubectl`/`curl` command with expected output (no unit tests here).
- Always pin `helm upgrade --version 85.3.3` (drift discipline). Render with `helm template … | kubectl … --dry-run` before applying where noted.
- Secrets never enter the repo; created via `kubectl create secret` from Bitwarden at apply time. `bw sync` after `bw unlock`.

______________________________________________________________________

## File structure

| File                                                 | Responsibility                                                                                                     |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `k8s/kube-prometheus-stack/values.yaml`              | All Helm values: nodeSelectors, storage, retention, Grafana (ingress/OIDC/persistence), Alertmanager email config  |
| `k8s/kube-prometheus-stack/pv-pvc.yaml`              | Pre-created hostPath PV `/opt/grafana` (gandalf, `manual` SC) + PVC for Grafana persistence                        |
| `k8s/kube-prometheus-stack/secret.example.yaml`      | Documents the `grafana-secrets` Secret keys (admin creds + OIDC client id/secret)                                  |
| `k8s/kube-prometheus-stack/README.md`                | One-time setup runbook + day-to-day ops + "what we don't back up"                                                  |
| `k8s/authentik/blueprints/applications/grafana.yaml` | New: Grafana OIDC provider + application + `homelab-users` gate                                                    |
| `k8s/authentik/secret.example.yaml` (modify)         | Add `oidc-grafana-client-secret` key to `authentik-oidc-secrets`; add `monitoring` to `smtp-relay` reflection list |
| `k8s/authentik/values.yaml` (modify)                 | Add `AUTHENTIK_OIDC_GRAFANA_SECRET` env entry                                                                      |
| `k8s/backup/backup-cronjob.yaml` (modify)            | Add `/opt/grafana` hostPath source + `restic backup --tag grafana` block                                           |
| `k8s/homepage/values.yaml` (modify)                  | Add Grafana service group + live widget                                                                            |
| `k8s/homepage/secret.example.yaml` (modify)          | Document `grafana-token` key                                                                                       |
| `CLAUDE.md` (modify)                                 | Backup-wiring note: `/opt/grafana` added; Prometheus TSDB deliberately not backed up                               |

______________________________________________________________________

## Task 1: Core stack — install with scraping only (Stage 1)

**Files:**

- Create: `k8s/kube-prometheus-stack/values.yaml`

- [ ] **Step 1: Register the chart repo** (laptop/gandalf)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update prometheus-community
helm search repo prometheus-community/kube-prometheus-stack --version 85.3.3
```

Expected: lists chart `85.3.3` / appVersion `v0.90.1`.

- [ ] **Step 2: Write the initial values** (Grafana Ingress OFF, Alertmanager receiver = null — scraping-only first)

`k8s/kube-prometheus-stack/values.yaml`:

```yaml
# kube-prometheus-stack v85.3.3 (appVersion v0.90.1). Metrics + alerting for
# the homelab (#76). monitoring namespace. Heavy pods pinned to gandalf;
# node-exporter is a DaemonSet on every node. See README.md.

# --- Prometheus -----------------------------------------------------------
prometheus:
  prometheusSpec:
    # Pin to gandalf (amd64, RAM headroom). local-path is node-local, so the
    # TSDB volume lands on gandalf with the pod.
    nodeSelector:
      kubernetes.io/hostname: gandalf
    retention: 15d
    retentionSize: "18GB"   # headroom under the 20Gi PVC
    # Scrape everything in-cluster the chart wires by default; also pick up
    # ServiceMonitors/PodMonitors in any namespace (for future per-app metrics).
    serviceMonitorSelectorNilUsesHelmValues: false
    podMonitorSelectorNilUsesHelmValues: false
    ruleSelectorNilUsesHelmValues: false
    probeSelectorNilUsesHelmValues: false
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: local-path
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 20Gi
    resources:
      requests:
        cpu: 250m
        memory: 1Gi
      limits:
        memory: 3Gi

# --- Alertmanager (receivers wired in Task 5) -----------------------------
alertmanager:
  alertmanagerSpec:
    nodeSelector:
      kubernetes.io/hostname: gandalf
    storage:
      volumeClaimTemplate:
        spec:
          storageClassName: local-path
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 2Gi
    resources:
      requests:
        cpu: 10m
        memory: 64Mi
      limits:
        memory: 128Mi

# --- Grafana (Ingress + OIDC + persistence wired in Tasks 2 & 4) ----------
grafana:
  nodeSelector:
    kubernetes.io/hostname: gandalf
  # Ingress + persistence + OIDC added in later tasks; start minimal so Stage 1
  # only proves scraping. Default admin creds are chart-generated for now.
  resources:
    requests:
      cpu: 50m
      memory: 128Mi
    limits:
      memory: 256Mi

# --- kube-state-metrics ----------------------------------------------------
kube-state-metrics:
  nodeSelector:
    kubernetes.io/hostname: gandalf

# --- node-exporter: DaemonSet on all nodes (default). Keep tolerations broad
# so it schedules on every node including any tainted ones. ----------------
prometheus-node-exporter:
  tolerations:
    - operator: Exists
```

- [ ] **Step 3: Render to catch errors before applying**

```bash
cd ~/git/nickvigilante/homelab
helm template kps prometheus-community/kube-prometheus-stack --version 85.3.3 \
  -n monitoring -f k8s/kube-prometheus-stack/values.yaml >/tmp/kps-render.yaml
echo "rc=$?"; grep -c '^kind:' /tmp/kps-render.yaml
```

Expected: rc=0, a non-zero count of rendered manifests.

- [ ] **Step 4: Install** (laptop/gandalf, context = homelab)

```bash
helm install kps prometheus-community/kube-prometheus-stack --version 85.3.3 \
  -n monitoring -f k8s/kube-prometheus-stack/values.yaml
kubectl -n monitoring rollout status statefulset/prometheus-kps-kube-prometheus-stack-prometheus --timeout=300s
```

Expected: Prometheus StatefulSet becomes Ready (CRDs install, operator creates the StatefulSet; allow a few minutes).

- [ ] **Step 5: Verify scraping + node-exporter coverage**

```bash
# node-exporter DaemonSet on all 3 nodes
kubectl -n monitoring get ds -l app.kubernetes.io/name=prometheus-node-exporter
# Prometheus targets UP (port-forward, then query the targets API)
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/tmp/pf-prom.log 2>&1 &
sleep 4
curl -s 'http://localhost:9090/api/v1/targets?state=active' | \
  python3 -c "import sys,json; t=json.load(sys.stdin)['data']['activeTargets']; up=sum(1 for x in t if x['health']=='up'); print(f'{up}/{len(t)} targets up')"
kill %1 2>/dev/null
```

Expected: DaemonSet DESIRED=3 / READY=3; the targets query reports most/all targets `up`.

- [ ] **Step 6: Commit**

```bash
git add k8s/kube-prometheus-stack/values.yaml
git commit -m "Observability: install kube-prometheus-stack (metrics scraping)"
```

______________________________________________________________________

## Task 2: Grafana hostPath PV + persistence

**Files:**

- Create: `k8s/kube-prometheus-stack/pv-pvc.yaml`

- Modify: `k8s/kube-prometheus-stack/values.yaml`

- [ ] **Step 1: Pre-create the host dir on gandalf** (Grafana runs as uid 472)

```bash
# gandalf
sudo mkdir -p /opt/grafana && sudo chown 472:472 /opt/grafana
```

(If gandalf is Ubuntu 26.04, use `sudo.ws` per the sudo-rs note when run via Ansible; manual ssh sudo is fine here.)

- [ ] **Step 2: Write the PV + PVC**

`k8s/kube-prometheus-stack/pv-pvc.yaml`:

```yaml
# Pre-created hostPath PV for Grafana so the nightly restic backup captures
# user-added dashboards/settings (Prometheus/Alertmanager use disposable
# local-path and are NOT backed up). gandalf-pinned, storageClass: manual.
apiVersion: v1
kind: PersistentVolume
metadata:
  name: grafana-data
spec:
  capacity:
    storage: 5Gi
  accessModes: ["ReadWriteOnce"]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: manual
  hostPath:
    path: /opt/grafana
    type: Directory
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: ["gandalf"]
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: grafana-data
  namespace: monitoring
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: manual
  resources:
    requests:
      storage: 5Gi
  volumeName: grafana-data
```

- [ ] **Step 3: Apply the PV/PVC**

```bash
kubectl apply -f k8s/kube-prometheus-stack/pv-pvc.yaml
kubectl -n monitoring get pvc grafana-data
```

Expected: PVC `grafana-data` is `Bound`.

- [ ] **Step 4: Wire Grafana persistence in values.yaml**

In `k8s/kube-prometheus-stack/values.yaml`, under the `grafana:` block, add:

```yaml
  persistence:
    enabled: true
    type: pvc
    existingClaim: grafana-data
```

- [ ] **Step 5: Upgrade + verify Grafana mounts the PV**

```bash
helm upgrade kps prometheus-community/kube-prometheus-stack --version 85.3.3 \
  -n monitoring -f k8s/kube-prometheus-stack/values.yaml
kubectl -n monitoring rollout status deploy/kps-grafana --timeout=180s
kubectl -n monitoring get pod -l app.kubernetes.io/name=grafana \
  -o jsonpath='{.items[0].spec.volumes[?(@.persistentVolumeClaim)].persistentVolumeClaim.claimName}{"\n"}'
```

Expected: prints `grafana-data`.

- [ ] **Step 6: Commit**

```bash
git add k8s/kube-prometheus-stack/pv-pvc.yaml k8s/kube-prometheus-stack/values.yaml
git commit -m "Observability: Grafana hostPath PV at /opt/grafana"
```

______________________________________________________________________

## Task 3: Authentik Grafana OIDC blueprint

**Files:**

- Create: `k8s/authentik/blueprints/applications/grafana.yaml`

- Modify: `k8s/authentik/secret.example.yaml`, `k8s/authentik/values.yaml`

- [ ] **Step 1: Generate the OIDC client secret + store in Bitwarden**

In Bitwarden, create item `Homelab Grafana` with a field `oidc-client-secret` set to a fresh 128-char token:

```bash
openssl rand -hex 64    # paste into the Bitwarden field oidc-client-secret
```

(Also add `admin-user` = `admin-local` and `admin-password` = a strong password — used in Task 4 for the local fallback.)

- [ ] **Step 2: Write the blueprint** (mirrors `applications/outline.yaml` from #104; implicit-consent)

`k8s/authentik/blueprints/applications/grafana.yaml`:

```yaml
version: 1
metadata:
  name: "homelab - application - grafana"
# Grafana OIDC provider + application + group gate. client_secret injected via
# !Env from authentik-oidc-secrets (never in the repo). Gated on homelab-users.
entries:
  - id: grafana-provider
    model: authentik_providers_oauth2.oauth2provider
    state: present
    identifiers:
      name: grafana
    attrs:
      client_type: confidential
      client_id: grafana
      client_secret: !Env [AUTHENTIK_OIDC_GRAFANA_SECRET, ""]
      sub_mode: hashed_user_id
      include_claims_in_id_token: true
      authorization_flow: !Find [authentik_flows.flow, [slug, default-provider-authorization-implicit-consent]]
      invalidation_flow: !Find [authentik_flows.flow, [slug, default-provider-invalidation-flow]]
      signing_key: !Find [authentik_crypto.certificatekeypair, [name, authentik Self-signed Certificate]]
      property_mappings:
        - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-openid]]
        - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-email]]
        - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-profile]]
      redirect_uris:
        - matching_mode: strict
          url: https://grafana.vigihome.net/login/generic_oauth
  - id: grafana-app
    model: authentik_core.application
    state: present
    identifiers:
      slug: grafana
    attrs:
      name: Grafana
      provider: !KeyOf grafana-provider
      meta_launch_url: https://grafana.vigihome.net
      policy_engine_mode: any
  - model: authentik_policies.policybinding
    state: present
    identifiers:
      target: !KeyOf grafana-app
      group: !Find [authentik_core.group, [name, homelab-users]]
      order: 0
    attrs:
      enabled: true
      negate: false
```

- [ ] **Step 3: Add the env wiring to authentik values.yaml**

In `k8s/authentik/values.yaml`, append to `global.env` (after the `AUTHENTIK_OIDC_OUTLINE_SECRET` block from #104):

```yaml
    - name: AUTHENTIK_OIDC_GRAFANA_SECRET
      valueFrom:
        secretKeyRef:
          name: authentik-oidc-secrets
          key: oidc-grafana-client-secret
```

- [ ] **Step 4: Document the new secret key**

In `k8s/authentik/secret.example.yaml`, add to the `authentik-oidc-secrets` `stringData` block:

```yaml
  oidc-grafana-client-secret: REPLACE_WITH_GRAFANA_OIDC_CLIENT_SECRET
```

And add the source comment line near the existing ones:

```
#   oidc-grafana-client-secret ← item 'Homelab Grafana', field oidc-client-secret
```

- [ ] **Step 5: Apply to prod — recreate the oidc-secrets Secret with the new key, render the ConfigMap, upgrade, trigger discovery**

```bash
# laptop, context = homelab. Re-create authentik-oidc-secrets WITH the grafana key.
export BW_SESSION="$(bw unlock --raw)"; bw sync
ITEM_C=$(bw get item 'Homelab Coder'); ITEM_O=$(bw get item 'Homelab Outline'); ITEM_G=$(bw get item 'Homelab Grafana')
kubectl -n auth create secret generic authentik-oidc-secrets \
  --from-literal=oidc-coder-client-secret="$(echo "$ITEM_C" | jq -r '.fields[]|select(.name=="oidc-client-secret")|.value')" \
  --from-literal=oidc-outline-client-secret="$(echo "$ITEM_O" | jq -r '.fields[]|select(.name=="oidc-client-secret")|.value')" \
  --from-literal=oidc-grafana-client-secret="$(echo "$ITEM_G" | jq -r '.fields[]|select(.name=="oidc-client-secret")|.value')" \
  --dry-run=client -o yaml | kubectl apply -f -
unset BW_SESSION ITEM_C ITEM_O ITEM_G
# Render the blueprints ConfigMap (now includes grafana.yaml) + upgrade authentik (pinned) + trigger discovery
kubectl -n auth create configmap authentik-blueprints \
  $(find k8s/authentik/blueprints -name '*.yaml' -printf '--from-file=%f=%p ') \
  --dry-run=client -o yaml | kubectl apply -f -
CHART_VER="$(helm list -n auth -f '^authentik$' -o json | jq -r '.[0].chart' | sed 's/^authentik-//')"
helm -n auth upgrade authentik authentik/authentik --version "$CHART_VER" -f k8s/authentik/values.yaml
kubectl -n auth rollout status deploy/authentik-worker
kubectl -n auth exec deploy/authentik-worker -- \
  ak shell -c "from authentik.blueprints.v1.tasks import blueprints_discovery; blueprints_discovery.send()"
```

- [ ] **Step 6: Verify the Grafana provider/app were created with the injected secret**

```bash
TOK=$(kubectl -n auth get secret authentik-secrets -o jsonpath='{.data.bootstrap-token}' | base64 -d)
kubectl -n auth exec deploy/authentik-server -- sh -c "curl -s -H 'Authorization: Bearer $TOK' 'http://localhost:9000/api/v3/providers/oauth2/?ordering=name'" \
  | python3 -c "import sys,json; [print(p['name'],'secret_len=',len(p.get('client_secret') or '')) for p in json.load(sys.stdin)['results'] if p['name']=='grafana']"
unset TOK
```

Expected: prints `grafana secret_len=128`. (If the grafana blueprint instance shows `NOT DISCOVERED`, re-run the discovery trigger — boot can race the CM mount, per `blueprints/README.md`.)

- [ ] **Step 7: Commit**

```bash
git add k8s/authentik/blueprints/applications/grafana.yaml k8s/authentik/values.yaml k8s/authentik/secret.example.yaml
git commit -m "Authentik blueprint: Grafana OIDC application (#76)"
```

______________________________________________________________________

## Task 4: Grafana OIDC + local-admin + Ingress (Stage 2)

**Files:**

- Create: `k8s/kube-prometheus-stack/secret.example.yaml`

- Modify: `k8s/kube-prometheus-stack/values.yaml`

- [ ] **Step 1: Document + create the `grafana-secrets` Secret**

`k8s/kube-prometheus-stack/secret.example.yaml`:

```yaml
# TEMPLATE ONLY. Real Secret created via kubectl from Bitwarden item
# 'Homelab Grafana' (see README.md). Holds the local-admin fallback creds
# AND the OIDC client secret (same value fed to Authentik via
# authentik-oidc-secrets). Keys admin-user/admin-password match the chart's
# grafana.admin.existingSecret default key names.
apiVersion: v1
kind: Secret
metadata:
  name: grafana-secrets
  namespace: monitoring
type: Opaque
stringData:
  admin-user: REPLACE_WITH_GRAFANA_ADMIN_USER
  admin-password: REPLACE_WITH_GRAFANA_ADMIN_PASSWORD
  oidc-client-secret: REPLACE_WITH_GRAFANA_OIDC_CLIENT_SECRET
```

Create it (laptop, context = homelab):

```bash
export BW_SESSION="$(bw unlock --raw)"; bw sync
ITEM=$(bw get item 'Homelab Grafana')
kubectl -n monitoring create secret generic grafana-secrets \
  --from-literal=admin-user="$(echo "$ITEM" | jq -r '.fields[]|select(.name=="admin-user")|.value')" \
  --from-literal=admin-password="$(echo "$ITEM" | jq -r '.fields[]|select(.name=="admin-password")|.value')" \
  --from-literal=oidc-client-secret="$(echo "$ITEM" | jq -r '.fields[]|select(.name=="oidc-client-secret")|.value')"
unset BW_SESSION ITEM
```

- [ ] **Step 2: Wire admin, OIDC, and Ingress in values.yaml**

Replace the `grafana:` block's tail (keep `nodeSelector`, `resources`, `persistence` from earlier tasks) by adding:

```yaml
  # Local-admin fallback (reachable when Authentik is down — SPOF discipline).
  admin:
    existingSecret: grafana-secrets
    userKey: admin-user
    passwordKey: admin-password
  # Inject the OIDC client secret as an env var referenced by grafana.ini.
  envValueFrom:
    GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET:
      secretKeyRef:
        name: grafana-secrets
        key: oidc-client-secret
  grafana.ini:
    server:
      root_url: https://grafana.vigihome.net
    auth.generic_oauth:
      enabled: true
      name: Authentik
      client_id: grafana
      scopes: "openid email profile"
      auth_url: https://authentik.vigihome.net/application/o/authorize/
      token_url: https://authentik.vigihome.net/application/o/token/
      api_url: https://authentik.vigihome.net/application/o/userinfo/
      # homelab-users members become Admin; everyone else Viewer.
      role_attribute_path: "contains(groups[*], 'homelab-users') && 'Admin' || 'Viewer'"
      # Don't force-replace the local admin; both login paths coexist.
      allow_assign_grafana_admin: true
  ingress:
    enabled: true
    ingressClassName: traefik
    annotations:
      traefik.ingress.kubernetes.io/router.entrypoints: websecure
    hosts:
      - grafana.vigihome.net
    tls:
      - hosts: ["grafana.vigihome.net"]
        secretName: vigihome-tls
```

- [ ] **Step 3: Upgrade + verify cert, OIDC, and local-admin**

```bash
helm upgrade kps prometheus-community/kube-prometheus-stack --version 85.3.3 \
  -n monitoring -f k8s/kube-prometheus-stack/values.yaml
kubectl -n monitoring rollout status deploy/kps-grafana --timeout=180s
curl -sv https://grafana.vigihome.net 2>&1 | grep -E "issuer|HTTP/"
```

Expected: Let's Encrypt issuer, HTTP 200/302. Then in a browser:

- `https://grafana.vigihome.net` → "Sign in with Authentik" → a `homelab-users` member logs in as Admin.

- `https://grafana.vigihome.net/login` with the local `admin-user`/`admin-password` also works (Authentik-independent).

- [ ] **Step 4: Commit**

```bash
git add k8s/kube-prometheus-stack/secret.example.yaml k8s/kube-prometheus-stack/values.yaml
git commit -m "Observability: Grafana OIDC + local-admin + Ingress at grafana.vigihome.net"
```

______________________________________________________________________

## Task 5: Alert routing — email via Forward Email (Stage 3)

**Files:**

- Modify: `k8s/authentik/secret.example.yaml` (smtp-relay reflection annotation), `k8s/kube-prometheus-stack/values.yaml`

- [ ] **Step 1: Reflect smtp-relay into monitoring**

Edit the `smtp-relay` Secret's annotation in `k8s/authentik/secret.example.yaml`:

```yaml
    reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: "backup,monitoring"
```

Apply live (laptop, context = homelab):

```bash
kubectl -n auth annotate secret smtp-relay --overwrite \
  reflector.v1.k8s.emberstack.com/reflection-auto-namespaces=backup,monitoring
kubectl -n monitoring get secret smtp-relay   # appears within seconds
```

Expected: `smtp-relay` present in `monitoring`.

- [ ] **Step 2: Wire the Alertmanager email receiver in values.yaml**

Mount the smtp-relay Secret into Alertmanager and point the config at the password file. Under `alertmanager:` add `alertmanagerSpec.secrets` and replace the default `config`:

```yaml
alertmanager:
  alertmanagerSpec:
    nodeSelector:
      kubernetes.io/hostname: gandalf
    # Mount the reflected smtp-relay Secret at /etc/alertmanager/secrets/smtp-relay
    secrets:
      - smtp-relay
    storage:
      volumeClaimTemplate:
        spec:
          storageClassName: local-path
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 2Gi
    resources:
      requests:
        cpu: 10m
        memory: 64Mi
      limits:
        memory: 128Mi
  config:
    global:
      smtp_smarthost: "smtp.forwardemail.net:465"
      smtp_require_tls: false   # 465 is implicit TLS
      smtp_hello: vigihome.net
    route:
      group_by: ["alertname", "namespace"]
      group_wait: 30s
      group_interval: 5m
      repeat_interval: 4h
      receiver: email
      routes:
        - matchers: ['alertname = "Watchdog"']
          receiver: email
          repeat_interval: 24h
    receivers:
      - name: email
        email_configs:
          - to: vigihome-admin@vigiemail.com
            from: noreply@vigihome.net
            smarthost: "smtp.forwardemail.net:465"
            auth_username_file: /etc/alertmanager/secrets/smtp-relay/smtp-username
            auth_password_file: /etc/alertmanager/secrets/smtp-relay/smtp-password
            require_tls: false
            send_resolved: true
```

- [ ] **Step 3: Upgrade + verify a real alert delivers**

```bash
helm upgrade kps prometheus-community/kube-prometheus-stack --version 85.3.3 \
  -n monitoring -f k8s/kube-prometheus-stack/values.yaml
kubectl -n monitoring rollout status statefulset/alertmanager-kps-kube-prometheus-stack-alertmanager --timeout=180s
# The always-firing Watchdog alert should route immediately; check the inbox,
# or inspect Alertmanager state:
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-alertmanager 9093:9093 >/tmp/pf-am.log 2>&1 &
sleep 4
curl -s http://localhost:9093/api/v2/status | python3 -c "import sys,json; print('config loaded:', 'email' in json.load(sys.stdin)['config']['original'])"
kill %1 2>/dev/null
```

Expected: config contains the `email` receiver; a Watchdog email arrives at `vigihome-admin@vigiemail.com` within a few minutes.

- [ ] **Step 4: Commit**

```bash
git add k8s/authentik/secret.example.yaml k8s/kube-prometheus-stack/values.yaml
git commit -m "Observability: Alertmanager email routing via Forward Email relay"
```

______________________________________________________________________

## Task 6: Integration — backup wiring + Homepage widget (Stage 4)

**Files:**

- Modify: `k8s/backup/backup-cronjob.yaml`, `k8s/homepage/values.yaml`, `k8s/homepage/secret.example.yaml`

- [ ] **Step 1: Add /opt/grafana to the restic backup**

In `k8s/backup/backup-cronjob.yaml`: add a `restic backup` block after the existing ones (before the snapshot summary):

```bash
                  echo "=== Grafana config ==="
                  restic backup \
                    --host gandalf \
                    --tag grafana \
                    /backup/grafana
                  echo
```

Add the volumeMount (readOnly) and hostPath volume mirroring the existing pattern:

```yaml
                - name: grafana
                  mountPath: /backup/grafana
                  readOnly: true
```

```yaml
            - name: grafana
              hostPath:
                path: /opt/grafana
                type: DirectoryOrCreate
```

Apply + smoke-test:

```bash
kubectl apply -f k8s/backup/backup-cronjob.yaml
kubectl -n backup create job --from=cronjob/restic-backup test-grafana-$(date +%s)
kubectl -n backup logs -f -l job-name=$(kubectl -n backup get jobs -o name | tail -1 | cut -d/ -f2) | grep -A2 "Grafana config"
```

Expected: the Grafana tag backs up without error.

- [ ] **Step 2: Add the Grafana Homepage widget**

gethomepage's Grafana widget uses **basic auth** (a Grafana username + password), not an API token. Create a dedicated read-only Grafana login: Grafana UI → Administration → Users → add `homepage` with the **Viewer** role and a strong password. Store both in Bitwarden `Homelab Grafana` (fields `homepage-user` = `homepage`, `homepage-password`), then add them to the homepage Secret (re-create with all keys so nothing is dropped):

```bash
export BW_SESSION="$(bw unlock --raw)"; bw sync
ITEM_G=$(bw get item 'Homelab Grafana')
kubectl -n homepage create secret generic homepage-secrets \
  --from-literal=octoprint-api-key="$(bw get item 'Homelab OctoPrint' | jq -r '.fields[]|select(.name=="API key")|.value')" \
  --from-literal=grafana-user="$(echo "$ITEM_G" | jq -r '.fields[]|select(.name=="homepage-user")|.value')" \
  --from-literal=grafana-password="$(echo "$ITEM_G" | jq -r '.fields[]|select(.name=="homepage-password")|.value')" \
  --dry-run=client -o yaml | kubectl apply -f -
unset BW_SESSION ITEM_G
```

In `k8s/homepage/secret.example.yaml` add documented `grafana-user` + `grafana-password` keys. In `k8s/homepage/values.yaml`: add two env entries `HOMEPAGE_VAR_GRAFANA_USER` and `HOMEPAGE_VAR_GRAFANA_PASSWORD` (each a `secretKeyRef` → `homepage-secrets`/`grafana-user` and `/grafana-password`), and a Grafana service entry under an "Observability" group:

```yaml
    - Observability:
        - Grafana:
            href: https://grafana.vigihome.net
            description: Dashboards & metrics
            icon: grafana.png
            widget:
              type: grafana
              url: http://kps-grafana.monitoring.svc.cluster.local
              username: "{{HOMEPAGE_VAR_GRAFANA_USER}}"
              password: "{{HOMEPAGE_VAR_GRAFANA_PASSWORD}}"
```

Add the matching `Observability: {style: row, columns: 3}` layout row in `settingsString`. Then `helm upgrade homepage jameswynn/homepage -n homepage --version 2.1.0 -f values.yaml`.

- [ ] **Step 3: Commit**

```bash
git add k8s/backup/backup-cronjob.yaml k8s/homepage/values.yaml k8s/homepage/secret.example.yaml
git commit -m "Observability: back up /opt/grafana + Grafana Homepage widget"
```

______________________________________________________________________

## Task 7: Docs + finish

**Files:**

- Create: `k8s/kube-prometheus-stack/README.md`

- Modify: `CLAUDE.md`

- [ ] **Step 1: Write the README** (runbook)

`k8s/kube-prometheus-stack/README.md` covering: chart + version, `monitoring` ns, gandalf pinning + node-exporter DaemonSet, the install/upgrade commands (pinned `--version 85.3.3`), the `grafana-data` PV pre-flight (`mkdir /opt/grafana` + `chown 472:472`), the `grafana-secrets` create-from-Bitwarden recipe, OIDC (blueprint + local-admin fallback), accessing Prometheus/Alertmanager via port-forward (and WHY no Ingress — open admin APIs), alert routing via the reflected `smtp-relay`, and a "What we don't back up" note (Prometheus TSDB + Alertmanager are disposable `local-path`; only Grafana's `/opt/grafana` is in restic).

- [ ] **Step 2: Update CLAUDE.md backup-wiring note**

In `CLAUDE.md` "Backup wiring", note `/opt/grafana` is now a restic source, and add to "What we don't back up" that Prometheus TSDB + Alertmanager data are deliberately excluded (disposable `local-path`, reconstructable).

- [ ] **Step 3: Commit**

```bash
git add k8s/kube-prometheus-stack/README.md CLAUDE.md
git commit -m "Observability: README runbook + backup-wiring docs (#76)"
```

- [ ] **Step 4: Finish the branch**

Announce and use **superpowers:finishing-a-development-branch** — verify lint (yamllint + kubeconform; note the chart's rendered output isn't linted, but the raw manifests `pv-pvc.yaml`/`secret.example.yaml` are — confirm they're covered by the kubeconform filter or add them), open the PR referencing #76, squash-merge per repo convention (no Co-Authored-By).

______________________________________________________________________

## Self-review notes

- **Spec coverage:** chart + ns + gandalf pinning + node-exporter DaemonSet (T1) · Prometheus 15d/20Gi local-path, not backed up (T1) · Grafana hostPath `/opt/grafana` backed up (T2, T6) · Grafana OIDC blueprint (T3) · OIDC + local-admin + Ingress (T4) · Prometheus/Alertmanager port-forward only — no Ingress added anywhere (T1/T4) · email alerts via reflected smtp-relay (T5) · Homepage widget (T6) · README + CLAUDE.md + "what we don't back up" (T7) · staged rollout = Tasks 1→4→5→6 (T1–T6) · acceptance criteria mapped to the verify steps. All spec sections covered.
- **Out of scope (tracked):** Loki/logs #116; ServiceMonitors / long-term storage / HA / forward-auth exposure #117. No tasks here touch those.
- **Resource names** used consistently: release `kps`, Grafana svc `kps-grafana`, Prometheus svc `kps-kube-prometheus-stack-prometheus`, Alertmanager svc `kps-kube-prometheus-stack-alertmanager`, Secrets `grafana-secrets` / `authentik-oidc-secrets` / `smtp-relay`, PVC `grafana-data`. (Verify the exact generated Service/StatefulSet names with `kubectl -n monitoring get svc,sts` right after Task 1 install — chart name prefixes can vary by release name; adjust the port-forward/rollout targets in later tasks to match.)
- **Lint caveat (T7):** `pv-pvc.yaml` + `secret.example.yaml` are raw manifests the kubeconform filter should cover; `values.yaml` is excluded (Helm values) like other services. The blueprint `grafana.yaml` is covered by the (passing) yamllint, not kubeconform — consistent with #104.
