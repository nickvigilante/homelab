# Homelab alert rules and health dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three homelab-specific Prometheus alert rules and one "Homelab Health" Grafana dashboard, reconciled by Flux, plus enable cert-manager metrics so the cert-expiry alert has data.

**Architecture:** A new `k8s/homelab-monitoring/` directory (mirroring `k8s/flux-monitoring/`) holds a `PrometheusRule` and a dashboard `ConfigMap`, reconciled by a new `clusters/gandalf/homelab-monitoring.yaml` Flux `Kustomization`. cert-manager's ServiceMonitor is turned on via a one-line `values.yaml` change applied with a pinned `helm upgrade`. Everything else (Alertmanager email routing, Prometheus all-namespace discovery) already exists and is inherited.

**Tech Stack:** Prometheus Operator (`PrometheusRule` CRD), kube-state-metrics, node-exporter, cert-manager metrics, Grafana sidecar dashboard ConfigMaps, Flux Kustomize, Helm.

**Spec:** `docs/superpowers/specs/2026-06-27-homelab-monitoring-design.md`

## Global Constraints

Every task implicitly includes these:

- **Worktree:** all work happens in `~/git/nickvigilante/homelab/.worktrees/138-homelab-monitoring` on branch `138-homelab-monitoring`. Never commit on `main`; never push to `main` (branch protection).
- **Secrets never enter the repo.** Public repo; betterleaks runs pre-commit and in CI. Nothing in this plan introduces secrets.
- **Markdown style:** semantic line breaks (one clause per source line). Write "and"/"&", never `+`, in prose. Markdown is formatted by **mdformat** via pre-commit (not prettier). Run `pre-commit run --files <file>` after editing any `.md` and re-stage if it reformats.
- **YAML formatting:** the repo uses `yamlfmt -conf .yamlfmt`. Run pre-commit before each commit; it runs yamlfmt + yamllint and will reformat.
- **Commit trailer:** every commit message ends with `Assisted-by: AI`. **No `Co-Authored-By` trailer** anywhere.
- **Commit subjects:** imperative mood, ≤ 70 chars. Body explains "why," not "what."
- **Helm pin discipline:** any `helm upgrade` MUST pass `--version <installed-version>`. Never run an unpinned upgrade.
- **Live cluster steps run from the laptop** over the tailnet (`KUBECONFIG=~/.kube/homelab.yaml`), not on gandalf (where the kubeconfig is root-only). Flux-managed resources need no manual apply — they reconcile from git after merge. Tasks 1–4 are fully offline-validatable on the branch; live application (Task 5) happens after the PR merges.

______________________________________________________________________

## File Structure

| File                                                    | Responsibility                                      |
| ------------------------------------------------------- | --------------------------------------------------- |
| `k8s/homelab-monitoring/prometheusrule.yaml`            | The three `homelab`-group alert rules               |
| `k8s/homelab-monitoring/dashboards/homelab-health.yaml` | The "Homelab Health" dashboard ConfigMap            |
| `k8s/homelab-monitoring/kustomization.yaml`             | Kustomize meta-file listing the two resources       |
| `k8s/homelab-monitoring/README.md`                      | Runbook: what the alerts mean, how to add more      |
| `clusters/gandalf/homelab-monitoring.yaml`              | Flux `Kustomization` reconciling the directory      |
| `k8s/cert-manager/values.yaml`                          | (modify) enable `prometheus.servicemonitor.enabled` |

______________________________________________________________________

## Task 1: PrometheusRule — the three homelab alerts

**Files:**

- Create: `k8s/homelab-monitoring/prometheusrule.yaml`

**Interfaces:**

- Produces: a `PrometheusRule` named `homelab-alerts` in namespace `monitoring` with rule group `homelab` containing alerts `ResticBackupStale`, `ContainerOOMKilled`, `CertificateExpiringSoon`. Prometheus discovers it via `ruleSelectorNilUsesHelmValues: false` (already set in kps values). The `CertificateExpiringSoon` alert depends on the cert-manager metric enabled in Task 4; it loads regardless and simply has no data until then.

- [ ] **Step 1: Write the PrometheusRule**

Create `k8s/homelab-monitoring/prometheusrule.yaml`:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: homelab-alerts
  namespace: monitoring
