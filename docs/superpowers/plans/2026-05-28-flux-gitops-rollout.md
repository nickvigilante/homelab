# Flux GitOps Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up Flux on the k3s cluster so merges to `main` reconcile automatically, prove it on one pilot app, then bring secrets under management via External Secrets Operator (ESO) and Bitwarden Secrets Manager (BWS).

**Architecture:** Flux is bootstrapped from the existing public `homelab` repo via an SSH deploy key, self-managing under `clusters/gandalf/`. The existing `k8s/<service>/` layout is kept; migrated services gain a `HelmRelease`/`Kustomization` that Flux reconciles. Secrets are pulled at runtime by ESO from BWS via the `bitwarden-sdk-server`, leaving zero ciphertext in git. Flux's own metrics flow into the existing kube-prometheus-stack (Grafana + Alertmanager from #76).

**Tech Stack:** Flux (source/kustomize/helm/notification controllers), Helm, kustomize, External Secrets Operator, `bitwarden-sdk-server`, Bitwarden Secrets Manager, cert-manager, kube-prometheus-stack.

**Scope:** This plan covers **Phase 0–2** (bootstrap → `uptime-kuma` pilot + monitoring → ESO/BWS + `homepage-secrets` pilot). Phase 3+ (migrating the remaining services) is documented as a repeatable pattern in the final task, not enumerated here. Reference spec: `docs/superpowers/specs/2026-05-28-flux-gitops-rollout-design.md`.

**Note on verification:** this is infrastructure, not application code, so "tests" are reconcile/health checks (`flux check`, `flux get ...`, `kubectl get ...`) with expected output, not unit tests.

**Spec refinement (bootstrap auth):** the spec said "write-scoped deploy key." Precisely: we use `flux bootstrap git` over SSH with a **per-repo deploy key that has write access** (write is needed so bootstrap can commit `flux-system/`; ongoing reconcile only reads). This is the least-privilege option — a single repo-scoped key, no account-wide PAT.

**Machine convention:** unless tagged otherwise, run commands on **gandalf** (it has cluster admin via `~/.kube/config`, `helm`, and the repo checkout). Steps needing the GitHub web/`gh` or the Bitwarden web console are tagged **[console]**.

______________________________________________________________________

## File Structure

Created/modified across the plan:

| Path                                              | Responsibility                                                                                                                      |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `clusters/gandalf/flux-system/`                   | Flux components + self-sync (created by `flux bootstrap`)                                                                           |
| `clusters/gandalf/flux-system/kustomization.yaml` | Patched to pin Flux controllers to gandalf                                                                                          |
| `clusters/gandalf/apps.yaml`                      | Flux `Kustomization` for the `uptime-kuma` pilot (per-app pattern; later services each get their own `clusters/gandalf/<svc>.yaml`) |
| `clusters/gandalf/homepage.yaml`                  | Flux `Kustomization` reconciling `k8s/homepage` (ExternalSecret pilot)                                                              |
| `clusters/gandalf/infrastructure.yaml`            | Flux `Kustomization` reconciling cluster infra (ESO, issuer, store)                                                                 |
| `clusters/gandalf/monitoring.yaml`                | Flux `Kustomization` reconciling Flux's own monitoring objects                                                                      |
| `sources/uptime-kuma.yaml`                        | `HelmRepository` for the uptime-kuma chart                                                                                          |
| `sources/external-secrets.yaml`                   | `HelmRepository` for the external-secrets chart                                                                                     |
| `k8s/uptime-kuma/helmrelease.yaml`                | `HelmRelease` (takeover of the existing release)                                                                                    |
| `k8s/uptime-kuma/kustomization.yaml`              | kustomize entry listing uptime-kuma resources                                                                                       |
| `k8s/flux-monitoring/podmonitor.yaml`             | Scrape Flux controllers into Prometheus                                                                                             |
| `k8s/flux-monitoring/dashboards/`                 | Flux Grafana dashboards as labelled ConfigMaps                                                                                      |
| `k8s/flux-monitoring/prometheusrule.yaml`         | Alert on failed reconciliations → Alertmanager                                                                                      |
| `k8s/flux-monitoring/kustomization.yaml`          | kustomize entry for the monitoring objects                                                                                          |
| `k8s/cert-manager/internal-issuer.yaml`           | Self-signed CA + internal `ClusterIssuer` for in-cluster certs                                                                      |
| `k8s/external-secrets/helmrelease.yaml`           | ESO + `bitwarden-sdk-server` (HelmRelease)                                                                                          |
| `k8s/external-secrets/sdk-server-cert.yaml`       | Cert for the sdk-server from the internal CA                                                                                        |
| `k8s/external-secrets/clustersecretstore.yaml`    | `ClusterSecretStore` (BWS provider)                                                                                                 |
| `k8s/external-secrets/kustomization.yaml`         | kustomize entry for ESO objects                                                                                                     |
| `k8s/homepage/externalsecret.yaml`                | `ExternalSecret` producing `homepage-secrets`                                                                                       |
| `k8s/homepage/kustomization.yaml`                 | kustomize entry for homepage's Flux-managed resources                                                                               |
| `CLAUDE.md`                                       | DR model + GitOps/secrets discipline updates                                                                                        |

