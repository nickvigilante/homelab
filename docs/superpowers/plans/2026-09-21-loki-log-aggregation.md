# Loki Log Aggregation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Loki with 30-day retention and Grafana Alloy collectors, under Flux, so pod logs, node journald and Kubernetes events are searchable in the existing Grafana.

**Architecture:** One new service directory, `k8s/loki/`, reconciled by a new Flux Kustomization.
It holds three `HelmRelease`s (`loki` in Monolithic mode, an `alloy` DaemonSet, and a single-replica `alloy-events` Deployment), a raw least-privilege `rbac.yaml` for the two Alloy ServiceAccounts, and a Grafana datasource ConfigMap that kps-grafana's existing sidecar loads.

**Tech Stack:** Flux 2.x (`HelmRelease`, `Kustomization`), Helm charts `grafana-community/loki` and `grafana/alloy`, Alloy configuration syntax, kubeconform.

**Spec:** `docs/superpowers/specs/2026-09-21-loki-log-aggregation-design.md`

## Global Constraints

- Namespace for every workload: `monitoring`.
- Chart pins: `loki` 18.13.4 from `https://grafana-community.github.io/helm-charts`; `alloy` 1.12.1 from `https://grafana.github.io/helm-charts`. Before Task 1, check each index for a newer release; if one exists, use it and re-run every render test.
- Loki: `deploymentMode: Monolithic` (the chart's current name for SingleBinary), `auth_enabled: false`, replication factor 1, TSDB schema v13, filesystem storage, `retention_period: 720h` enforced by the compactor.
- Loki storage: `local-path` PVC, 20Gi, pinned to gandalf. Not backed up by restic.
- Loki and `alloy-events` pin to `kubernetes.io/hostname: gandalf`; `alloy` runs on all nodes.
- Resources: `loki` requests 100m/512Mi, limit 1Gi; `alloy` 50m/64Mi, limit 256Mi; `alloy-events` 25m/64Mi, limit 128Mi.
- Index labels only: `job`, `namespace`, `container`, `app`, `node`, plus `unit` and `level` for journald. The pod name is structured metadata, never an index label.
- No workload may read Secrets. Both charts' defaults grant cluster-wide Secret reads (Loki's rules sidecar; Alloy's `remote.kubernetes.*` rule); both are removed.
- Datasource uid `loki`, URL `http://loki.monitoring.svc.cluster.local:3100`, `isDefault: false`.
- Markdown: one sentence per line; write "and", never `+`, for "and" in prose.
- Commits: Conventional Commits, imperative, ≤ 70 characters, ending with the `Assisted-by: AI` trailer. No `Co-Authored-By`.

## Tooling (used by every task's tests)

Nothing below is committed.
Run once per session from the worktree root; `$T` is a scratch directory outside the repo.

```bash
export T=/tmp/loki-plan && mkdir -p "$T/bin" "$T/charts"
curl -fsSL https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz | tar xz -C "$T/bin" --strip-components=1 linux-amd64/helm
curl -fsSL https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz | tar xz -C "$T/bin" kubeconform
curl -fsSL https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv5.7.1/kustomize_v5.7.1_linux_amd64.tar.gz | tar xz -C "$T/bin"
curl -fsSL -o "$T/alloy.zip" https://github.com/grafana/alloy/releases/download/v1.19.2/alloy-linux-amd64.zip \
  && python3 -c "import zipfile;zipfile.ZipFile('$T/alloy.zip').extractall('$T/bin')" && chmod +x "$T/bin/alloy-linux-amd64"
"$T/bin/helm" repo add grafana-community https://grafana-community.github.io/helm-charts
"$T/bin/helm" repo add grafana https://grafana.github.io/helm-charts
"$T/bin/helm" pull grafana-community/loki --version 18.13.4 -d "$T/charts" --untar
"$T/bin/helm" pull grafana/alloy --version 1.12.1 -d "$T/charts" --untar
```

`render NAME CHART` renders one `HelmRelease`'s inline values from `k8s/loki/helmrelease.yaml` with the real chart:

```bash
render() {
  uvx --with pyyaml python -c "
import sys, yaml
for d in yaml.safe_load_all(open('k8s/loki/helmrelease.yaml')):
    if d and d['metadata']['name'] == sys.argv[1]:
        yaml.safe_dump(d['spec']['values'], open('$T/' + sys.argv[1] + '-values.yaml', 'w'))
        break
else:
    sys.exit('no HelmRelease named ' + sys.argv[1])
" "$1" && "$T/bin/helm" template "$1" "$T/charts/$2" -n monitoring -f "$T/$1-values.yaml" > "$T/$1-render.yaml"
}
```

`kinds FILE` prints each rendered object's kind and name, one per line:

```bash
kinds() { uvx --with pyyaml python -c "
import sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d: print(d['kind'], d['metadata']['name'])
" "$1"; }
```

______________________________________________________________________

### Task 1: Loki under Flux

Delivers a running Loki with no collectors yet, so it can be reviewed and deployed on its own.

**Files:**

- Create: `sources/grafana-community-loki.yaml`
- Create: `k8s/loki/helmrelease.yaml` (the `loki` release only; Task 2 appends to it)
- Create: `k8s/loki/kustomization.yaml`
- Create: `clusters/gandalf/loki.yaml`
- Modify: `docs/superpowers/specs/2026-09-21-loki-log-aggregation-design.md` (terminology and findings)

**Interfaces:**

- Produces: Service `loki` in `monitoring`, port 3100 (`http-metrics`), push path `/loki/api/v1/push`. Tasks 2 and 3 point at `http://loki.monitoring.svc.cluster.local:3100`.

- Produces: `k8s/loki/kustomization.yaml` with a `resources:` list that Tasks 2 and 3 append to.

- [ ] **Step 1: Write the failing render test**

```bash
render loki loki && kinds "$T/loki-render.yaml"
```

- [ ] **Step 2: Run it to verify it fails**

Expected: `no HelmRelease named loki` (the file does not exist yet, so Python raises `FileNotFoundError`; either failure counts).

- [ ] **Step 3: Create the source**

`sources/grafana-community-loki.yaml`:

```yaml
# Same repository as grafana-community.yaml, under a second name on purpose.
# grafana-community.yaml is owned by the claude-mcp Kustomization (prune: true).
# If two pruning Kustomizations own one object, whichever stops listing it
# first deletes it for both. Collapse into one shared source when a cluster
# sources Kustomization exists (deferred in CLAUDE.md, "GitOps with Flux").
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: grafana-community-loki
  namespace: flux-system
spec:
  interval: 1h
  url: https://grafana-community.github.io/helm-charts
```

- [ ] **Step 4: Create the Loki HelmRelease**

`k8s/loki/helmrelease.yaml`:

```yaml
# Loki log store (#116), Monolithic mode on local disk with 30-day retention.
# Design: docs/superpowers/specs/2026-09-21-loki-log-aggregation-design.md.
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: loki
  namespace: monitoring
spec:
  releaseName: loki
  targetNamespace: monitoring
  interval: 30m
  chart:
    spec:
      chart: loki
      version: "18.13.4"
      sourceRef:
        kind: HelmRepository
        name: grafana-community-loki
        namespace: flux-system
  # First install: cold image pull plus PVC provisioning.
  timeout: 10m
  install:
    remediation:
      retries: 3
  values:
    # "Monolithic" is the chart's current name for SingleBinary.
    deploymentMode: Monolithic
    fullnameOverride: loki
    loki:
      auth_enabled: false
      commonConfig:
        replication_factor: 1
      storage:
        type: filesystem
      schemaConfig:
        configs:
          - from: "2026-09-21"
            store: tsdb
            object_store: filesystem
            schema: v13
            index:
              prefix: loki_index_
              period: 24h
      compactor:
        retention_enabled: true
        delete_request_store: filesystem
      limits_config:
        retention_period: 720h
      pattern_ingester:
        enabled: false
    singleBinary:
      replicas: 1
      nodeSelector:
        kubernetes.io/hostname: gandalf
      persistence:
        enabled: true
        storageClass: local-path
        size: 20Gi
      resources:
        requests:
          cpu: 100m
          memory: 512Mi
        limits:
          memory: 1Gi
    read:
      replicas: 0
    write:
      replicas: 0
    backend:
      replicas: 0
    # Extras a single-node homelab doesn't need.
    gateway:
      enabled: false
    chunksCache:
      enabled: false
    resultsCache:
      enabled: false
    lokiCanary:
      enabled: false
    test:
      enabled: false
    minio:
      enabled: false
    # The rules sidecar reads ConfigMaps and Secrets cluster-wide. It only
    # loads ruler rules, and the ruler is a follow-up, so keep it off.
    sidecar:
      rules:
        enabled: false
    monitoring:
      serviceMonitor:
        enabled: true
```

- [ ] **Step 5: Create the kustomization**

`k8s/loki/kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../sources/grafana-community-loki.yaml
  - helmrelease.yaml
```

- [ ] **Step 6: Run the render test to verify it passes**

```bash
render loki loki && kinds "$T/loki-render.yaml"
grep -c 'secrets' "$T/loki-render.yaml"
grep -E 'retention_period: 720h|auth_enabled: false|storageClassName: local-path' "$T/loki-render.yaml"
```

Expected: kinds include `StatefulSet loki`, `Service loki`, `Service loki-headless`, `ServiceMonitor loki`, and no `Deployment`; the `secrets` count is `0`; all three grep patterns match.
Also check the Service port:

```bash
uvx --with pyyaml python -c "
import yaml
for d in yaml.safe_load_all(open('$T/loki-render.yaml')):
    if d and d['kind']=='Service' and d['metadata']['name']=='loki':
        print([(p['name'], p['port']) for p in d['spec']['ports']])"
```

Expected: `('http-metrics', 3100)` is in the list.

- [ ] **Step 7: Create the Flux Kustomization**

`clusters/gandalf/loki.yaml`:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: loki
  namespace: flux-system
spec:
  interval: 10m
  path: ./k8s/loki
  # Flux owns everything in k8s/loki.
  prune: true
  wait: true
  timeout: 10m
  sourceRef:
    kind: GitRepository
    name: flux-system
  # The Grafana this registers a datasource with belongs to kps.
  dependsOn:
    - name: kps
```

- [ ] **Step 8: Validate the manifests**

```bash
"$T/bin/kustomize" build --load-restrictor LoadRestrictionsNone k8s/loki > "$T/loki-kustomize.yaml" && kinds "$T/loki-kustomize.yaml"
"$T/bin/kubeconform" -summary -strict -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  k8s/loki/helmrelease.yaml sources/grafana-community-loki.yaml clusters/gandalf/loki.yaml
```

Expected: kustomize prints `HelmRepository grafana-community-loki` and `HelmRelease loki`; kubeconform reports every resource valid and 0 errors.

- [ ] **Step 9: Record the spec corrections**

In the spec, apply these edits so it matches what the charts actually support:

1. Replace every `SingleBinary` with `Monolithic`, and after the first mention in "Storage and retention" add the sentence: `"Monolithic" is the chart's current name for what older versions called SingleBinary.`
2. In "Storage and retention", add a bullet: `- The chart's rules sidecar is disabled: it reads ConfigMaps and Secrets cluster-wide and only exists to load ruler rules.`
3. In "Kubernetes events", replace the sentence beginning `Its ServiceAccount needs` with: `The Alloy chart's default ClusterRole grants far more than needed, including Secrets cluster-wide, and its template breaks when either rule list is empty, so both Alloy releases set rbac.create: false and use a raw least-privilege rbac.yaml: pods for alloy, events for alloy-events.`
4. In "Deployment method", add `k8s/loki/rbac.yaml` to the file list, and replace `../../sources/grafana-community.yaml` with `../../sources/grafana-community-loki.yaml` plus the sentence: `A second name for the same repository keeps two pruning Kustomizations from owning one object.`
5. In "Deployment method", change the lint sentence to name both `datasource.yaml` and `rbac.yaml`.
6. In "Pod logs", add: `Read positions live on a hostPath (/var/lib/alloy) so a restart resumes rather than re-reading.`

- [ ] **Step 10: Format and commit**

```bash
uvx pre-commit run --files sources/grafana-community-loki.yaml k8s/loki/helmrelease.yaml k8s/loki/kustomization.yaml clusters/gandalf/loki.yaml docs/superpowers/specs/2026-09-21-loki-log-aggregation-design.md
git add sources/grafana-community-loki.yaml k8s/loki clusters/gandalf/loki.yaml docs/superpowers/specs/2026-09-21-loki-log-aggregation-design.md
git commit -m "feat(loki): deploy Loki in Monolithic mode under Flux (#116)" -m "Assisted-by: AI"
```

`yamlfmt` and `yamllint` are not installed locally; pre-commit reports them as missing, and CI runs both.
Run `uvx yamllint -c .github/yamllint.yml` on the new YAML files instead.

______________________________________________________________________

### Task 2: Alloy collectors and least-privilege RBAC

**Files:**

- Create: `sources/grafana.yaml`
- Create: `k8s/loki/rbac.yaml`
- Modify: `k8s/loki/helmrelease.yaml` (append `alloy` and `alloy-events`)
- Modify: `k8s/loki/kustomization.yaml`

**Interfaces:**

- Consumes: Loki push URL `http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push` (Task 1).

- Produces: ServiceAccounts `alloy` and `alloy-events` in `monitoring` (the charts create them via `fullnameOverride`), bound by `rbac.yaml`.

- Produces: log streams with `job` = `pods`, `journal` or `events`, used by Task 3's verification queries.

- [ ] **Step 1: Write the failing tests**

```bash
render alloy alloy && kinds "$T/alloy-render.yaml"
render alloy-events alloy && kinds "$T/alloy-events-render.yaml"
```

- [ ] **Step 2: Run them to verify they fail**

Expected: `no HelmRelease named alloy` and `no HelmRelease named alloy-events`.

- [ ] **Step 3: Create the source**

`sources/grafana.yaml`:

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: grafana
  namespace: flux-system
spec:
  interval: 1h
  url: https://grafana.github.io/helm-charts
```

- [ ] **Step 4: Append the two Alloy releases**

Append to `k8s/loki/helmrelease.yaml`:

```yaml
---
# Alloy on every node: pod logs from /var/log/pods and the systemd journal.
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: alloy
  namespace: monitoring
spec:
  releaseName: alloy
  targetNamespace: monitoring
  interval: 30m
  chart:
    spec:
      chart: alloy
      version: "1.12.1"
      sourceRef:
        kind: HelmRepository
        name: grafana
        namespace: flux-system
  timeout: 10m
  install:
    remediation:
      retries: 3
  values:
    fullnameOverride: alloy
    crds:
      create: false
    # The chart's default ClusterRole includes Secrets cluster-wide, and its
    # template breaks when either rule list is empty. See rbac.yaml.
    rbac:
      create: false
    serviceMonitor:
      enabled: true
    controller:
      type: daemonset
      volumes:
        extra:
          # Volatile journal, the default on some Pi images.
          - name: run-journal
            hostPath:
              path: /run/log/journal
              type: DirectoryOrCreate
          - name: machine-id
            hostPath:
              path: /etc/machine-id
              type: File
          # Read positions survive restarts, so Alloy resumes instead of re-reading.
          - name: alloy-data
            hostPath:
              path: /var/lib/alloy
              type: DirectoryOrCreate
    alloy:
      storagePath: /var/lib/alloy
      mounts:
        # /var/log covers /var/log/pods and the persistent journal.
        varlog: true
        extra:
          - name: run-journal
            mountPath: /run/log/journal
            readOnly: true
          - name: machine-id
            mountPath: /etc/machine-id
            readOnly: true
          - name: alloy-data
            mountPath: /var/lib/alloy
      # Root to read root-owned log files; no capabilities beyond ownership.
      securityContext:
        runAsUser: 0
        runAsGroup: 0
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      resources:
        requests:
          cpu: 50m
          memory: 64Mi
        limits:
          memory: 256Mi
      configMap:
        content: |
          // Pods on this node only; the chart sets HOSTNAME to the node name.
          discovery.kubernetes "pods" {
            role = "pod"
            selectors {
              role  = "pod"
              field = "spec.nodeName=" + sys.env("HOSTNAME")
            }
          }

          discovery.relabel "pods" {
            targets = discovery.kubernetes.pods.targets
            rule {
              source_labels = ["__meta_kubernetes_namespace"]
              target_label  = "namespace"
            }
            rule {
              source_labels = ["__meta_kubernetes_pod_container_name"]
              target_label  = "container"
            }
            rule {
              source_labels = ["__meta_kubernetes_pod_label_app"]
              target_label  = "app"
            }
            // app.kubernetes.io/name wins over app when present.
            rule {
              source_labels = ["__meta_kubernetes_pod_label_app_kubernetes_io_name"]
              regex         = "(.+)"
              target_label  = "app"
            }
            rule {
              source_labels = ["__meta_kubernetes_pod_node_name"]
              target_label  = "node"
            }
            rule {
              source_labels = ["__meta_kubernetes_pod_name"]
              target_label  = "pod"
            }
            rule {
              source_labels = ["__meta_kubernetes_pod_uid", "__meta_kubernetes_pod_container_name"]
              separator     = "/"
              target_label  = "__path__"
              replacement   = "/var/log/pods/*$1/*.log"
            }
            rule {
              target_label = "job"
              replacement  = "pods"
            }
          }

          local.file_match "pods" {
            path_targets = discovery.relabel.pods.output
          }

          loki.source.file "pods" {
            targets    = local.file_match.pods.targets
            forward_to = [loki.process.pods.receiver]
          }

          loki.process "pods" {
            stage.cri {}
            // Pod names churn (Coder workspaces), so keep pod out of the index.
            stage.structured_metadata {
              values = {pod = ""}
            }
            stage.label_drop {
              values = ["pod", "filename"]
            }
            forward_to = [loki.write.default.receiver]
          }

          loki.relabel "journal" {
            forward_to = []
            rule {
              source_labels = ["__journal__systemd_unit"]
              target_label  = "unit"
            }
            rule {
              source_labels = ["__journal_priority_keyword"]
              target_label  = "level"
            }
          }

          loki.source.journal "node" {
            max_age       = "12h"
            relabel_rules = loki.relabel.journal.rules
            labels        = {job = "journal", node = sys.env("HOSTNAME")}
            forward_to    = [loki.write.default.receiver]
          }

          loki.write "default" {
            endpoint {
              url = "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push"
            }
          }
---
# One Alloy for Kubernetes events; a DaemonSet would store each event per node.
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: alloy-events
  namespace: monitoring
spec:
  releaseName: alloy-events
  targetNamespace: monitoring
  interval: 30m
  chart:
    spec:
      chart: alloy
      version: "1.12.1"
      sourceRef:
        kind: HelmRepository
        name: grafana
        namespace: flux-system
  timeout: 10m
  install:
    remediation:
      retries: 3
  values:
    fullnameOverride: alloy-events
    crds:
      create: false
    rbac:
      create: false
    serviceMonitor:
      enabled: true
    controller:
      type: deployment
      replicas: 1
      nodeSelector:
        kubernetes.io/hostname: gandalf
      volumes:
        extra:
          - name: alloy-data
            hostPath:
              path: /var/lib/alloy-events
              type: DirectoryOrCreate
    alloy:
      storagePath: /var/lib/alloy-events
      mounts:
        extra:
          - name: alloy-data
            mountPath: /var/lib/alloy-events
      securityContext:
        runAsUser: 0
        runAsGroup: 0
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      resources:
        requests:
          cpu: 25m
          memory: 64Mi
        limits:
          memory: 128Mi
      configMap:
        content: |
          loki.source.kubernetes_events "cluster" {
            job_name   = "events"
            log_format = "logfmt"
            forward_to = [loki.write.default.receiver]
          }

          loki.write "default" {
            endpoint {
              url = "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push"
            }
          }
```

- [ ] **Step 5: Create the RBAC**

`k8s/loki/rbac.yaml`:

```yaml
# Least-privilege RBAC for the two Alloy releases (rbac.create: false in both).
# The chart default grants Secrets cluster-wide, which a log shipper must not have.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: alloy-pods
rules:
  # discovery.kubernetes, role "pod".
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: alloy-pods
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: alloy-pods
subjects:
  - kind: ServiceAccount
    name: alloy
    namespace: monitoring
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: alloy-events
rules:
  # loki.source.kubernetes_events.
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: alloy-events
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: alloy-events
subjects:
  - kind: ServiceAccount
    name: alloy-events
    namespace: monitoring
```

- [ ] **Step 6: Add both files to the kustomization**

`k8s/loki/kustomization.yaml` becomes:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../sources/grafana-community-loki.yaml
  - ../../sources/grafana.yaml
  - rbac.yaml
  - helmrelease.yaml
```

- [ ] **Step 7: Run the render tests to verify they pass**

```bash
render alloy alloy && kinds "$T/alloy-render.yaml"
render alloy-events alloy && kinds "$T/alloy-events-render.yaml"
grep -cE 'kind: (ClusterRole|Role)' "$T/alloy-render.yaml" "$T/alloy-events-render.yaml"
```

Expected: `alloy` renders `ServiceAccount alloy`, `ConfigMap alloy`, `DaemonSet alloy`, `Service alloy`, `ServiceMonitor alloy`; `alloy-events` renders the same set with a `Deployment alloy-events` instead of a DaemonSet; the role counts are `0` for both (RBAC comes only from `rbac.yaml`).

- [ ] **Step 8: Validate both Alloy configurations with the real binary**

```bash
uvx --with pyyaml python -c "
import yaml
for d in yaml.safe_load_all(open('k8s/loki/helmrelease.yaml')):
    if d and d['metadata']['name'] in ('alloy', 'alloy-events'):
        open('$T/' + d['metadata']['name'] + '.alloy', 'w').write(d['spec']['values']['alloy']['configMap']['content'])"
for f in alloy alloy-events; do HOSTNAME=gandalf "$T/bin/alloy-linux-amd64" validate "$T/$f.alloy"; echo "$f rc=$?"; done
sed 's/stage.cri {}/stage.cri { bogus = 1 }/' "$T/alloy.alloy" > "$T/bad.alloy"
"$T/bin/alloy-linux-amd64" validate "$T/bad.alloy"; echo "bad rc=$?"
```

Expected: `alloy rc=0`, `alloy-events rc=0`, and `bad rc=1` with `unrecognized attribute name "bogus"` (proving the validator actually checks).

- [ ] **Step 9: Validate the manifests**

```bash
"$T/bin/kustomize" build --load-restrictor LoadRestrictionsNone k8s/loki > "$T/loki-kustomize.yaml" && kinds "$T/loki-kustomize.yaml"
"$T/bin/kubeconform" -summary -strict -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  k8s/loki/helmrelease.yaml k8s/loki/rbac.yaml sources/grafana.yaml
```

Expected: kustomize lists two `HelmRepository`, two `ClusterRole`, two `ClusterRoleBinding` and three `HelmRelease` objects; kubeconform reports 0 errors.

- [ ] **Step 10: Format and commit**

```bash
uvx pre-commit run --files sources/grafana.yaml k8s/loki/rbac.yaml k8s/loki/helmrelease.yaml k8s/loki/kustomization.yaml
uvx yamllint -c .github/yamllint.yml sources/grafana.yaml k8s/loki/rbac.yaml k8s/loki/helmrelease.yaml k8s/loki/kustomization.yaml
git add sources/grafana.yaml k8s/loki
git commit -m "feat(loki): collect pod, journald and event logs with Alloy (#116)" -m "Assisted-by: AI"
```

______________________________________________________________________

### Task 3: Grafana datasource, CI coverage and docs

**Files:**

- Create: `k8s/loki/datasource.yaml`
- Create: `k8s/loki/README.md`
- Modify: `k8s/loki/kustomization.yaml`
- Modify: `.github/workflows/lint.yml` (kubeconform filter)
- Modify: `k8s/kube-prometheus-stack/README.md:11` ("Logs (Loki) are #116")
- Modify: `CLAUDE.md` ("What we don't back up")
- Modify: `k8s/claude-mcp/README.md` (log-rotation bullet)

**Interfaces:**

- Consumes: Service `loki:3100` (Task 1); streams `job="pods"|"journal"|"events"` (Task 2).

- Produces: Grafana datasource uid `loki`.

- [ ] **Step 1: Write the failing CI-coverage test**

This reproduces the CI kubeconform file selection:

```bash
find k8s/loki -type f \( -name helmrelease.yaml -o -name datasource.yaml -o -name rbac.yaml \) | sort
grep -cE "name (datasource|rbac)\.yaml" .github/workflows/lint.yml
```

- [ ] **Step 2: Run it to verify it fails**

Expected: `datasource.yaml` is not listed (the file doesn't exist yet), and the grep count is `0` (CI would skip `rbac.yaml` and `datasource.yaml`).

- [ ] **Step 3: Create the datasource**

`k8s/loki/datasource.yaml`:

```yaml
# Loaded by kps-grafana's datasource sidecar, which watches the monitoring
# namespace for this label. No change to kube-prometheus-stack is needed.
apiVersion: v1
kind: ConfigMap
metadata:
  name: loki-datasource
  namespace: monitoring
  labels:
    grafana_datasource: "1"
data:
  loki-datasource.yaml: |
    apiVersion: 1
    datasources:
      - name: Loki
        type: loki
        uid: loki
        access: proxy
        url: http://loki.monitoring.svc.cluster.local:3100
        isDefault: false
        jsonData:
          maxLines: 1000
```

Add `  - datasource.yaml` as the last entry of `resources:` in `k8s/loki/kustomization.yaml`.

- [ ] **Step 4: Extend the CI filter**

In `.github/workflows/lint.yml`, in the `find k8s -type f \( ... \)` list, add after `-o -name podmonitor.yaml \`:

```yaml
              -o -name datasource.yaml \
              -o -name rbac.yaml \
```

- [ ] **Step 5: Run the CI-coverage test to verify it passes**

```bash
find k8s/loki -type f \( -name helmrelease.yaml -o -name datasource.yaml -o -name rbac.yaml \) | sort
grep -cE "name (datasource|rbac)\.yaml" .github/workflows/lint.yml
"$T/bin/kubeconform" -summary -strict -schema-location default k8s/loki/datasource.yaml k8s/loki/rbac.yaml
```

Expected: three files listed, grep count `2`, kubeconform 0 errors.
Also confirm no other repo file named `rbac.yaml` or `datasource.yaml` newly fails validation: `find k8s -name rbac.yaml -o -name datasource.yaml` must list only the two `k8s/loki/` files; if it lists others, run kubeconform on them too and fix or report before committing.

- [ ] **Step 6: Write the README**

`k8s/loki/README.md`:

````markdown
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

| `job`     | Source                              | Extra labels     |
| --------- | ----------------------------------- | ---------------- |
| `pods`    | `/var/log/pods` on every node       | `container`, `app` |
| `journal` | systemd journal on every node       | `unit`, `level`  |
| `events`  | Kubernetes events, cluster-wide     | —                |

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
````

- [ ] **Step 7: Update the other docs**

1. `k8s/kube-prometheus-stack/README.md` line 11: replace `Logs (Loki) are #116;` with `Logs live in Loki (see ../loki/);`.
2. `CLAUDE.md`, under "What we don't back up (and why it's fine)", after the Prometheus bullet, add:

```markdown
- **Loki log data** (#116). Lives on a disposable `local-path` PVC and is
  deliberately excluded, for the same reasons as the Prometheus TSDB. See
  `k8s/loki/README.md`.
```

3. `k8s/claude-mcp/README.md`: replace the bullet beginning `- Container logs rotate within hours` with `- Container logs rotate within hours on busy pods, so query Loki through the Grafana MCP (datasource uid loki) for anything older than the current container.`

- [ ] **Step 8: Format and commit**

```bash
uvx pre-commit run --files k8s/loki/datasource.yaml k8s/loki/README.md k8s/loki/kustomization.yaml .github/workflows/lint.yml k8s/kube-prometheus-stack/README.md CLAUDE.md k8s/claude-mcp/README.md
git add k8s/loki .github/workflows/lint.yml k8s/kube-prometheus-stack/README.md CLAUDE.md k8s/claude-mcp/README.md
git commit -m "feat(loki): add the Grafana datasource, CI coverage and docs (#116)" -m "Assisted-by: AI"
```

______________________________________________________________________

### Task 4: PR, rollout and verification

Merging to `main` deploys: Flux reconciles `clusters/gandalf/loki.yaml` within 10 minutes.
Nothing here is automated by an agent; the Kubernetes MCP is read-only, so the operator merges and any mutating step is theirs.

- [ ] **Step 1: Open the PR**

Push the branch and open a PR that closes #116, using the repo template (`.github/pull_request_template.md`) and ending with the `🤖 Built with AI assistance.` footer.
Wait for CI (kubeconform, pre-commit) to pass.

- [ ] **Step 2: After merge, confirm the releases**

Operator: `flux get helmreleases -n monitoring`.
Expected: `loki`, `alloy` and `alloy-events` are `Ready True`.
Agent (read-only MCP): `pods_list` with label selector `app.kubernetes.io/instance in (loki,alloy,alloy-events)` shows one Loki pod on gandalf, one `alloy` pod per node, and one `alloy-events` pod on gandalf, all Running.

- [ ] **Step 3: Verify each source through the Grafana MCP**

1. Datasource health: `check_datasources_health` with uid `loki` returns OK.
2. Pod logs: operator runs `kubectl -n monitoring rollout restart deploy/kps-grafana`; then `{job="pods", namespace="monitoring", container="grafana"}` returns the new pod's startup lines.
3. Journald on a Pi: `{job="journal", node="samwise"}` returns entries (expect `k3s-agent.service` among `unit` values).
4. Events: `{job="events"} |= "kps-grafana"` shows the restart's events, each once.
5. Pod as metadata, not label: `list_loki_label_names` must not include `pod` or `filename`.

- [ ] **Step 4: Verify retention**

Operator (port-forward rather than exec, since the Loki image may have no shell tools):

```bash
kubectl -n monitoring port-forward svc/loki 3100:3100 &
curl -s localhost:3100/config | grep -E 'retention_period|retention_enabled'
kill %1
```

Expected: `retention_period: 30d` (Loki prints 720h as `30d`) and `retention_enabled: true`.

- [ ] **Step 5: File the follow-ups and close out**

File three issues on this repo and link them from `k8s/loki/README.md` in a follow-up commit:

1. `Loki ruler: alert on log patterns through Alertmanager`
2. `Loki: longer per-stream retention for security and audit streams` (reference #59 and #126)
3. `Loki: drop or sample noisy streams once a week of volume data exists`

Confirm #116 closed with the PR.

- [ ] **Step 6: One week later, check sizing**

Query `sum(rate(loki_distributor_bytes_received_total[7d])) * 86400` for bytes per day, and `kubelet_volume_stats_used_bytes{persistentvolumeclaim=~"storage-loki-0"}` for disk used.
If projected 30-day usage exceeds 15Gi, open an issue to resize or filter.
