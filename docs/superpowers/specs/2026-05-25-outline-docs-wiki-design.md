# Outline documentation wiki: deployment design

**Date:** 2026-05-25
**Issue:** #92 (filed as "BookStack"; repurposed to Outline as part of this work)
**Status:** Design approved — awaiting implementation plan

## Problem

The homelab has no narrative documentation platform. Operational
knowledge about the `nickvigilante/homelab` k8s repo and the sibling
`infrastructure` (OpenTofu) repo lives only in per-repo READMEs and in
the operator's head. The goal is a browsable, editable reference so the
operator can learn and understand the system in depth — "what each
component does and how it fits together" — separate from the
break-glass runbooks that must stay in git.

**Outline** is the chosen platform (switched from BookStack on
2026-05-20; not revisiting). FOSS, BSL-licensed, self-hostable.

## Scope decisions (settled during brainstorming)

| Decision | Choice | Why |
|---|---|---|
| Deployment shape | Raw Outline `Deployment` + `bitnami/postgresql` release + raw ephemeral Redis | Outline app is a single Deployment and only community Outline charts exist → chart-vs-raw rule says raw. Postgres mirrors the Coder precedent exactly. |
| File/upload storage | Storj S3 (gateway) | Reuses existing object storage; no new PV, no extra restic path; offsite-durable. Outline has first-class S3 support. |
| Hostname | `docs.vigihome.net` | Reads as published reference material (the stated purpose). |
| SMTP / email | **Deferred** | Only needed for invites/notifications/magic-link. Login is via Authentik OIDC and there is one user. Trivial to add later via the existing `smtp-relay` Secret. |
| Auth | Authentik OIDC only (no local fallback) | Outline has no native password auth. See "SPOF exception" below. |

## Architecture

Namespace `outline`, three workloads:

1. **Outline app** — raw `Deployment` + `Service`, image `outlinewiki/outline:<pinned tag>`, container port 3000. An **initContainer runs `yarn db:migrate`** (idempotent) before the app container so schema migrations self-heal on every deploy/upgrade. Exposed at `https://docs.vigihome.net` via Traefik.
2. **Postgres** — separate `bitnami/postgresql` Helm release named `postgres` (release name fixed so the StatefulSet auto-names its PVC `data-postgres-postgresql-0`). PV `outline-postgres-data` → hostPath `/opt/outline/postgres` on gandalf. Reads credentials from `outline-secrets` via `existingSecret` (Coder pattern). Reachable at `postgres-postgresql.outline.svc.cluster.local:5432`.
3. **Redis** — tiny raw `Deployment` + `Service`, `redis:7-alpine`, single replica, **no persistence, no auth**. Outline uses it only for cache + websocket pub/sub; loss on restart is harmless. Reachable at `outline-redis.outline.svc.cluster.local:6379`.

### File layout (`k8s/outline/`)

| File | Purpose |
|------|---------|
| `namespace.yaml` | Namespace `outline` |
| `pv-pvc.yaml` | PV `outline-postgres-data` (hostPath `/opt/outline/postgres`, gandalf) + PVC `data-postgres-postgresql-0` |
| `postgres-values.yaml` | bitnami/postgresql values (release `postgres`) |
| `deployment.yaml` | Outline `Deployment` + `Service` (+ migrate initContainer) |
| `redis.yaml` | Redis `Deployment` + `Service` (added to the kubeconform lint filter) |
| `ingress-vigihome.yaml` | Ingress for `docs.vigihome.net` (websecure, `vigihome-tls`) |
| `secret.example.yaml` | Documents `outline-secrets` keys (`REPLACE_WITH_*` placeholders) |
| `README.md` | One-time setup runbook + ops notes |

## Configuration (env)

**Core:** `URL=https://docs.vigihome.net`, `PORT=3000`, `NODE_ENV=production`, `SECRET_KEY` (generated `openssl rand -hex 32`), `UTILS_SECRET` (generated, distinct).

**Database / Redis:** `DATABASE_URL=postgres://outline:<pw>@postgres-postgresql.outline.svc.cluster.local:5432/outline`, `PGSSLMODE=disable` (cluster-internal), `REDIS_URL=redis://outline-redis.outline.svc.cluster.local:6379`.

**Storj S3:** `FILE_STORAGE=s3`, `AWS_S3_UPLOAD_BUCKET_URL=https://gateway.storjshare.io` (Storj S3 gateway endpoint), `AWS_S3_UPLOAD_BUCKET_NAME=<bucket>`, `AWS_S3_FORCE_PATH_STYLE=true`, `AWS_S3_ACL=private`, `AWS_REGION=us-east-1` (Storj accepts any region string), `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (Storj S3 gateway credentials). The Storj bucket is created out of band (Storj console or `uplink`/`rclone`). Exact env-var spellings verified against Outline's `.env.sample` at implementation time.

**OIDC (generic) → Authentik:**
- `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`
- `OIDC_AUTH_URI=https://authentik.vigihome.net/application/o/authorize/`
- `OIDC_TOKEN_URI=https://authentik.vigihome.net/application/o/token/`
- `OIDC_USERINFO_URI=https://authentik.vigihome.net/application/o/userinfo/`
- `OIDC_USERNAME_CLAIM=preferred_username`, `OIDC_DISPLAY_NAME=Authentik`, `OIDC_SCOPES="openid profile email"`

