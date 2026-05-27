#!/usr/bin/env bash
# Delete Authentik signing certificates that are:
#   1. Older than the retention window (default: 30 days), AND
#   2. Not currently referenced by any OAuth2/OIDC provider's signing_key.
#
# Run this after a successful rotate-authentik-signing-cert.sh, once
# enough time has passed for any in-flight refresh tokens signed by the
# old keypair to expire (refresh_token_validity, default 30 days).
#
# Usage:
#   ./cleanup-old-authentik-signing-certs.sh                 # delete certs > 30 days old
#   ./cleanup-old-authentik-signing-certs.sh --days=14       # custom retention
#   ./cleanup-old-authentik-signing-certs.sh --dry-run       # print plan; don't delete
#
# Environment:
#   AUTHENTIK_BASE_URL    Defaults to https://authentik.vigihome.net

set -euo pipefail

AUTHENTIK_BASE_URL="${AUTHENTIK_BASE_URL:-https://authentik.vigihome.net}"
RETENTION_DAYS=30
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --days=*)
      RETENTION_DAYS="${1#*=}"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      echo "Unknown flag: $1" >&2
      exit 2
      ;;
  esac
done

for cmd in kubectl curl jq date; do
  command -v "$cmd" >/dev/null || {
    echo "Required command not found: $cmd" >&2
    exit 3
  }
done

TOKEN=$(kubectl -n auth get secret authentik-secrets -o jsonpath='{.data.bootstrap-token}' | base64 -d)
[ -n "$TOKEN" ] || {
  echo "Empty bootstrap-token in Secret" >&2
  exit 4
}

api() {
  curl -sk --fail-with-body -H "Authorization: Bearer $TOKEN" "$@"
}

# 1. Gather all signing_key references currently in use across every provider.
echo "→ Collecting current signing_key references..."
IN_USE_PKS=$(api "$AUTHENTIK_BASE_URL/api/v3/providers/oauth2/?page_size=200" \
  | jq -r '.results[].signing_key' | sort -u)
echo "  In-use signing_key pks:"
# shellcheck disable=SC2001  # sed is clearer than ${var//search/replace} for multi-line prefixing
echo "$IN_USE_PKS" | sed 's/^/    /'

# 2. List all certs; filter to "name starts with authentik-signing- AND older than retention AND not in use".
echo ""
echo "→ Scanning certificates for cleanup candidates (retention=${RETENTION_DAYS}d)..."

# `date -d ...` (GNU) vs `date -v ...` (BSD/macOS). Try GNU first.
if date -d "$RETENTION_DAYS days ago" >/dev/null 2>&1; then
  CUTOFF_ISO=$(date -u -d "$RETENTION_DAYS days ago" +%Y-%m-%dT%H:%M:%SZ)
else
  CUTOFF_ISO=$(date -u -v-"${RETENTION_DAYS}"d +%Y-%m-%dT%H:%M:%SZ)
fi
echo "  Cutoff: $CUTOFF_ISO"

CERTS=$(api "$AUTHENTIK_BASE_URL/api/v3/crypto/certificatekeypairs/?page_size=200" \
  | jq -r --argjson in_use "$(echo "$IN_USE_PKS" | jq -R . | jq -s .)" \
    --arg cutoff "$CUTOFF_ISO" \
    '.results[] |
         select(.name | startswith("authentik-signing-")) |
         select(.cert_expiry > $cutoff or .cert_expiry == null) |
         select(. as $c | $in_use | index($c.pk) | not) |
         "\(.pk)\t\(.name)\t\(.cert_expiry)"')

if [ -z "$CERTS" ]; then
  echo "  No cleanup candidates found."
  exit 0
fi

echo ""
echo "Cleanup candidates:"
printf "  %-40s  %-40s  %s\n" "PK" "NAME" "EXPIRY"
echo "$CERTS" | while IFS=$'\t' read -r pk name expiry; do
  printf "  %-40s  %-40s  %s\n" "$pk" "$name" "$expiry"
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo ""
  echo "→ Dry run — no certs deleted."
  exit 0
fi

echo ""
echo "→ Deleting..."
echo "$CERTS" | while IFS=$'\t' read -r pk name expiry; do
  if api -X DELETE "$AUTHENTIK_BASE_URL/api/v3/crypto/certificatekeypairs/$pk/" >/dev/null; then
    echo "  ✓ deleted $name ($pk)"
  else
    echo "  ✗ failed to delete $name ($pk)" >&2
  fi
done

echo "Done."