spec:
  groups:
    - name: homelab
      rules:
        # The nightly restic CronJob (k8s/backup/backup-cronjob.yaml,
        # schedule "0 3 * * *") in the `backup` namespace. Alert on time since
        # last SUCCESS, not on job failure -- a single rule on
        # last_successful_time catches both "ran and failed" and "never ran".
        # 25h gives a 1h grace past the 24h cadence.
        - alert: ResticBackupStale
          expr: |
            (time() - max(kube_cronjob_status_last_successful_time{cronjob="restic-backup", namespace="backup"})) > 25 * 3600
          for: 30m
          labels:
            severity: warning
          annotations:
            summary: "Nightly restic backup has not succeeded in over 25h"
            description: "No successful restic-backup CronJob run in >25h. Check `kubectl -n backup get jobs` and the most recent job's logs."
        # kube_pod_container_status_last_terminated_reason is a sticky gauge --
        # it stays 1 until the next termination for another reason. Correlate
        # with a restart in the last 10m so this reflects a FRESH OOM kill, not
        # a long-resolved one. (The #176 Grafana OOM was this failure mode.)
        - alert: ContainerOOMKilled
          expr: |
            (max by (namespace, pod, container) (kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}) == 1)
            and on (namespace, pod, container)
            (changes(kube_pod_container_status_restarts_total[10m]) > 0)
          for: 0m
          labels:
            severity: warning
          annotations:
            summary: "Container {{ $labels.namespace }}/{{ $labels.pod }} ({{ $labels.container }}) was OOMKilled"
            description: "Container was OOMKilled and restarted in the last 10m. Consider raising its memory limit."
        # certmanager_certificate_expiration_timestamp_seconds comes from the
        # cert-manager ServiceMonitor (enabled in Task 4). Renewal runs at 30d
        # before expiry, so <14d means DNS-01 renewal has been failing ~16+
        # days -- a real safety net for a revoked Cloudflare token.
        - alert: CertificateExpiringSoon
          expr: |
            (min by (name, namespace) (certmanager_certificate_expiration_timestamp_seconds) - time()) / 86400 < 14
          for: 1h
          labels:
            severity: warning
          annotations:
            summary: "Certificate {{ $labels.namespace }}/{{ $labels.name }} expires in <14d"
            description: "cert-manager certificate is within 14 days of expiry, meaning automatic renewal has likely been failing. Check `kubectl -n cert-manager get certificate,order,challenge`."
```

- [ ] **Step 2: Validate rule structure and PromQL syntax with promtool**

promtool checks a native rules file (top-level `groups:`), which is exactly the `PrometheusRule` `.spec`. Extract it and check:

Run:

```bash
cd ~/git/nickvigilante/homelab/.worktrees/138-homelab-monitoring
python3 -c 'import yaml,sys; yaml.safe_dump(yaml.safe_load(open("k8s/homelab-monitoring/prometheusrule.yaml"))["spec"], sys.stdout)' > /tmp/homelab-rules.yaml
promtool check rules /tmp/homelab-rules.yaml
```

Expected: `SUCCESS: 3 rules found` (and no parse errors). If promtool reports a PromQL syntax error, fix the expression and re-run.

- [ ] **Step 3: Validate the manifest schema with kubeconform**

Run (same CRD schema source the CI lint job uses):

```bash
cd ~/git/nickvigilante/homelab/.worktrees/138-homelab-monitoring
kubeconform -strict -ignore-missing-schemas \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  k8s/homelab-monitoring/prometheusrule.yaml
```

Expected: `k8s/homelab-monitoring/prometheusrule.yaml - PrometheusRule homelab-alerts is valid`

- [ ] **Step 4: Commit**

```bash
cd ~/git/nickvigilante/homelab/.worktrees/138-homelab-monitoring
pre-commit run --files k8s/homelab-monitoring/prometheusrule.yaml   # yamlfmt/yamllint/betterleaks; re-stage if reformatted
git add k8s/homelab-monitoring/prometheusrule.yaml
git commit -m "Add homelab PrometheusRule (restic, OOM, cert-expiry)

