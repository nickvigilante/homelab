# Restic Restore-Verification Drill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A weekly `restic-verify` CronJob that proves the restic backups are restorable, alerting via a `ResticVerifyStale` dead-man PrometheusRule when it fails or stops running (#139).

**Architecture:** One new CronJob in `k8s/backup/` (born Flux-owned via the directory's kustomization) runs a four-phase drill: repo check with rotating `--read-data-subset=n/5`, per-tag snapshot freshness, and a real restore of the seven small tags with sentinel assertions.
One new alert rule appended to the existing `homelab-alerts` PrometheusRule watches the CronJob's success timestamp.
Spec: `docs/superpowers/specs/2026-07-01-restic-restore-drill-design.md`.

**Tech Stack:** Kubernetes CronJob (batch/v1), restic 0.18 (`restic/restic:latest`), kube-state-metrics + PrometheusRule (`monitoring.coreos.com/v1`), Flux Kustomization, kubeconform/promtool/shellcheck for validation.

## Global Constraints

- Work in the worktree `/home/nickv/git/nickvigilante/homelab/.worktrees/139-restic-verify-drill` (branch `139-restic-verify-drill`). Never commit on main.
- Secrets never enter the repo; never echo secret values. The job consumes the existing ESO-managed `restic-credentials` Secret by name only.
- Commit subjects: imperative mood, ≤ 70 chars; body explains "why". End every commit message with the trailer `Assisted-by: AI`. No `Co-Authored-By` lines.
- Markdown: semantic line breaks (one clause per line), write "and" not "+" in prose. mdformat runs via pre-commit.
- YAML is formatted by yamlfmt and linted by yamllint via pre-commit; kubeconform runs in CI on `*-cronjob.yaml` and `prometheusrule.yaml` (both already in the CI filename filter — no filter change needed).
- All commands below run on **gandalf** (this box) unless stated otherwise. Live cluster commands need `export KUBECONFIG=~/.kube/config` first.
- `k8s/backup`'s Flux Kustomization has `prune: false` — do NOT flip it; the sibling CronJobs are still manually managed.

______________________________________________________________________

### Task 1: The `restic-verify` CronJob

**Files:**

- Create: `k8s/backup/verify-cronjob.yaml`
- Modify: `k8s/backup/kustomization.yaml`

**Interfaces:**

- Consumes: Secret `backup/restic-credentials` (exists, ESO-managed — referenced by name only).

- Produces: CronJob `backup/restic-verify` whose success timestamp Task 2's alert expression watches via `kube_cronjob_status_last_successful_time{cronjob="restic-verify", namespace="backup"}`.

- [ ] **Step 1: Write `k8s/backup/verify-cronjob.yaml`**

Sentinel paths below were verified against the live latest snapshots on 2026-07-01
(via `restic ls latest --tag <tag>`) — restored trees keep the `/backup/<dir>` prefix
because that is the absolute path the backup pod snapshots.

```yaml
# Weekly restore-verification drill (#139). Untested backups aren't
# backups: this job proves the repo is intact, snapshots keep appearing
# for every tag, and a restore actually produces usable data.
#
# Four phases, fail-fast (set -euo pipefail):
#   1+2. restic check --read-data-subset=n/5 -- structure/index check
#        PLUS hash-verification of a rotating 1/5 of all pack files.
#        Subset selection is deterministic by pack ID, so stepping n
#        weekly covers 100% of packs every 5 weeks (~32 GiB Storj
#        egress per run, ~$0.90/mo at $7/TB).
#   3.   Freshness: latest snapshot per tag must be < 48h old. Catches
#        "backup job green but one tag silently stopped appearing".
#   4.   Restore drill: the 7 small tags (~1.8 GiB total) restored into
#        emptyDir scratch with --verify (content re-hashed against the
#        repo index), then a per-tag sentinel file must exist. Catches
#        "snapshot fresh but the source dir was empty or wrong".
#        syncthing-data (176 GiB) is deliberately NOT restored -- pack
#        integrity is covered by phase 2 and freshness by phase 3; see
#        docs/superpowers/specs/2026-07-01-restic-restore-drill-design.md.
#
# Failure signal: NONE in-job -- no Uptime Kuma push, no SMTP, no ERR
# trap (unlike the sibling CronJobs). The single signal is the
# ResticVerifyStale dead-man rule (k8s/homelab-monitoring): a failed,
# crashed, or never-scheduled run means the success timestamp stops
# advancing and Alertmanager emails after 8 days.
#
# Schedule: Sunday 05:00 ET, one hour after restic-forget's Sunday
# 04:00 prune, so every prune is verified before Monday's backup
# builds on it.
#
# Manual trigger:
#   kubectl -n backup create job --from=cronjob/restic-verify verify-test-$(date +%s)
apiVersion: batch/v1
kind: CronJob
metadata:
  name: restic-verify
  namespace: backup
spec:
  schedule: "0 5 * * 0"
  timeZone: America/New_York
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 5
  jobTemplate:
    spec:
      backoffLimit: 1
      # The read-data phase downloads ~32 GiB; kill a wedged run so
      # concurrencyPolicy: Forbid can't jam future runs forever.
      activeDeadlineSeconds: 10800
      template:
        spec:
          restartPolicy: OnFailure
          nodeSelector:
            kubernetes.io/hostname: gandalf
          containers:
            - name: restic
              image: restic/restic:latest
              command:
                - /bin/sh
                - -c
                - |
                  set -euo pipefail

                  # jq is required by the freshness phase. Unlike the
                  # backup job's best-effort curl install, a missing jq
                  # must FAIL the job -- the check can't run without it.
                  apk add --no-cache jq >/dev/null

                  # Phases 1+2 share one invocation: --read-data-subset
                  # performs the full structure/index check and then
                  # hash-verifies the selected packs. n cycles 1..5 by
                  # epoch week, covering every pack every 5 weeks.
                  n=$(( ($(date +%s) / 604800) % 5 + 1 ))
                  echo "=== Phase 1+2: repo check, pack subset $n/5 ==="
                  restic check --read-data-subset="$n/5"

                  echo
                  echo "=== Phase 3: snapshot freshness (<48h) ==="
                  now=$(date +%s)
                  for tag in jellyfin-config pihole-config uptime-kuma-data \
                             authentik-postgres coder-postgres outline-postgres \
                             syncthing-data grafana; do
                    ts=$(restic snapshots --tag "$tag" --latest 1 --json | jq -r '.[0].time // empty')
                    if [ -z "$ts" ]; then
                      echo "FAIL: no snapshot found for tag $tag"
                      exit 1
                    fi
                    # busybox date can't parse RFC3339 fractional seconds
                    # or numeric offsets; strip both and parse as UTC.
                    # Snapshot times are UTC (backup pod TZ), so this is
                    # exact; were an offset ever present the error is
                    # bounded (<= a few hours) and only makes the check
                    # stricter -- fine against a 48h threshold.
                    clean=$(echo "$ts" | sed 's/\..*//; s/Z$//; s/[+-][0-9][0-9]:[0-9][0-9]$//')
                    snap=$(date -u -D "%Y-%m-%dT%H:%M:%S" -d "$clean" +%s)
                    age=$(( now - snap ))
                    if [ "$age" -gt 172800 ]; then
                      echo "FAIL: latest $tag snapshot is ${age}s old (>48h)"
                      exit 1
                    fi
                    echo "OK: $tag latest snapshot ${age}s old"
                  done

                  echo
                  echo "=== Phase 4: restore drill (7 small tags) ==="
                  restore_and_check() {
                    tag=$1
                    sentinel=$2
                    echo "--- $tag"
                    restic restore latest --tag "$tag" --target "/scratch/$tag" --verify
                    if [ ! -e "/scratch/$tag$sentinel" ]; then
                      echo "FAIL: sentinel $sentinel missing from restored $tag"
                      exit 1
                    fi
                    count=$(find "/scratch/$tag" -type f | wc -l)
                    if [ "$count" -lt 1 ]; then
                      echo "FAIL: restored $tag contains no files"
                      exit 1
                    fi
                    echo "OK: $tag restored ($count files, sentinel present)"
                    # Free scratch before the next tag so peak usage is
                    # one tag's tree, not the sum of all seven.
                    rm -rf "/scratch/${tag:?}"
                  }
                  # Sentinel paths verified against live snapshots
                  # 2026-07-01; the /backup/<dir> prefix is the absolute
                  # path the backup pod snapshots, preserved on restore.
                  restore_and_check jellyfin-config    /backup/jellyfin/data/jellyfin.db
                  restore_and_check pihole-config      /backup/pihole/gravity.db
                  restore_and_check uptime-kuma-data   /backup/uptime-kuma/kuma.db
                  restore_and_check authentik-postgres /backup/authentik-postgres/data/PG_VERSION
                  restore_and_check coder-postgres     /backup/coder-postgres/data/PG_VERSION
                  restore_and_check outline-postgres   /backup/outline-postgres/data/PG_VERSION
                  restore_and_check grafana            /backup/grafana/grafana.db

                  echo
                  echo "=== All phases passed ==="
              envFrom:
                - secretRef:
                    name: restic-credentials
              volumeMounts:
                - name: scratch
                  mountPath: /scratch
          volumes:
            - name: scratch
              emptyDir: {}
```

- [ ] **Step 2: Add the file to the backup kustomization**

Edit `k8s/backup/kustomization.yaml` so the resource list reads
(keep the existing header comment, update its claim that only the
ExternalSecret is Flux-owned):

```yaml
# The ExternalSecret and the verify CronJob are Flux-owned; the
# backup/forget CronJobs, init job, namespace, and the reflected
# smtp-relay / hand-applied uptime-kuma-push-urls Secrets stay
# manually-managed (full takeover is future work).
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - external-secret.yaml
  - verify-cronjob.yaml
```

- [ ] **Step 3: Validate — kubeconform**

Run (in the worktree root):

```bash
kubeconform -strict -summary \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  k8s/backup/verify-cronjob.yaml
```

Expected: `Valid: 1, Invalid: 0, Errors: 0`.

- [ ] **Step 4: Validate — kustomize build**

```bash
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s/backup >/dev/null && echo BUILD-OK
```

Expected: `BUILD-OK`.

- [ ] **Step 5: Validate — shellcheck the embedded script**

Extract the script and lint it at the severity CI uses
(SC3040 excluded: `set -o pipefail` is not POSIX but busybox ash supports it,
and the sibling CronJobs already rely on it):

```bash
/usr/bin/python3 - <<'EOF'
import yaml
d = yaml.safe_load(open('k8s/backup/verify-cronjob.yaml'))
script = d['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['command'][2]
open('._verify-script.sh', 'w').write('#!/bin/sh\n' + script)
EOF
shellcheck -S info -e SC3040 ._verify-script.sh && rm ._verify-script.sh && echo SHELLCHECK-OK
```

Expected: `SHELLCHECK-OK` (no findings). If shellcheck reports other SC3xxx
POSIX-portability infos for constructs busybox ash demonstrably supports,
fix the script rather than widening the exclusions, unless the construct is
`pipefail`-class (supported, idiomatic in the siblings).

- [ ] **Step 6: Commit**

```bash
git add k8s/backup/verify-cronjob.yaml k8s/backup/kustomization.yaml
git commit -m "$(cat <<'EOF'
Add weekly restic restore-verification CronJob (#139)

Four-phase drill: repo check with rotating 1/5 read-data subset
(full pack coverage every 5 weeks), per-tag snapshot freshness
(<48h), and a real restore of the seven small tags with sentinel
assertions. No in-job alerting plumbing -- the ResticVerifyStale
dead-man rule is the single failure signal.

Assisted-by: AI
EOF
)"
```

Pre-commit runs yamlfmt/yamllint/betterleaks; if yamlfmt rewrites the file,
`git add` the reformatted file and re-commit.

______________________________________________________________________

### Task 2: The `ResticVerifyStale` alert rule

**Files:**

- Modify: `k8s/homelab-monitoring/prometheusrule.yaml` (append one rule to the `homelab` group)
- Modify: `k8s/homelab-monitoring/README.md` (add one row to the alert table)

**Interfaces:**

- Consumes: metric `kube_cronjob_status_last_successful_time{cronjob="restic-verify", namespace="backup"}` produced (via kube-state-metrics) once Task 1's CronJob has its first successful run.

- Produces: alert `ResticVerifyStale`, routed by the existing Alertmanager email receiver (no Alertmanager config change needed).

- [ ] **Step 1: Append the rule**

Add to the END of the `rules:` list in `k8s/homelab-monitoring/prometheusrule.yaml`
(after the `CertificateExpiringSoon` rule, same indentation as its siblings):

```yaml
        # The weekly restore-verification drill (k8s/backup/verify-cronjob.yaml,
        # schedule "0 5 * * 0"). Same dead-man pattern as ResticBackupStale:
        # 8d = weekly cadence + 1d grace; failed, crashed, and never-scheduled
        # runs all stop the success timestamp advancing. Two known gaps, both
        # accepted: (a) the metric is absent until the FIRST successful run
        # (mitigated by the manual green-path run at rollout), and (b) if the
        # CronJob object is deleted the metric goes absent and this never
        # fires -- the CronJob is git-managed, so its disappearance would be
        # a reviewed change (same accepted gap as ResticBackupStale).
        - alert: ResticVerifyStale
          expr: |
            (time() - max(kube_cronjob_status_last_successful_time{cronjob="restic-verify", namespace="backup"})) > 8 * 86400
          for: 1h
          labels:
            severity: warning
          annotations:
            summary: "Weekly restic restore-verification drill has not succeeded in over 8d"
            description: "No successful restic-verify CronJob run in >8d -- the backups are currently unverified. Check `kubectl -n backup get jobs` and the most recent restic-verify job's logs."
```

- [ ] **Step 2: Validate — promtool**

promtool can't see files in /tmp from this sandbox; use an in-worktree temp file:

```bash
/usr/bin/python3 - <<'EOF'
import yaml
d = yaml.safe_load(open('k8s/homelab-monitoring/prometheusrule.yaml'))
yaml.safe_dump(d['spec'], open('._verify-rules.yaml', 'w'))
EOF
promtool check rules ._verify-rules.yaml && rm ._verify-rules.yaml
```

Expected: `SUCCESS: 4 rules found`.

- [ ] **Step 3: Validate — kubeconform**

```bash
kubeconform -strict -summary \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  k8s/homelab-monitoring/prometheusrule.yaml
```

Expected: `Valid: 1, Invalid: 0, Errors: 0`.

- [ ] **Step 4: Add the alert to the monitoring README table**

In `k8s/homelab-monitoring/README.md`, find the alert table
(rows for `ResticBackupStale`, `ContainerOOMKilled`, `CertificateExpiringSoon`)
and append a row, matching the existing column layout exactly:

```markdown
| `ResticVerifyStale` | No successful `restic-verify` run in >8d | The weekly restore drill (Sun 05:00 ET) failed, crashed, or stopped being scheduled — the backups are unverified. `kubectl -n backup get jobs`, then read the newest `restic-verify-*` job's logs. |
```

Adjust the row's cells to the table's actual columns
(read the existing rows first — column count and header wording win over this snippet).

- [ ] **Step 5: Commit**

```bash
git add k8s/homelab-monitoring/prometheusrule.yaml k8s/homelab-monitoring/README.md
git commit -m "$(cat <<'EOF'
Alert when the restic verify drill goes stale (#139)

Dead-man rule on kube_cronjob_status_last_successful_time for
restic-verify, mirroring ResticBackupStale: one expression catches
failed, crashed, and never-scheduled drills. 8d = weekly + 1d grace.

Assisted-by: AI
EOF
)"
```

______________________________________________________________________

### Task 3: Backup README — restore-drill runbook

**Files:**

- Modify: `k8s/backup/README.md`

**Interfaces:**

- Consumes: nothing from other tasks (documentation of Task 1's CronJob and Task 2's alert).

- Produces: operator runbook; no downstream consumers.

- [ ] **Step 1: Add a "Restore-verification drill" section**

Insert after the existing "Failure alerts" section (keep mdformat-compatible
markdown: semantic line breaks, tables padded like the neighbors):

````markdown
## Restore-verification drill

Untested backups aren't backups.
`verify-cronjob.yaml` runs a weekly drill (Sunday 05:00 ET,
one hour after the Sunday prune) in four phases,
failing the Job on the first broken phase.
Phase numbering matches the job's log banners
(phases 1 and 2 share one restic invocation):

- **Phase 1+2: repo check + rotating data read** —
  `restic check --read-data-subset=n/5` with `n` stepping by epoch week.
  Verifies index/snapshot structure every run and hash-verifies 1/5 of all
  pack data, covering 100% of packs every 5 weeks
  (~32 GiB Storj egress per run ≈ $0.90/mo).
- **Phase 3: snapshot freshness** — the latest snapshot for each of the
  8 tags must be < 48 h old.
  Catches a tag silently disappearing while the backup job stays green.
- **Phase 4: restore drill** — the 7 small tags (~1.8 GiB total) are
  restored into an `emptyDir` scratch with `--verify` (restored content
  re-hashed against the repo index) and checked for a per-tag sentinel file
  (`PG_VERSION` for the postgres tags, the main SQLite DB for the rest).
  `syncthing-data` (176 GiB) is deliberately not restored —
  its pack data is covered by the phase 1+2 rotating read and its
  freshness by phase 3;
  see `docs/superpowers/specs/2026-07-01-restic-restore-drill-design.md`.

**Alerting:** intentionally none in-job (no Uptime Kuma push, no SMTP).
The single signal is the `ResticVerifyStale` Prometheus rule
(`k8s/homelab-monitoring/prometheusrule.yaml`):
if no run succeeds for 8 days — failure, crash, or scheduler jam —
Alertmanager emails.
The metric only exists after the first successful run,
so trigger one manually at rollout (below).

**Manual trigger:**

```bash
kubectl -n backup create job --from=cronjob/restic-verify verify-test-$(date +%s)
kubectl -n backup logs -f job/$(kubectl -n backup get jobs --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1].metadata.name}')
```

**When the alert fires:**
`kubectl -n backup get jobs`, read the newest `restic-verify-*` job's logs,
and fix the first failed phase — the log names it
(`Phase 1+2`, `Phase 3`, `Phase 4`) and the failing tag or pack.
A transient Storj hiccup already got one automatic retry (`backoffLimit: 1`);
re-run manually after fixing.
````

Note: the drill is **Flux-owned** (listed in `kustomization.yaml`),
unlike the backup/forget CronJobs — mention that in the section if the
surrounding README text still claims all CronJobs are manually applied,
and update any such stale claim.

- [ ] **Step 2: Verify mdformat is happy**

```bash
pre-commit run mdformat --files k8s/backup/README.md
```

Expected: `Passed` (or it rewrites the file — review the diff, keep it).

- [ ] **Step 3: Commit**

```bash
git add k8s/backup/README.md
git commit -m "$(cat <<'EOF'
Document the restore-verification drill (#139)

Assisted-by: AI
EOF
)"
```

______________________________________________________________________

### Task 4: Live verification (post-merge, on gandalf)

Runs AFTER the PR merges to main — Flux reconciles the new resources from git.
All commands on **gandalf** with `export KUBECONFIG=~/.kube/config`.

**Files:** none (live checks only).

**Interfaces:**

- Consumes: everything from Tasks 1–3 via Flux reconciliation of `main`.

- Produces: evidence for closing #139.

- [ ] **Step 1: Reconcile and confirm the CronJob exists**

```bash
export KUBECONFIG=~/.kube/config
flux reconcile source git flux-system
flux reconcile kustomization backup --with-source
flux reconcile kustomization homelab-monitoring --with-source
kubectl -n backup get cronjob restic-verify
```

Expected: CronJob listed, `SCHEDULE 0 5 * * 0`, `SUSPEND False`.

- [ ] **Step 2: Green path — manual run**

```bash
kubectl -n backup create job --from=cronjob/restic-verify verify-test-$(date +%s)
kubectl -n backup get jobs --watch
```

Wait for `Complete` (expect 20–45 min: the subset read downloads ~32 GiB).
Then read the logs and confirm all four phase banners and `=== All phases passed ===`:

```bash
kubectl -n backup logs job/$(kubectl -n backup get jobs --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1].metadata.name}') | tail -40
```

- [ ] **Step 3: Red path — deliberate failure**

Confirm a broken sentinel actually fails a Job.
Run a one-off pod (NOT a job from the CronJob — don't pollute its history)
with a bogus assertion:

```bash
kubectl -n backup run verify-red-test --rm -i --restart=Never \
  --image=restic/restic:latest \
  --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"gandalf"},"containers":[{"name":"verify-red-test","image":"restic/restic:latest","stdin":true,"envFrom":[{"secretRef":{"name":"restic-credentials"}}],"command":["/bin/sh","-c","set -euo pipefail; restic restore latest --tag grafana --target /scratch/grafana --verify; test -e /scratch/grafana/backup/grafana/DOES-NOT-EXIST"]}]}}'
echo "exit: $?"
```

Expected: pod fails, non-zero exit — proving sentinel misses terminate the drill.

- [ ] **Step 4: Alert wiring**

Confirm the rule loaded and the metric advanced
(fresh local port per the port-forward gotcha — never reuse 9090 blindly, never `pkill -f port-forward`):

```bash
kubectl -n monitoring port-forward pod/prometheus-kps-kube-prometheus-stack-prometheus-0 9139:9090 >/dev/null 2>&1 &
PF_PID=$!
sleep 3
curl -s 'http://localhost:9139/api/v1/rules' | jq -r '.data.groups[] | select(.name=="homelab") | .rules[] | "\(.name) \(.health) \(.state)"'
curl -s 'http://localhost:9139/api/v1/query' --data-urlencode 'query=time() - max(kube_cronjob_status_last_successful_time{cronjob="restic-verify", namespace="backup"})' | jq -r '.data.result[0].value[1]'
kill $PF_PID
```

Expected: `ResticVerifyStale ok inactive` among the four rules,
and the query returns a small number (seconds since Step 2's run — minutes, not days).

- [ ] **Step 5: Close #139**

Comment with the verification evidence (rule inactive/ok, green run log tail,
red-path failure, metric age) and close the issue.

```bash
gh issue close 139 --repo nickvigilante/homelab --comment "..."
```

(Write the comment from the actual outputs; include the drill's coverage math
and the accepted metric-absent-before-first-success caveat.)
