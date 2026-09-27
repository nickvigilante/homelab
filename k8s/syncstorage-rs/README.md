# syncstorage-rs — self-hosted Firefox Sync storage

Self-hosted [Firefox Sync](https://github.com/mozilla-services/syncstorage-rs) storage at
`https://firefox-sync.vigihome.net`, reachable only over the tailnet (see "Reachability" below).
**Storage-only**: Mozilla's Firefox Accounts still handles login/auth — this deploys the
tokenserver + storage API, not a self-hosted FxA. See `images/syncstorage-rs/README.md` for why a
custom-built image is required (upstream publishes none) and why Postgres was chosen over MySQL.

## What actually syncs

Firefox Sync's collections, and whether each is enabled here:

| Collection          | Synced? | Notes                                                                          |
| ------------------- | ------- | ------------------------------------------------------------------------------ |
| `history`           | Yes     |                                                                                |
| `tabs`              | Yes     | Open tabs across devices                                                       |
| `addons`            | Yes     | Which extensions are installed                                                 |
| `prefs`             | Yes     | A curated Firefox subset, not all of `about:config`                            |
| `extension-storage` | Yes     | Extension data via `browser.storage.sync` (e.g. Dark Reader's site exclusions) |
| `bookmarks`         | Yes     | Bookmarks are managed in Raindrop; synced here too since it's free to enable   |
| `passwords`         | **No**  | Disabled client-side (Firefox Sync settings) — handled by Bitwarden instead    |
| `forms`             | **No**  | Same — autofill is Bitwarden's job                                             |

Passwords/forms are excluded per-device in each Firefox's Sync settings, not by anything on this
server — the server stores whatever a client sends it.

Both personal and work machines point at this same tokenserver. Data stays isolated per Firefox
Account: personal machines log into a personal Mozilla account, work machines a work account, and
Mozilla's FxA keeps their sync data in completely separate buckets on this server. See the dotfiles
repo's `home/run_once_after_configure-firefox-sync.sh.tmpl` for the client-side `user.js` wiring.

## Reachability

Tailscale-only. `firefox-sync.vigihome.net` resolves via the Pi-hole `*.vigihome.net` wildcard,
which is only reachable on the tailnet — there's no public DNS/ingress path to this host. Firefox
on a device not connected to Tailscale will fail to sync (falls back silently; it just won't sync
until reconnected).

## Files

| File                         | Purpose                                                                                                                        |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `namespace.yaml`             | Namespace `syncstorage-rs`                                                                                                     |
| `pv-pvc.yaml`                | PV `syncstorage-rs-postgres-data` → hostPath `/opt/syncstorage-rs/postgres` (gandalf) + PVC `data-postgres-postgresql-0`       |
| `postgres-helmrelease.yaml`  | `bitnami/postgresql` HelmRelease (release name MUST be `postgres`); creates both the `syncstorage` and `tokenserver` databases |
| `deployment.yaml`            | syncstorage-rs `Deployment` + `Service` (raw — upstream ships no chart)                                                        |
| `ingress-vigihome.yaml`      | Ingress for `firefox-sync.vigihome.net` (websecure, `vigihome-tls`)                                                            |
| `netpol-syncstorage-rs.yaml` | NetworkPolicies: app reachable from Traefik only; postgres same-namespace only                                                 |
| `external-secret.yaml`       | ESO `ExternalSecret` sourcing `syncstorage-rs-secrets` from BWS                                                                |
| `secret.example.yaml`        | Template documenting the `syncstorage-rs-secrets` keys (never applied)                                                         |
| `kustomization.yaml`         | Full Flux takeover of this directory                                                                                           |

## One-time setup

### 1. Secrets — done via `scripts/bws-bootstrap-secrets.sh`

The four BWS secrets are already wired into `external-secret.yaml` (created via
`scripts/bws-bootstrap-secrets.sh` — see its usage in `CLAUDE.md`, or homelab#226 for how it
works). Nothing to do here unless a secret needs rotating: delete the field from the
**"Homelab syncstorage-rs"** vault item and re-run the same tuple through the script to
regenerate just that one value.

### 2. Publish the image (if not already built)

Confirm `ghcr.io/nickvigilante/syncstorage-rs:0.23.3-postgres` exists and is public — see
`images/syncstorage-rs/README.md`. If the GHCR package is still private, either flip its
visibility or add an `imagePullSecret` to `deployment.yaml`.

### 3. Firefox client configuration

Each device's Firefox needs `identity.sync.tokenserver.uri` set to
`https://firefox-sync.vigihome.net/1.0/sync/1.5` in `about:config`. Automated for Linux desktop
machines via the dotfiles repo (`run_once_after_configure-firefox-sync.sh.tmpl`, gated on
`display = true`, runs on both personal and work profiles). Manual for anything else (phones,
other OSes not yet covered).

### 4. Verify

After `chezmoi apply` on a device and a Firefox restart, Settings → Sync should show it connecting.
`kubectl -n syncstorage-rs logs -l app=syncstorage-rs` on first sync attempt should show a
tokenserver request succeed (a 200 on `/1.0/sync/1.5/...`), not an FxA verification error.

## Backup

Postgres data (`/opt/syncstorage-rs/postgres`) is snapshotted nightly under tag
`syncstorage-rs-postgres` — see `k8s/backup/backup-cronjob.yaml`. Losing it loses all synced data
(history, tabs, addon settings) but no credentials — passwords/autofill are excluded client-side,
never reach this server.
