# authentik

Self-hosted identity provider. Will eventually front the other services
(Jellyfin, Pi-hole admin, Uptime Kuma) via OIDC or Traefik forward-auth,
giving you one login instead of one per service.

## Layout

- `namespace.yaml` — `auth` namespace
- `pv-pvc.yaml` — PV `authentik-postgres-data` → hostPath `/opt/authentik/postgres` (gandalf), matching PVC named for what the bitnami postgres StatefulSet expects (`data-authentik-postgresql-0`)
- `values.yaml` — Helm values for `authentik/authentik`
- `secret.example.yaml` — template documenting the keys required in the `authentik-secrets` Secret. Never apply this directly — the real Secret is created from out-of-repo material (see below).

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
