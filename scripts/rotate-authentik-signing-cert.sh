#!/usr/bin/env bash
# Rotate Authentik's OIDC signing certificate across every OAuth2 provider.
#
# Designed to scale: auto-discovers providers via Authentik's API, so
# adding a new OIDC integration doesn't require touching this script.
# Run annually (or on demand) from gandalf or the MacBook.
#
# What it does:
#   1. Reads the Authentik admin API token from the k8s Secret
#      `auth/authentik-secrets`.
#   2. Generates a new self-signed signing keypair via Authentik's API.
#   3. Lists every OAuth2/OIDC provider and PATCHes each one's
#      `signing_key` field to the new keypair.
#   4. Restarts `authentik-server` for a clean JWKS cache.
#   5. Verifies every Application's JWKS endpoint serves the new key.
#   6. (Optional, if SMTP creds present) Emails a summary of the run.
#
# What it intentionally does NOT do:
#   - Delete the previous signing cert. In-flight refresh tokens signed
#     by the old key remain valid until they expire (30 days by default).
#     Use `cleanup-old-authentik-signing-certs.sh` after the retention
#     window to remove unreferenced old keypairs.
#   - Touch downstream OIDC clients (Coder, HA, etc). They re-fetch JWKS
#     automatically and pick up the new `kid` on next token validation.
#
# Usage:
#   ./rotate-authentik-signing-cert.sh
#   ./rotate-authentik-signing-cert.sh --key-type=rsa
#   AUTHENTIK_BASE_URL=https://authentik.example.com ./rotate-authentik-signing-cert.sh
#
# Flags:
#   --key-type=ecdsa|rsa  Key algorithm (default: ecdsa — smaller tokens, faster verify)
#   --name=NAME           Override the auto-generated cert name
#   --dry-run             Print the plan and exit without changes
#   --no-restart          Skip the authentik-server rollout restart
#   --quiet               Suppress per-step output (still prints final summary)
#
# Environment:
#   AUTHENTIK_BASE_URL    Defaults to https://authentik.vigihome.net
#   SMTP_HOST,
#   SMTP_PORT,
#   SMTP_USER,
#   SMTP_PASS,
#   SMTP_FROM,
#   SMTP_TO              If all set, the script emails a run summary.
#                        Sourced from the k8s Secret `smtp-relay` in
#                        whatever NS calls this — or set explicitly.
#                        Skipped silently if any are unset.

set -euo pipefail

# ───────────────────────────────── config ──────────────────────────────────

AUTHENTIK_BASE_URL="${AUTHENTIK_BASE_URL:-https://authentik.vigihome.net}"
KEY_TYPE="ecdsa"
CERT_NAME="authentik-signing-$(date -u +%Y%m%d)"
DRY_RUN=0
DO_RESTART=1
QUIET=0

while [ $# -gt 0 ]; do
    case "$1" in
        --key-type=*) KEY_TYPE="${1#*=}"; shift ;;
        --name=*) CERT_NAME="${1#*=}"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --no-restart) DO_RESTART=0; shift ;;
        --quiet) QUIET=1; shift ;;
        *) echo "Unknown flag: $1" >&2; exit 2 ;;
    esac
done

case "$KEY_TYPE" in
    ecdsa|rsa) ;;
    *) echo "Invalid --key-type: must be 'ecdsa' or 'rsa'" >&2; exit 2 ;;
esac

# ───────────────────────────────── helpers ─────────────────────────────────

say() {
    [ "$QUIET" -eq 1 ] || echo "$@"
}

# Print to stderr regardless of --quiet.
err() {
    echo "$@" >&2
}

# Call Authentik API with auth header. Args: HTTP method, path, optional JSON body.
api() {
    local method="$1"
    local path="$2"
    local body="${3:-}"
    if [ -n "$body" ]; then
        curl -sk --fail-with-body \
            -H "Authorization: Bearer $TOKEN" \
            -H "Content-Type: application/json" \
            -X "$method" \
            -d "$body" \
            "$AUTHENTIK_BASE_URL/api/v3$path"
    else
        curl -sk --fail-with-body \
            -H "Authorization: Bearer $TOKEN" \
            -X "$method" \
            "$AUTHENTIK_BASE_URL/api/v3$path"
    fi
}

