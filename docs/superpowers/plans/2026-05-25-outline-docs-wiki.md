# Outline documentation wiki Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy Outline at `https://docs.vigihome.net` as the homelab documentation wiki — raw Outline Deployment + bitnami postgres + ephemeral Redis, Storj S3 for uploads, Authentik OIDC for auth.

**Architecture:** Mirrors the Coder service shape. Outline app is raw manifests (single Deployment); postgres is a separate `bitnami/postgresql` Helm release named `postgres`; Redis is a throwaway raw Deployment. Uploads go to Storj's S3 gateway. Auth is Authentik OIDC only (no local fallback — a documented SPOF exception).

**Tech Stack:** k3s, Helm (bitnami/postgresql), Traefik ingress, cert-manager + reflector (`vigihome-tls`), Authentik OIDC, Storj S3 gateway, Bitwarden CLI, restic.

**Spec:** `docs/superpowers/specs/2026-05-25-outline-docs-wiki-design.md`
**Issue:** #92 (filed as BookStack; repurposed to Outline in Task 18)
**Branch:** `outline-docs` (already created off main; spec committed as `b4c4fcd`)

**Execution note:** This plan interleaves repo-file authoring (Tasks 1–9, committable with no live impact) with operator-driven external setup (Tasks 10–12: Storj console, Authentik UI, Bitwarden) and live cluster applies (Tasks 13–16). Tag every command with the machine it runs on. `kubectl`/`helm` run from the operator's machine with the homelab kube-context; UI/console steps run in a browser; secret material is sourced from Bitwarden at apply time and never committed.

---

## File / state structure

| Path                                                        | Action                                          | Task |
| ----------------------------------------------------------- | ----------------------------------------------- | ---- |
| `k8s/outline/namespace.yaml`                                | Create                                          | 1    |
| `k8s/outline/pv-pvc.yaml`                                   | Create                                          | 1    |
| `k8s/outline/postgres-values.yaml`                          | Create                                          | 2    |
| `k8s/outline/redis.yaml`                                    | Create                                          | 3    |
| `.github/workflows/lint.yml`                                | Modify (add `redis.yaml`, `networkpolicy.yaml`) | 3    |
| `k8s/outline/deployment.yaml`                               | Create (ConfigMap + Deployment + Service)       | 4    |
| `k8s/outline/ingress-vigihome.yaml`                         | Create                                          | 5    |
| `k8s/cert-manager/certificate.yaml`                         | Modify (add `outline` to reflection list)       | 5    |
| `k8s/outline/networkpolicy.yaml`                            | Create                                          | 6    |
| `k8s/outline/secret.example.yaml`                           | Create                                          | 7    |
| `k8s/outline/README.md`                                     | Create                                          | 8    |
| `k8s/backup/backup-cronjob.yaml`                            | Modify (add `outline-postgres`)                 | 9    |
| Storj bucket + S3 gateway creds                             | Create (external)                               | 10   |
| Authentik OIDC provider + application                       | Create (UI)                                     | 11   |
| Bitwarden item `Homelab Outline` + `outline-secrets` Secret | Create                                          | 12   |

---

## Task 1: Namespace + postgres PV/PVC

**Files:**

- Create: `k8s/outline/namespace.yaml`
- Create: `k8s/outline/pv-pvc.yaml`

- [ ] **Step 1: Write `k8s/outline/namespace.yaml`**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: outline
```

- [ ] **Step 2: Write `k8s/outline/pv-pvc.yaml`**

```yaml
# Persistent storage for Outline's PostgreSQL. The bitnami/postgresql
# StatefulSet adopts the PVC named `data-postgres-postgresql-0` when
# installed with release name `postgres` in the `outline` namespace.
#
# Lose this PV -> lose all Outline content (documents, collections,
# users). Uploaded files are NOT here — they live in Storj S3.
# Restic snapshots /opt/outline/postgres nightly under tag
# `outline-postgres` (see k8s/backup/backup-cronjob.yaml).
apiVersion: v1
kind: PersistentVolume
metadata:
  name: outline-postgres-data
