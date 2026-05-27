# backup

Restic-based encrypted, deduplicated backups of cluster-side state to a
Storj S3 bucket. Single repository, multiple tagged snapshots per source.

## What gets backed up

| Source                    | Mount in pod                 | Tag                  | Notes                                                                                                                    |
| ------------------------- | ---------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `/opt/jellyfin/config`    | `/backup/jellyfin`           | `jellyfin-config`    | Jellyfin SQLite DB + settings                                                                                            |
| `/opt/pihole/etc-pihole`  | `/backup/pihole`             | `pihole-config`      | Pi-hole settings + `gravity.db`; `pihole-FTL.db*` excluded                                                               |
| `/opt/uptime-kuma/data`   | `/backup/uptime-kuma`        | `uptime-kuma-data`   | Uptime Kuma SQLite DB + monitor config                                                                                   |
| `/opt/authentik/postgres` | `/backup/authentik-postgres` | `authentik-postgres` | Authentik PostgreSQL data dir; **useless without `AUTHENTIK_SECRET_KEY`** (stored in Bitwarden item `Homelab Authentik`) |

Add a path by editing `backup-cronjob.yaml`: add a hostPath volume, a
readOnly volumeMount, and a `restic backup --tag <tag> /backup/<dir>` line.

## Storage layout

- **Repository:** `s3:https://gateway.storjshare.io/homelab/restic`
- **Bucket:** `homelab` (same one used for OpenTofu state under a different prefix)
- **Encryption:** AES-256 via restic; password is the only key. Lose the
  password → lose every snapshot. Source of truth is the Bitwarden item
  `Homelab Restic Repository`.

## Retention

`forget-cronjob.yaml` keeps **7 daily / 4 weekly / 6 monthly / 2 yearly**
snapshots per (host, tag) group. Runs Sundays at 04:00 with `--prune` so
the bucket actually shrinks.

## Failure alerts

Two independent channels fire on a failed backup, so a silently-broken
nightly job doesn't go unnoticed:

1. **Uptime Kuma push monitor** (`restic-backup`) — pinged on success
   and on failure via `trap ... ERR`. If the job dies before reaching
   either branch, the monitor goes DOWN on its own when its heartbeat
   interval expires.
2. **Email via Forward Email's SMTP relay** — sent on failure only.
   Recipient: hardcoded in `backup-cronjob.yaml`'s `SMTP_TO` env var.
   Out-of-band channel for cases when Uptime Kuma is also unreachable
   or not being actively watched.

The SMTP creds come from a Secret `smtp-relay` in this namespace, which
is **mirrored from `auth/smtp-relay`** by emberstack/reflector. The
annotations live on the source Secret in the `auth` namespace — see
`k8s/authentik/secret.example.yaml` and `k8s/authentik/README.md`. No
separate `kubectl create secret` step in this namespace; the mirror
happens automatically within seconds of reflector seeing the source.

**Forcing a failure for verification:**

```bash
# Trigger a real failure by pointing the job at an invalid hostPath
# source briefly, OR simulate by exec-ing into a running test pod and
# running `exit 1`. Cleanest test:

kubectl -n backup create job test-fail-$(date +%s) \
  --from=cronjob/restic-backup \
  --image=restic/restic:latest -- /bin/sh -c 'apk add --no-cache curl; echo "simulated failure"; exit 1'
# (Note: this won't actually exercise the real trap; use it only as
# a smoke test that the email path itself works. Real failures during
# `restic backup` will fire `on_failure` via ERR trap.)
```

Easier: just watch a real failure (e.g., the next time Storj has a
hiccup and the upload retries exhaust). The email contains exit code,
job pod hostname, and the kubectl commands to inspect logs.

**Adding more namespaces that need SMTP later:**

Append the namespace name to the `reflection-auto-namespaces`
annotation on `auth/smtp-relay`:

```bash
# Example: also mirror into the cert-manager namespace
kubectl -n auth annotate secret smtp-relay --overwrite \
  reflector.v1.k8s.emberstack.com/reflection-auto-namespaces=backup,cert-manager
```

Reflector picks up the change within seconds.

## One-time setup

1. **Generate a strong repo password** (Bitwarden → new item `Homelab Restic Repository`).

2. **Create a Storj S3 access grant** scoped to the `homelab` bucket with
   **read + write + list + delete** permissions (delete is required for
   `forget --prune`). Note the access key id + secret key.

3. **Create the Kubernetes Secret** (values stay out of git):

   ```bash
   export BW_SESSION="$(bw unlock --raw)"
   kubectl apply -f namespace.yaml
   kubectl -n backup create secret generic restic-credentials \
     --from-literal=RESTIC_REPOSITORY="s3:https://gateway.storjshare.io/homelab/restic" \
     --from-literal=RESTIC_PASSWORD="$(bw get password 'Homelab Restic Repository')" \
     --from-literal=AWS_ACCESS_KEY_ID="<storj key id>" \
     --from-literal=AWS_SECRET_ACCESS_KEY="<storj secret>"
   unset BW_SESSION
   ```

