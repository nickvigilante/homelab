# authentik

Self-hosted identity provider. Will eventually front the other services
(Jellyfin, Pi-hole admin, Uptime Kuma) via OIDC or Traefik forward-auth,
giving you one login instead of one per service.

## Layout

- `namespace.yaml` — `auth` namespace; carries Pod Security Standards labels (`enforce: baseline`, `audit + warn: restricted`) — see "Security posture" below
- `netpol-authentik-server.yaml` — `NetworkPolicy` restricting ingress to `authentik-server:9000` to Traefik + same-namespace pods only — see "Security posture" below
- `pv-pvc.yaml` — PV `authentik-postgres-data` → hostPath `/opt/authentik/postgres` (gandalf), matching PVC named for what the bitnami postgres StatefulSet expects (`data-authentik-postgresql-0`)
- `values.yaml` — Helm values for `authentik/authentik`. The chart's own Ingress is disabled (`server.ingress.enabled: false`) — Ingress lives in `ingress-vigihome.yaml` instead.
- `ingress-vigihome.yaml` — raw Ingress at `https://authentik.vigihome.net` (TLS via reflected `vigihome-tls`). This is the sole user-facing entry point for Authentik post-cutover.
- `secret.example.yaml` — template documenting the keys required in the `authentik-secrets` Secret. Never apply this directly — the real Secret is created from out-of-repo material (see below).

## Cleanup after D3

Once this PR (D3) is applied:

- Remove the Pi-hole local DNS record for `authentik.home` (Settings → Local DNS Records → delete). The `pi.hole` and `*.home` records for other services stay until their own Phase 3 cutovers.
- Verify with `curl http://authentik.home` from gandalf — expect Traefik's 404 (no Ingress matches that host anymore).
- Verify `https://authentik.vigihome.net` still serves the Authentik UI and Coder OIDC login still works.

For the original D1→D2→D3 cutover sequence (kept here as the runbook for any future Authentik hostname change), see commit history on this directory.

## ⚠️ SPOF discipline

Authentik *is* the login system. When it's down, every service behind it is
locked out. **Always preserve a local-fallback credential for every service
you put behind Authentik** so you can get in via the service's native login
when Authentik is broken.

- Jellyfin: keep a local admin account with username/password in Bitwarden, configure OIDC as an *additional* identity provider rather than replacing local auth.
- Pi-hole admin: native password stays in Bitwarden item `Pi-Hole`. Don't put it solely behind forward-auth.
- Uptime Kuma: same — local admin in Bitwarden, OIDC as an alternative login.

## Security posture

Two defense-in-depth layers live in this directory:

### Pod Security Standards (namespace label)

The `auth` namespace is labeled with:

- `pod-security.kubernetes.io/enforce: baseline` — hard-blocks the worst patterns (privileged containers, `hostNetwork`, `hostPID`/`hostIPC`, untrusted hostPath volumes).
- `pod-security.kubernetes.io/audit: restricted` + `warn: restricted` — surfaces drift toward the tighter standard without breaking anything.

**Not `enforce: restricted`** because the bitnami postgresql chart's `volumePermissions` init container runs as root to chown the hostPath PV mount before postgres starts. Moving to restricted would require either (a) disabling `volumePermissions` and chowning manually on gandalf, or (b) switching postgres charts. Either is its own change.

### NetworkPolicy on `authentik-server`

`netpol-authentik-server.yaml` restricts ingress to the `authentik-server` pods on port 9000 (the HTTP port Traefik proxies to after TLS termination):

