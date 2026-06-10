# Homepage

Dashboard at `https://vigihome.net` (the apex of the homelab's
vigihome.net zone). First service on the HTTPS-via-vigihome stack:
exercises Ingress + reflected `vigihome-tls` Secret + Pi-hole local
DNS apex record end-to-end on a low-stakes deploy before the
existing-services migration begins.

Chart: `jameswynn/homepage` v2.1.0 (Homepage appVersion `v1.2.0`).

## Prereqs

- cert-manager + ClusterIssuers installed (PR #23) — issued.
- `vigihome-tls` Certificate issued + prod issuer (PRs #24, #25) —
  Secret in `cert-manager` namespace.
- `emberstack/reflector` installed (PR #26) — controller running.
- `vigihome-tls` Secret reflected into the `homepage` namespace via
  cert-manager's `secretTemplate` annotations (added in this PR).
- `helm` on gandalf, jameswynn repo registered:
  ```sh
  helm repo add jameswynn https://jameswynn.github.io/helm-charts
  helm repo update
  ```

## One-time install

1. **Apply the cert-manager Certificate update** so reflector mirrors
   `vigihome-tls` into the `homepage` namespace:

   ```sh
   kubectl apply -f ../cert-manager/certificate.yaml
   # cert-manager rolls the Secret's annotations onto next reconcile.
   ```

2. **Apply the namespace:**

   ```sh
   kubectl apply -f namespace.yaml
   ```

3. **Verify the Secret has been mirrored** into `homepage`:

   ```sh
   kubectl -n homepage get secret vigihome-tls
   # Should appear within ~5s of reflector picking up the annotation.
   ```

4. **Install Homepage via Helm:**

   ```sh
   helm install homepage jameswynn/homepage \
     --namespace homepage \
     --version 2.1.0 \
     -f values.yaml
   ```

5. **Wait for the Deployment to be Ready:**

   ```sh
   kubectl -n homepage rollout status deploy/homepage
   ```

6. **Add the Pi-hole local DNS record for the apex.** Pi-hole on v6
   stores local records in `pihole.toml`, managed via the web UI:

   - Open the Pi-hole admin UI.
   - **Settings → Local DNS Records → Add**:
     - Domain: `vigihome.net`
     - IP: `192.168.50.135` (gandalf)
   - Save.
   - The change is picked up immediately; no Pi-hole pod restart needed
     for record additions (v6 watches `pihole.toml`).

7. **Verify HTTPS end-to-end** from any LAN client whose DNS goes
   through Pi-hole:

   ```sh
   curl -sv https://vigihome.net 2>&1 | grep -E "(subject|issuer|HTTP/)"
   ```

   Expect:

   - `subject: CN=vigihome.net`
   - `issuer: ... O = Let's Encrypt, CN = E7` (real LE intermediate,
     not Fake LE Root)
   - `HTTP/2 200`
   - Browser at `https://vigihome.net` shows the Homepage UI with no
     cert warning.

## Day-to-day ops

- **Edit the services list / widgets:** modify `values.yaml`, then
  `helm upgrade homepage jameswynn/homepage -n homepage --version 2.1.0 -f values.yaml`. The Deployment is restarted; Homepage re-reads its
  ConfigMap on boot (~5s). No data loss — Homepage is stateless.

- **Add a new service after a Phase 3 cutover.** When a `*.home`
  service moves to `*.vigihome.net`, update its `href` in `services:`
  here. Two edits per cutover (the service's own PR + this one) is
  fine for now; consolidate into a single PR when easier.

- **Enable Ingress auto-discovery later.** Each migrated service can
  add `gethomepage.dev/enabled: "true"` + metadata annotations to its
  Ingress, and Homepage will pick them up via the cluster mode RBAC
  this chart provisions. Skipped here to keep the initial deploy
  scope-clean — flip on after a few services are migrated.

- **Upgrade Homepage:** bump `tag` in `values.yaml` (Homepage version)
  and/or the `--version` flag (chart version), then `helm upgrade`.
  Watch for Homepage major-version notes in the release notes — v1.x
  added `HOMEPAGE_ALLOWED_HOSTS` and broke un-allowed hostnames.

- **Uninstall:** `helm uninstall homepage -n homepage` then
  `kubectl delete -f namespace.yaml`. Removing the Pi-hole DNS record
  is a manual cleanup step in the web UI.

## Service widgets & secrets

Integrated services use their **live Homepage widget** (not just a link
tile). A widget that hits an authenticated API needs a key, injected via the
`homepage-secrets` Secret → a `HOMEPAGE_VAR_*` env var (in `values.yaml`
`env:`) → `{{HOMEPAGE_VAR_...}}` in the widget config. Widgets target the
service's **direct / in-cluster address**, never an Authentik-gated Ingress,
so forward-auth can't block the widget's API calls.

`secret.example.yaml` documents the keys in `homepage-secrets`.

### Secrets pipeline

`homepage-secrets` is managed end-to-end by ESO + BWS as of #135 Task 8:

- Values live in BWS (Bitwarden Secrets Manager) → `homelab` project →
  one entry per `homepage-secrets` key (currently just
  `octoprint-api-key`).
- `external-secret.yaml` (Flux-owned via `clusters/gandalf/homepage.yaml`)
  declares the keys + their BWS secret-IDs; ESO syncs the in-cluster
  `homepage-secrets` Secret from BWS every `refreshInterval`.
- The Bitwarden password vault item (`Homelab OctoPrint`) stays
  populated as a DR mirror -- same pattern as
  `Homelab Restic Repository`.
- No `kubectl create secret` step. Adding a new widget secret means:
  (a) put the value in BWS in the `homelab` project, then
  (b) add a `data:` entry to `external-secret.yaml` referencing the new
  secret-ID, and either `flux reconcile externalsecret -n homepage homepage-secrets`
  or wait for the next refresh.

**OctoPrint widget** (`octoprint.vigihome.net`):

1. Generate an OctoPrint API key (Settings → Application Keys), store it
   in Bitwarden vault item `Homelab OctoPrint` field `API key`, AND
   add it to BWS as secret `octoprint-api-key` in the `homelab` project.
   The reusable `scripts/bws-migrate.sh` (#160) does both BW → BWS hops
   in one shot via the dedicated `homelab-bootstrap` machine account
   (Read/Write, kept separate from runtime `flux-eso` which stays
   Read-only). Pipe tuples on stdin:
   ```sh
   ./scripts/bws-migrate.sh <<'EOF'
   octoprint-api-key|Homelab OctoPrint|API key
   EOF
   ```
2. Wait for ESO to reconcile the ExternalSecret (a few seconds), or
   force it with `flux reconcile externalsecret -n homepage homepage-secrets`.
3. If the widget config in `values.yaml` changed too, `helm upgrade`
   (pin the version) so the new `env` block + widget block load:
   ```sh
   helm upgrade homepage jameswynn/homepage -n homepage --version 2.1.0 -f values.yaml
   ```

The widget points at `http://192.168.50.118:5000` directly (Homepage pod →
LAN), so it works regardless of the public Ingress.

**Grafana** is a **link tile, not a live widget** (#155).
Homepage's `grafana` widget makes a mandatory `GET /api/admin/stats` call,
and that endpoint is gated by Grafana *server admin*.
Grafana OSS can't grant server-admin to a service account:
the `isGrafanaAdmin` flag doesn't persist on a SA,
an org-`Admin` SA token still gets 403,
and the RBAC role-assignment API (`fixed:*:reader`) is Enterprise-only.
The only credential that satisfies the widget is a server-admin *user*,
which would put root-on-Grafana in the Homepage pod for a counts tile --
below the least-privilege bar we hold every other widget to.
A Viewer-scoped SA token *can* read the underlying counts via non-admin
endpoints (`/api/search?type=dash-db`, `/api/datasources`,
`/api/alertmanager/grafana/api/v2/alerts`),
but only Homepage's one-endpoint-per-tile `customapi` widget can consume them,
which doesn't reproduce the built-in 4-stat layout.
Not worth the complexity for low-value counts, so Grafana stays a plain href.

## External bookmarks

The `config.bookmarks` block in `values.yaml` is a quick-launch dashboard for
the external SaaS and cloud apps used regularly,
grouped as **Infra & Cloud**, **Productivity**, and **Finance**.
These are plain link tiles, not widgets:
no Homepage widget exists for them, and the goal is fast access rather than
live status.
Self-hosted services stay in `config.services` (their live widgets are tracked
in #91).

Each bookmark group needs a matching `layout` entry in `settingsString` to be
styled and ordered.
Tiles use a `dashboard-icons` slug (`icon:`) where one exists,
falling back to a two-letter `abbr:` otherwise.

## Pitfalls

- **`HOMEPAGE_ALLOWED_HOSTS` is mandatory on Homepage v1.x.** Without
  it (or with the wrong list), Homepage 403s every request and the
  Traefik Ingress shows a useless "200 OK" backed by an empty body in
  curl. Already set in `values.yaml` to `vigihome.net` + the cluster
  Service name (so `kubectl port-forward` works).

- **First-load delay.** Homepage's kubernetes widget makes its first
  API call to the apiserver lazily; the dashboard can take 2–5s to
  paint the cluster + node panels on a cold load. Not a bug.

- **Certificate renewal is invisible.** When cert-manager renews
  `vigihome-tls` (~60 days), reflector mirrors the new Secret into
  the `homepage` namespace automatically; Traefik picks it up
  without a restart. No action needed.

- **Don't put the Pi-hole admin URL on this dashboard's homepage
  if the dashboard hostname depends on Pi-hole DNS.** Circular: if
  Pi-hole is down, you can't reach the dashboard to click into
  Pi-hole. We accept the bootstrap dependency because the alternative
  (memorizing 192.168.50.135 paths) is worse — but Pi-hole admin
  should always have a known IP-based backup path documented in the
  Bitwarden `Pi-Hole` item.