4. **Initialize the repository** (one shot):

   ```bash
   kubectl apply -f init-job.yaml
   kubectl -n backup wait --for=condition=complete job/restic-init --timeout=120s
   kubectl -n backup logs job/restic-init
   # Expect: "created restic repository … at s3:…"
   ```

5. **(Optional) Wire heartbeats to Uptime Kuma.** Both CronJobs read a
   second Secret named `uptime-kuma-push-urls` and ping a push monitor on
   success/failure. URLs use cluster-internal DNS so they work even when
   Pi-hole is down (and `uptime.home` doesn't resolve from inside pods).
   Skip this if you don't run Uptime Kuma — `envFrom` on a missing Secret
   makes the pod fail to start, so either deploy the Secret or strip the
   `envFrom` line for `uptime-kuma-push-urls` from both CronJobs.

   ```bash
   # Get push URLs from Uptime Kuma → create two `Push` monitors named
   # `restic-backup` and `restic-forget`. Heartbeat Intervals: 90000s
   # (25h) and 691200s (8d) respectively to match the cron schedules.
   # Copy the API URL from each monitor (it ends in /api/push/<token>).

   kubectl -n backup create secret generic uptime-kuma-push-urls \
     --from-literal=UPTIME_KUMA_PUSH_BACKUP_URL='http://uptime-kuma.monitoring.svc.cluster.local:3001/api/push/<token-backup>' \
     --from-literal=UPTIME_KUMA_PUSH_FORGET_URL='http://uptime-kuma.monitoring.svc.cluster.local:3001/api/push/<token-forget>'
   ```

6. **Install the schedule**:

   ```bash
   kubectl apply -f backup-cronjob.yaml
   kubectl apply -f forget-cronjob.yaml
   ```

7. **Smoke test** — run the backup once on demand, don't wait until 3am:

   ```bash
   kubectl -n backup create job --from=cronjob/restic-backup test-backup-$(date +%s)
   kubectl -n backup logs -f -l job-name=$(kubectl -n backup get jobs -o name | tail -1 | cut -d/ -f2)
   ```

## Day-to-day operations

### List snapshots

```bash
kubectl -n backup run restic-shell --rm -it \
  --image=restic/restic:latest \
  --overrides='{"spec":{"containers":[{"name":"restic-shell","image":"restic/restic:latest","stdin":true,"tty":true,"envFrom":[{"secretRef":{"name":"restic-credentials"}}]}]}}' \
  -- snapshots --compact
```

### Restore a path

```bash
# Find the snapshot you want
kubectl -n backup run restic-shell --rm -it \
  --image=restic/restic:latest \
  --overrides='{"spec":{"containers":[{"name":"restic-shell","image":"restic/restic:latest","stdin":true,"tty":true,"envFrom":[{"secretRef":{"name":"restic-credentials"}}]}]}}' \
  -- snapshots --tag jellyfin-config --compact

# Restore (example — restores into /tmp/restore inside the pod)
# In practice you'd use restic from gandalf itself (with env vars set)
# so you can write directly to /opt/jellyfin/config.
```

For a real restore, easier is to run restic on gandalf directly with the
same env vars (sourced from the same Storj access grant + Bitwarden):

```bash
# On gandalf, with restic installed via brew or apt
export RESTIC_REPOSITORY="s3:https://gateway.storjshare.io/homelab/restic"
export RESTIC_PASSWORD="$(bw get password 'Homelab Restic Repository')"
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
restic snapshots --compact
restic restore <snapshot-id> --target /tmp/restore
```

### Check repo health

```bash
# In the cluster
kubectl -n backup create job --from=cronjob/restic-forget restic-check-$(date +%s)
# (forget-cronjob has all the env it needs; you can also just run `restic check`
#  via a one-shot pod with the same secret ref)
```

## Failure modes worth knowing

- **Lost RESTIC_PASSWORD** → backups become unrecoverable. Bitwarden is the only durable copy. Verify Bitwarden export discipline.
- **Storj access grant rotated / revoked** → backups fail until the Secret is updated. CronJob will mark jobs as failed; `failedJobsHistoryLimit: 5` keeps the last few for inspection.
- **Repo grows unboundedly** → forget-cronjob misfires. Check its Sunday run via `kubectl -n backup logs -l app=restic-forget` (or whatever last `restic-forget-*` job exists). Manual prune is safe to run on demand.
- **Source dir not present** at backup time (mount lost, dir deleted) → `hostPath` with `type: Directory` makes the Pod fail to schedule with a clear error. Look at `kubectl -n backup describe pod -l job-name=…`.
