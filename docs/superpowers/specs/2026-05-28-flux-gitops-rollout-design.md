# Flux GitOps rollout — design

**Goal:** Adopt Flux so merges to `main` reconcile into the cluster automatically,
replacing the manual `kubectl apply` / `helm upgrade` step,
and bring secrets under management via External Secrets Operator (ESO) and Bitwarden Secrets Manager (BWS).

**Status:** Approved design, pre-implementation.

**Date:** 2026-05-28.

**Related issues:** #135 (GitOps), #136 (secrets-as-code), #143 (OpenTofu under GitOps — deferred), #137 (forward-auth — separate).

## Context and current state

The cluster is single-control-plane k3s on `gandalf` (amd64) with `frodo` and `samwise` as agents.
The repo is the source of truth, but applying is a deliberate human step today:
`kubectl apply -f` for raw manifests, `helm install/upgrade -f values.yaml` for charted services.
There is no GitOps controller (this is a documented "deliberately not done" that we are now reversing).

Services live under `k8s/<service>/`, roughly 16 of them, mostly Helm-chart-managed (9 `values.yaml`) with a couple raw (`deployment.yaml`).
The repo is public, with zero plaintext secrets:
secrets are created out-of-band from the Bitwarden vault via `kubectl create secret` at apply time, and betterleaks scans every commit.
Observability already exists from #76 (kube-prometheus-stack: Prometheus, Grafana, Alertmanager with an email route).

