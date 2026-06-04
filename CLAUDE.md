# CLAUDE.md

Guidance for agents and future-you working in this repo.

## What this repo is

The k3s manifest + host-config repo for the home lab running on
`gandalf` (192.168.50.135, Ubuntu Server 26.04, amd64 control plane +
worker). `frodo` (Pi 5) and `samwise` (Pi 4B) join as agent nodes;
heavy pods pin to gandalf via nodeSelector and the Pis stay light.
Traefik is the default ingress.

Apply pattern is **mixed** as of #135:

- **GitOps via Flux** for everything migrated into `clusters/gandalf/`.
  The repo is the source of truth; Flux pulls and applies on a
  schedule. Adopted incrementally (HelmRelease takeover for chart-managed
  apps, ExternalSecret takeover for Secrets), so not every workload is
  Flux-owned yet -- inventory in `flux get kustomizations`. See
  "GitOps with Flux" below.
- **Manual `kubectl apply` / `helm upgrade`** for everything Flux hasn't
  adopted yet. Migration is per-service and incremental; see #161 for
  the remaining-Secret-migrations queue.

Sibling repo for IaC (Tailscale ACLs, GitHub branch protection, OAuth
secrets, etc.) lives at `~/git/nickvigilante/infrastructure/`, public
on GitHub, OpenTofu-managed. Reach for it when state is "outside the
cluster."

## Service directory convention

Each service under `k8s/<service>/` follows the same layout:

