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

1. **Save secrets to Bitwarden.** Create a Bitwarden item named `Homelab
   Authentik` with five custom fields:
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
   unset BW_SESSION
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
   to add the new hostPath source, then `kubectl apply -f
   ../backup/backup-cronjob.yaml`. Mirror the `uptime-kuma-data` pattern.

7. **First login.** Open http://authentik.home/if/flow/initial-setup/ and
   log in as `akadmin` with the bootstrap password. Set a permanent
   password (rotates in DB; the env-var bootstrap is ignored after).

## Day-to-day operations

### Helm upgrade

```bash
helm -n auth upgrade authentik authentik/authentik -f values.yaml
kubectl -n auth rollout status deployment/authentik-server
```

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

4. **Apply via helm upgrade.** The chart's existing values block already references this Secret via env vars; no manifest change is needed once the Secret exists.

   ```bash
   helm -n auth upgrade authentik authentik/authentik -f values.yaml
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
6. Optionally emails a run summary (if SMTP_* env vars are set — wire from the Forward Email creds in Bitwarden `Homelab Mail Relay`).

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

In the Authentik UI: **Applications → Providers → Create → OAuth2/OpenID
Provider** → bind to a new application → copy client ID + secret into
the downstream service's config. Each downstream app gets its own
provider record so you can revoke individually.

For Traefik forward-auth (services that don't speak OIDC, like Pi-hole
admin): use the Proxy Provider type instead, then add a Traefik
`Middleware` that calls Authentik's outpost.

### Customize the `email` scope mapping

Authentik's default `email` scope mapping returns
`email_verified: False` because this deployment has no SMTP / email
verification flow configured. Strict OIDC clients (e.g. Coder with
`CODER_OIDC_IGNORE_EMAIL_VERIFIED=false`) reject the sign-in and the
user sees `Verify your email address on your OIDC provider` with no
way forward. The fix is a one-time global edit, applied here once and
re-used by every future OIDC integration:

1. **Customization → Property Mappings**.
2. Find the row named `authentik default OAuth Mapping: OpenID 'email'`
   (type `Scope Mapping`). Click **Edit**.
3. The expression returns a dict with `email` and `email_verified`.
   Change the `email_verified` line to return `True` unconditionally:

   ```python
   return {
       "email": request.user.email,
       "email_verified": True,
   }
   ```

4. Save. No restart needed — Authentik re-evaluates the expression on
   the next token exchange.

This mapping lives in the Authentik database, *not* in this repo. A
restore from postgres backup recovers it; a rebuild-from-scratch does
not. Re-apply after any from-scratch reinstall.

### Promoting an OIDC user to owner/admin in a downstream

Authentik's first OIDC sign-in to a brand-new downstream creates a
user with the downstream's default role. If the downstream was *already*
bootstrapped with a local admin (e.g. Coder's first-visit form), the
OIDC-created user lands as a member and needs manual promotion via the
downstream's UI from the bootstrap account. See
`../coder/README.md` step 8 for the exact pattern.