This effort is the gap-closer for repeated "merged but not applied" friction,
and it folds in secrets-as-code (#136) because the user chose to solve secrets now rather than defer.

## Decisions (locked during brainstorming)

- **Tool:** Flux (chosen over Argo CD for native HelmRelease semantics, native fit with a Helm-heavy repo, and a lean, CLI-native footprint).
- **Adoption:** incremental pilot — bootstrap Flux, migrate one low-risk service, expand over later PRs. No big-bang.
- **Secrets:** ESO and Bitwarden Secrets Manager, so there is zero ciphertext in any git repo (chosen over SOPS+age, which would publish ciphertext or need a private repo).
- **Monitoring/interaction:** `flux` CLI and k9s, plus Flux's Prometheus metrics into the existing Grafana and Alertmanager (reuse #76). No web UI (Headlamp/Capacitor) for now.
- **Bootstrap:** `flux bootstrap github` against the existing public `homelab` repo, authenticated with a write-scoped deploy key for that one repo, so Flux is self-managing (its components and version upgrades are reconciled from git).
- **Repo:** single public repo. Keep the `k8s/<service>/` layout as is; add a Flux entrypoint rather than restructuring.

## Architecture and phasing

Two new subsystems are being added (Flux, and ESO+BWS), so each is provable in isolation before the next.

- **Phase 0 — Bootstrap Flux.** Install Flux on gandalf, self-managing, pointed at the public repo. No app behavior changes.
- **Phase 1 — Pilot one app, no secrets path yet.** Migrate a single low-risk chart-managed service to a `HelmRelease`; its Secret (if any) stays manual. Proves the reconcile loop and the monitoring wiring.
- **Phase 2 — Secrets foundation.** Stand up ESO and the `bitwarden-sdk-server`, create a BWS project and machine account, bootstrap the single BWS token Secret, then convert one low-risk secret to an `ExternalSecret`. Proves secrets-as-code end-to-end.
- **Phase 3+ — Migrate the rest** over later PRs, each service becoming a `HelmRelease`/`Kustomization` with `ExternalSecret`s. Controllers migrate last.

## Repo structure

No restructure of `k8s/`. Add a Flux entrypoint and per-chart sources:

```
clusters/gandalf/
  flux-system/          # created by `flux bootstrap` (Flux managing itself)
  apps.yaml             # Flux Kustomization(s) → point at k8s/<service> paths as they migrate
sources/
  <chart>.yaml          # HelmRepository CRs (chart repos), version-pinned
k8s/<service>/
  helmrelease.yaml      # NEW per migrated chart service
  kustomization.yaml    # NEW — kustomize lists the resources Flux should apply
  (existing files stay)
```

## Phase 0 — bootstrap

`flux bootstrap github --owner nickvigilante --repository homelab --path clusters/gandalf --branch main`,
authenticated with a **write-scoped deploy key for this repo only** (least privilege).
This creates `clusters/gandalf/flux-system/` (Flux components and the self-sync Kustomization),
pins the Flux version, and (via a kustomize patch) pins the controllers to gandalf for consistency with the rest of the stack.

**Acceptance:** `flux check` is green, `flux-system` reconciles, a trivial change to a flux-system file is picked up on the next sync.

## Phase 1 — pilot takeover (`uptime-kuma`)

`uptime-kuma` is the pilot: chart-managed (so it rehearses the dominant pattern), a low-risk leaf (nothing depends on it),
and it has no Secret, so Phase 1 stays clean. (`homepage` is the stateless alternative but has a secret and is the apex page.)

**Migration is a takeover, not a reinstall.** helm-controller keys a release by `releaseName` and namespace.
If the `HelmRelease` CR uses the same release name, namespace, chart, and version as the current manual `helm install`,
helm-controller adopts the existing release (it reads the same `sh.helm.release.v1.uptime-kuma` secret) — no reinstall, the PVC and data are untouched.
This is the recipe for every later chart service.

Objects:

- `HelmRepository` for the uptime-kuma chart (version-pinned, preserving the `helm upgrade --version` discipline).
- `HelmRelease` with `releaseName` and `targetNamespace` matching the current manual release, chart version pinned, values inlined from the existing `values.yaml`.
- A Flux `Kustomization` under `clusters/gandalf/` pointing at `k8s/uptime-kuma` (and the sources), with `prune: false` initially.

**Acceptance:** `flux get helmreleases -A` shows it `Ready` (adopted, no data loss);
a trivial values change via PR merges and is applied by Flux with no manual `helm upgrade`.

## Monitoring wiring (done once, in Phase 1)

- A `PodMonitor` for the flux-system controllers so kube-prometheus-stack scrapes Flux's metrics.
- The official Flux Grafana dashboards as ConfigMaps labelled `grafana_dashboard: "1"` (the Grafana sidecar auto-imports them).
- A `PrometheusRule` alerting on `gotk_reconcile_condition{type="Ready",status="False"}`, routed through the existing Alertmanager email path.

**Acceptance:** Flux panels render in Grafana; a deliberately broken reconcile produces an Alertmanager email.

## Phase 2 — ESO and Bitwarden Secrets Manager

**Capacity gate (Phase 2a, runs first):** confirm the Bitwarden plan covers the design.
The BWS free plan provides unlimited secrets and up to 3 projects, 3 machine accounts, and 2 users.
The design needs 1 project and 1 machine account, so free is sufficient — but verify at build time before building anything, since plans change.

**Components (all deployed by Flux, so they are GitOps-managed too):**

- External Secrets Operator (HelmRelease).
- `bitwarden-sdk-server` — the REST wrapper ESO's Bitwarden provider talks to.
  It must serve HTTPS, so cert-manager issues its cert.
  Only the public Let's Encrypt issuers exist today, so add a self-signed/internal `ClusterIssuer` for in-cluster certs and feed that CA to ESO so it trusts the sdk-server.
- One bootstrap Secret: the BWS machine-account access token, created manually from the Bitwarden vault (kept there as backup).
  This is the single manual secret; everything else flows through ESO from BWS.

**ESO objects:**

- A `ClusterSecretStore` (cluster-wide, shared by every namespace's `ExternalSecret`) of provider type `bitwardensecretsmanager`,
  pointing at the sdk-server URL, the token Secret, the org/project, and the internal CA.
- Per-secret `ExternalSecret` CRs that materialize k8s Secrets with the same names and keys the apps already expect
  (`grafana-secrets`, `smtp-relay`, etc.), so no app config changes.

**Secrets-migration mechanic (a takeover, like Phase 1):** for each secret,
put its values in BWS, then create an `ExternalSecret` producing a k8s Secret of the same name and keys.
ESO overwrites with identical values, so the running app never notices;
then drop the manual `kubectl create secret` step from that service's runbook.
Sequence per secret, never big-bang.

**Secrets pilot:** convert one low-risk secret first — `homepage-secrets`
(a briefly-wrong widget key only blanks a tile) — prove ESO syncs it from BWS end-to-end, then expand to the important ones.

**Acceptance:** `ClusterSecretStore` reports valid; the `homepage-secrets` `ExternalSecret` syncs from BWS; the app is unaffected.

## Phase 3+ — migration order (lowest → highest blast radius)

1. Leaf stateless apps (`homepage`, …).
2. Leaf stateful apps (`jellyfin`, `syncthing`, `coder`, `outline`) — takeover preserves PVCs;
   `syncthing` is raw, so it becomes a plain Kustomization, not a HelmRelease.
3. SPOF/shared apps (`authentik`, `pihole`) — carefully, keeping local-fallback credentials per the SPOF discipline.
4. Controllers last (`cert-manager`, `reflector`, `kube-prometheus-stack`, `coredns`).

`traefik` stays out of Flux: it is k3s-bundled and managed via the `HelmChartConfig` in `ansible/system/`, so it remains Ansible-owned rather than contending with Flux.

## Safety and error-handling

- `prune: true` deletes the live resource when a manifest is removed — powerful but a footgun.
  Start the pilot with prune off; enable once trusted, and rely on PR review.
- `flux suspend`/`resume` pauses reconciliation while hand-debugging an incident, so Flux does not revert manual fixes.
- helm-controller keeps release history, so rollback is available via Flux/Helm.
- ESO or BWS unreachable: existing Secrets persist (ESO-cached) and running pods are unaffected; only new or changed secrets stall. Acceptable degradation.
- The deploy key is write-scoped to this one repo; document its rotation.

## Disaster-recovery model (updates `CLAUDE.md`)

Rebuild becomes: install k3s, run `flux bootstrap`, let Flux reconcile the whole repo,
create the one BWS-token Secret, and ESO repopulates the rest —
simpler than today's "re-apply manifests and re-`kubectl create secret` ×11."

## Out of scope / deferred

- A Flux web UI (Headlamp/Capacitor) — CLI, k9s, and Grafana cover monitoring for now.
- SOPS+age — not chosen; ESO+BWS keeps ciphertext out of git entirely.
- OpenTofu under GitOps via tofu-controller — deferred to #143.
- `traefik` under Flux — stays Ansible-owned.
- forward-auth (#137) — independent effort.

## Prerequisites and open items

- A Bitwarden organization with Secrets Manager enabled (the Phase 2a gate verifies tier/capacity).
- Exact chart names and currently-installed versions are pinned at build time from `helm list` per service.