______________________________________________________________________

## Prerequisites

- [ ] **flux CLI installed on gandalf.** It is not present today.

```bash
brew install fluxcd/tap/flux
flux --version   # expect: flux version 2.x.x
```

- [ ] **Cluster reachable.** `kubectl get nodes` lists gandalf/frodo/samwise Ready.
- [ ] **Helm release inventory captured** (needed for the takeover):

```bash
helm list -A
# Confirm: release "uptime-kuma" in ns "monitoring", chart "uptime-kuma-4.1.0".
```

______________________________________________________________________

## Task 1: Bootstrap Flux (Phase 0)

**Files:**

- Create (via `flux bootstrap`): `clusters/gandalf/flux-system/{gotk-components.yaml,gotk-sync.yaml,kustomization.yaml}`

- Modify: `clusters/gandalf/flux-system/kustomization.yaml` (pin controllers to gandalf)

- [ ] **Step 1: Generate a dedicated SSH deploy key for Flux** (gandalf)

```bash
ssh-keygen -t ed25519 -C "flux-homelab" -f ~/.ssh/flux-homelab -N ""
cat ~/.ssh/flux-homelab.pub
```

Expected: a new keypair at `~/.ssh/flux-homelab{,.pub}`.

- [ ] **Step 2: [console] Add the public key as a write-enabled deploy key** on `nickvigilante/homelab`

```bash
gh repo deploy-key add ~/.ssh/flux-homelab.pub --repo nickvigilante/homelab --title "flux-homelab" --allow-write
gh repo deploy-key list --repo nickvigilante/homelab   # confirm it appears
```

Expected: deploy key listed. (Write is needed only so bootstrap can commit `flux-system/`.)

- [ ] **Step 3: Bootstrap Flux** (gandalf)

```bash
flux bootstrap git \
  --url=ssh://git@github.com/nickvigilante/homelab \
  --branch=main \
  --path=clusters/gandalf \
  --private-key-file=$HOME/.ssh/flux-homelab \
  --silent
```

Expected: Flux commits `clusters/gandalf/flux-system/` to `main`, installs the controllers, and the `flux-system` Kustomization reports applied. (`--silent` skips the confirmation prompt.)

- [ ] **Step 4: Verify the install**

```bash
flux check
kubectl -n flux-system get pods
flux get kustomizations
```

Expected: `flux check` all‑green; source/kustomize/helm/notification controller pods `Running`; `flux-system` Kustomization `Ready=True`.

- [ ] **Step 5: Pin Flux controllers to gandalf** — pull the bootstrap commit, then add a patch

```bash
git checkout main && git pull
```

Append to `clusters/gandalf/flux-system/kustomization.yaml`:

```yaml
patches:
  - target:
      kind: Deployment
      labelSelector: app.kubernetes.io/part-of=flux
    patch: |
      - op: add
        path: /spec/template/spec/nodeSelector
        value:
          kubernetes.io/hostname: gandalf
```

- [ ] **Step 6: Commit via PR; Flux applies it**

```bash
git checkout -b flux-pin-controllers
git add clusters/gandalf/flux-system/kustomization.yaml
git commit -m "Pin Flux controllers to gandalf"
git push -u origin flux-pin-controllers
gh pr create --fill && gh pr merge --squash --delete-branch
```

Then on gandalf:

```bash
flux reconcile kustomization flux-system --with-source
kubectl -n flux-system get pods -o wide   # all on gandalf
```

Expected: controllers reschedule onto gandalf. **This is the first proof that a merge auto-applies.**

______________________________________________________________________

## Task 2: Pilot — `uptime-kuma` HelmRelease takeover (Phase 1)

**Files:**

- Create: `sources/uptime-kuma.yaml`, `k8s/uptime-kuma/helmrelease.yaml`, `k8s/uptime-kuma/kustomization.yaml`, `clusters/gandalf/apps.yaml`

- Remove: `k8s/uptime-kuma/values.yaml` (its content moves into the HelmRelease — single source)

- [ ] **Step 1: Capture the chart repo + version** (gandalf)

```bash
grep -i 'helm repo add' k8s/uptime-kuma/README.md   # the chart repo URL used at install
helm list -n monitoring | grep uptime-kuma          # chart = uptime-kuma-4.1.0
```

Record the repo URL (call it `<REPO_URL>`) and confirm chart version `4.1.0`.

- [ ] **Step 2: Create the `HelmRepository`** at `sources/uptime-kuma.yaml`

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: uptime-kuma
  namespace: flux-system
spec:
  interval: 1h
  url: <REPO_URL>   # from Step 1
```

- [ ] **Step 3: Create the `HelmRelease`** at `k8s/uptime-kuma/helmrelease.yaml` — `releaseName`, `targetNamespace`, chart, and version MUST match the current manual release so helm-controller adopts it (no reinstall)

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: uptime-kuma
  namespace: monitoring
spec:
  releaseName: uptime-kuma          # MUST equal the existing release name
  targetNamespace: monitoring       # MUST equal the existing namespace
  interval: 30m
  chart:
    spec:
      chart: uptime-kuma
      version: "4.1.0"              # pin (matches current); bump deliberately later
      sourceRef:
        kind: HelmRepository
        name: uptime-kuma
        namespace: flux-system
  # Values copied verbatim from the (now-deleted) k8s/uptime-kuma/values.yaml.
  values:
    # <<< paste the FULL current contents of k8s/uptime-kuma/values.yaml here >>>
```

- [ ] **Step 4: Create the kustomize entry** at `k8s/uptime-kuma/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - pv-pvc.yaml
  - helmrelease.yaml
```

- [ ] **Step 5: Create the Flux `Kustomization`** at `clusters/gandalf/apps.yaml`

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
  namespace: flux-system
spec:
  interval: 10m
  path: ./k8s/uptime-kuma
  prune: false           # OFF for the pilot; enable later once trusted
  sourceRef:
    kind: GitRepository
    name: flux-system
  wait: true
  timeout: 5m
