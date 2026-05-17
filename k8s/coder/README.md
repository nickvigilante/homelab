# coder

Self-hosted browser dev environments. Coder spins up workspaces as
Kubernetes pods inside this cluster; templates are written in
Terraform/OpenTofu (which pairs well with the existing IaC setup).

## Layout

- `namespace.yaml` — `coder` namespace
- `pv-pvc.yaml` — PV `coder-postgres-data` → hostPath `/opt/coder/postgres` (gandalf), matching PVC named for what the bitnami postgres StatefulSet expects (`data-postgres-postgresql-0`)
- `postgres-values.yaml` — Helm values for the bitnami/postgresql backing DB
- `values.yaml` — Helm values for the official `coder-v2/coder` chart
- `secret.example.yaml` — template documenting the keys required in the `coder-secrets` Secret. Never apply this directly — the real Secret is created from out-of-repo material (see below).

## SPOF discipline

Coder logins go through Authentik when OIDC is configured. If Authentik
is down, OIDC login breaks. Coder still supports local-password auth
*alongside* OIDC — keep a local-only admin account so you can recover
when Authentik is unavailable. See step 8 below.

## Two Helm releases, one namespace

Unlike Authentik (whose chart bundles postgres as a subchart), the
official Coder chart does *not* bundle postgres. So we run two Helm
releases in the `coder` namespace:

- `postgres` — bitnami/postgresql, backing DB
- `coder` — Coder server

The cluster-internal DB URL is
`postgres-postgresql.coder.svc.cluster.local:5432`.

## Workspace storage

Workspace home directories are provisioned dynamically by the
Kubernetes workspace template via the `local-path` storage class
(shipped enabled in k3s). Those PVCs live under
`/var/lib/rancher/k3s/storage/` on gandalf — separate from the postgres
PV here. Restic backs up the postgres data; workspace homes are
considered ephemeral (rebuild from the git repo + dotfiles inside the
workspace).

## One-time setup

1. **Save secrets to Bitwarden.** Create a Bitwarden item named `Homelab
   Coder` with four custom fields:
   - `postgres-password` — password for the `coder` postgres user
   - `postgres-superuser-password` — password for the `postgres` superuser
   - `oidc-client-id` — filled in after step 4
   - `oidc-client-secret` — filled in after step 4

   Generate the postgres passwords now with
   `openssl rand -base64 24 | tr -d '/+='`. Leave the OIDC fields empty
   until step 4.

2. **Apply the namespace + PV/PVC**:

   ```bash
   kubectl apply -f namespace.yaml -f pv-pvc.yaml
   ```

3. **No Pi-hole DNS edits needed.** `coder.vigihome.net` is resolved
   by the wildcard `address=/vigihome.net/...` directive in Pi-hole's
   `misc.dnsmasq_lines` (see `k8s/pihole/values.yaml`).

4. **Create the Authentik OIDC provider.** In the Authentik UI:

   - **Applications → Providers → Create → OAuth2/OpenID Provider**
     - Name: `coder`
     - Authorization flow: `default-provider-authorization-explicit-consent`
     - Client type: Confidential
     - Redirect URIs:
       - `https://coder.vigihome.net/api/v2/users/oidc/callback`
     - Signing key: leave default
   - Copy the **Client ID** and **Client Secret** into the Bitwarden
     item from step 1.
   - **Applications → Applications → Create**
     - Name: `Coder`
     - Slug: `coder` (must match the issuer URL in values.yaml)
     - Provider: `coder`
     - Launch URL: `https://coder.vigihome.net`

5. **Create the Secret.** With Bitwarden CLI session active:

   > **Gotcha:** if the Bitwarden item or its fields were created/edited
   > earlier in the same shell session, `bw get item` may still see the
   > stale local cache and return `Not found` (or empty field values).
   > Run `bw sync` before the block below if the item is brand new or
   > was just modified.

   ```bash
   export BW_SESSION="$(bw unlock --raw)"
   bw sync
   PG_PASSWORD="$(bw get item 'Homelab Coder' | jq -r '.fields[] | select(.name=="postgres-password") | .value')"
   PG_SUPER_PASSWORD="$(bw get item 'Homelab Coder' | jq -r '.fields[] | select(.name=="postgres-superuser-password") | .value')"
   OIDC_ID="$(bw get item 'Homelab Coder' | jq -r '.fields[] | select(.name=="oidc-client-id") | .value')"
   OIDC_SECRET="$(bw get item 'Homelab Coder' | jq -r '.fields[] | select(.name=="oidc-client-secret") | .value')"
   PG_URL="postgres://coder:${PG_PASSWORD}@postgres-postgresql.coder.svc.cluster.local:5432/coder?sslmode=disable"

   kubectl -n coder create secret generic coder-secrets \
     --from-literal=postgres-password="$PG_PASSWORD" \
     --from-literal=postgres-superuser-password="$PG_SUPER_PASSWORD" \
     --from-literal=pg-connection-url="$PG_URL" \
     --from-literal=oidc-client-id="$OIDC_ID" \
     --from-literal=oidc-client-secret="$OIDC_SECRET"

   unset BW_SESSION PG_PASSWORD PG_SUPER_PASSWORD OIDC_ID OIDC_SECRET PG_URL
   ```