spec:
  capacity:
    storage: 8Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: manual
  hostPath:
    path: /opt/outline/postgres
    type: DirectoryOrCreate
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - gandalf
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-postgres-postgresql-0
  namespace: outline
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: manual
  resources:
    requests:
      storage: 8Gi
  volumeName: outline-postgres-data
```

- [ ] **Step 3: Validate locally with kubeconform**

Run (laptop, repo root):

```bash
kubeconform -strict -summary k8s/outline/namespace.yaml k8s/outline/pv-pvc.yaml
```

Expected: `Valid: 3, Invalid: 0` (Namespace + PV + PVC).

- [ ] **Step 4: Commit**

```bash
git add k8s/outline/namespace.yaml k8s/outline/pv-pvc.yaml
git commit -m "Outline: namespace + postgres PV/PVC (#92)"
```

---

## Task 2: Postgres Helm values

**Files:**

- Create: `k8s/outline/postgres-values.yaml`

- [ ] **Step 1: Write `k8s/outline/postgres-values.yaml`**

```yaml
# Helm values for the bitnami/postgresql chart that backs Outline.
#
# Why a separate Helm release: Outline ships no chart and no bundled
# postgres, so we run postgres standalone (same pattern as Coder) and
# point Outline at it via DATABASE_URL.
#
# Release name MUST be `postgres` so the bitnami StatefulSet auto-names
# its PVC `data-postgres-postgresql-0`, matching pv-pvc.yaml.
auth:
  username: outline
  database: outline
  # Bitnami expects keys `password` (user) and `postgres-password`
  # (superuser). Map them to the keys we store in outline-secrets.
  existingSecret: outline-secrets
  secretKeys:
    adminPasswordKey: postgres-superuser-password
    userPasswordKey: postgres-password

primary:
  persistence:
    enabled: true
    storageClass: manual
    existingClaim: data-postgres-postgresql-0
  nodeSelector:
    kubernetes.io/hostname: gandalf
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      memory: 1Gi

# kubelet creates the hostPath dir as root:root, but bitnami postgres
# runs as uid 1001. Without this init container the pod CrashLoopBackOffs
# on "cannot create directory ... Permission denied" on first start.
volumePermissions:
  enabled: true
```

- [ ] **Step 2: Commit**

```bash
git add k8s/outline/postgres-values.yaml
git commit -m "Outline: bitnami postgres values (#92)"
```

---

## Task 3: Redis manifest + lint filter

**Files:**

- Create: `k8s/outline/redis.yaml`
- Modify: `.github/workflows/lint.yml`

- [ ] **Step 1: Write `k8s/outline/redis.yaml`**

```yaml
# Throwaway Redis for Outline (cache + websocket pub/sub only). No
# persistence and no auth on purpose: losing it on restart is harmless,
# and it's only reachable in-cluster (locked down by networkpolicy.yaml).
# Reachable at outline-redis.outline.svc.cluster.local:6379.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: outline-redis
  namespace: outline
  labels:
    app: outline-redis
spec:
  replicas: 1
  selector:
    matchLabels:
      app: outline-redis
  template:
    metadata:
      labels:
        app: outline-redis
    spec:
      nodeSelector:
        kubernetes.io/hostname: gandalf
      containers:
        - name: redis
          image: redis:7-alpine
          args: ["--save", "", "--appendonly", "no"] # disable all persistence
          ports:
            - containerPort: 6379
          resources:
            requests:
              cpu: 25m
              memory: 32Mi
            limits:
              memory: 128Mi
          livenessProbe:
            tcpSocket:
              port: 6379
            initialDelaySeconds: 5
            periodSeconds: 15
---
apiVersion: v1
kind: Service
metadata:
  name: outline-redis
  namespace: outline
spec:
  selector:
    app: outline-redis
  ports:
    - port: 6379
      targetPort: 6379
