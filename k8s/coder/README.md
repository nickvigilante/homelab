# coder

Self-hosted browser dev environments. Coder spins up workspaces as
Kubernetes pods inside this cluster; templates are written in
Terraform/OpenTofu (which pairs well with the existing IaC setup).

## Layout

- `namespace.yaml` — `coder` namespace
- `pv-pvc.yaml` — PV `coder-postgres-data` → hostPath `/opt/coder/postgres` (gandalf), matching PVC named for what the bitnami postgres StatefulSet expects (`data-postgres-postgresql-0`)
- `postgres-helmrelease.yaml` — Flux `HelmRelease` for the bitnami/postgresql backing DB
- `helmrelease.yaml` — Flux `HelmRelease` for the official `coder-v2/coder` chart
- `kustomization.yaml` — lists the resources above plus the `../../sources/bitnami.yaml` and `../../sources/coder.yaml` `HelmRepository` refs
- `secret.example.yaml` — template documenting the keys required in the `coder-secrets` Secret. Never apply this directly — the real Secret is created from out-of-repo material (see below).

Reconciled by the `coder` Flux Kustomization (`clusters/gandalf/coder.yaml`) — both Helm releases are Flux-owned (full takeover, same treatment uptime-kuma got in #147). The `coder-secrets` Secret stays manually-managed for now (ESO migration tracked in #161).

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

The steps below are written for a from-scratch install. On gandalf,
Coder and its postgres backend are already running as helm-installed
releases with values identical to `helmrelease.yaml` /
`postgres-helmrelease.yaml` — steps 01–05 are already done, and merging
this Flux Kustomization is an adoption (a no-op `helm upgrade` under
the hood, not a fresh install). Only step 06 onward is new work.

1. **Save secrets to Bitwarden.** Create a Bitwarden item named `Homelab Coder` with four custom fields:

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

6. **Let Flux install postgres and Coder.** Once `coder-secrets` exists
   and `clusters/gandalf/coder.yaml` is merged to `main`, Flux
   reconciles `k8s/coder/` on its next 10m interval and installs both
   HelmReleases in dependency order (postgres first, per
   `helmrelease.yaml`'s `dependsOn`). To force it immediately instead
   of waiting:

   ```bash
   flux reconcile kustomization coder --with-source
   ```

   Wait for `postgres-postgresql-0` to reach `Ready` before Coder's
   migrations run. First start runs the volumePermissions init
   container (chowns the hostPath mount), then postgres initializes.
   The `coder` deployment should be Ready in 1–2 minutes after that.

   > **Prereq:** Coder rejects OIDC logins when the IdP returns
   > `email_verified: False`. Authentik's default `email` scope mapping
   > does exactly that (no SMTP / verification flow is configured). See
   > `../authentik/README.md` → "Customize the `email` scope mapping"
   > before opening Coder, or first-time OIDC sign-in will land on a
   > `Verify your email address on your OIDC provider` error page.

7. **Seed both an OIDC owner and a local-only owner.** Coder's first
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

8. **Create a workspace template.** Coder's docs cover this end-to-end
   — start with the [`kubernetes` starter template](https://coder.com/docs/templates/tutorial)
   and tweak resources to match what gandalf can spare (start with 2
   CPU / 4 Gi memory per workspace, drop further if scheduling
   conflicts with other workloads).

   > **Mixed-arch cluster gotcha:** this cluster has both an amd64
   > node (gandalf) and arm64 Pi nodes (frodo, samwise). The starter
   > template's `coder_agent` resource hardcodes `arch = "amd64"`,
   > which picks the `coder-linux-amd64` agent binary — but the
   > generated `kubernetes_pod` resource has no `node_selector` at
   > all, so the k8s scheduler is free to place a workspace pod on an
   > arm64 Pi. When that happens the agent binary fails immediately
   > with `Exec format error`, and the pod's startup script traps the
   > failure and sleeps 24h (so `kubectl get pods` misleadingly shows
   > `1/1 Running` for a workspace that never actually started). Fix:
   > pin workspace pods to the matching architecture in the template's
   > `kubernetes_pod` resource:
   >
   > ```hcl
   > resource "kubernetes_pod" "main" {
   >   spec {
   >     node_selector = {
   >       "kubernetes.io/arch" = "amd64"
   >     }
   >     # ...
   >   }
   > }
   > ```
   >
   > Pull the template locally with `coder templates pull`, add this,
   > then `coder templates push` a new version before creating any
   > workspace.

9. **Apply the updated restic CronJob.** `../backup/backup-cronjob.yaml`
   already lists `/opt/coder/postgres` under tag `coder-postgres`
   (added in the same PR as this directory). Apply it so the next
   nightly run captures the Coder DB:

   ```bash
   kubectl apply -f ../backup/backup-cronjob.yaml
   ```

## Day-to-day operations

### Chart upgrades

Bump `spec.chart.spec.version` (and any `values:` changes) in
`helmrelease.yaml` or `postgres-helmrelease.yaml`, commit, and push.
Flux picks it up on the next 10m reconcile of the `coder` Kustomization,
or immediately via:

```bash
flux reconcile kustomization coder --with-source
kubectl -n coder rollout status deployment/coder
```

Always pin the version explicitly in the HelmRelease — never leave it
unbounded — per the homelab-wide pin discipline (an unpinned upgrade
took the auth namespace down on 2026-05-23).

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
2. Setting `CODER_WILDCARD_ACCESS_URL=*.coder.home` in `helmrelease.yaml`.
3. A Traefik IngressRoute that catches the wildcard.

Workspace SSH (`coder ssh <workspace>`) does not need any of this and
works out of the box.

### MCP servers for Coder Agents

Coder Agents can call external MCP servers registered under AI Settings → Coder Agents → MCP servers.
The registrations are managed with OpenTofu in the sibling `infrastructure` repo, under `coder/`, not here (#204).
Coder keeps them in its database, outside these manifests, and the provider's write-only attributes keep the secrets out of state.

This directory owns one related setting: `CODER_MCP_ALLOWED_PRIVATE_CIDRS` in `helmrelease.yaml`.
Coder's SSRF guard blocks private and CGNAT destinations for MCP traffic by default, and every `*.vigihome.net` name resolves to one of gandalf's two addresses.
The setting allows those two `/32`s, which covers every service behind Traefik, plus the fixed ClusterIPs `10.43.0.200` (Grafana MCP) and `10.43.0.201` (Kubernetes MCP) of the in-cluster MCP servers in `k8s/claude-mcp/`.
Only exact `/32`s are listed, so the pod and service CIDRs as a whole stay blocked.
If Pi-hole's wildcard ever points at different addresses, update it to match, or MCP servers on `*.vigihome.net` will fail to connect.

Registered servers reach Outline and Home Assistant through their public `*.vigihome.net` hostnames, not cluster-internal service DNS.
Outline forces HTTPS and advertises its public URL in its OAuth metadata, so an in-cluster HTTP URL would break discovery.