Assisted-by: AI"
```

______________________________________________________________________

## Task 2: Homelab Health dashboard

**Files:**

- Create: `k8s/homelab-monitoring/dashboards/homelab-health.yaml`

**Interfaces:**

- Produces: a `ConfigMap` named `homelab-health-dashboard` in namespace `monitoring` carrying label `grafana_dashboard: "1"`, with one data key `homelab-health.json`. The Grafana sidecar (already running, same mechanism as `k8s/flux-monitoring/dashboards/`) loads it automatically.

This task authors a Grafana dashboard JSON. The **panel specification below is the contract** — every panel's title, type, unit, and exact PromQL query is given. Build the JSON so each panel matches its row in the table; the surrounding JSON structure (ids, `gridPos`, datasource refs) is mechanical. A complete two-panel skeleton follows the table to lock the exact structure to copy.

**Panel specification** (datasource: the default Prometheus, `${DS_PROMETHEUS}` / `type: prometheus`):

| #   | Title                        | Type       | Unit              | PromQL                                                                                                                                                     | Notes                                            |
| --- | ---------------------------- | ---------- | ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 1   | Nodes Ready                  | stat       | none              | `count(kube_node_status_condition{condition="Ready", status="true"})`                                                                                      | thresholds: red if < 3                           |
| 2   | Hours Since Last Backup      | stat       | `h` (hours)       | `(time() - max(kube_cronjob_status_last_successful_time{cronjob="restic-backup", namespace="backup"})) / 3600`                                             | thresholds: green < 24, red ≥ 25                 |
| 3   | Min Cert Days Remaining      | stat       | `d` (days)        | `min(certmanager_certificate_expiration_timestamp_seconds - time()) / 86400`                                                                               | thresholds: red < 14, yellow < 30, green ≥ 30    |
| 4   | Containers Flagged OOMKilled | stat       | none              | `count(kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1) or vector(0)`                                                            | thresholds: green 0, red ≥ 1                     |
| 5   | Flux Resources Not Ready     | stat       | none              | `sum(gotk_resource_info{ready="False"}) or vector(0)`                                                                                                      | thresholds: green 0, red ≥ 1                     |
| 6   | Node CPU Usage               | timeseries | `percent` (0–100) | `100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)`                                                                          | legend `{{instance}}`                            |
| 7   | Node Memory Usage            | timeseries | `percent` (0–100) | `100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)`                                                                                  | legend `{{instance}}`                            |
| 8   | Root Filesystem Usage        | timeseries | `percent` (0–100) | `100 * (1 - node_filesystem_avail_bytes{mountpoint="/", fstype!~"tmpfs\|overlay"} / node_filesystem_size_bytes{mountpoint="/", fstype!~"tmpfs\|overlay"})` | legend `{{instance}}`                            |
| 9   | PV Used %                    | timeseries | `percent` (0–100) | `100 * (kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes)`                                                                            | legend `{{persistentvolumeclaim}}`               |
| 10  | Top Pod Restarts             | table      | none              | `topk(10, sum by (namespace, pod) (kube_pod_container_status_restarts_total))`                                                                             | instant query, format `table`                    |
| 11  | Certificate Expiry (days)    | table      | none              | `(certmanager_certificate_expiration_timestamp_seconds - time()) / 86400`                                                                                  | instant query, format `table`, legend `{{name}}` |

- [ ] **Step 1: Author the dashboard ConfigMap**

Create `k8s/homelab-monitoring/dashboards/homelab-health.yaml`. Use this exact ConfigMap wrapper and structure; the two panels shown (one `stat`, one `timeseries`) are complete and correct — replicate the pattern for the remaining nine panels per the table, incrementing `id` and laying out `gridPos` in reading order (stats across the top in an 8-wide × 2 grid is fine, timeseries full or half width below, tables full width at the bottom):

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: homelab-health-dashboard
  namespace: monitoring
  labels:
    grafana_dashboard: "1"
data:
  homelab-health.json: |
    {
      "annotations": { "list": [] },
      "editable": true,
      "schemaVersion": 39,
      "tags": ["homelab"],
      "title": "Homelab Health",
      "uid": "homelab-health",
      "time": { "from": "now-6h", "to": "now" },
      "templating": { "list": [] },
      "panels": [
        {
          "id": 1,
          "type": "stat",
          "title": "Nodes Ready",
          "gridPos": { "h": 4, "w": 4, "x": 0, "y": 0 },
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "fieldConfig": {
            "defaults": {
              "thresholds": {
                "mode": "absolute",
                "steps": [
                  { "color": "red", "value": null },
                  { "color": "green", "value": 3 }
                ]
              }
            },
            "overrides": []
          },
          "targets": [
            {
              "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
              "expr": "count(kube_node_status_condition{condition=\"Ready\", status=\"true\"})",
              "refId": "A"
            }
          ]
        },
        {
          "id": 6,
          "type": "timeseries",
          "title": "Node CPU Usage",
          "gridPos": { "h": 8, "w": 12, "x": 0, "y": 6 },
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "fieldConfig": {
            "defaults": { "unit": "percent", "min": 0, "max": 100 },
            "overrides": []
          },
          "targets": [
            {
              "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
              "expr": "100 - (avg by (instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[5m])) * 100)",
              "legendFormat": "{{instance}}",
              "refId": "A"
            }
          ]
        }
      ]
    }
```