```

- [ ] **Step 2: Add `redis.yaml` and `networkpolicy.yaml` to the kubeconform filter**

Modify `.github/workflows/lint.yml` — the file-name filter (around line 53-61). Add two `-o -name` clauses after the `ingress-*.yaml` line:

```yaml
-o -name 'ingress-*.yaml' \
-o -name redis.yaml \
-o -name networkpolicy.yaml \
```

(Insert the two new lines preserving the existing trailing `\` continuation and the closing `\)` that follows.)

- [ ] **Step 3: Validate Redis manifest**

Run (laptop):

```bash
kubeconform -strict -summary k8s/outline/redis.yaml
```

Expected: `Valid: 2, Invalid: 0` (Deployment + Service).

- [ ] **Step 4: Commit**

```bash
git add k8s/outline/redis.yaml .github/workflows/lint.yml
git commit -m "Outline: ephemeral Redis + lint filter (#92)"
```

---

## Task 4: Outline ConfigMap + Deployment + Service

**Files:**

- Create: `k8s/outline/deployment.yaml`

Env is split: non-secret values in a ConfigMap (`outline-env`); secret values mapped individually from the `outline-secrets` Secret (created in Task 12) via `secretKeyRef` — NOT `envFrom`, because the Secret also holds postgres keys whose hyphenated names are invalid env vars. The `migrate` initContainer runs `yarn db:migrate` (idempotent) before the app starts.

- [ ] **Step 1: Write `k8s/outline/deployment.yaml`**

```yaml
# Outline app: ConfigMap (non-secret env) + Deployment + Service.
# Raw manifests — Outline has no official chart and the app is a single
# Deployment, so per the chart-vs-raw rule we stay raw.
#
# Postgres: bitnami release `postgres` (postgres-values.yaml).
# Redis: redis.yaml. Uploads: Storj S3 (FILE_STORAGE=s3).
# Auth: Authentik OIDC only — no local fallback (SPOF exception, see README).
#
# IMAGE TAG: pinned. Verify the current stable release at
# https://github.com/outline/outline/releases and set it before first
# apply; never use a moving `latest` tag (per the helm/image-pin lesson).
apiVersion: v1
kind: ConfigMap
metadata:
  name: outline-env
  namespace: outline
data:
  NODE_ENV: "production"
  PORT: "3000"
  URL: "https://docs.vigihome.net"
  # Cluster-internal postgres has no TLS; Outline must not insist on it.
  PGSSLMODE: "disable"
  REDIS_URL: "redis://outline-redis.outline.svc.cluster.local:6379"
  # ---- Storj S3 (file uploads) ----
  FILE_STORAGE: "s3"
  AWS_REGION: "us-east-1"
  AWS_S3_UPLOAD_BUCKET_URL: "https://gateway.storjshare.io"
  AWS_S3_UPLOAD_BUCKET_NAME: "outline-uploads"
  AWS_S3_FORCE_PATH_STYLE: "true"
  AWS_S3_ACL: "private"
  # ---- Authentik OIDC ----
  OIDC_AUTH_URI: "https://authentik.vigihome.net/application/o/authorize/"
  OIDC_TOKEN_URI: "https://authentik.vigihome.net/application/o/token/"
  OIDC_USERINFO_URI: "https://authentik.vigihome.net/application/o/userinfo/"
  OIDC_USERNAME_CLAIM: "preferred_username"
  OIDC_DISPLAY_NAME: "Authentik"
  OIDC_SCOPES: "openid profile email"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: outline
  namespace: outline
  labels:
    app: outline
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: outline
  template:
    metadata:
      labels:
        app: outline
    spec:
      nodeSelector:
        kubernetes.io/hostname: gandalf
      initContainers:
        # Idempotent schema migration — self-heals on every deploy/upgrade.
        - name: migrate
          image: outlinewiki/outline:0.85.1
          command: ["yarn", "db:migrate"]
          envFrom:
            - configMapRef:
                name: outline-env
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: outline-secrets
                  key: database-url
            - name: SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: outline-secrets
                  key: secret-key
            - name: UTILS_SECRET
              valueFrom:
                secretKeyRef:
                  name: outline-secrets
                  key: utils-secret
      containers:
        - name: outline
          image: outlinewiki/outline:0.85.1
          ports:
            - containerPort: 3000
          envFrom:
            - configMapRef:
                name: outline-env
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: outline-secrets
                  key: database-url
            - name: SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: outline-secrets
                  key: secret-key
            - name: UTILS_SECRET
              valueFrom:
                secretKeyRef:
                  name: outline-secrets
                  key: utils-secret
            - name: AWS_ACCESS_KEY_ID
              valueFrom:
                secretKeyRef:
                  name: outline-secrets
                  key: s3-access-key
            - name: AWS_SECRET_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: outline-secrets
                  key: s3-secret-key
            - name: OIDC_CLIENT_ID
              valueFrom:
                secretKeyRef:
                  name: outline-secrets
                  key: oidc-client-id
            - name: OIDC_CLIENT_SECRET
              valueFrom:
                secretKeyRef:
                  name: outline-secrets
                  key: oidc-client-secret
          resources:
            requests:
              cpu: 250m
              memory: 512Mi
            limits:
              memory: 1Gi
          readinessProbe:
            httpGet:
              path: /_health
              port: 3000
            initialDelaySeconds: 20
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /_health
              port: 3000
            initialDelaySeconds: 60
            periodSeconds: 30
