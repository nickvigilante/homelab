#!/usr/bin/env bash
# Verify a list of BWS keys are visible to the in-cluster flux-eso token.
#
# Pre-merge sanity check before opening an ExternalSecret PR (#161): if a
# referenced key doesn't exist in BWS, the ExternalSecret lands in
# SecretSyncedError post-reconcile with a context-deadline error pointing
# at the missing key, requiring a rollback. This script catches it ahead
# of merge.
#
# Usage (run from gandalf, where k3s kubectl + bws are installed):
#   ./scripts/bws-verify-keys.sh grafana-admin-user grafana-admin-password
#
# Reads the read-only flux-eso access token from
# external-secrets/bws-access-token and lists all secrets visible to it.
# Never echoes the token or secret values; only key NAMES are printed.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <key> [<key> ...]" >&2
  exit 2
fi

BWS_TOKEN_B64="$(k3s kubectl -n external-secrets get secret bws-access-token \
  -o jsonpath='{.data.token}')"
if [ -z "$BWS_TOKEN_B64" ]; then
  echo "error: external-secrets/bws-access-token has no .data.token field" >&2
  exit 1
fi
BWS_ACCESS_TOKEN="$(printf '%s' "$BWS_TOKEN_B64" | base64 -d)"
export BWS_ACCESS_TOKEN
unset BWS_TOKEN_B64

# List ALL secrets visible to this token -- no project filter. The
# flux-eso machine account is project-scoped at the access level, so the
# resulting list is implicitly the homelab project.
mapfile -t PRESENT_KEYS < <(bws secret list | jq -r '.[].key')

if [ "${#PRESENT_KEYS[@]}" -eq 0 ]; then
  echo "error: bws returned 0 secrets -- token may lack secret-read perms" >&2
  unset BWS_ACCESS_TOKEN
  exit 1
fi

rc=0
for key in "$@"; do
  if printf '%s\n' "${PRESENT_KEYS[@]}" | grep -qx "$key"; then
    printf 'OK      %s\n' "$key"
  else
    printf 'MISSING %s\n' "$key"
    rc=1
  fi
done

unset BWS_ACCESS_TOKEN
exit "$rc"