Notes for the remaining panels:

- Stat panels (#1–#5): copy panel 1's shape; change `id`, `title`, `gridPos.x` (0, 4, 8, 12, 16 across the top row, each `w: 4`), `targets[0].expr`, `fieldConfig.defaults.unit` (`h` for #2, `d` for #3, none for #4/#5), and `thresholds.steps` per the table.

- Timeseries (#6–#9): copy panel 6's shape; change `id`, `title`, `gridPos`, `expr`, `legendFormat`.

- Tables (#10–#11): `"type": "table"`, full width (`"w": 24`), and on each target set `"format": "table"` and `"instant": true`.

- Escape every double-quote inside `expr` strings (`\"`), as shown.

- [ ] **Step 2: Validate the embedded JSON parses**

The dashboard JSON is the failure-prone part. Extract and parse it:

```bash
cd ~/git/nickvigilante/homelab/.worktrees/138-homelab-monitoring
python3 -c 'import yaml,json; d=yaml.safe_load(open("k8s/homelab-monitoring/dashboards/homelab-health.yaml")); json.loads(d["data"]["homelab-health.json"]); print("dashboard JSON OK:", len(json.loads(d["data"]["homelab-health.json"])["panels"]), "panels")'
```

Expected: `dashboard JSON OK: 11 panels`. If `json.loads` raises, fix the JSON (usually an unescaped quote or trailing comma) and re-run.

- [ ] **Step 3: Validate the ConfigMap schema**

Run:

```bash
cd ~/git/nickvigilante/homelab/.worktrees/138-homelab-monitoring
kubeconform -strict -ignore-missing-schemas k8s/homelab-monitoring/dashboards/homelab-health.yaml
```

Expected: `... ConfigMap homelab-health-dashboard is valid`

- [ ] **Step 4: Commit**

```bash
cd ~/git/nickvigilante/homelab/.worktrees/138-homelab-monitoring
pre-commit run --files k8s/homelab-monitoring/dashboards/homelab-health.yaml   # re-stage if reformatted
git add k8s/homelab-monitoring/dashboards/homelab-health.yaml
git commit -m "Add Homelab Health Grafana dashboard

Assisted-by: AI"
```

______________________________________________________________________

## Task 3: Kustomization, Flux wiring, and README

**Files:**

- Create: `k8s/homelab-monitoring/kustomization.yaml`
- Create: `clusters/gandalf/homelab-monitoring.yaml`
- Create: `k8s/homelab-monitoring/README.md`

**Interfaces:**

- Consumes: `prometheusrule.yaml` (Task 1) and `dashboards/homelab-health.yaml` (Task 2).

- Produces: a Flux `Kustomization` named `homelab-monitoring` that reconciles `./k8s/homelab-monitoring`.

- [ ] **Step 1: Write the kustomization**

Create `k8s/homelab-monitoring/kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - prometheusrule.yaml
  - dashboards/homelab-health.yaml
```

- [ ] **Step 2: Write the Flux Kustomization**

Create `clusters/gandalf/homelab-monitoring.yaml`. Mirrors `clusters/gandalf/monitoring.yaml` (the `flux-monitoring` precedent), with `dependsOn: kps` added so the `monitoring` namespace and the `PrometheusRule` CRD exist before first reconcile on a cold boot:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: homelab-monitoring
  namespace: flux-system
spec:
  interval: 1h
  path: ./k8s/homelab-monitoring
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  dependsOn:
    - name: kps
```

- [ ] **Step 3: Write the README**

Create `k8s/homelab-monitoring/README.md`:

```markdown
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
- **CertificateExpiringSoon** — a cert-manager certificate is <14d from expiry,
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
```

- [ ] **Step 4: Validate kustomize build and Flux Kustomization schema**

Run:

```bash
cd ~/git/nickvigilante/homelab/.worktrees/138-homelab-monitoring
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s/homelab-monitoring >/dev/null && echo "kustomize build OK"
kubeconform -strict -ignore-missing-schemas \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  clusters/gandalf/homelab-monitoring.yaml
```

Expected: `kustomize build OK` and `... Kustomization homelab-monitoring is valid`.

- [ ] **Step 5: Commit**

```bash
cd ~/git/nickvigilante/homelab/.worktrees/138-homelab-monitoring
pre-commit run --files k8s/homelab-monitoring/kustomization.yaml clusters/gandalf/homelab-monitoring.yaml k8s/homelab-monitoring/README.md   # re-stage if reformatted
git add k8s/homelab-monitoring/kustomization.yaml clusters/gandalf/homelab-monitoring.yaml k8s/homelab-monitoring/README.md
git commit -m "Wire homelab-monitoring Flux Kustomization

Assisted-by: AI"
```

______________________________________________________________________

## Task 4: Enable cert-manager metrics

**Files:**

- Modify: `k8s/cert-manager/values.yaml`

**Interfaces:**

- Produces: a ServiceMonitor (created by the cert-manager chart) that Prometheus discovers, exposing `certmanager_certificate_expiration_timestamp_seconds`. Consumed by Task 1's `CertificateExpiringSoon` alert and Task 2's cert panels. The live `helm upgrade` that applies this is in Task 5 (post-merge); this task is the values change plus an offline render check.

- [ ] **Step 1: Add the prometheus block to values.yaml**

Append to `k8s/cert-manager/values.yaml` (after the existing `extraArgs` block):

```yaml
# Expose cert-manager's controller metrics to Prometheus (#138). The chart
# already serves metrics on port 9402; this adds the ServiceMonitor that the
# cluster Prometheus (all-namespace ServiceMonitor discovery) scrapes. Needed
# for the CertificateExpiringSoon alert and the Homelab Health cert panels --
# the safety net for DNS-01 renewal silently breaking.
prometheus:
  enabled: true
  servicemonitor:
    enabled: true
```

- [ ] **Step 2: Render-check that the chart produces a ServiceMonitor**

Confirm the values key is consumed by the installed chart version before relying on it at upgrade time. First find the installed version, then template:

```bash
cd ~/git/nickvigilante/homelab/.worktrees/138-homelab-monitoring
helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1; helm repo update jetstack >/dev/null
# Use the installed chart version. From the laptop you can confirm it with:
#   KUBECONFIG=~/.kube/homelab.yaml helm list -n cert-manager
# README records v1.20.2 at install; substitute the live value if different.
helm template cert-manager jetstack/cert-manager --version v1.20.2 \
  -n cert-manager -f k8s/cert-manager/values.yaml \
  --set crds.enabled=true \
  --show-only templates/servicemonitor.yaml
```

Expected: a rendered `ServiceMonitor` manifest (not an empty output or a "could not find template" error). If the template path differs for the installed version, render the full chart and grep: `helm template ... | grep -A2 'kind: ServiceMonitor'`. If no ServiceMonitor renders, stop and report — the values key name may differ for that chart version.

- [ ] **Step 3: Commit**

```bash
cd ~/git/nickvigilante/homelab/.worktrees/138-homelab-monitoring
pre-commit run --files k8s/cert-manager/values.yaml   # re-stage if reformatted
git add k8s/cert-manager/values.yaml
git commit -m "Enable cert-manager ServiceMonitor for metrics

Assisted-by: AI"
```

______________________________________________________________________

## Task 5: Apply and verify (post-merge, live cluster)

This task runs **after the PR is reviewed and merged to `main`**. The Flux-managed pieces (Tasks 1–3) reconcile from git automatically; the cert-manager change (Task 4) needs a manual pinned `helm upgrade`. All commands run **from the laptop** over the tailnet. Do not force-fire alerts — verification is read-only.

Hand these to the user as `!`-prefixed commands (they execute in-session). Each cluster command assumes `export KUBECONFIG=~/.kube/homelab.yaml`.

- [ ] **Step 1: Apply the cert-manager values change (pinned helm upgrade)**

```bash
cd ~/git/nickvigilante/homelab && git checkout main && git pull
export KUBECONFIG=~/.kube/homelab.yaml
# Confirm the installed version, then pin to it:
VER=$(helm list -n cert-manager -o json | jq -r '.[] | select(.name=="cert-manager") | .chart' | sed 's/cert-manager-//')
echo "Pinning cert-manager upgrade to installed chart version: $VER"
helm upgrade cert-manager jetstack/cert-manager -n cert-manager --version "$VER" -f k8s/cert-manager/values.yaml
```

Expected: `Release "cert-manager" has been upgraded`. Same chart version in/out (values-only change).

- [ ] **Step 2: Confirm cert-manager metrics are now scraped**

```bash
export KUBECONFIG=~/.kube/homelab.yaml
kubectl -n cert-manager get servicemonitor
```

Expected: a `cert-manager` ServiceMonitor exists. Then, in the Prometheus UI (`https://prometheus.vigihome.net` → Graph), query `certmanager_certificate_expiration_timestamp_seconds` and confirm it returns series (one per certificate, including `vigihome-tls`). Allow ~1 scrape interval (≤1m).

- [ ] **Step 3: Reconcile and confirm the Flux Kustomization is Ready**

```bash
export KUBECONFIG=~/.kube/homelab.yaml
flux reconcile kustomization homelab-monitoring --with-source
flux get kustomization homelab-monitoring
```

Expected: `homelab-monitoring` shows `Ready: True`. Confirm the objects landed:

```bash
kubectl -n monitoring get prometheusrule homelab-alerts
kubectl -n monitoring get configmap homelab-health-dashboard
```

- [ ] **Step 4: Confirm the rules load green in Prometheus**

Confirm the `homelab` group shows all three rules with no evaluation errors. The Prometheus container image is minimal (no shell/wget), and the Ingress is forward-auth gated, so query the API via a pod-direct port-forward from the laptop (port-forward to the StatefulSet/pod, not the multi-port Service):

```bash
export KUBECONFIG=~/.kube/homelab.yaml
kubectl -n monitoring port-forward sts/prometheus-kps-kube-prometheus-stack-prometheus 9090:9090 >/dev/null 2>&1 &
PF=$!; sleep 3
curl -s localhost:9090/api/v1/rules | jq -r '.data.groups[] | select(.name=="homelab") | .rules[] | "\(.name): health=\(.health)"'
kill $PF
```

Expected: each rule prints `health=ok`. (`firing`/`inactive` state is fine; `health=err` is not.) Equivalently, check the Prometheus UI → Status → Rule Health in the browser.

- [ ] **Step 5: Spot-check each expression returns a sane current value**

In the Prometheus UI, run each query and sanity-check:

- `(time() - max(kube_cronjob_status_last_successful_time{cronjob="restic-backup", namespace="backup"})) / 3600` → a small positive number of hours (< 24 if last night's backup succeeded). **If this returns empty, the `kube_cronjob_status_last_successful_time` metric is unavailable in this kube-state-metrics version** — switch `ResticBackupStale` to the failure-based fallback and re-open the branch: `expr: max(kube_job_status_failed{namespace="backup", job_name=~"restic-backup.*"}) > 0` (note: detects failures only, not missed runs — acceptable degradation).

- `min(certmanager_certificate_expiration_timestamp_seconds - time()) / 86400` → days remaining on the soonest cert (~60–90 for a freshly renewed wildcard).

- `count(kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1)` → small or empty (0 is good).

- [ ] **Step 6: Confirm the dashboard renders in Grafana**

In Grafana (`https://grafana.vigihome.net`) → Dashboards, open **Homelab Health**. Confirm panels render data (node CPU/mem/disk populated; backup-freshness and cert panels show values now that scraping is live; tables populate). A panel reading "No data" for OOM/restarts is expected when there are none.

- [ ] **Step 7: Close the issue**

Once verified, close #138 with a short summary of what shipped (the three alerts, cert-manager metrics, the dashboard) and note the deferred per-app dashboards remain tracked in #117.

______________________________________________________________________

## Self-Review notes

- **Spec coverage:** ResticBackupStale, ContainerOOMKilled, CertificateExpiringSoon (Task 1) ✓; cert-manager ServiceMonitor (Task 4) ✓; Homelab Health dashboard (Task 2) ✓; flux-monitoring-style layout + Flux wiring (Task 3) ✓; verification without forced firing (Task 5) ✓; per-app dashboards explicitly deferred to #117 (not implemented, by design) ✓.
- **Metric-existence risk** (`kube_cronjob_status_last_successful_time`) is handled with a concrete fallback expr in Task 5 Step 5, not a placeholder.
- **OOM sticky-gauge trap** is handled via the restart-correlation join, per the spec note.
- **Names are consistent across tasks:** `homelab-alerts` (PrometheusRule), `homelab-health-dashboard` (ConfigMap), `homelab-monitoring` (Flux Kustomization), group `homelab`.