---
apiVersion: v1
kind: Service
metadata:
  name: outline
  namespace: outline
spec:
  selector:
    app: outline
  ports:
    - port: 3000
      targetPort: 3000
```

- [ ] **Step 2: Validate**

Run (laptop):

```bash
kubeconform -strict -summary k8s/outline/deployment.yaml
```

Expected: `Valid: 3, Invalid: 0` (ConfigMap + Deployment + Service).

- [ ] **Step 3: Commit**

```bash
git add k8s/outline/deployment.yaml
git commit -m "Outline: app Deployment, Service, env ConfigMap (#92)"
```

---

## Task 5: Ingress + TLS reflection

**Files:**

- Create: `k8s/outline/ingress-vigihome.yaml`
- Modify: `k8s/cert-manager/certificate.yaml`

- [ ] **Step 1: Write `k8s/outline/ingress-vigihome.yaml`**

```yaml
# HTTPS Ingress for Outline at https://docs.vigihome.net.
# Traefik terminates TLS on websecure (443) and proxies plain HTTP to
# the outline Service on 3000. vigihome-tls is reflected into the
# outline namespace by emberstack/reflector (see certificate.yaml).
# DNS: the Pi-hole *.vigihome.net wildcard already resolves this host.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: outline-vigihome
  namespace: outline
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
spec:
  ingressClassName: traefik
  tls:
    - hosts:
        - docs.vigihome.net
      secretName: vigihome-tls
  rules:
    - host: docs.vigihome.net
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: outline
                port:
                  number: 3000
```

- [ ] **Step 2: Add `outline` to the reflection list in `k8s/cert-manager/certificate.yaml`**

Find the line (≈41):

```yaml
reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: "homepage,networking,auth,monitoring,media,syncthing,coder,home-assistant"
```

Append `,outline`:

```yaml
reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: "homepage,networking,auth,monitoring,media,syncthing,coder,home-assistant,outline"
```

- [ ] **Step 3: Validate**

Run (laptop):

```bash
kubeconform -strict -summary k8s/outline/ingress-vigihome.yaml k8s/cert-manager/certificate.yaml
```

Expected: `Valid: 2, Invalid: 0` (Ingress + Certificate).

- [ ] **Step 4: Commit**

```bash
git add k8s/outline/ingress-vigihome.yaml k8s/cert-manager/certificate.yaml
git commit -m "Outline: vigihome Ingress + TLS reflection (#92)"
```

---

## Task 6: NetworkPolicy

**Files:**

- Create: `k8s/outline/networkpolicy.yaml`

Three policies: Outline app reachable on 3000 only from Traefik + same-namespace; postgres (5432) and redis (6379) reachable only from same-namespace.

- [ ] **Step 1: Write `k8s/outline/networkpolicy.yaml`**

```yaml
# Lateral-movement hardening for the outline namespace (matches the
# Authentik posture). k3s's bundled kube-router netpol controller still
# whitelists local-node->Pod traffic, so kubelet probes keep working.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: outline-app-ingress
  namespace: outline
spec:
  podSelector:
    matchLabels:
      app: outline
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              app.kubernetes.io/name: traefik
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: outline
      ports:
        - port: 3000
          protocol: TCP
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: outline-postgres-ingress
  namespace: outline
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: postgresql
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: outline
      ports:
        - port: 5432
          protocol: TCP
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: outline-redis-ingress
  namespace: outline
