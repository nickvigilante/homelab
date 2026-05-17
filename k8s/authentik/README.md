# authentik

Self-hosted identity provider. Will eventually front the other services
(Jellyfin, Pi-hole admin, Uptime Kuma) via OIDC or Traefik forward-auth,
giving you one login instead of one per service.

## Layout

- `namespace.yaml` — `auth` namespace
- `pv-pvc.yaml` — PV `authentik-postgres-data` → hostPath `/opt/authentik/postgres` (gandalf), matching PVC named for what the bitnami postgres StatefulSet expects (`data-authentik-postgresql-0`)
- `values.yaml` — Helm values for `authentik/authentik`. Includes the chart-managed Ingress at `http://authentik.home`.
- `ingress-vigihome.yaml` — raw Ingress at `https://authentik.vigihome.net` (TLS via reflected `vigihome-tls`). Lives alongside the chart Ingress during the vigihome.net cutover; see [vigihome HTTPS cutover](#vigihome-https-cutover-in-progress) below.
- `secret.example.yaml` — template documenting the keys required in the `authentik-secrets` Secret. Never apply this directly — the real Secret is created from out-of-repo material (see below).

## vigihome HTTPS cutover (in progress)

Three-PR sequence to move Authentik from `http://authentik.home` to
`https://authentik.vigihome.net`. Coordinated with downstream OIDC
clients because Authentik derives token issuer URLs from the request
Host header — each client validates `iss` against exactly one URL, so
the OIDC client and Authentik have to flip together.

| PR | Scope | Effect |
|----|-------|--------|
| **D1 (this one)** | Add `ingress-vigihome.yaml` + reflect `vigihome-tls` into `auth` ns. | Both URLs work. Visiting `https://authentik.vigihome.net` issues tokens with `iss=https://authentik.vigihome.net/...`; visiting `http://authentik.home` issues `iss=http://authentik.home/...` tokens. OIDC clients still pinned to the old URL, unaffected. |
| **D2** | `k8s/coder/values.yaml`: flip OIDC issuer URL to `https://authentik.vigihome.net/application/o/coder/`; helm-upgrade Coder. **Must be applied with Authentik already serving HTTPS.** Login flow tested end-to-end (incl. Bitwarden-backed local fallback). | Coder users authenticate via HTTPS Authentik. |
| **D3** | `values.yaml`: `server.ingress.enabled: false` (drop the HTTP Ingress) + remove Pi-hole `authentik.home` local DNS record. | Only `https://authentik.vigihome.net` works. Cleanup. |

**Before applying D1**: add Pi-hole local DNS record
`authentik.vigihome.net → 192.168.50.135` via the Pi-hole admin UI
(Settings → Local DNS Records). Reflector mirrors the cert in <5s
after `cert-manager/certificate.yaml` is re-applied with the updated
auto-namespaces list.

**Don't apply D1 yet from the merged PR.** The PR is intentionally
shipped DRAFT-only — the apply needs you at a real browser with
Bitwarden local-fallback creds open in case anything misbehaves.

## ⚠️ SPOF discipline

Authentik *is* the login system. When it's down, every service behind it is
locked out. **Always preserve a local-fallback credential for every service
you put behind Authentik** so you can get in via the service's native login
when Authentik is broken.

- Jellyfin: keep a local admin account with username/password in Bitwarden, configure OIDC as an *additional* identity provider rather than replacing local auth.
- Pi-hole admin: native password stays in Bitwarden item `Pi-Hole`. Don't put it solely behind forward-auth.
- Uptime Kuma: same — local admin in Bitwarden, OIDC as an alternative login.

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
