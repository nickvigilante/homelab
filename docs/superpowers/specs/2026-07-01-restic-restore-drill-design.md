# Restic restore-verification drill — design

Issue: #139.
Depends on the alerting stack from #138 and #76.

## Problem

Untested backups aren't backups.
The nightly restic CronJob reports that it *ran*,
but nothing proves the repo is intact,
that snapshots for every tag keep appearing,
or that a restore actually produces usable data.
The Storj-grant incident (2026-05-26) showed exactly this failure mode:
the backup broke silently and nothing noticed.

## Goal

A scheduled drill that:

- verifies restic repo integrity, including the pack data stored in Storj,
- proves the restore path works by restoring real snapshots and verifying contents,
- asserts every expected tag has a fresh snapshot,
- alerts via the existing Prometheus and Alertmanager stack when any of that fails or stops running.

## Constraints and facts (measured 2026-07-01)

- Repo size: ~160 GiB raw data, 96 snapshots.
- Latest-snapshot restore sizes per tag:
  syncthing-data ~176 GiB;
  the other seven tags (jellyfin-config, pihole-config, uptime-kuma-data,
  authentik-postgres, coder-postgres, outline-postgres, grafana)
  total ~1.8 GiB.
- Storj egress ≈ $7/TB — a full syncthing restore costs ~$1.20 per run
  and would need ~176 GiB scratch disk on gandalf.
- `k8s/backup/` is partially Flux-owned:
  the Flux Kustomization `backup` reconciles the directory with `prune: false`
  and `k8s/backup/kustomization.yaml` lists only `external-secret.yaml`;
  the CronJobs are hand-applied.
  New resources can be born Flux-owned by adding them to the resource list.