spec:
  podSelector:
    matchLabels:
      app: outline-redis
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: outline
      ports:
        - port: 6379
          protocol: TCP
```

- [ ] **Step 2: Validate**

Run (laptop):

```bash
kubeconform -strict -summary k8s/outline/networkpolicy.yaml
```

Expected: `Valid: 3, Invalid: 0`.

- [ ] **Step 3: Commit**

```bash
git add k8s/outline/networkpolicy.yaml
git commit -m "Outline: NetworkPolicies for app/postgres/redis (#92)"
```

---

## Task 7: secret.example.yaml

**Files:**

- Create: `k8s/outline/secret.example.yaml`

- [ ] **Step 1: Write `k8s/outline/secret.example.yaml`**

```yaml
# TEMPLATE ONLY — documents the keys in the `outline-secrets` Secret.
# Never applied; the real Secret is created via `kubectl create secret
# generic` sourcing from Bitwarden item `Homelab Outline` + generated
# values (see README.md). Secrets never enter this public repo.
apiVersion: v1
kind: Secret
metadata:
  name: outline-secrets
  namespace: outline
type: Opaque
stringData:
  # Generated: openssl rand -hex 32 (two distinct values)
  secret-key: REPLACE_WITH_SECRET_KEY
  utils-secret: REPLACE_WITH_UTILS_SECRET
  # Postgres credentials (generated). Consumed by the bitnami chart via
  # existingSecret, and folded into database-url below.
  postgres-password: REPLACE_WITH_PG_PASSWORD
  postgres-superuser-password: REPLACE_WITH_PG_SUPERUSER_PASSWORD
  # Full connection string Outline reads (embeds postgres-password):
  # postgres://outline:<postgres-password>@postgres-postgresql.outline.svc.cluster.local:5432/outline
  database-url: REPLACE_WITH_DATABASE_URL
  # Storj S3 gateway credentials (Task 10)
  s3-access-key: REPLACE_WITH_STORJ_ACCESS_KEY
  s3-secret-key: REPLACE_WITH_STORJ_SECRET_KEY
  # Authentik OIDC provider credentials (Task 11)
  oidc-client-id: REPLACE_WITH_OIDC_CLIENT_ID
  oidc-client-secret: REPLACE_WITH_OIDC_CLIENT_SECRET
```

- [ ] **Step 2: Commit**

```bash
git add k8s/outline/secret.example.yaml
git commit -m "Outline: secret.example.yaml (key shape) (#92)"
```

---

## Task 8: README runbook

**Files:**

- Create: `k8s/outline/README.md`

- [ ] **Step 1: Write `k8s/outline/README.md`**

Write a runbook covering, in order: (1) the file map; (2) the SPOF exception (OIDC-only, no local fallback — break-glass runbooks stay in git, never solely in Outline); (3) Storj bucket + S3 gateway credential creation (Task 10); (4) the Authentik OIDC provider/application setup with redirect URI `https://docs.vigihome.net/auth/oidc.callback` and `homelab-users` binding (Task 11); (5) generating `secret-key`/`utils-secret`/postgres passwords and the exact `kubectl create secret generic outline-secrets` invocation sourcing from Bitwarden item `Homelab Outline` (Task 12); (6) the apply order (Task 13–15) with `helm install postgres --version` pinned; (7) "first OIDC user becomes admin"; (8) the as-built regression checklist (the Task 16 acceptance tests); (9) backup note (postgres under restic tag `outline-postgres`; uploads in Storj). Mirror the depth/structure of `k8s/coder/README.md`.

- [ ] **Step 2: Commit**

```bash
git add k8s/outline/README.md
git commit -m "Outline: setup runbook + SPOF exception note (#92)"
```

---

## Task 9: Backup wiring

**Files:**

- Modify: `k8s/backup/backup-cronjob.yaml`

- [ ] **Step 1: Add the restic backup block**

In `k8s/backup/backup-cronjob.yaml`, after the Coder postgres `restic backup` block (≈line 163), add:

```bash
                  echo "=== Outline postgres ==="
                  restic backup \
                    --tag outline-postgres \
                    /backup/outline-postgres
```