# Send a summary email via SMTP if all SMTP_* env vars are set; no-op otherwise.
maybe_send_email() {
    local subject="$1"
    local body="$2"

    if [ -z "${SMTP_HOST:-}" ] || [ -z "${SMTP_PORT:-}" ] || \
       [ -z "${SMTP_USER:-}" ] || [ -z "${SMTP_PASS:-}" ] || \
       [ -z "${SMTP_FROM:-}" ] || [ -z "${SMTP_TO:-}" ]; then
        say "  (skipping email — SMTP_* env vars not set)"
        return 0
    fi

    # Use Python's stdlib smtplib for portability. Avoids tool sprawl
    # (no swaks/mailx/curl-smtp quirks across distros). Pass subject +
    # body via env vars rather than interpolating into Python source —
    # otherwise a provider name containing triple-quotes breaks parsing.
    EMAIL_SUBJECT="$subject" EMAIL_BODY="$body" python3 - <<'PY'
import os, smtplib, ssl
from email.message import EmailMessage

msg = EmailMessage()
msg["Subject"] = os.environ["EMAIL_SUBJECT"]
msg["From"]    = os.environ["SMTP_FROM"]
msg["To"]      = os.environ["SMTP_TO"]
msg.set_content(os.environ["EMAIL_BODY"])

ctx = ssl.create_default_context()
with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), context=ctx) as s:
    s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
    s.send_message(msg)
PY
    say "  Email sent to $SMTP_TO"
}

# ───────────────────────────────── preflight ───────────────────────────────

for cmd in kubectl curl jq python3; do
    command -v "$cmd" >/dev/null || { err "Required command not found: $cmd"; exit 3; }
done

say "→ Reading Authentik admin token from k8s Secret auth/authentik-secrets..."
TOKEN=$(kubectl -n auth get secret authentik-secrets -o jsonpath='{.data.bootstrap-token}' | base64 -d)
[ -n "$TOKEN" ] || { err "Empty bootstrap-token in Secret — cannot continue"; exit 4; }

# Sanity-check API auth before doing anything mutating.
ME=$(api GET "/core/users/me/" | jq -r '.user.username // "?"')
[ "$ME" != "?" ] || { err "API auth failed — token rejected by Authentik"; exit 5; }
say "  Authenticated as: $ME"

# ─────────────────────────── enumerate providers ───────────────────────────

say "→ Discovering OAuth2/OIDC providers..."
PROVIDERS_JSON=$(api GET "/providers/oauth2/?page_size=200&ordering=name")
PROVIDER_PKS=$(echo "$PROVIDERS_JSON" | jq -r '.results[].pk')
PROVIDER_COUNT=$(echo "$PROVIDER_PKS" | wc -l | tr -d ' ')

if [ "$PROVIDER_COUNT" -eq 0 ] || [ -z "$PROVIDER_PKS" ]; then
    err "No OAuth2 providers found — nothing to rotate. Bailing."
    exit 0
fi

say "  Found $PROVIDER_COUNT provider(s):"
echo "$PROVIDERS_JSON" | jq -r '.results[] | "    \(.pk) \(.name)"' | while IFS= read -r line; do
    say "$line"
done

if [ "$DRY_RUN" -eq 1 ]; then
    say ""
    say "→ Dry run — would generate cert '$CERT_NAME' (alg=$KEY_TYPE) and swap signing_key on the $PROVIDER_COUNT provider(s) above."
    exit 0
fi

# ─────────────────────────── generate new cert ─────────────────────────────

say "→ Generating new signing certificate: $CERT_NAME (alg=$KEY_TYPE, validity=365d)..."
GENERATE_BODY=$(jq -n \
    --arg name "$CERT_NAME" \
    --arg alg "$KEY_TYPE" \
    '{common_name: $name, subject_alt_name: "", validity_days: 365, alg: $alg}')