| File                    | Purpose                                                                                                                                                                                                                                                                                                                                                                   |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `namespace.yaml`        | The k8s Namespace                                                                                                                                                                                                                                                                                                                                                         |
| `pv-pvc.yaml`           | Pre-created PV (hostPath, gandalf-pinned) + PVC for any persistent data the chart can't manage with its own dynamic provisioning                                                                                                                                                                                                                                          |
| `values.yaml`           | Helm values (when chart-managed) — Ingress lives inside the chart's `ingress` key, not as a separate file                                                                                                                                                                                                                                                                 |
| `deployment.yaml`       | Raw `Deployment` + `Service` (when raw-managed)                                                                                                                                                                                                                                                                                                                           |
| `secret.example.yaml`   | **Template only** — documents which keys must live in the real Secret. Never applied; the real Secret comes from ESO (`external-secret.yaml`) for migrated services, otherwise `kubectl create secret generic` sourced from a Bitwarden item                                                                                                                              |
| `ingress-vigihome.yaml` | Raw `Ingress` at `https://<service>.vigihome.net`, **only for raw-managed services** (chart-managed services keep the Ingress in `values.yaml`). See `k8s/syncthing/` for the canonical example.                                                                                                                                                                          |
| `helmrelease.yaml`      | Flux `HelmRelease` for chart-managed services that have been adopted under Flux. Companion to (or replacement for) `values.yaml`; values are usually inlined for single-source-of-truth. See `k8s/uptime-kuma/` for the canonical takeover example (#147).                                                                                                                |
| `external-secret.yaml`  | `ExternalSecret` that produces the in-cluster Secret from BWS via the `bitwarden` ClusterSecretStore. Used in place of any `kubectl create secret generic` recipe once a service is migrated. See `k8s/homepage/` for the canonical example (#159).                                                                                                                       |
| `kustomization.yaml`    | Kustomize meta-file listing the local resources plus cross-dir refs (e.g. `../../sources/<chart>.yaml`, `../cert-manager/internal-issuer.yaml`). Required when a directory is reconciled by a Flux `Kustomization`. Cross-dir refs need Flux's `LoadRestrictionsNone` (default); plain `kubectl kustomize` rejects them without `--load-restrictor LoadRestrictionsNone`. |
| `README.md`             | One-time setup runbook + day-to-day ops notes                                                                                                                                                                                                                                                                                                                             |

When adding a new service, mirror this layout. See `k8s/authentik/`
and `k8s/coder/` for chart-managed examples; `k8s/syncthing/` for the
single raw-managed exception.

### Chart vs raw: which to pick

**Chart-first when an official or widely-used chart exists.** Raw
manifests when only community-maintained charts exist *and* the app
is a single Deployment.

Reasoning: the consistency win from chart-managed services
(uniform `helm upgrade` ergonomics, uniform Ingress placement in
`values.yaml`, atomic rollback) is worth a lot in a one-operator
repo. Community charts for single-Deployment apps add abstraction
that costs more than it saves — overriding chart defaults (e.g.
NodePort vs `hostNetwork` conflicts on the Syncthing chart),
managing chart-version drift, and "fighting the chart" when its
opinions misalign. Reference precedent: Syncthing kept raw
(2026-05-17) because only community charts exist and it's one
Deployment.

## GitOps with Flux

Flux 2.x reconciles per-app `Kustomization`s out of `k8s/` from
`clusters/gandalf/`. Source of truth is git; apply is schedule-driven
(default 10–30m interval, on-demand via `flux reconcile`). Bootstrap +
takeover landed in #135 Phase 0–2.

**Layout:**

- `clusters/gandalf/flux-system/` — `flux bootstrap`-managed.
  Hand-edited only for the controller-pin patch in `kustomization.yaml`
  (every Flux controller pinned to gandalf via nodeSelector — same
  heavy-pods-on-gandalf rule as the rest of the stack, PR #146).
- `clusters/gandalf/<app>.yaml` — one per app or logical group. Names
  visible via `flux get kustomizations`: `apps`, `infrastructure`,
  `flux-monitoring`, `homepage`, etc.
- `k8s/<service>/kustomization.yaml` — lists per-service resources +
  any cross-dir refs.
- `sources/<chart>.yaml` — `HelmRepository` objects. Referenced from
  each consuming service's `kustomization.yaml` via
  `../../sources/<chart>.yaml`. The per-service relative reference
  keeps each app self-contained; a cluster-level `sources` Kustomization
  is deferred until enough services share repos to justify centralizing.

**Conventions:**

- **One Flux `Kustomization` per app or logical group.** New apps get
  their own `clusters/gandalf/<app>.yaml`.
- **`dependsOn` whenever timing matters.** Apps that consume ESO get
  `dependsOn: [{ name: infrastructure }]` so their ExternalSecret never
  reconciles before its store is Ready on a cold boot.
- **`wait: true`** by default — failures surface promptly rather than
  silently lingering as Ready=False.
- **`prune: true`** only when Flux owns 100% of the directory's
  lifecycle. With a manually-managed coexistent (e.g. the chart not yet
  taken over, a hand-applied `namespace.yaml`), keep `prune: false` to
  avoid accidental deletion. Flip to `true` after full takeover.
- **Cross-dir resource refs** (`../sources/...`, `../cert-manager/...`)
  work because Flux's kustomize-controller runs with
  `LoadRestrictionsNone`. Plain `kubectl kustomize` rejects them
  without `--load-restrictor LoadRestrictionsNone`; the CI lint job
  uses that flag.

**HelmRelease takeover pattern.** Adopting an existing helm-installed
chart under Flux: write a `HelmRelease` whose `releaseName`,
`targetNamespace`, `chart.name`, and `chart.version` exactly match the
live release, with `values:` semantically identical (comments and
key-order are ignored by the diff). helm-controller adopts as the
next revision — typically a no-op upgrade with the existing pod
preserved. See `k8s/uptime-kuma/helmrelease.yaml` (#147) for the
canonical takeover; #157 for the more involved ESO chart-with-subchart
takeover.

**Runbook — after `flux bootstrap`.** Flux's bootstrap output uses
different YAML conventions than this repo's `.yamlfmt`, so the local
pre-commit and CI yamlfmt checks fail on unmodified bootstrap output.
Always normalize before committing:

```sh
cd ~/git/nickvigilante/homelab
yamlfmt -conf .yamlfmt clusters/gandalf/flux-system/*.yaml
```

Hit on PR #146; codified into the runbook to avoid the same failure
on every subsequent bootstrap (re-bootstrap for version bumps, DR
rebuild, etc.).

**Helm pin discipline.** Always pin `--version` on `helm upgrade`,
whether the chart is helm-managed or HelmRelease-managed. Unpinned
upgrades silently drift the chart and bundled images; a values-only
change on the auth namespace once shipped a chart version bump that
broke OIDC (2026-05-23). For HelmRelease, `chart.spec.version` is
the pin; for direct `helm upgrade`, `--version <X>`.

**Validation.** kubeconform in CI covers Flux-era filenames via the
widened filter pattern (see the "Lint" callout under TLS). Flux's
kustomize-controller does its own server-side validation at reconcile
time as the safety net.

## TLS / HTTPS — the `*.vigihome.net` stack

Every internal service moves onto HTTPS at `*.vigihome.net` (cleanly
separated from the public `nickvigilante.com`). The supporting infra
runs in three namespaces:

| Namespace      | What                                                                                                                                                       | PR            |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `cert-manager` | cert-manager controller + LE staging/prod ClusterIssuers using native Cloudflare DNS-01 + the wildcard `Certificate` for `vigihome.net` + `*.vigihome.net` | #23, #24, #25 |
| `reflector`    | emberstack/reflector — mirrors annotated Secrets across namespaces                                                                                         | #26           |
| `homepage`     | First end-to-end exercise of the stack at the apex                                                                                                         | #27           |

**The flow:** cert-manager issues `vigihome-tls` (ECDSA P-256, 90d /
30d-renew) into `cert-manager/vigihome-tls`. Reflector reads the
`secretTemplate` annotations on `k8s/cert-manager/certificate.yaml`
and copies the Secret into every namespace listed in
`reflection-auto-namespaces`. Consumer Ingresses reference
`secretName: vigihome-tls` in their TLS block.

**Migrating a service onto HTTPS:**

1. Add the consumer namespace to `reflection-auto-namespaces` in
   `k8s/cert-manager/certificate.yaml` (comma-separated). `kubectl apply` it. Reflector mirrors `vigihome-tls` into the namespace in
   < 5s.
2. **Chart-managed service:** modify `values.yaml`'s `ingress` block
   to set the vigihome host with
   `traefik.ingress.kubernetes.io/router.entrypoints: websecure` and
   `tls.secretName: vigihome-tls`. Each chart's Ingress shape varies
   slightly — singular `host` (Coder) vs `hosts[]` (Authentik,
   Uptime Kuma, Jellyfin), flat `tls.{enable, secretName}` (Coder)
   vs `tls[].{hosts, secretName}` (others). Render with `helm template ... --show-only templates/ingress.yaml` before applying
   to verify the chart consumes the values as expected.
   **Raw-managed service:** add `k8s/<service>/ingress-vigihome.yaml`
   with the same Traefik annotation + TLS block. See
   `k8s/syncthing/ingress-vigihome.yaml` for the canonical example.
3. DNS: nothing per-service. Pi-hole's
   `address=/vigihome.net/192.168.50.135` and
   `address=/vigihome.net/100.92.2.25` wildcard directives in
   `dns.dnsmasq_lines` (managed via the Pi-hole admin UI, see
   `k8s/pihole/values.yaml`'s big DNS comment) resolve any new
   `*.vigihome.net` host to gandalf automatically on both LAN and
   tailnet.
4. Apply: `helm upgrade` for chart-managed, `kubectl apply -f ingress-vigihome.yaml` for raw. For chart-managed services that
   previously had both a raw `ingress-vigihome.yaml` *and* a chart
   Ingress on the legacy `*.home` host, the chart's Ingress takes
   over the vigihome host on `helm upgrade`; the orphaned raw
   resource needs `kubectl delete ingress <name>` afterward.
5. Test in a real browser. Expect a trusted cert from
   `O=Let's Encrypt, CN=E7` (ECDSA intermediate).
6. If any legacy Pi-hole local DNS record (`<service>.home`) is left
   over, retire it via the Pi-hole admin UI.

**OIDC clients gotcha:** Authentik derives token `iss` claims from
the request `Host` header — not a single configured external URL.
When migrating Authentik to a new HTTPS host, every OIDC client
must update its issuer URL in lockstep. The original Authentik
cutover landed as a 3-PR sequence (D1: add HTTPS Ingress → D2: flip
each client's `OIDC_ISSUER_URL` → D3: drop legacy HTTP Ingress; PRs
#28 / #30 / #31). Subsequent client migrations to the chart's
`values.yaml` Ingress (Coder #46, etc.) didn't trigger the same
`iss` flip because the *Authentik* host stayed put — only the
client's own external URL changed. Two related but distinct
gotchas to remember:

- **Authentik `iss` flip:** every downstream OIDC client must
  bounce when Authentik's external host changes.
- **OIDC redirect URI allowlist:** when a *client*'s external URL
  changes (e.g. Coder → `coder.vigihome.net`), Authentik's provider
  redirect URI list must include the new callback URL **byte-for-
  byte**. Strict mode rejects `http` vs `https` and trailing-slash
  mismatches.

**Lint:** the kubeconform CI filter at `.github/workflows/lint.yml`
covers raw + Flux-era filenames:
`namespace.yaml`, `pv-pvc.yaml`, `secret.example.yaml`,
`deployment.yaml`, `*-job.yaml`, `*-cronjob.yaml`,
`clusterissuer-*.yaml`, `certificate.yaml`, `ingress-*.yaml`,
`middleware-*.yaml`, `netpol-*.yaml`, `redis.yaml`,
`helmrelease.yaml`, `external-secret.yaml`, `clustersecretstore.yaml`,
`prometheusrule.yaml`, `podmonitor.yaml`, `*-cert.yaml`,
`*-issuer.yaml`. CRD schemas come from the datreeio CRDs-catalog
fallback so Flux/cert-manager/ESO/Prometheus-operator types validate
out of the box. When a new service uses a filename outside that set,
add it to the filter rather than letting it silently bypass
validation. Flux's kustomize-controller still catches schema issues
server-side at reconcile time as a backstop.

## Secrets discipline

- **Secrets never enter the repo.** The repo is public; betterleaks
  (gitleaks successor) runs as a pre-commit hook
  (`.pre-commit-config.yaml`) and again in CI.

- **Bitwarden is the source of truth for every secret**, in two
  surfaces:

  - **Password Manager vault items** named `Homelab <Service>` (e.g.
    `Homelab Restic Repository`, `Homelab Authentik`). Hold the
    human-readable values + per-service custom fields, and act as the
    DR mirror.
  - **Bitwarden Secrets Manager (BWS)** project `homelab` holds the
    cluster-readable copies that ESO syncs into in-cluster Secrets.
    Free tier; project + machine accounts created per #135 Task 4.

- **Two BWS machine accounts** with different scopes:

  - `flux-eso` (Read on `homelab`) — credential ESO uses at runtime.
    Token in BW vault `Homelab BWS Token`. Standing access is
    read-only; ESO never has standing write access.
  - `homelab-bootstrap` (Read/Write on `homelab`) — used **only** by
    `scripts/bws-migrate.sh` to push new values from vault → BWS.
    Token in BW vault `Homelab BWS Bootstrap Token`. Never assigned
    to runtime workloads.

- **Migrated services read Secrets via ESO**, not `kubectl create secret`. Each migrated service has a
  `k8s/<service>/external-secret.yaml` declaring its keys + BWS
  secret-IDs. The BWS UUIDs in the manifest carry `# gitleaks:allow`
  inline — they're identifiers, not credentials. ESO refreshes the
  Secret on `refreshInterval` (default 1h) or on demand via
  `flux reconcile externalsecret -n <ns> <name>`. New BWS values get
  pushed via:

  ```sh
  ./scripts/bws-migrate.sh <<'EOF'
  <bws-name>|<bw-vault-item>|<bw-field>
  ...
  EOF
  ```

- **Bootstrap Secrets** that ESO itself needs
  (`external-secrets/bws-access-token`) and any chart-install creds
  that block first reconcile (rare) get created directly. The
  pattern, with the **mandatory** annotate-strip step:

  ```sh
  kubectl create secret generic <name> --namespace <ns> \
    --from-literal=key="$(bw get item '<Item>' | jq -r '.fields[]|select(.name=="<f>")|.value')" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n <ns> annotate secret <name> kubectl.kubernetes.io/last-applied-configuration-
  ```

  Note the trailing `-` on the annotate (removes the annotation). The
  `--dry-run | apply` pattern is idempotent, but without the strip
  step it leaks base64 `.data` through the
  `last-applied-configuration` annotation — which `kubectl describe`
  surfaces while redacting the actual `.data` field, making
  describe-style output look safe-to-share when it isn't. A cluster-wide
  sweep on 2026-05-30 (#135 Task 4) found six leaked Secrets and
  stripped them; subsequent recipes always include the strip.

- `secret.example.yaml` files document each Secret's keys + roles
  with `REPLACE_WITH_*` placeholders. Never applied — documentation
  only.

- **Migration queue:** #161 tracks the remaining Secrets still on the
  manual recipe (`auth/smtp-relay`, `auth/authentik-oidc-secrets`,
  `monitoring/grafana-secrets`, `backup/restic-credentials`,
  `outline/outline-secrets`). Each migration is a small PR following
  the `k8s/homepage/` pattern.

- Storj S3 access keys live ONLY in `/etc/rclone/rclone.conf`
  (root:root 0600) and `~/.homelab-opentofu.env`. The cluster-side
  restic credential lives there too until #161 migrates it to BWS.

## DNS pattern (recurring gotcha)

Pi-hole serves custom records to the LAN and tailnet via two
mechanisms in `/opt/pihole/etc-pihole/pihole.toml` (managed by the
Pi-hole admin UI, persisted on the PV, captured by the nightly
restic backup — but **not** in this repo):

- **`misc.dnsmasq_lines` wildcard** — the canonical source of
  truth for `*.vigihome.net`. Two lines (order matters for
  sequential resolvers; LAN first so on-LAN clients don't pay
  tailnet overhead):

  ```
  address=/vigihome.net/192.168.50.135
  address=/vigihome.net/100.92.2.25
  ```

  Any new `*.vigihome.net` host resolves automatically — no
  per-service record needed. See `k8s/pihole/values.yaml`'s big
  DNS comment for the full rationale.

- **`dns.hosts` per-record list** — for one-off A records that
  don't fit a pattern (e.g. `pi.hole` itself, hardware on the LAN
  without its own DNS like a printer or NAS). Avoid for
  `*.vigihome.net` hosts — the wildcard above covers those.

The legacy `*.home` namespace (`jellyfin.home`, `uptime.home`,
`coder.home`, etc.) has been **fully retired**. Any leftover
`dns.hosts` entries for `*.home` are dead and can be cleaned up.

**CoreDNS inside the cluster does not see Pi-hole's records** — it
uses upstream DNS but is configured separately. So inside a pod,
`*.vigihome.net` hostnames may NXDOMAIN or resolve to gandalf's
external IPs (defeating the purpose). Always use cluster-internal
service DNS for pod-to-pod traffic:
`<svc>.<namespace>.svc.cluster.local`. Examples:

- `jellyfin.media.svc.cluster.local:8096`
- `pihole-web.networking.svc.cluster.local`
- `uptime-kuma.monitoring.svc.cluster.local:3001`

## Backup wiring

The nightly restic CronJob at `k8s/backup/backup-cronjob.yaml` mounts
persistent dirs from gandalf via hostPath and pushes encrypted
snapshots to Storj. **Any new persistent dir under `/opt/<service>/`
needs adding to that CronJob** — pattern: new `volume`, `volumeMount`, and
`restic backup --tag <service>` block. Repo password lives in
Bitwarden item `Homelab Restic Repository` — losing it loses every
snapshot.

Heartbeats: the CronJob pings Uptime Kuma push monitors on success
and on failure (via `trap ERR`). Push URLs live in Secret
`backup/uptime-kuma-push-urls`.

### What we don't back up (and why it's fine)

- **The k3s state DB** (`/var/lib/rancher/k3s/server/db/state.db`).
  SQLite mode has no online snapshot — `k3s etcd-snapshot save` is
  etcd-only, and a raw file copy while k3s is mid-write would be
  inconsistent. Recovery instead: reinstall k3s, re-apply manifests
  from this repo, re-`kubectl create secret` from Bitwarden, let
  `local-path` re-provision dynamic PVCs from restored hostPaths.
- **Coder workspace home directories**. Considered ephemeral; rebuild
  via the workspace template from git + dotfiles inside the workspace.
- **Authentik UI-only configuration state**. The recovery flow, the
  Coder/Outline OIDC providers + apps + group bindings, the groups, and
  the `email` scope-mapping override are now captured as Blueprints in
  `k8s/authentik/blueprints/` (#104) — a from-scratch rebuild
  reconstructs them from the `authentik-blueprints` ConfigMap without a
  postgres restore. Remaining DB-only state (lost with the DB if
  `AUTHENTIK_SECRET_KEY` is gone): users, group *memberships*, sessions,
  and event history. The postgres PVC is still in the nightly restic
  backup. See `k8s/authentik/README.md` and `blueprints/README.md`.
- **Prometheus TSDB and Alertmanager data** (#76). Both live on disposable
  `local-path` and are deliberately excluded — large, churny, and
  reconstructable (metrics re-scrape; silences are transient). Only Grafana's
  `/opt/grafana` (dashboards and settings) is in restic, tag `grafana`. See
  `k8s/kube-prometheus-stack/README.md`.

## Disaster recovery (full cluster rebuild)

If gandalf needs a full reinstall (hardware failure, OS reset), the
sequence to get back to a working cluster:

1. **Reimage / reinstall the host.** Ubuntu Server 26.04 base, then
   run `provision-gandalf.yml` on the freshly set-up gandalf:

   ```sh
   # On gandalf, after a fresh OS + git clone:
   cd ~/git/nickvigilante/homelab
   ansible-playbook -i ansible/hosts ansible/provision-gandalf.yml \
     -e ansible_connection=local
   ```

   The `ansible_connection=local` override is required because the
   playbook normally SSHes to gandalf, and self-SSH-without-key
   fails on a fresh host.

   The playbook provisions the host-side dirs the cluster depends on,
   including `/opt/grafana` owned `472:472` mode `0755` (Grafana's PV
   is `type: Directory` so kubelet refuses to start the pod if it's
   missing). It also installs a `/etc/tmpfiles.d/` drop-in that
   re-creates `/opt/grafana` at every boot, so a disk swap or
   accidental `rm` self-heals on the next reboot.

2. **Install k3s.** The install command lives in shell history rather
   than IaC -- a documented accepted trade-off. Add the `tls-san`
   overrides from `provision-gandalf.yml`'s templated k3s `config.yaml`
   block so remote kubectl works over the tailnet from the start.

3. **Recreate any remaining persistent host directories** under
   `/opt/<service>/` that the playbook doesn't yet own (most
   `DirectoryOrCreate`-typed PVs let kubelet auto-create them on first
   apply). Order matters when an ESO-managed Secret depends on a path
   restic later restores into -- run the playbook first, then bring
   Flux up, then restic-restore. See #139 for the restore sequence.

4. **Bootstrap Flux** against this repo. From the laptop:

   ```sh
   flux bootstrap git \
     --url=ssh://git@github.com/nickvigilante/homelab \
     --branch=main \
     --path=clusters/gandalf \
     --private-key-file=~/.ssh/flux-homelab
   ```

   Requires a write-enabled GitHub deploy key (the existing key from
   #135 Task 1 is `~/.ssh/flux-homelab` on gandalf; GitHub key id
   152989808). Write is only needed for the initial bootstrap commit;
   ongoing reconcile is read-only.

   Immediately after bootstrap, normalize the generated files:

   ```sh
   yamlfmt -conf .yamlfmt clusters/gandalf/flux-system/*.yaml
   git add clusters/gandalf/flux-system/ && git commit -m "Re-bootstrap Flux" && git push
   ```

5. **Restore the bootstrap Secret** `external-secrets/bws-access-token`
   from BW vault item `Homelab BWS Token`. This is the one Secret ESO
   itself needs in order to bootstrap every other Secret:

   ```sh
   export BW_SESSION="$(bw unlock --raw)"; bw sync
   kubectl create namespace external-secrets --dry-run=client -o yaml | kubectl apply -f -
   kubectl create secret generic bws-access-token \
     --namespace external-secrets \
     --from-literal=token="$(bw get item 'Homelab BWS Token' | jq -r '.notes')" \
     --dry-run=client -o yaml | kubectl apply -f -
   kubectl -n external-secrets annotate secret bws-access-token \
     kubectl.kubernetes.io/last-applied-configuration-
   unset BW_SESSION
   ```

6. **Wait for Flux to converge.** The `infrastructure` Kustomization
   brings up cert-manager's internal CA + ESO + bitwarden-sdk-server +
   the `bitwarden` ClusterSecretStore. Within a few minutes, every
   ExternalSecret in the cluster repopulates its Secret from BWS
   automatically. No per-service kubectl create needed for any
   migrated Secret.

7. **Install any still-helm-managed charts.** Most services are still
   helm-managed: walk through each service's README "One-time install"
   section. The (ESO-managed) Secrets are already in place by the time
   the chart starts.

8. **Restore data PVCs from restic** per #139.

What's recoverable from this sequence: every Secret currently in BWS,
every Flux-managed manifest. **Not** recoverable: anything in DB-only
state without backups (Authentik user passwords + group memberships
beyond what the Blueprints reconstruct, Outline page history beyond
the postgres dump, etc.).

## SPOF discipline (Authentik is a SPOF)

Authentik fronts SSO for downstream services. When Authentik is down,
every service integrated via OIDC / forward-auth loses its login flow.
**Every service put behind Authentik must keep a local-fallback
credential** so it can be reached when Authentik is broken:

- Jellyfin: native admin account stays in Bitwarden
- Pi-hole admin: native password stays in Bitwarden (item `Pi-Hole`)
- Uptime Kuma: native admin in Bitwarden
- Coder: `coder users create admin-local --password=...` (see `k8s/coder/README.md` step 8)

When wiring a new downstream integration, verify the fallback works
*before* declaring the integration done.

## Host-level changes

Anything host-level on gandalf (apt, systemd, files in `/etc/`,
sysctl, etc.) belongs in `ansible/provision-gandalf.yml`. It's
idempotent and meant to be re-run after pulling repo changes that
touch `system/`. Don't ad-hoc edit gandalf — if it matters enough to
remember, capture it in the playbook.

For Pi workers, the analogous playbook is `ansible/provision-pi.yml`.
The Tailscale auth keys it needs are minted via
`ansible/bin/mint-tailscale-authkey.sh` (OAuth, on-demand, short-lived).

## Pull requests + commits

- Feature branches → PRs → squash-merge to `main`. Never push to main
  directly (branch protection enforces this).
- **No `Co-Authored-By` trailer in commit messages or PR bodies.**
- Commit subjects: imperative mood, ≤ 70 chars. Body for "why," not "what."
- PR template at `.github/pull_request_template.md` lists the
  before-merge checklist (secrets, backup wiring, SPOF impact).

## Markdown / doc style

- **Semantic line breaks** — break source lines at sentence/clause
  boundaries, not arbitrary column wraps. mdformat (`--wrap keep`)
  preserves them, and clause-intact lines never get a stray `+`/`-`/`N)`
  at a line-start that a CommonMark formatter would misread as a list item.
- **Write "and"/"&", never `+`, to mean "and" in prose** — more accessible
  to screen readers, and avoids the line-start ambiguity above.
- Markdown is formatted by **mdformat** (see `.pre-commit-config.yaml`), not
  prettier — prettier rewrites embedded code blocks and mangles
  snake_case-near-emphasis in these identifier-heavy docs.

## Tracking open work

Open work — known gaps, deferred features, audit follow-ups — lives
as **GitHub issues** on this repo. Markdown docs (audit doc, READMEs,
specs) reference work by issue number (e.g. `#62`) rather than
describing the work inline. The issue list is the source of truth for
follow-up status.

**Disclosure screen before filing.** This repo is public. Before
opening an issue, ask: *would this be the first public mention of a
known-but-unpatched weakness?* If yes, route privately (Todoist, a
private notes repo, or resolve before public mention). If no — the
common case, because the audit doc already documents posture openly —
file on GitHub.

**What does NOT belong as an issue:**

- "Things deliberately not done" (accepted risks). Issues imply
  intent to resolve; these are decisions. Document in the relevant
  doc's "Things deliberately not done" section instead.
- Daily-flow tasks not tied to this repo. Those live in Todoist.
- In-conversation context or implementation details. Those live in
  the relevant spec or plan doc.

## Things deliberately not done

- **Traefik stays Ansible-owned.** It ships with k3s and is part of
  the bootstrap; keeping it outside the Flux loop means a Flux
  outage can't take down ingress. Custom middleware / IngressRoutes
  can still be Flux-managed raw manifests reconciled into the
  cluster; the chart itself stays manual.
- No bundled chart wrappers / Kustomize layers. Helm values + raw
  YAML, either via Flux `HelmRelease` or direct `helm upgrade`.
- No CI test suite for manifests beyond `.github/workflows/lint.yml`
  (kubeconform + yamllint). Flux's kustomize-controller validates
  server-side at reconcile time as the safety net for anything the
  local filter misses.
- k3s itself is not under IaC. `provision-gandalf.yml` captures
  *some* host state, but the k3s install command lives in shell
  history. A from-scratch rebuild means re-running `curl … | sh -`
  (see "Disaster recovery"). Accepted trade-off.
- No declarative management of BWS objects (projects, machine
  accounts, tokens, secrets). Considered: a Bitwarden Secrets
  Terraform provider could go in the sibling infrastructure repo and
  an Ansible collection could go in `ansible/`. For a single-operator
  homelab with two machine accounts and one project, the web-UI +
  `scripts/bws-migrate.sh` flow is fine. Revisit if either count grows.