- [ ] **Step 2: Add the volumeMount**

After the `coder-postgres` volumeMount (≈line 237):

```yaml
- name: outline-postgres
  mountPath: /backup/outline-postgres
```

- [ ] **Step 3: Add the volume**

After the `coder-postgres` volume (≈line 265):

```yaml
- name: outline-postgres
  hostPath:
    # DirectoryOrCreate so the backup doesn't fail if this
    # change lands before the Outline deploy creates the dir.
    path: /opt/outline/postgres
    type: DirectoryOrCreate
```

- [ ] **Step 4: Update the header comment**

In the top comment block listing backed-up paths (≈line 9), add after the coder line:

```
#   - /opt/outline/postgres                         (Outline PostgreSQL data; uploads live in Storj, not here)
```

- [ ] **Step 5: Validate**

Run (laptop):

```bash
kubeconform -strict -summary k8s/backup/backup-cronjob.yaml
```

Expected: `Valid: 1, Invalid: 0`.

- [ ] **Step 6: Commit**

```bash
git add k8s/backup/backup-cronjob.yaml
git commit -m "Outline: add postgres to nightly restic backup (#92)"
```

---

## Task 10: Create Storj bucket + S3 gateway credentials (operator, external)

- [ ] **Step 1: Create the bucket**

In the Storj console (or `uplink mb sj://outline-uploads`), create a bucket named **`outline-uploads`**.

- [ ] **Step 2: Create S3 gateway credentials scoped to that bucket**

In the Storj console → Access → create an **S3 Gateway** access key restricted to the `outline-uploads` bucket. This yields an Access Key ID, Secret Access Key, and the gateway endpoint (`https://gateway.storjshare.io`).

- [ ] **Step 3: Stash the credentials in Bitwarden**