NEW_CERT=$(api POST "/crypto/certificatekeypairs/generate/" "$GENERATE_BODY")
NEW_CERT_PK=$(echo "$NEW_CERT" | jq -r '.pk')
[ -n "$NEW_CERT_PK" ] && [ "$NEW_CERT_PK" != "null" ] || \
    { err "Failed to generate cert: $NEW_CERT"; exit 6; }
say "  New cert pk=$NEW_CERT_PK"

# ─────────────────────────── swap signing_key ──────────────────────────────

say "→ Swapping signing_key on each provider..."
SWAPPED_PROVIDERS=""
PATCH_BODY=$(jq -n --arg pk "$NEW_CERT_PK" '{signing_key: $pk}')
for pk in $PROVIDER_PKS; do
    NAME=$(echo "$PROVIDERS_JSON" | jq -r ".results[] | select(.pk==$pk) | .name")
    if api PATCH "/providers/oauth2/$pk/" "$PATCH_BODY" >/dev/null; then
        say "  ✓ $NAME"
        SWAPPED_PROVIDERS+="$NAME (pk=$pk)"$'\n'
    else
        err "  ✗ $NAME — PATCH failed; rotation is now in mixed state"
        err "    Investigate manually before retrying."
        exit 7
    fi
done

# ─────────────────────────── restart server ────────────────────────────────

if [ "$DO_RESTART" -eq 1 ]; then
    say "→ Restarting authentik-server for clean JWKS cache..."
    kubectl -n auth rollout restart deployment/authentik-server >/dev/null
    kubectl -n auth rollout status deployment/authentik-server --timeout=120s >/dev/null
    say "  Rolled out cleanly"
fi

# ─────────────────────────── verify JWKS ───────────────────────────────────

say "→ Verifying JWKS endpoints..."
VERIFIED=""
FAILED=""
for slug in $(api GET "/core/applications/?page_size=200" | jq -r '.results[].slug'); do
    JWKS_URL="$AUTHENTIK_BASE_URL/application/o/$slug/jwks/"
    # Use a fresh curl (no auth header — JWKS is public)
    JWKS=$(curl -sk "$JWKS_URL")
    KIDS=$(echo "$JWKS" | jq -r '.keys[].kid' 2>/dev/null | tr '\n' ',' | sed 's/,$//')
    if [ -n "$KIDS" ]; then
        say "  ✓ $slug — kid(s): $KIDS"
        VERIFIED+="$slug"$'\n'
    else
        err "  ✗ $slug — JWKS endpoint returned no keys"
        FAILED+="$slug"$'\n'
    fi
done

# ─────────────────────────── summary + email ───────────────────────────────

SUMMARY=$(printf '%s\n' \
    "Authentik signing-cert rotation complete" \
    "" \
    "New cert: $CERT_NAME (pk=$NEW_CERT_PK, alg=$KEY_TYPE)" \
    "" \
    "Providers updated:" \
    "$(printf '%s' "$SWAPPED_PROVIDERS" | sed 's/^/  /')" \
    "JWKS endpoints verified ($(printf '%s' "$VERIFIED" | grep -c .)):" \
    "$(printf '%s' "$VERIFIED" | sed 's/^/  /')" \
)

if [ -n "$FAILED" ]; then
    SUMMARY+=$'\n'"WARNING — JWKS verification failed for:"
    SUMMARY+=$'\n'"$(printf '%s' "$FAILED" | sed 's/^/  /')"
fi

SUMMARY+=$'\n\n'"Cleanup reminder: in 30+ days, run scripts/cleanup-old-authentik-signing-certs.sh to delete the previous keypair."

say ""
say "$SUMMARY"

maybe_send_email "[homelab] Authentik signing-cert rotation: $CERT_NAME" "$SUMMARY"

if [ -n "$FAILED" ]; then
    exit 8
fi