6. **Install postgres**:

   ```bash
   helm repo add bitnami https://charts.bitnami.com/bitnami
   helm repo update bitnami
   helm install postgres bitnami/postgresql \
     -n coder \
     -f postgres-values.yaml
   ```

   Wait for `postgres-postgresql-0` to reach `Ready`. First start runs
   the volumePermissions init container (chowns the hostPath mount),
   then postgres initializes.

7. **Install Coder**:

   ```bash
   helm repo add coder-v2 https://helm.coder.com/v2
   helm repo update coder-v2
   helm install coder coder-v2/coder \
     -n coder \
     -f values.yaml
   ```

   First start runs DB migrations. The `coder` deployment should be
   Ready in 1–2 minutes.

   > **Prereq:** Coder rejects OIDC logins when the IdP returns
   > `email_verified: False`. Authentik's default `email` scope mapping
   > does exactly that (no SMTP / verification flow is configured). See
   > `../authentik/README.md` → "Customize the `email` scope mapping"
   > before opening Coder, or first-time OIDC sign-in will land on a
   > `Verify your email address on your OIDC provider` error page.

8. **Seed both an OIDC owner and a local-only owner.** Coder's first
   visit shows a "Create your first user" form *and* the "Sign in
   with Authentik" button side by side. Which one you click first
   determines the flow; both paths end at the same final state (one
   OIDC owner + one local-password owner) so pick whichever fits.

   **Path A — OIDC first (recommended for a clean cluster):**

   1. On the first-visit page, click **Sign in with Authentik** (do
      *not* fill out "Create your first user"). The first user to sign
      in via OIDC is auto-promoted to owner. Confirm the role in
      **Deployment → Users**.
   2. Create the local-password parachute via CLI:

      ```bash
      kubectl -n coder exec deployment/coder -- \
        coder users create \
          --username=admin-local \
          --email=admin-local@vigiemail.com \
          --password="<from Bitwarden>"
      kubectl -n coder exec deployment/coder -- \
        coder users edit admin-local --roles=owner
      ```

   **Path B — bootstrap user first (what you get if you click "Create
   your first user" before noticing the OIDC button):**

   1. Fill out the form. The bootstrap user is local-password and
      auto-promoted to owner — congratulations, this *is* your
      `admin-local` fallback, just with a different username. Rename
      it to `admin-local` in **Deployment → Users → Edit** if you
      want the name to match the convention; purely cosmetic.
   2. Sign in via Authentik in an incognito window. Coder auto-creates
      an OIDC user, but because a user already exists it lands as a
      **member**, not owner. Switch back to the bootstrap tab and
      promote it: **Deployment → Users →** select the OIDC user **→
      Edit → Roles → Owner**.

   Save the local-fallback password to Bitwarden as a new field
   `local-admin-password` on the `Homelab Coder` item.

9. **Create a workspace template.** Coder's docs cover this end-to-end
   — start with the [`kubernetes` starter template](https://coder.com/docs/templates/tutorial)
   and tweak resources to match what gandalf can spare (start with 2
   CPU / 4 Gi memory per workspace, drop further if scheduling
   conflicts with other workloads).

10. **Apply the updated restic CronJob.** `../backup/backup-cronjob.yaml`
    already lists `/opt/coder/postgres` under tag `coder-postgres`
    (added in the same PR as this directory). Apply it so the next
    nightly run captures the Coder DB:

    ```bash
    kubectl apply -f ../backup/backup-cronjob.yaml
    ```

## Day-to-day operations

### Helm upgrades

```bash
helm -n coder upgrade coder coder-v2/coder -f values.yaml
kubectl -n coder rollout status deployment/coder
```

For postgres:

```bash
helm -n coder upgrade postgres bitnami/postgresql -f postgres-values.yaml
```

### Postgres backup / restore

The data dir at `/opt/coder/postgres` is captured by the nightly restic
CronJob under tag `coder-postgres`. For a restore:

```bash
kubectl -n coder scale statefulset/postgres-postgresql --replicas=0
# restore /opt/coder/postgres from a restic snapshot, e.g.:
#   restic restore <snap> --target / --include /opt/coder/postgres
kubectl -n coder scale statefulset/postgres-postgresql --replicas=1
```

### Rotating OIDC credentials

In the Authentik UI, regenerate the client secret on the `coder`
provider. Update the Bitwarden item, then:

```bash
kubectl -n coder delete secret coder-secrets
# re-run the create-secret command from step 5
kubectl -n coder rollout restart deployment/coder
```

Local password auth continues to work during the rollout, so logged-in
users via OIDC will be bounced but the admin-local fallback isn't
affected.

### Resource pressure

Coder workspaces compete with everything else on gandalf for CPU and
memory. If the cluster starts evicting pods, the cheapest fix is to
lower the per-workspace resource requests in the template, then
recreate workspaces. Long-term fix is adding a Pi worker (see
`../../ansible/` once the Pi 5 is imaged) — workspace pods can land on
Pi workers instead of gandalf.

### Wildcard subdomain for port forwarding

Browser-based port forwarding from a workspace uses URLs like
`8080--main--user.coder.home`. That requires:

1. A wildcard DNS record `*.coder.home → 192.168.50.135` (add to
   Pi-hole's `dns.hosts`, or move to a real domain).
2. Setting `CODER_WILDCARD_ACCESS_URL=*.coder.home` in `values.yaml`.
3. A Traefik IngressRoute that catches the wildcard.

Workspace SSH (`coder ssh <workspace>`) does not need any of this and
works out of the box.