Add fields `s3-access-key` and `s3-secret-key` to Bitwarden item **`Homelab Outline`** (create the item if it doesn't exist).

- [ ] **Step 4: Verify (optional, laptop)**

```bash
AWS_ACCESS_KEY_ID=<key> AWS_SECRET_ACCESS_KEY=<secret> \
  aws --endpoint-url https://gateway.storjshare.io s3 ls s3://outline-uploads
```

Expected: no error (empty listing is fine).

---

## Task 11: Create the Authentik OIDC provider + application (operator, UI)

- [ ] **Step 1: Create an OAuth2/OpenID provider**

Authentik admin → Applications → Providers → Create → **OAuth2/OpenID Provider**:

- Name: `outline`
- Authorization flow: the default implicit-consent authorization flow
- Client type: Confidential
- Redirect URIs (Strict): `https://docs.vigihome.net/auth/oidc.callback` (byte-exact)
- Signing Key: the configured signing certificate
- Scopes: openid, profile, email
- Copy the generated **Client ID** and **Client Secret**.

- [ ] **Step 2: Create the application**

Applications → Applications → Create:

- Name: `Outline`, Slug: `outline` (the slug appears in the issuer URL)
- Provider: `outline` (from Step 1)

- [ ] **Step 3: Restrict to `homelab-users`**

On the `Outline` application → Policy / Group / User Bindings → bind the **`homelab-users` group** (matching Coder/HA, audit finding 4d). Non-members get no access.

- [ ] **Step 4: Stash OIDC creds in Bitwarden**

Add fields `oidc-client-id` and `oidc-client-secret` to Bitwarden item **`Homelab Outline`**.

- [ ] **Step 5: Verify the discovery endpoint (laptop)**

```bash
curl -sSf https://authentik.vigihome.net/application/o/outline/.well-known/openid-configuration | jq '.issuer, .authorization_endpoint, .token_endpoint, .userinfo_endpoint'
```

Expected: the four URLs, with `authorization_endpoint`/`token_endpoint`/`userinfo_endpoint` matching the `OIDC_*_URI` values in `outline-env`.

---

## Task 12: Generate secrets + create the `outline-secrets` Secret (operator)

- [ ] **Step 1: Generate the app secrets and postgres passwords (laptop)**

```bash
SECRET_KEY="$(openssl rand -hex 32)"
UTILS_SECRET="$(openssl rand -hex 32)"
PG_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=')"
PG_SUPER_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=')"
```

Record `SECRET_KEY`, `UTILS_SECRET`, `PG_PASSWORD`, `PG_SUPER_PASSWORD` as fields `secret-key`, `utils-secret`, `postgres-password`, `postgres-superuser-password` in Bitwarden item **`Homelab Outline`**.

- [ ] **Step 2: Pull S3 + OIDC creds from Bitwarden and assemble the Secret (laptop)**

```bash
BW_SESSION="$(bw unlock --raw)"; export BW_SESSION
get() { bw get item 'Homelab Outline' | jq -r ".fields[] | select(.name==\"$1\") | .value"; }
S3_KEY="$(get s3-access-key)";      S3_SECRET="$(get s3-secret-key)"
OIDC_ID="$(get oidc-client-id)";    OIDC_SECRET="$(get oidc-client-secret)"
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

- [ ] **Step 3: Verify the Secret has all nine keys**

```bash
kubectl -n outline get secret outline-secrets -o jsonpath='{.data}' | jq 'keys'
```

Expected: `["database-url","oidc-client-id","oidc-client-secret","postgres-password","postgres-superuser-password","s3-access-key","s3-secret-key","secret-key","utils-secret"]`.

---

## Task 13: Apply base resources + postgres + redis (live)

- [ ] **Step 1: Pull latest and apply namespace, PV/PVC, reflection, redis, netpol (laptop)**

```bash
git checkout outline-docs && git pull
kubectl apply -f k8s/outline/namespace.yaml
kubectl apply -f k8s/outline/pv-pvc.yaml
kubectl apply -f k8s/cert-manager/certificate.yaml   # adds outline to reflection
kubectl apply -f k8s/outline/redis.yaml
kubectl apply -f k8s/outline/networkpolicy.yaml
```

- [ ] **Step 2: Confirm vigihome-tls reflected into the namespace**

```bash
kubectl -n outline get secret vigihome-tls
```

Expected: the secret exists (reflector copies it within ~5s).

- [ ] **Step 3: Install postgres (pinned version)**

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami 2>/dev/null; helm repo update bitnami
# Pin to the chart version currently used by Coder for consistency:
CHART_VER="$(helm list -n coder -f '^postgres$' -o json | jq -r '.[0].chart' | sed 's/^postgresql-//')"
echo "Pinning postgresql chart to $CHART_VER"
helm -n outline install postgres bitnami/postgresql --version "$CHART_VER" -f k8s/outline/postgres-values.yaml
kubectl -n outline rollout status statefulset/postgres-postgresql --timeout=180s
```

Expected: `statefulset rolling update complete`, pod `postgres-postgresql-0` is `1/1`.

- [ ] **Step 4: Confirm redis is up**

```bash
kubectl -n outline rollout status deployment/outline-redis
```

Expected: `deployment "outline-redis" successfully rolled out`.

---

## Task 14: Apply Outline app + run migrations (live)

- [ ] **Step 1: Pin the image tag, then apply**

Confirm the `outlinewiki/outline:<tag>` in `deployment.yaml` is the current stable release (https://github.com/outline/outline/releases). Then (laptop):

```bash
kubectl apply -f k8s/outline/deployment.yaml
```

- [ ] **Step 2: Watch the migrate initContainer succeed, then the app**

```bash
kubectl -n outline get pods -w
```

Expected: the `outline-…` pod shows `Init:0/1` → the migrate container completes → app container becomes `1/1 Running`. If it sticks in `Init`, inspect:

```bash
kubectl -n outline logs deploy/outline -c migrate
```

Expected migrate log: sequelize migrations run to completion with no error.

- [ ] **Step 3: Apply the Ingress and confirm rollout**

```bash
kubectl apply -f k8s/outline/ingress-vigihome.yaml
kubectl -n outline rollout status deployment/outline --timeout=180s
```

Expected: `deployment "outline" successfully rolled out`.

---

## Task 15: Apply backup wiring (live)

- [ ] **Step 1: Apply the updated CronJob (laptop)**

```bash
kubectl apply -f k8s/backup/backup-cronjob.yaml
```

- [ ] **Step 2: Trigger an ad-hoc run and confirm the tag**

```bash
kubectl -n backup create job --from=cronjob/restic-backup outline-backup-test
kubectl -n backup wait --for=condition=complete job/outline-backup-test --timeout=300s
kubectl -n backup logs job/outline-backup-test | grep -A2 'Outline postgres'
```

Expected: the `=== Outline postgres ===` block runs and restic reports a snapshot saved for tag `outline-postgres`. Clean up: `kubectl -n backup delete job outline-backup-test`.

---

## Task 16: End-to-end acceptance tests (live)

Maps to the spec's acceptance criteria.

- [ ] **Step 1: TLS + reachability**

```bash
curl -sSI https://docs.vigihome.net/ | head -1
```

Expected: `HTTP/2 200` (or a redirect to the OIDC login), served with a trusted `vigihome-tls` cert (no cert warning in a browser).

- [ ] **Step 2: OIDC login as admin (browser)**

Open `https://docs.vigihome.net/` → click sign in with Authentik → authenticate as a `homelab-users` member (`nickv`). Expected: lands in Outline; this first user is the admin (Settings → Members shows Admin role).

- [ ] **Step 3: Non-member denied (browser)**

In a private window, attempt sign-in as akadmin (not in `homelab-users`). Expected: Authentik denies the application (no Outline access).

- [ ] **Step 4: Upload persists to Storj**

In Outline, create a document and paste/insert an image. Then restart the app and confirm the image still renders:

```bash
kubectl -n outline rollout restart deployment/outline
kubectl -n outline rollout status deployment/outline
```

Expected: after restart, the document's image still loads (served from Storj). Confirm the object exists:

```bash
AWS_ACCESS_KEY_ID=<key> AWS_SECRET_ACCESS_KEY=<secret> \
  aws --endpoint-url https://gateway.storjshare.io s3 ls --recursive s3://outline-uploads | head
```

Expected: at least one uploaded object listed.

- [ ] **Step 5: Postgres data survives pod restart**

```bash
kubectl -n outline delete pod postgres-postgresql-0
kubectl -n outline rollout status statefulset/postgres-postgresql
```

Expected: pod returns `1/1`; the document created in Step 4 is still present in Outline (data persisted on the PV).

---

## Task 17: Finalize — repurpose #92, open + merge PR

- [ ] **Step 1: Repurpose issue #92 to Outline**

```bash
gh issue edit 92 --title "Service: Outline — self-hosted documentation wiki"
gh issue comment 92 --body "Switched from BookStack to Outline (decided 2026-05-20). Implemented per docs/superpowers/specs/2026-05-25-outline-docs-wiki-design.md."
```

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin outline-docs
gh pr create --base main --head outline-docs \
  --title "Outline: documentation wiki at docs.vigihome.net (#92)" \
  --body "Deploys Outline (raw app + bitnami postgres + ephemeral Redis), Storj S3 uploads, Authentik OIDC (no local fallback — documented SPOF exception). Closes #92. See spec + k8s/outline/README.md. Verified: TLS, OIDC admin login, non-member denied, image upload survives restart (Storj), postgres survives pod restart, restic outline-postgres tag."
```

- [ ] **Step 3: Confirm mergeable, then squash-merge**

```bash
gh pr checks; gh pr view --json mergeable,mergeStateStatus
gh pr merge --squash
```

Expected: PR merged, #92 closed.

---

## Self-review notes

- **Spec coverage:** S3 uploads (Tasks 4/10), OIDC + homelab-users (Tasks 4/11, criteria 2-3), Redis ephemeral (Task 3), postgres + PV (Tasks 1-2), backup (Tasks 9/15, criterion 5), TLS/ingress (Task 5, criterion 1), NetworkPolicy (Task 6), SPOF exception (Task 8 README), version pinning (Tasks 13/14), migrations (Task 4 initContainer), first-user-admin (criterion 2), upload persistence (criterion 4). All spec sections mapped.
- **Pinning:** postgres chart pinned to Coder's installed version; Outline image pinned with a verify-before-apply step.
- **Secret hygiene:** only `secret.example.yaml` (placeholders) is committed; the real Secret is built from Bitwarden + generated values at apply time.