```

- [ ] **Step 6: Remove the now-duplicated values file**

```bash
git rm k8s/uptime-kuma/values.yaml
```

- [ ] **Step 7: Open a PR and merge** (the change reaches the cluster only via Flux)

```bash
git checkout -b flux-pilot-uptime-kuma
git add sources/uptime-kuma.yaml k8s/uptime-kuma/ clusters/gandalf/apps.yaml
git commit -m "Bring uptime-kuma under Flux (HelmRelease takeover)"
git push -u origin flux-pilot-uptime-kuma
gh pr create --fill && gh pr merge --squash --delete-branch
```

- [ ] **Step 8: Verify the takeover (no reinstall, no data loss)**

```bash
flux reconcile kustomization apps --with-source
flux get helmreleases -n monitoring
kubectl -n monitoring get pods -l app.kubernetes.io/name=uptime-kuma
kubectl -n monitoring get pvc uptime-kuma-data
```

Expected: HelmRelease `uptime-kuma` `Ready=True`; the **same** pod keeps running (not recreated); PVC `uptime-kuma-data` `Bound` and unchanged. Browse Uptime Kuma — monitors/history intact.

- [ ] **Step 9: Acceptance — prove GitOps end-to-end** — make a trivial values change via PR (e.g. bump the memory request), merge, and confirm Flux applies it with no manual `helm upgrade`

```bash
flux reconcile kustomization apps --with-source
kubectl -n monitoring get deploy uptime-kuma -o jsonpath='{.spec.template.spec.containers[0].resources}'; echo
```

Expected: the new value is live without any human `helm` command.

______________________________________________________________________

## Task 3: Wire Flux into Grafana + Alertmanager (Phase 1)

**Files:**

- Create: `k8s/flux-monitoring/podmonitor.yaml`, `k8s/flux-monitoring/prometheusrule.yaml`, `k8s/flux-monitoring/dashboards/*.yaml`, `k8s/flux-monitoring/kustomization.yaml`, `clusters/gandalf/monitoring.yaml`

- [ ] **Step 1: PodMonitor for the Flux controllers** at `k8s/flux-monitoring/podmonitor.yaml`

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: flux-system
  namespace: flux-system
  labels:
    app.kubernetes.io/part-of: flux
spec:
  namespaceSelector:
    matchNames: [flux-system]
  selector:
    matchExpressions:
      - key: app.kubernetes.io/part-of
        operator: In
        values: [flux]
      - key: app.kubernetes.io/component
        operator: In
        values: [source-controller, kustomize-controller, helm-controller, notification-controller]
  podMetricsEndpoints:
    - port: http-prom
```

- [ ] **Step 2: Fetch the official Flux dashboards as ConfigMaps** (gandalf)

```bash
mkdir -p k8s/flux-monitoring/dashboards
for d in cluster control-plane; do
  curl -fsSL -o /tmp/flux-$d.json \
    https://raw.githubusercontent.com/fluxcd/flux2-monitoring-example/main/monitoring/configs/dashboards/$d.json
  kubectl create configmap flux-grafana-$d \
    --from-file=$d.json=/tmp/flux-$d.json \
    -n monitoring --dry-run=client -o yaml \
    > k8s/flux-monitoring/dashboards/$d.yaml
done
```

Then add the Grafana sidecar label to each generated ConfigMap (so it auto-imports): add under `metadata.labels`:

```yaml
    grafana_dashboard: "1"
```

Expected: two ConfigMap manifests, each labelled `grafana_dashboard: "1"`.

- [ ] **Step 3: PrometheusRule alerting on failed reconciliations** at `k8s/flux-monitoring/prometheusrule.yaml`

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: flux-reconciliation
  namespace: monitoring
spec:
  groups:
    - name: flux
      rules:
        - alert: FluxReconciliationFailure
          expr: |
            max by (namespace, name, kind) (
              gotk_reconcile_condition{type="Ready",status="False"}
            ) == 1
          for: 15m
          labels:
            severity: warning
          annotations:
            summary: "Flux resource {{ $labels.kind }}/{{ $labels.name }} not ready"
            description: "{{ $labels.kind }}/{{ $labels.name }} in {{ $labels.namespace }} has been failing to reconcile for >15m."
```

- [ ] **Step 4: kustomize entry** at `k8s/flux-monitoring/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - podmonitor.yaml
  - prometheusrule.yaml
  - dashboards/cluster.yaml
  - dashboards/control-plane.yaml
```

- [ ] **Step 5: Flux `Kustomization`** at `clusters/gandalf/monitoring.yaml`

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: flux-monitoring
  namespace: flux-system
spec:
  interval: 1h
  path: ./k8s/flux-monitoring
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
```

- [ ] **Step 6: PR, merge, verify**

```bash
git checkout -b flux-monitoring
git add k8s/flux-monitoring/ clusters/gandalf/monitoring.yaml
git commit -m "Wire Flux metrics into Grafana and Alertmanager"
git push -u origin flux-monitoring && gh pr create --fill && gh pr merge --squash --delete-branch
flux reconcile kustomization flux-monitoring --with-source
```

Verify:

- Prometheus targets include the flux-system PodMonitor: `kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090` is unreliable for multi-port — instead check via the Prometheus pod: target `flux-system` is `up`.

- Grafana shows the two Flux dashboards.

- [ ] **Step 7: Acceptance — prove an alert fires** — temporarily point the uptime-kuma HelmRelease at a non-existent chart version via PR, merge, wait, confirm an Alertmanager email arrives, then revert via PR.

Expected: `FluxReconciliationFailure` fires → email through the existing route; reverting clears it.

______________________________________________________________________

## Task 4: BWS capacity gate + project/token setup (Phase 2a)

**Files:** none in-repo (external setup + one bootstrap Secret created imperatively).

- [ ] **Step 1: [console] Capacity gate** — verify Bitwarden plan covers the design: Secrets Manager enabled, free tier limits of 3 projects / 3 machine accounts / 2 users are sufficient (design needs 1 project + 1 machine account; secrets are unlimited on free). If a paid tier is required, STOP and confirm with the user before proceeding.

- [ ] **Step 2: [console] Create BWS objects** in the Bitwarden web app → Secrets Manager:

  - Project: `homelab`
  - Machine account: `flux-eso`, granted **read** access to the `homelab` project
  - Generate an **access token** for `flux-eso`
  - Record the **organization ID** and **project ID** (visible in the BWS URLs / project settings)

- [ ] **Step 3: [console] Back up the token in the vault** — store the access token as a Bitwarden vault item `Homelab BWS Token` (so it follows the existing "Homelab \<Service>" convention and survives cluster loss).

- [ ] **Step 4: Create the one bootstrap Secret** (gandalf; uses the existing Bitwarden CLI flow — `bw sync` after unlock, export `BW_SESSION`)

```bash
export BW_SESSION="$(bw unlock --raw)"; bw sync
kubectl create namespace external-secrets --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic bws-access-token \
  --namespace external-secrets \
  --from-literal=token="$(bw get item 'Homelab BWS Token' | jq -r '.notes')" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Expected: Secret `external-secrets/bws-access-token` exists with key `token`. (Created via `--dry-run | apply` so no plaintext lands in a last-applied annotation.)

______________________________________________________________________

## Task 5: Internal CA ClusterIssuer for in-cluster certs (Phase 2)

**Files:**

- Create: `k8s/cert-manager/internal-issuer.yaml`

- [ ] **Step 1: Self-signed CA + internal issuer** at `k8s/cert-manager/internal-issuer.yaml` (standard cert-manager bootstrap-CA pattern)

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: selfsigned
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: internal-ca
  namespace: cert-manager
spec:
  isCA: true
  commonName: vigihome-internal-ca
  secretName: internal-ca
  privateKey:
    algorithm: ECDSA
    size: 256
  issuerRef:
    name: selfsigned
    kind: ClusterIssuer
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: internal-ca
spec:
  ca:
    secretName: internal-ca
```

- [ ] **Step 2: Leave it to Flux.** Do not `kubectl apply` this by hand — it is listed in `k8s/external-secrets/kustomization.yaml` (Task 6) and reconciled by the `infrastructure` Kustomization, so Flux owns it from the start (avoids field-manager dual-ownership). It is verified in Task 6 Step 7. cert-manager resolves the self-signed → CA → issuer chain via its own controllers regardless of apply order.

______________________________________________________________________

## Task 6: Deploy ESO + bitwarden-sdk-server (Phase 2)

**Files:**

- Create: `sources/external-secrets.yaml`, `k8s/external-secrets/sdk-server-cert.yaml`, `k8s/external-secrets/helmrelease.yaml`, `k8s/external-secrets/kustomization.yaml`, `clusters/gandalf/infrastructure.yaml`

- [ ] **Step 1: Capture the current ESO chart version** (gandalf)

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm repo update external-secrets
helm search repo external-secrets/external-secrets --versions | head -3
```

Record the latest stable version (call it `<ESO_VERSION>`).

- [ ] **Step 2: `HelmRepository`** at `sources/external-secrets.yaml`

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: external-secrets
  namespace: flux-system
spec:
  interval: 1h
  url: https://charts.external-secrets.io
```

- [ ] **Step 3: Cert for the sdk-server** at `k8s/external-secrets/sdk-server-cert.yaml` (issued by the internal CA from Task 5)

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: bitwarden-sdk-server-tls
  namespace: external-secrets
spec:
  secretName: bitwarden-sdk-server-tls
  dnsNames:
    - bitwarden-sdk-server.external-secrets.svc.cluster.local
    - bitwarden-sdk-server.external-secrets.svc
  issuerRef:
    name: internal-ca
    kind: ClusterIssuer
```

- [ ] **Step 4: ESO HelmRelease with the sdk-server enabled** at `k8s/external-secrets/helmrelease.yaml`

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: external-secrets
  namespace: external-secrets
spec:
  releaseName: external-secrets
  targetNamespace: external-secrets
  interval: 30m
  chart:
    spec:
      chart: external-secrets
      version: "<ESO_VERSION>"   # from Step 1
      sourceRef:
        kind: HelmRepository
        name: external-secrets
        namespace: flux-system
  values:
    nodeSelector:
      kubernetes.io/hostname: gandalf
    bitwarden-sdk-server:
      enabled: true
      nodeSelector:
        kubernetes.io/hostname: gandalf
      # Serve TLS using the cert-manager-issued cert from Step 3.
      # (Key names follow the chart's bitwarden-sdk-server values; verify
      # against `helm show values external-secrets/external-secrets` at the
      # pinned version and adjust if the chart expects a different layout.)
      tls:
        enabled: true
        existingSecret: bitwarden-sdk-server-tls
```

- [ ] **Step 5: kustomize entry** at `k8s/external-secrets/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: external-secrets
resources:
  - ../cert-manager/internal-issuer.yaml
  - sdk-server-cert.yaml
  - helmrelease.yaml
  - clustersecretstore.yaml   # created in Task 7
```

- [ ] **Step 6: Flux `Kustomization` for infra** at `clusters/gandalf/infrastructure.yaml` — `dependsOn` ensures the issuer/cert exist before the store reconciles

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: infrastructure
  namespace: flux-system
spec:
  interval: 30m
  path: ./k8s/external-secrets
  prune: true
  wait: true
  sourceRef:
    kind: GitRepository
    name: flux-system
```

- [ ] **Step 7: PR, merge, verify ESO + sdk-server** (defer adding `clustersecretstore.yaml` to the kustomization until Task 7 — comment that line out for this merge, or land Tasks 6+7 together)

```bash
flux reconcile kustomization infrastructure --with-source
kubectl get clusterissuer selfsigned internal-ca
kubectl -n external-secrets get pods
kubectl -n external-secrets get certificate bitwarden-sdk-server-tls
```

Expected: both `ClusterIssuer`s `Ready=True` (Task 5's chain); external-secrets controller + webhook + cert-controller pods `Running`; `bitwarden-sdk-server` pod `Running`; the cert `Ready=True`.

______________________________________________________________________

## Task 7: ClusterSecretStore (Phase 2)

**Files:**

- Create: `k8s/external-secrets/clustersecretstore.yaml`

- [ ] **Step 1: Define the store** at `k8s/external-secrets/clustersecretstore.yaml` (fill the org/project IDs from Task 4)

```yaml
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: bitwarden
spec:
  provider:
    bitwardensecretsmanager:
      apiURL: https://api.bitwarden.com
      identityURL: https://identity.bitwarden.com
      bitwardenServerSDKURL: https://bitwarden-sdk-server.external-secrets.svc.cluster.local:9998
      organizationID: "<ORG_ID>"      # from Task 4
      projectID: "<PROJECT_ID>"       # from Task 4
      auth:
        secretRef:
          credentials:
            name: bws-access-token
            namespace: external-secrets
            key: token
      caProvider:
        type: Secret
        name: internal-ca
        namespace: cert-manager
        key: ca.crt
```

- [ ] **Step 2: Ensure it's listed** in `k8s/external-secrets/kustomization.yaml` (uncomment the `clustersecretstore.yaml` line from Task 6 Step 5).

- [ ] **Step 3: PR, merge, verify**

```bash
flux reconcile kustomization infrastructure --with-source
kubectl get clustersecretstore bitwarden -o jsonpath='{.status.conditions[?(@.type=="Ready")]}'; echo
```

Expected: `ClusterSecretStore` `Ready=True` (it can reach the sdk-server, trusts its cert, and authenticates to BWS).

______________________________________________________________________

## Task 8: Pilot one secret — `homepage-secrets` via ExternalSecret (Phase 2)

**Files:**

- Create: `k8s/homepage/externalsecret.yaml`

- Modify: `k8s/homepage/kustomization.yaml` (created here if homepage isn't Flux-managed yet — see note), `k8s/homepage/README.md`

- [ ] **Step 1: [console] Put the three homepage values in BWS** — in the `homelab` project, create secrets for the keys homepage expects: `octoprint-api-key`, `grafana-user`, `grafana-password` (values copied from the existing `homepage-secrets` Secret / the matching Bitwarden vault items). Record each secret's **ID**.

```bash
# Reference: read the current values to copy (gandalf). Do NOT print to shared logs.
kubectl -n homepage get secret homepage-secrets -o jsonpath='{.data}'   # base64; decode locally
```

- [ ] **Step 2: ExternalSecret** at `k8s/homepage/externalsecret.yaml` (fill BWS secret IDs from Step 1)

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: homepage-secrets
  namespace: homepage
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: bitwarden
    kind: ClusterSecretStore
  target:
    name: homepage-secrets        # same name the app already consumes
    creationPolicy: Owner
  data:
    - secretKey: octoprint-api-key
      remoteRef:
        key: "<BWS_SECRET_ID_octoprint>"
    - secretKey: grafana-user
      remoteRef:
        key: "<BWS_SECRET_ID_grafana_user>"
    - secretKey: grafana-password
      remoteRef:
        key: "<BWS_SECRET_ID_grafana_password>"
```

- [ ] **Step 3: Wire it in via a per-app Flux Kustomization.** Create `k8s/homepage/kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - externalsecret.yaml
```

and `clusters/gandalf/homepage.yaml`:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: homepage
  namespace: flux-system
spec:
  interval: 30m
  path: ./k8s/homepage
  prune: false
  dependsOn:
    - name: infrastructure
  sourceRef:
    kind: GitRepository
    name: flux-system
```

(`dependsOn: infrastructure` ensures the `ClusterSecretStore` exists before this ExternalSecret reconciles.)

- [ ] **Step 4: PR, merge, verify the sync (takeover — identical values, no app blip)**

```bash
flux reconcile kustomization homepage --with-source
kubectl -n homepage get externalsecret homepage-secrets
kubectl -n homepage get secret homepage-secrets -o jsonpath='{.data.grafana-user}' | base64 -d; echo
```

Expected: ExternalSecret `SecretSynced=True`; the `homepage-secrets` Secret now has `managed-by: external-secrets` ownership and the **same** values; the homepage pod is undisturbed and widgets still load.

- [ ] **Step 5: Retire the manual path** — remove the `kubectl create secret ... homepage-secrets` instructions from `k8s/homepage/README.md` and note it's now sourced from BWS via ESO. Commit via PR.

______________________________________________________________________

## Task 9: Document the pattern + DR model (Phase 2 close-out)

**Files:**

- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `CLAUDE.md`** with:

  - **Repeatable chart-service migration (Phase 3+):** add `sources/<chart>.yaml` (HelmRepository), `k8s/<svc>/helmrelease.yaml` (match `releaseName`/namespace/version for takeover) + `kustomization.yaml`, add the path to `clusters/gandalf/apps.yaml`; raw services become plain Kustomizations. `traefik` stays Ansible-owned.
  - **Repeatable secret migration:** put values in the BWS `homelab` project, add an `ExternalSecret` producing the same Secret name/keys, drop the manual `kubectl create secret` from the service README.
  - **DR model:** rebuild = install k3s → `flux bootstrap git ...` → Flux reconciles the repo → create the one `bws-access-token` Secret from the vault → ESO repopulates the rest.
  - **"Things deliberately not done":** remove the "No GitOps controller" entry (now reversed); keep `traefik` as Ansible-owned noted.
  - **Enabling prune:** once the pilot is trusted, flip `prune: false` → `true` on `clusters/gandalf/apps.yaml`.

- [ ] **Step 2: PR and merge**

```bash
git checkout -b flux-docs
git add CLAUDE.md
git commit -m "Document Flux/ESO migration pattern and GitOps DR model"
git push -u origin flux-docs && gh pr create --fill && gh pr merge --squash --delete-branch
```

______________________________________________________________________

## Done-when

- Flux is bootstrapped and self-managing; a merge to `main` reconciles with no manual apply.
- `uptime-kuma` is a Flux `HelmRelease` (adopted, no data loss); a values change via PR auto-applies.
- Flux metrics show in Grafana and a failed reconcile emails through Alertmanager.
- ESO + `bitwarden-sdk-server` are running; the `ClusterSecretStore` is `Ready`.
- `homepage-secrets` is sourced from BWS via an `ExternalSecret`; the manual path is retired.
- `CLAUDE.md` documents the repeatable pattern and the new DR model.
