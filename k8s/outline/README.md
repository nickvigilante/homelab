# Outline — documentation wiki

Self-hosted [Outline](https://www.getoutline.com/) at
`https://docs.vigihome.net`. Narrative/reference documentation for the
homelab and `infrastructure` repos. Raw app manifests + a `bitnami/postgresql`
release + a throwaway Redis, with uploads on Storj S3 and auth via
Authentik OIDC.

Design: `docs/superpowers/specs/2026-05-25-outline-docs-wiki-design.md`.

## Files

| File                    | Purpose                                                                                                    |
| ----------------------- | ---------------------------------------------------------------------------------------------------------- |
| `namespace.yaml`        | Namespace `outline`                                                                                        |
| `pv-pvc.yaml`           | PV `outline-postgres-data` → hostPath `/opt/outline/postgres` (gandalf) + PVC `data-postgres-postgresql-0` |
| `postgres-values.yaml`  | `bitnami/postgresql` values (release name MUST be `postgres`)                                              |
| `redis.yaml`            | Ephemeral Redis `Deployment` + `Service` (no persistence, no auth)                                         |
| `deployment.yaml`       | `outline-env` ConfigMap + Outline `Deployment` (with migrate initContainer) + `Service`                    |
| `ingress-vigihome.yaml` | Ingress for `docs.vigihome.net` (websecure, `vigihome-tls`)                                                |
| `netpol-outline.yaml`   | NetworkPolicies: app reachable from Traefik only; postgres/redis same-namespace only                       |
| `secret.example.yaml`   | Template documenting the `outline-secrets` keys (never applied)                                            |

## ⚠ SPOF exception — Outline is OIDC-only

Every other Authentik-fronted service keeps a local-fallback credential
(see the SPOF discipline section in the repo `CLAUDE.md`). **Outline
cannot** — it has no native password auth, only OIDC. Outline is
therefore a deliberate **exception** to that rule.

The mitigation is editorial, not technical: **Outline holds only
narrative/reference docs. Every break-glass runbook — Authentik
recovery, restic restore, cluster rebuild — stays in a git README**,
which is readable when Authentik (and therefore Outline) is down. Don't
put the fire-escape plan inside the building that's on fire.

## One-time setup

### 1. Storj bucket + S3 gateway credentials

In the Storj console, create bucket **`outline-uploads`** and an **S3
Gateway** access key scoped to it. Save the Access Key ID and Secret
Access Key as fields `s3-access-key` / `s3-secret-key` on Bitwarden item
**`Homelab Outline`**. Endpoint is `https://gateway.storjshare.io`.

### 2. Authentik OIDC provider + application

- **Provider** (OAuth2/OpenID): name `outline`, confidential, redirect
  URI `https://docs.vigihome.net/auth/oidc.callback` (Strict — byte-exact),
  signing key set, scopes openid/profile/email. Copy the Client ID +
  Secret to Bitwarden fields `oidc-client-id` / `oidc-client-secret`.
- **Application**: name `Outline`, slug `outline` (the slug appears in
  the issuer URL), provider `outline`.
- **Bind the `homelab-users` group** to the application (Policy / Group /
  User Bindings) — matches Coder/HA (audit finding 4d). Non-members get
  no access.

### 3. Generate secrets + create the `outline-secrets` Secret

On the laptop (kube-context = homelab):

```bash
SECRET_KEY="$(openssl rand -hex 32)"
UTILS_SECRET="$(openssl rand -hex 32)"
PG_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=')"
PG_SUPER_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=')"
# Record all four in Bitwarden item 'Homelab Outline' as fields
# secret-key / utils-secret / postgres-password / postgres-superuser-password.

BW_SESSION="$(bw unlock --raw)"; export BW_SESSION
get() { bw get item 'Homelab Outline' | jq -r ".fields[] | select(.name==\"$1\") | .value"; }
S3_KEY="$(get s3-access-key)";   S3_SECRET="$(get s3-secret-key)"
OIDC_ID="$(get oidc-client-id)"; OIDC_SECRET="$(get oidc-client-secret)"
DB_URL="postgres://outline:${PG_PASSWORD}@postgres-postgresql.outline.svc.cluster.local:5432/outline"

kubectl create namespace outline --dry-run=client -o yaml | kubectl apply -f -
kubectl -n outline create secret generic outline-secrets \
  --from-literal=secret-key="$SECRET_KEY" \
  --from-literal=utils-secret="$UTILS_SECRET" \
  --from-literal=postgres-password="$PG_PASSWORD" \
  --from-literal=postgres-superuser-password="$PG_SUPER_PASSWORD" \
  --from-literal=database-url="$DB_URL" \
  --from-literal=s3-access-key="$S3_KEY" \
  --from-literal=s3-secret-key="$S3_SECRET" \
  --from-literal=oidc-client-id="$OIDC_ID" \
  --from-literal=oidc-client-secret="$OIDC_SECRET"

unset BW_SESSION SECRET_KEY UTILS_SECRET PG_PASSWORD PG_SUPER_PASSWORD S3_KEY S3_SECRET OIDC_ID OIDC_SECRET DB_URL
```

### 4. Apply (order matters)

```bash
kubectl apply -f k8s/outline/namespace.yaml
kubectl apply -f k8s/outline/pv-pvc.yaml
kubectl apply -f k8s/cert-manager/certificate.yaml   # reflects vigihome-tls into outline
kubectl apply -f k8s/outline/redis.yaml
kubectl apply -f k8s/outline/netpol-outline.yaml

# Postgres — PIN the chart version (unpinned helm upgrade once took the
# auth namespace down). Match the version Coder runs for consistency:
CHART_VER="$(helm list -n coder -f '^postgres$' -o json | jq -r '.[0].chart' | sed 's/^postgresql-//')"
helm -n outline install postgres bitnami/postgresql --version "$CHART_VER" -f k8s/outline/postgres-values.yaml
kubectl -n outline rollout status statefulset/postgres-postgresql

# Outline app (image tag pinned in deployment.yaml — verify it's the
# current stable release first) + ingress
kubectl apply -f k8s/outline/deployment.yaml
kubectl -n outline rollout status deployment/outline
kubectl apply -f k8s/outline/ingress-vigihome.yaml
```

The `migrate` initContainer runs the sequelize CLI directly
(`node_modules/.bin/sequelize db:migrate --env=production-ssl-disabled`),
idempotently, before the app starts — so schema migrations self-heal on
every deploy/upgrade. It does *not* use `yarn db:migrate`: the image ships
global Yarn 1.22 while the project pins Yarn 4 via Corepack, so `yarn`
aborts. The `--env=production-ssl-disabled` flag is required because the
CLI's `production` env forces TLS while the in-cluster postgres serves
plain TCP (`PGSSLMODE` only affects the running app, not the CLI).

### 5. First login = admin

The **first user to sign in via OIDC becomes the Outline admin** — sign
in as yourself (`nickv`, a `homelab-users` member) first.

## Backup

Restic snapshots `/opt/outline/postgres` nightly under tag
`outline-postgres` (wired in `k8s/backup/backup-cronjob.yaml`). Uploaded
files are not backed up by restic — they live in Storj, already durable.
Redis is ephemeral (nothing to back up).

## Regression checklist (re-run after any chart/image/policy change)

1. `https://docs.vigihome.net` serves over a trusted `vigihome-tls` cert.
2. A `homelab-users` member completes OIDC sign-in and lands in Outline.
3. A non-member (e.g. akadmin) is denied by the Authentik application binding.
4. A document with an embedded image keeps the image after an Outline pod
   restart (served from Storj).
5. Postgres data survives `postgres-postgresql-0` pod deletion (PV).
