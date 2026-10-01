# shellcheck shell=bash
# Shared Bitwarden unlock for scripts/bw-import-env.sh and
# scripts/bws-bootstrap-secrets.sh. Source it; do not execute it.
#
#   bw_ensure_unlocked   -- leave an unlocked session exported as BW_SESSION,
#                           or return 1 with a FATAL message on stderr.
#
# - An already-exported BW_SESSION that `bw status` reports as unlocked is
#   reused with no prompt, so one `export BW_SESSION="$(bw unlock --raw)"` in
#   the caller's shell covers a multi-script run.
# - Otherwise `bw unlock --raw` is attempted up to BW_UNLOCK_ATTEMPTS times
#   (default 3), so one mistyped master password does not abort the run.
# - Conditions that retrying cannot fix fail fast: `bw` missing, or not logged
#   in (`bw login` first).
# - The master password is read by `bw` itself from the terminal. It never
#   passes through this script, so it cannot be logged or printed here.
#
# BW_UNLOCK_TTY overrides the terminal device (default /dev/tty). It exists so
# tests can run without a controlling terminal. The terminal is used, not the
# script's stdin, because stdin may be a heredoc of tuples.
#
# Callers keep their own `bw sync` after this returns: a stale local cache
# returns empty values.

bw_ensure_unlocked() {
  local max="${BW_UNLOCK_ATTEMPTS:-3}"
  local tty="${BW_UNLOCK_TTY:-/dev/tty}"
  local state session attempt=1

  if ! command -v bw >/dev/null 2>&1; then
    echo "FATAL: bw (Bitwarden CLI) not found on PATH" >&2
    return 1
  fi

  state="$(bw status 2>/dev/null | jq -r '.status // empty' 2>/dev/null)"
  if [ "$state" = "unauthenticated" ]; then
    echo "FATAL: not logged in to Bitwarden -- run 'bw login' first" >&2
    return 1
  fi

  if [ -n "${BW_SESSION:-}" ] && [ "$state" = "unlocked" ]; then
    return 0
  fi

  while [ "$attempt" -le "$max" ]; do
    if session="$(bw unlock --raw <"$tty")" && [ -n "$session" ]; then
      export BW_SESSION="$session"
      return 0
    fi
    echo "Unlock failed (attempt $attempt/$max) -- check the master password." >&2
    attempt=$((attempt + 1))
  done

  echo "FATAL: bw unlock failed $max times (bad password or vault locked)" >&2
  return 1
}
