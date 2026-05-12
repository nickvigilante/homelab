#!/usr/bin/env bash
# Mint a short-lived, single-use Tailscale auth key tagged `tag:homelab`
# and write it to ~/.tailscale-authkey for the provision-pi playbook to
# pick up.
#
# Why this script exists:
#   The Ansible playbook needs a Tailscale auth key to bring a fresh Pi
#   onto the tailnet with `tag:homelab` (which the ACL grants
#   homelab-cluster access to). Hand-clicking a key in the admin UI
#   works fine but expires in 90 days and ages out unused. Minting via
#   OAuth gives us a fresh key on demand with a shorter expiry — the
#   OAuth client itself doesn't expire, so the rotation is self-service.
#
# Requires:
#   - `~/.homelab-opentofu.env` with TF_VAR_tailscale_oauth_client_{id,secret}
#   - The `opentofu-homelab` OAuth client must have the `auth_keys` scope
#     (write) and `tag:homelab` in its allowed tags. See `../README.md`.
#   - curl + jq on PATH
#
# Usage:
#   ./bin/mint-tailscale-authkey.sh                    # 1h expiry (default)
#   ./bin/mint-tailscale-authkey.sh --expiry 24h       # human-friendly duration
#   ./bin/mint-tailscale-authkey.sh --expiry 3600      # explicit seconds
#   ./bin/mint-tailscale-authkey.sh --out /tmp/key     # custom output path

set -euo pipefail

# ---- Defaults ----------------------------------------------------------
EXPIRY_HUMAN="1h"
OUT_PATH="${HOME}/.tailscale-authkey"
ENV_FILE="${HOME}/.homelab-opentofu.env"
TAG="tag:homelab"

# ---- Argument parsing --------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --expiry)
      EXPIRY_HUMAN="$2"
      shift 2
      ;;
    --out)
      OUT_PATH="$2"
      shift 2
      ;;
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --tag)
      TAG="$2"
      shift 2
      ;;
    -h | --help)
      sed -n '2,30p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

# ---- Convert human duration to seconds ---------------------------------
to_seconds() {
  local v="$1"
  case "$v" in
    *h) echo "$(( ${v%h} * 3600 ))" ;;
    *m) echo "$(( ${v%m} * 60 ))" ;;
    *d) echo "$(( ${v%d} * 86400 ))" ;;
    *s) echo "${v%s}" ;;
    *[!0-9]*)
      echo "Invalid expiry: $v (expected like 1h, 30m, 7d, or seconds)" >&2
      exit 64
      ;;
    *) echo "$v" ;;
  esac
}
EXPIRY_SECONDS="$(to_seconds "$EXPIRY_HUMAN")"

# ---- Source OAuth credentials ------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing OAuth env file at $ENV_FILE" >&2
  exit 66
fi
# shellcheck disable=SC1090
set -a && source "$ENV_FILE" && set +a

: "${TF_VAR_tailscale_oauth_client_id:?TF_VAR_tailscale_oauth_client_id unset (check $ENV_FILE)}"
: "${TF_VAR_tailscale_oauth_client_secret:?TF_VAR_tailscale_oauth_client_secret unset (check $ENV_FILE)}"

# ---- Exchange client credentials for an access token -------------------
ACCESS_TOKEN="$(
  curl -sS --fail \
    -u "${TF_VAR_tailscale_oauth_client_id}:${TF_VAR_tailscale_oauth_client_secret}" \
    -d "grant_type=client_credentials&scope=auth_keys" \
    https://api.tailscale.com/api/v2/oauth/token | jq -r .access_token
)"

if [[ -z "$ACCESS_TOKEN" || "$ACCESS_TOKEN" == "null" ]]; then
  echo "OAuth token exchange failed. Verify the opentofu-homelab client has the 'auth_keys' scope." >&2
  exit 77
fi

# ---- Build the create-key request body ---------------------------------
# `description` is shown in the admin UI key list — date-stamped so old
# unused keys are easy to spot. Tailscale's description field rejects
# colons (and other punctuation), so use a colon-free compact ISO-ish
# format instead of `+%FT%TZ`.
# The capabilities block enforces what the downstream device can do:
# single-use, non-ephemeral, auto-tagged.
DESCRIPTION="ansible-provisioning-$(date -u +%Y%m%dT%H%M%SZ)"
BODY="$(
  jq -nc \
    --arg desc "$DESCRIPTION" \
    --arg tag "$TAG" \
    --argjson expiry "$EXPIRY_SECONDS" \
    '{
      description: $desc,
      expirySeconds: $expiry,
      capabilities: {
        devices: {
          create: {
            reusable: false,
            ephemeral: false,
            preauthorized: true,
            tags: [$tag]
          }
        }
      }
    }'
)"

# ---- Mint the key ------------------------------------------------------
RESPONSE="$(
  curl -sS --fail \
    -X POST \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$BODY" \
    https://api.tailscale.com/api/v2/tailnet/-/keys
)"

KEY="$(echo "$RESPONSE" | jq -r .key)"
KEY_ID="$(echo "$RESPONSE" | jq -r .id)"
EXPIRES="$(echo "$RESPONSE" | jq -r .expires)"

if [[ -z "$KEY" || "$KEY" == "null" ]]; then
  echo "Key creation returned no key. Response was:" >&2
  echo "$RESPONSE" >&2
  exit 78
fi

# ---- Write to file -----------------------------------------------------
umask 077
printf '%s\n' "$KEY" > "$OUT_PATH"
chmod 600 "$OUT_PATH"

# ---- Report ------------------------------------------------------------
cat <<EOF
Minted Tailscale auth key
  id:          $KEY_ID
  description: $DESCRIPTION
  expires:     $EXPIRES
  tag:         $TAG
  path:        $OUT_PATH (mode 0600)

Next:
  ansible-playbook provision-pi.yml --limit <hostname> \\
    --extra-vars "tailscale_authkey=\$(cat $OUT_PATH)"
EOF
