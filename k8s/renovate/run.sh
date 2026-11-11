#!/bin/sh
# Run Renovate once, then report the outcome to the Uptime Kuma push monitor.
#
# Uptime Kuma is the dead-man's switch: a run that never reaches this script
# (the token step failed, the pod never scheduled) sends no ping at all, and the
# monitor goes DOWN when its heartbeat interval expires.
set -u

# The renovate image has node but not necessarily wget, so the ping uses node's
# built-in fetch. Best-effort: a monitoring hiccup must not fail the run.
push() {
  [ -n "${UPTIME_KUMA_PUSH_URL:-}" ] || return 0
  node -e '
    const url = process.env.UPTIME_KUMA_PUSH_URL + "?status=" + process.argv[1] + "&msg=" + encodeURIComponent(process.argv[2]);
    fetch(url, { signal: AbortSignal.timeout(10000) }).catch(() => {});
  ' "$1" "$2" || true
}

renovate
rc=$?

if [ "$rc" -eq 0 ]; then
  push up OK
else
  push down "renovate exited with status ${rc}"
fi
exit "$rc"