- ✅ Traefik pods in `kube-system` (the legitimate public path)
- ✅ Same-namespace pods (worker, future outposts)
- ❌ Any other namespace (a compromised pod elsewhere can no longer hit Authentik's API directly to attempt credential stuffing / token introspection internally)

Enforcement: k3s ships with kube-router's NetworkPolicy controller built in, so this is actively enforced (verified by attempting a connection from the `coder` namespace and getting connection-refused). The controller also whitelists local-node → Pod traffic, so kubelet liveness/readiness probes continue to work without an explicit allow.

Ports `9443` (HTTPS) and `9300` (metrics) are deliberately not allowed; HTTPS is unused because TLS terminates at Traefik, and there's no in-cluster Prometheus scraper to allow yet. Add a rule here if/when monitoring lands.

## ⚠️ Don't lose `AUTHENTIK_SECRET_KEY`

The secret key encrypts every sensitive field stored in the database
(OIDC client secrets, SMTP passwords, etc.). If you lose it, restoring
the postgres backup gets you the data structure but not the encrypted
fields. Treat with the same care as the restic repository password.

Source of truth: Bitwarden item `Homelab Authentik`, field `secret-key`.

## One-time setup

1. **Save secrets to Bitwarden.** Create a Bitwarden item named `Homelab Authentik` with five custom fields:

   - `secret-key` — 60-char base64, Authentik's master encryption key
   - `postgres-password` — password for the `authentik` postgres user
   - `postgres-superuser-password` — password for the `postgres` superuser
   - `bootstrap-password` — initial `akadmin` UI login
   - `bootstrap-token` — initial API token (optional, but lets you script Authentik before logging in via the UI)

   Generate with `openssl rand -base64 60` (for the secret key) and
   `openssl rand -base64 24 | tr -d '/+='` for the passwords.

2. **Apply the namespace + PV/PVC**:

   ```bash
   kubectl apply -f namespace.yaml -f pv-pvc.yaml
   ```

3. **Create the Secret.** With Bitwarden CLI session active:

   ```bash
   export BW_SESSION="$(bw unlock --raw)"
   kubectl -n auth create secret generic authentik-secrets \
     --from-literal=secret-key="$(bw get item 'Homelab Authentik' | jq -r '.fields[] | select(.name=="secret-key") | .value')" \
     --from-literal=postgres-password="$(bw get item 'Homelab Authentik' | jq -r '.fields[] | select(.name=="postgres-password") | .value')" \
     --from-literal=postgres-superuser-password="$(bw get item 'Homelab Authentik' | jq -r '.fields[] | select(.name=="postgres-superuser-password") | .value')" \
     --from-literal=bootstrap-password="$(bw get item 'Homelab Authentik' | jq -r '.fields[] | select(.name=="bootstrap-password") | .value')" \
     --from-literal=bootstrap-token="$(bw get item 'Homelab Authentik' | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

   # OIDC client secrets for the blueprinted apps (#104). Same secret
   # each downstream service already holds. Needed BEFORE install — the
   # chart's global.env references this Secret (pod won't start without it).
   kubectl -n auth create secret generic authentik-oidc-secrets \
     --from-literal=oidc-coder-client-secret="$(bw get item 'Homelab Coder' | jq -r '.fields[] | select(.name=="oidc-client-secret") | .value')" \
     --from-literal=oidc-outline-client-secret="$(bw get item 'Homelab Outline' | jq -r '.fields[] | select(.name=="oidc-client-secret") | .value')"
   unset BW_SESSION
   ```

   Then render the blueprints ConfigMap (mounted via `blueprints.configMaps`
   in `values.yaml`; must exist before install — see `blueprints/README.md`):

   ```bash
   kubectl -n auth create configmap authentik-blueprints \
     $(find blueprints -name '*.yaml' -printf '--from-file=%f=%p ') \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

4. **Add Authentik to Pi-hole's DNS.** Append to `dns.hosts` in
   `/opt/pihole/etc-pihole/pihole.toml`:

   ```toml
   "192.168.50.135 authentik.home",
   "100.92.2.25 authentik.home"
   ```

   Then restart Pi-hole: `kubectl -n networking rollout restart deployment/pihole`.

5. **Install the chart**:

   ```bash
   helm repo add authentik https://charts.goauthentik.io
   helm repo update authentik
   helm install authentik authentik/authentik \
     -n auth --version 2026.2.2 \
     -f values.yaml
   ```

   First start takes a few minutes — the bitnami postgresql StatefulSet
   has to initialize, then Authentik's worker runs migrations.

6. **Add `/opt/authentik/postgres` to restic.** Edit `../backup/backup-cronjob.yaml`
   to add the new hostPath source, then `kubectl apply -f ../backup/backup-cronjob.yaml`. Mirror the `uptime-kuma-data` pattern.

7. **First login.** Open http://authentik.home/if/flow/initial-setup/ and
   log in as `akadmin` with the bootstrap password. Set a permanent
   password (rotates in DB; the env-var bootstrap is ignored after).

## Day-to-day operations

### Helm upgrade

**Always pin `--version` to the currently-installed chart.** An
unpinned `helm upgrade authentik authentik/authentik` (especially right
after `helm repo update`) pulls the *latest* chart, which silently
bumps both the Authentik image and the bundled postgres image. On
2026-05-23 that drifted 2026.2.2 → 2026.5.0 and the new postgres image
hit `ImagePullBackOff`, taking the whole `auth` namespace down. Pin the
version so a values-only change never moves the images:

```bash
CHART_VER="$(helm list -n auth -f '^authentik$' -o json | jq -r '.[0].chart' | sed 's/^authentik-//')"
helm -n auth upgrade authentik authentik/authentik --version "$CHART_VER" -f values.yaml
kubectl -n auth rollout status deployment/authentik-server
```

To intentionally bump Authentik, set `--version` to the target
explicitly and read the release notes for migration steps first.

### Outbound SMTP (Forward Email)

Authentik sends mail for password recovery, breach alerts, and event notifications via **Forward Email**'s managed SMTP relay (FOSS, MIT-licensed, $3/mo on the Enhanced Protection plan). Credentials live in a separate Secret (`smtp-relay`) rather than `authentik-secrets` so other services can mount their own copy of the relay creds without RBAC contagion.

**One-time setup (already done for the current deployment; documented for rebuild):**

1. **Forward Email dashboard:** verify your sender domain (MX + SPF + DKIM + DMARC records in DNS — Forward Email's UI walks through these). Generate SMTP credentials on the **Outbound Emails** page.

2. **Save to Bitwarden** as item `Homelab Mail Relay` with these custom fields:

   - `smtp-host` → `smtp.forwardemail.net`
   - `smtp-port` → `465`
   - `smtp-username` → the verified sender at your domain (e.g. `noreply@vigihome.net`)
   - `smtp-password` → the password from the Forward Email dashboard
   - `from-address` → typically the same as `smtp-username`

3. **Create the k8s Secret** sourced from Bitwarden:

   ```bash
   # On laptop (with bw CLI):
   export BW_SESSION="$(bw unlock --raw)"
   kubectl -n auth create secret generic smtp-relay \
     --from-literal=smtp-username="$(bw get item 'Homelab Mail Relay' | jq -r '.fields[] | select(.name=="smtp-username") | .value')" \
     --from-literal=smtp-password="$(bw get item 'Homelab Mail Relay' | jq -r '.fields[] | select(.name=="smtp-password") | .value')"
   unset BW_SESSION
   ```

4. **Apply via helm upgrade.** The chart's existing values block already references this Secret via env vars; no manifest change is needed once the Secret exists. (Pin `--version`, per the "Helm upgrade" section above.)

   ```bash
   CHART_VER="$(helm list -n auth -f '^authentik$' -o json | jq -r '.[0].chart' | sed 's/^authentik-//')"
   helm -n auth upgrade authentik authentik/authentik --version "$CHART_VER" -f values.yaml
   kubectl -n auth rollout status deployment/authentik-server
   ```

**Testing the relay:**

In the Authentik admin UI, navigate to **System → Tasks** → find any task ending in `email` (e.g., `notification_transport`), or trigger a real password recovery from the login screen. Inspect the resulting mail's headers — expect:

- `Authentication-Results: ... dkim=pass`
- `Authentication-Results: ... spf=pass`
- `Authentication-Results: ... dmarc=pass`

If any fail, check Forward Email's dashboard for delivery logs (the **Logs** page shows recent send attempts with the failure reason).

**Rotating SMTP creds:** generate a new password in the Forward Email dashboard, update the Bitwarden item, then re-run step 3 above (kubectl will overwrite the existing Secret) and bounce authentik-server via `kubectl -n auth rollout restart deployment/authentik-server`.

### Rotating the OIDC signing certificate

Authentik signs every OIDC token (id_token, access_token) with a single keypair stored in its DB. The default `authentik Self-signed Certificate` ships with each install and has a 365-day expiry; rotation is **manual on cadence, automated in execution** via two scripts in `scripts/`.

**Annual rotation** (one command, scales to N providers):

```bash
# On laptop or gandalf:
./scripts/rotate-authentik-signing-cert.sh
```

What it does:

1. Reads the admin API token from `auth/authentik-secrets`.
2. Generates a new signing keypair via Authentik's API (default: ECDSA P-256, 365d validity, name `authentik-signing-YYYYMMDD`).
3. Auto-discovers every OAuth2/OIDC provider and PATCHes each `signing_key` to the new keypair.
4. Restarts `authentik-server` for a clean JWKS cache.
5. Verifies every Application's JWKS endpoint serves the new key's `kid`.
6. Optionally emails a run summary (if SMTP\_\* env vars are set — wire from the Forward Email creds in Bitwarden `Homelab Mail Relay`).

Flags:

- `--key-type=ecdsa` (default) or `rsa`
- `--name=NAME` to override the auto-datestamped cert name
- `--dry-run` to print the plan without changes
- `--quiet` to suppress per-step output

**Real-world friction:** in-flight access tokens signed by the old key become unverifiable on the downstream client's next JWKS refresh. Active sessions get a brief "sign in again" prompt within the next ~5 minutes. Pick a quiet hour to run.

**Cleanup, 30+ days later:**

```bash
# On laptop or gandalf:
./scripts/cleanup-old-authentik-signing-certs.sh           # default retention: 30 days
./scripts/cleanup-old-authentik-signing-certs.sh --days=14 # custom
./scripts/cleanup-old-authentik-signing-certs.sh --dry-run # preview
```

Deletes any keypair whose `name` matches `authentik-signing-*` AND is older than the retention window AND is not currently referenced by any provider's `signing_key`. Idempotent and safe — bails on anything still in use.

**Defense in depth — JWKS health monitor:**

In Uptime Kuma, add one HTTP monitor per OIDC application that hits `https://authentik.vigihome.net/application/o/<slug>/.well-known/openid-configuration` every 5 minutes and asserts HTTP 200. Catches silent post-rotation breakage (e.g., a downstream client caching JWKS too aggressively) that the rotation script's own verify step would miss. Setup via the Uptime Kuma UI; reproducible-config tooling for Uptime Kuma is a separate follow-up.

### Postgres backup / restore

The postgres data dir at `/opt/authentik/postgres` is captured by the
nightly restic CronJob under tag `authentik-postgres`. For a restore:

```bash
kubectl -n auth scale statefulset/authentik-postgresql --replicas=0
# restore /opt/authentik/postgres from a restic snapshot, e.g.:
#   restic restore <snap> --target / --include /opt/authentik/postgres
kubectl -n auth scale statefulset/authentik-postgresql --replicas=1
```

You also need the matching `AUTHENTIK_SECRET_KEY` from Bitwarden to read
encrypted fields. Without it, the restore is structurally complete but
some columns are useless ciphertext.

### Adding an OIDC client

**The existing OIDC apps (Coder, Outline) are captured as code** in
`blueprints/applications/` (#104) — provider + application + group
binding, with the `client_secret` injected via `!Env` from the
`authentik-oidc-secrets` Secret. To add a new OIDC client, prefer adding
a `blueprints/applications/<app>.yaml` modeled on those (see
`blueprints/README.md`) rather than clicking it in the UI, so a rebuild
reconstructs it. Steps per new app: author the blueprint, add an
`oidc-<app>-client-secret` key to `authentik-oidc-secrets` + an
`AUTHENTIK_OIDC_<APP>_SECRET` env in `values.yaml`, re-render the
ConfigMap, `helm upgrade`.

If you do create one in the UI first (**Applications → Providers →
Create → OAuth2/OpenID Provider** → bind to an application), export it
with `ak export_blueprint` and fold it into a blueprint afterward. Each
downstream app gets its own provider record so you can revoke
individually.

For Traefik forward-auth (services that don't speak OIDC, like Pi-hole
admin): use the Proxy Provider type instead, then add a Traefik
`Middleware` that calls Authentik's outpost.

### Customize the `email` scope mapping

**This is now captured as code** — see
`blueprints/email-scope-mapping.yaml`, applied automatically by the
worker from the `authentik-blueprints` ConfigMap. To change it, edit the
file and re-render (see `blueprints/README.md`); no UI clicks.

Background: Authentik's default `email` scope mapping returns
`email_verified: False` because this deployment has no email
verification flow. Strict OIDC clients (e.g. Coder with
`CODER_OIDC_IGNORE_EMAIL_VERIFIED=false`, and Outline) reject the
sign-in (`Verify your email address on your OIDC provider`). The
blueprint overrides the shipped mapping (by its managed marker) to
return `email_verified: True` unconditionally, so the override
self-heals on every worker start.

### Promoting an OIDC user to owner/admin in a downstream

Authentik's first OIDC sign-in to a brand-new downstream creates a
user with the downstream's default role. If the downstream was *already*
bootstrapped with a local admin (e.g. Coder's first-visit form), the
OIDC-created user lands as a member and needs manual promotion via the
downstream's UI from the bootstrap account. See
`../coder/README.md` step 8 for the exact pattern.

### Self-service password recovery (homelab-users only)

`homelab-users` members can reset their own password via the "Forgot
password?" link on `https://authentik.vigihome.net/`. The flow is
**not** available to akadmin or any other admin account — see the
"akadmin recovery — last-resort paths" section below for the operator
runbook.

**How it's wired (captured as code in `blueprints/recovery-flow.yaml`, #104):**

Authentik ships no recovery flow by default, so this was built from
scratch and is now a blueprint — the worker reconstructs the whole stack
(flow, stages, the group-gate policy, bindings, and the brand wiring)
from the `authentik-blueprints` ConfigMap on startup, no postgres restore
needed. There is **one** policy object — an Expression policy. (Note for
anyone following an older draft: Authentik 2026.2 has no "Group
Membership" or "Rate Limit" policy *types* — group gating is done with
an expression, and there is no rate-limit primitive.) The table below
documents what the blueprint creates:

| Object                                 | Where                                     | Settings                                                                                                                               |
| -------------------------------------- | ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Group `homelab-users`                  | Directory → Groups                        | The group the recovery gate checks; non-members (incl. akadmin) can't reset                                                            |
| Policy `recovery-allowed-group`        | Customization → Policies (**Expression**) | Code below. Execution logging OFF                                                                                                      |
| Stage `recovery-identification`        | Flows & Stages → Stages (Identification)  | User fields: username + email; **Pretend user exists: ON**; case-insensitive matching ON                                               |
| Stage `recovery-email`                 | Flows & Stages → Stages (Email)           | Use global settings: ON; Subject: `vigihome.net — password reset requested`; Template: Password Recovery; Token expiry: `hours=1`      |
| Stage `default-password-change-prompt` | (reused)                                  | New-password prompt                                                                                                                    |
| Stage `default-password-change-write`  | (reused)                                  | Writes the new password                                                                                                                |
| Flow `recovery` ("Password Recovery")  | Flows & Stages → Flows                    | Designation: Recovery; Authentication: require no authentication. Stage bindings 10/20/30/40 = identification / email / prompt / write |
| Bindings 20, 30, 40                    | Stage bindings on the flow                | Each: "Evaluate when flow is planned" **OFF**, "Evaluate when stage is run" **ON**, with `recovery-allowed-group` bound (order 0)      |
| Brand (default)                        | System → Brands                           | Recovery flow = `Password Recovery` — this is what surfaces "Forgot password?" on the login screen                                     |
| Helm env `AUTHENTIK_EMAIL__FROM`       | `k8s/authentik/values.yaml`               | `"vigihome auth <noreply@vigihome.net>"` (display name)                                                                                |

The policy (`recovery-allowed-group`):

```python
pending_user = request.context.get("pending_user")
if not pending_user:
    return False
return ak_is_group_member(pending_user, name="homelab-users")
```

**How the gate works (and why it's built this way — both points were
verified the hard way):**

- Recovery users are **anonymous until the identification stage sets
  `pending_user`**, so the group check cannot be a flow-level policy
  binding — that evaluates at flow entry against the anonymous user and
  denies *everyone*. It must read `request.context.get("pending_user")`
  and run *after* identification, which is why bindings 20/30/40 use
  "Evaluate when stage is run".
- It is bound to **all three** post-identification stages, not just the
  email stage. When the policy denies, Authentik *skips* the bound
  stage — and if only the email stage were gated, a denied user's
  skipped email stage cascades straight into the password-change prompt
  (a full bypass; this was reproduced). Gating prompt + write as well
  means a denied user runs out of stages and is bounced back to the
  login flow having changed nothing.
- `pending_user` **survives the emailed-link flow restore**, so a
  legitimate member re-passes the gate at the prompt/write stages after
  clicking through. (Confirmed end-to-end: member resets successfully,
  akadmin dead-ends after identification.)

**Abuse posture:**

- Account-existence: the identification stage runs with "Pretend user
  exists", so valid and invalid identifiers get the same response — the
  flow doesn't leak which accounts exist.
- Group gate: non-`homelab-users` accounts (including akadmin) can
  neither receive a recovery email nor reach the password prompt.
- **No rate-limit or reputation throttle.** Deliberately deferred (see
  `audits/tier-1-authentik.md`, "Things deliberately not done"). With a
  single member the residual risk is inbox spam, not account compromise
  (completing a reset still requires access to the member's mailbox).
  Revisit before `homelab-users` grows or recovery is opened more
  broadly.

**Rebuild:** no manual steps. `blueprints/recovery-flow.yaml` recreates
the policy, both custom stages, the flow, all four stage bindings
(orders 10/20/30/40 with re-evaluate-on-run / not-on-plan), the three
group-gate policy bindings on 20/30/40, and the default brand's recovery
flow — adopting any that already exist. The shipped
`default-password-change-prompt` / `-write` stages are referenced, not
recreated. Validated by a from-empty-postgres reconstruction on a
throwaway instance (#104). Only thing still DB-only: the per-user
membership of `homelab-users` itself.

### akadmin recovery — last-resort paths

akadmin is the break-glass account and is deliberately excluded from
the self-service recovery flow above. When akadmin's password is lost
or needs rotating, use one of these two paths.

<!-- TODO: mirror this entire section to the Outline wiki when #92 lands -->

**Path A — postgres-direct via `ak shell`** (preferred when ssh +
kubectl access to the cluster are available, which is the common case
from gandalf or the operator laptop on tailnet):

```bash
# On the laptop (kubectl context = homelab):

# 1. Sanity-check akadmin exists
kubectl -n auth exec -it statefulset/authentik-postgresql -- \
  bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U authentik -d authentik \
    -c "SELECT id, username, email FROM authentik_core_user WHERE username = '"'"'akadmin'"'"';"'

# 2. Reset the password via the management shell (Django hashes it
#    before writing, so the literal here is plaintext)
kubectl -n auth exec -it deployment/authentik-server -- \
  ak shell -c "from authentik.core.models import User; \
    u = User.objects.get(username='akadmin'); \
    u.set_password('REPLACE_WITH_NEW_PASSWORD'); \
    u.save()"

# 3. Update Bitwarden item 'Homelab Authentik' with the new password
```

**Path B — bootstrap-token via the Authentik API** (for when shell
access is harder than API access, but the bootstrap token cached in
Bitwarden is available):

```bash
# On any machine with curl + bw + jq + tailnet access to vigihome:

# 1. Unlock Bitwarden and pull the bootstrap token
BW_SESSION="$(bw unlock --raw)"
TOKEN="$(bw get item 'Homelab Authentik' \
  | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

# 2. Look up akadmin's primary key
AKADMIN_PK="$(curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/core/users/?username=akadmin' \
  | jq -r '.results[0].pk')"

# 3. Reset the password
curl -sSf -H "Authorization: Bearer $TOKEN" \
  -X POST "https://authentik.vigihome.net/api/v3/core/users/$AKADMIN_PK/set_password/" \
  -H 'Content-Type: application/json' \
  -d '{"password":"REPLACE_WITH_NEW_PASSWORD"}'

# 4. Update Bitwarden item 'Homelab Authentik' with the new password
```

**Bootstrap token rotation:** the bootstrap token is admin-equivalent.
If used during recovery (Path B), rotate it afterward — edit
`AUTHENTIK_BOOTSTRAP_TOKEN` in chart values and re-`helm upgrade`
(pinned to the installed chart version, per the "Helm upgrade" section
above). Update Bitwarden in lockstep.

**Testing the recovery flow:**

The as-built regression checklist — re-run whenever the recovery flow
is touched (chart upgrade, Authentik upgrade, policy or stage change):

1. **Member happy path:** a `homelab-users` member identifies, receives
   the email, clicks the link, sets a new password, and the new
   password authenticates.
2. **Non-member denied:** akadmin (and any non-member) identifies and
   then dead-ends — no email, no password prompt — bounced to the login
   flow. Bind/stage skip must not cascade into the prompt.
3. **No enumeration:** a non-existent identifier returns the same
   on-screen response as a real one ("Pretend user exists" ON).

The design rationale is in
`docs/superpowers/specs/2026-05-20-authentik-recovery-flow-design.md`
(see the as-built note at its top — the spec predates implementation
and some specifics, e.g. rate-limiting, were dropped).