**Authentik side:** an OAuth2/OIDC provider + application (slug `outline`), redirect URI `https://docs.vigihome.net/auth/oidc.callback` (byte-exact; Strict mode), signing key set, **application bound to the `homelab-users` group** (per audit finding 4d, matching Coder/HA). The `iss` claim derives from the `authentik.vigihome.net` Host header — unchanged, so no downstream `iss` impact.

**First login:** the first user to sign in via OIDC becomes the Outline admin — that is the operator (`nickv`).

## Secrets

A single `outline-secrets` Secret in the `outline` namespace, created via
`kubectl create secret generic` sourced from Bitwarden item **`Homelab
Outline`** plus generated values. Keys:

| Key | Source |
|-----|--------|
| `secret-key` | generated `openssl rand -hex 32` |
| `utils-secret` | generated `openssl rand -hex 32` |
| `postgres-password` | generated; the `outline` DB user |
| `postgres-superuser-password` | generated; the `postgres` superuser |
| `oidc-client-id` / `oidc-client-secret` | from the Authentik provider |
| `s3-access-key` / `s3-secret-key` | Storj S3 gateway credentials |

`postgres-values.yaml` maps `existingSecret: outline-secrets` with
`secretKeys.{adminPasswordKey: postgres-superuser-password,
userPasswordKey: postgres-password}`. Secrets never enter the repo
(public, gitleaks pre-commit); `secret.example.yaml` documents shape only.

## Networking & TLS

- Add `outline` to `reflection-auto-namespaces` in
  `k8s/cert-manager/certificate.yaml` → reflector mirrors `vigihome-tls`
  into the namespace in < 5s. Ingress references `secretName:
  vigihome-tls` on the `websecure` entrypoint.
- DNS: nothing per-service — the Pi-hole `*.vigihome.net` wildcard
  already resolves `docs.vigihome.net` to gandalf on LAN + tailnet.
- **NetworkPolicy** (matches the Authentik hardening posture): postgres
  and redis accept ingress only from same-namespace pods; the Outline
  pod accepts ingress on 3000 only from Traefik + same-namespace. Closes
  lateral access to the DB/cache from a compromised neighbor.

## Persistence & backup

- Postgres data: hostPath `/opt/outline/postgres`, added to
  `k8s/backup/backup-cronjob.yaml` (new volume + mount + `restic backup
  --tag outline-postgres`), following the raw-hostPath-copy pattern
  already used for the Authentik and Coder postgres dirs. (Raw copy of a
  live postgres dir is crash-consistent; accepted repo-wide trade-off.)
- Uploads: in Storj, already durable — not backed up by restic.
- Redis: ephemeral, nothing to back up.

## Version pinning

Pin both the Outline image tag and the postgres chart `--version` to the
installed versions. Unpinned `helm upgrade` once took the auth namespace
down (2026-05-23); never repeat it.

## SPOF exception (explicit)

CLAUDE.md's SPOF discipline requires every Authentik-fronted service to
keep a local-fallback credential. **Outline cannot** — it is OIDC-only.
Outline is therefore a documented **exception** to that rule. Mitigation:
Outline holds only narrative/reference documentation; **all break-glass
runbooks (Authentik recovery, restic restore, etc.) stay in git
READMEs**, which are reachable when Authentik is down. This is recorded
in the Outline README and noted against the CLAUDE.md SPOF section. The
"don't put the fire-escape plan inside the burning building" rule.

## Acceptance criteria

1. `https://docs.vigihome.net` serves Outline over a trusted
   `vigihome-tls` cert.
2. Sign-in redirects to Authentik; a `homelab-users` member completes
   OIDC and lands in Outline; the first such user is admin.
3. A non-`homelab-users` account is denied by the Authentik application
   policy (no Outline access).
4. Creating a document with an embedded image succeeds and the image
   persists to Storj (survives an Outline pod restart).
5. Postgres data survives a pod restart (PV) and appears under the
   `outline-postgres` restic tag after a backup run.
6. `helm`/image versions are pinned; `kubectl`/`helm` apply is
   documented step-by-step in the README for a from-scratch rebuild.

## Things deliberately not done

- **No SMTP.** Deferred (see scope table). Add later via `smtp-relay`.
- **No local-auth fallback.** Impossible in Outline; accepted as the
  SPOF exception above rather than worked around.
- **No Redis persistence / HA.** It's a throwaway cache; persisting it
  adds a PV and backup wiring for zero durability value.
- **No community Helm chart.** Rejected per the chart-vs-raw rule.