- `k8s/homelab-monitoring/` (alert rules, #138) is fully Flux-owned, `prune: true`.

## Decisions

| Decision                  | Choice                             | Rationale                                                                                                                                                                                                              |
| ------------------------- | ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Big-tag (syncthing) depth | Rotating `--read-data-subset=n/5`  | Full pack coverage every 5 weeks at ~$0.90/mo egress; no 176 GiB scratch. Chosen over full restore (cost/disk) and metadata-only (misses bit-rot on photos with no other replica).                                     |
| Cadence                   | Weekly, Sunday 05:00 ET            | One hour after the Sunday 04:00 forget/prune, so every prune is verified before the next backup cycle builds on it.                                                                                                    |
| Alerting                  | Prometheus-only dead-man rule      | One `ResticVerifyStale` rule catches job-failed, job-crashed, and never-scheduled. No Uptime Kuma monitor and no direct SMTP — Alertmanager already emails. Revisitable: a Kuma push monitor is a small bolt-on later. |
| Architecture              | Standalone `restic-verify` CronJob | Keeps retention (forget) and verification as separate jobs with separate failure signals. Folding into forget or adding a metrics exporter/Pushgateway rejected (coupling / YAGNI).                                    |

## Components

### 1. CronJob `restic-verify` — `k8s/backup/verify-cronjob.yaml`

Mirrors the structure of `backup-cronjob.yaml` and `forget-cronjob.yaml`:
namespace `backup`, image `restic/restic:latest`, gandalf-pinned via nodeSelector,
`envFrom: restic-credentials`, `concurrencyPolicy: Forbid`,
`successfulJobsHistoryLimit: 3`, `failedJobsHistoryLimit: 5`, `backoffLimit: 1`.

Differences from the siblings:

- `schedule: "0 5 * * 0"`, `timeZone: America/New_York`.
- `activeDeadlineSeconds: 10800` (3 h) on the Job spec —
  the read-data phase downloads ~32 GiB and must not hang forever.
- An `emptyDir` volume mounted at `/scratch` for the restore drill
  (needs ~2 GiB; sized well within gandalf's disk).
- No Uptime Kuma push, no SMTP, no ERR trap:
  the only failure signal is "the Job did not succeed",
  observed by Prometheus via kube-state-metrics.

The script runs four phases under `set -euo pipefail` (fail-fast):

1. **Structure check** — `restic check`.
   Verifies index, snapshot, and pack metadata consistency. Metadata-only egress.

2. **Rotating data check** — `restic check --read-data-subset=$n/5`
   with `n=$(( ($(date +%s) / 604800) % 5 + 1 ))`.
   Subset selection is deterministic by pack ID,
   so stepping n weekly covers 100% of packs every 5 weeks (~32 GiB egress/run).

3. **Freshness check** — for each of the 8 expected tags, assert
   `restic snapshots --tag <tag> --latest 1 --json` returns a snapshot
   whose timestamp is < 48 h old.
   Requires jq (`apk add --no-cache jq`, same best-effort-install pattern
   the backup job uses for curl — but hard-required here: no jq → fail the job,
   since the check cannot run without it).
   Catches "backup job green but one tag silently stopped appearing".

4. **Restore drill** — for each of the 7 small tags:
   `restic restore latest --tag <tag> --target /scratch/<tag> --verify`
   (`--verify` re-hashes every restored file against the repo index — content-level proof),
   then per-tag sentinel assertions:

   - authentik-postgres, coder-postgres, outline-postgres: `PG_VERSION` exists in the restored data dir.
   - pihole-config: `gravity.db` exists.
   - jellyfin-config: `data/jellyfin.db` exists.
   - uptime-kuma-data: `kuma.db` exists.
   - grafana: `grafana.db` exists.
   - every tag: restored file count > 0.

   Sentinels catch "snapshot fresh and restorable but the source dir was empty or wrong".
   syncthing-data is deliberately excluded from the restore drill
   (covered by phases 2 and 3 per the depth decision).

   Sentinel paths are asserted against the live restored tree during implementation
   (restore once manually and confirm each path) — not assumed.

### 2. Alert rule `ResticVerifyStale` — appended to `k8s/homelab-monitoring/prometheusrule.yaml`

```promql
(time() - max(kube_cronjob_status_last_successful_time{cronjob="restic-verify", namespace="backup"})) > 8 * 86400
```

`for: 1h`, `severity: warning`, same shape as `ResticBackupStale`.
8 days = weekly cadence + 1 day grace.
Dead-man semantics: failed, crashed, or skipped runs all fire,
because the success timestamp simply stops advancing.
Known limitation: if the CronJob object is *deleted*, the metric goes absent
and the expression returns no data, so the alert does not fire.
`ResticBackupStale` accepts the same limitation today;
an `absent()`-based guard is deliberately out of scope for both (YAGNI —
the CronJobs are git-managed and their disappearance would be a reviewed change).

Annotations point at `kubectl -n backup get jobs` and the verify job's logs.

### 3. Wiring and docs

- `k8s/backup/kustomization.yaml`: add `verify-cronjob.yaml` to `resources`
  (born Flux-owned; `prune: false` untouched, manual siblings unaffected).
- `k8s/backup/README.md`: new "Restore-verification drill" section —
  what the four phases check, the rotating-subset coverage math,
  the manual trigger one-liner
  (`kubectl -n backup create job --from=cronjob/restic-verify verify-test-$(date +%s)`),
  and what to do when the alert fires.
- `k8s/homelab-monitoring/README.md`: add the new rule to the alert table.

## Data flow

Flux reconciles both directories from main →
CronJob fires Sundays 05:00 ET →
job reads the repo from Storj (S3 creds from the ESO-managed `restic-credentials` Secret) →
success updates `kube_cronjob_status_last_successful_time` via kube-state-metrics →
Prometheus evaluates `ResticVerifyStale` →
Alertmanager emails on staleness > 8 d.

## Error handling

- Fail-fast: the first failing phase fails the Job; `backoffLimit: 1`
  gives one retry for transient Storj hiccups.
- `activeDeadlineSeconds` kills a wedged run so the CronJob can't jam
  (`concurrencyPolicy: Forbid` would otherwise skip future runs forever).
- No trap/push/email plumbing: the dead-man rule is the single, unambiguous signal.
- Scratch is `emptyDir` — reclaimed automatically on pod deletion,
  no host cleanup to forget.

## Testing

- `promtool check rules` on the extracted rule group (CI-adjacent, same as #138).
- kubeconform: `*-cronjob.yaml` already matches the CI lint filter.
- Live green path: manual `kubectl create job --from=cronjob/restic-verify`,
  watch all four phases pass in logs, confirm the success timestamp advances.
- Live red path: one-off Job copy with a deliberately bogus sentinel assertion,
  confirm the Job fails.
- Alert sanity: run the `ResticVerifyStale` expression in Prometheus with the
  threshold lowered ad hoc (query-only, not committed) to confirm it would fire
  before the first successful run and clears after.

## Out of scope

- Restoring syncthing-data end-to-end (depth decision above).
- Uptime Kuma monitor for the drill (revisitable bolt-on).
- Per-tag Prometheus metrics / exporter / Pushgateway (YAGNI).
- Full-cluster DR rehearsal — this drill verifies the restic layer only;
  the DR sequence lives in CLAUDE.md "Disaster recovery".
