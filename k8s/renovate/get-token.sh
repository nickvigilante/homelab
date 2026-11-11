#!/bin/sh
# Mint a GitHub App installation token for Renovate and write it to /token/token.
#
# Renovate cannot sign in as a GitHub App on its own: it needs a ready-made
# installation token (ghs_...), and those expire after one hour. This runs as an
# init container, so every run starts with a fresh token and no long-lived
# credential is stored anywhere except the app's private key.
#
# The flow is GitHub's documented one: sign a JWT (RS256, issued by the app id,
# valid for under ten minutes) with the private key, then exchange it for an
# installation token.
set -eu

: "${GITHUB_APP_ID:?GITHUB_APP_ID is not set}"
: "${GITHUB_APP_INSTALLATION_ID:?GITHUB_APP_INSTALLATION_ID is not set}"
KEY_FILE="${KEY_FILE:-/credentials/private-key.pem}"
TOKEN_FILE="${TOKEN_FILE:-/token/token}"
API_URL="${API_URL:-https://api.github.com}"

# The alpine image ships none of these. Best-effort install, like the backup job.
for tool in curl openssl jq; do
  command -v "$tool" >/dev/null 2>&1 || {
    apk add --no-cache curl openssl jq >/dev/null
    break
  }
done

b64url() {
  openssl base64 -A | tr '+/' '-_' | tr -d '='
}

[ -r "$KEY_FILE" ] || {
  echo "private key is not readable at ${KEY_FILE}" >&2
  exit 1
}

now="$(date +%s)"
header="$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)"
payload="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$GITHUB_APP_ID" | b64url)"

# Sign into a file instead of piping: sh has no pipefail, so a failing openssl at
# the head of a pipeline would be masked by the encoder after it.
sig_file="$(mktemp)"
trap 'rm -f "$sig_file"' EXIT
printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "$KEY_FILE" -out "$sig_file"
signature="$(b64url <"$sig_file")"
jwt="${header}.${payload}.${signature}"

response="$(curl -sS -X POST \
  -H "Authorization: Bearer ${jwt}" \
  -H "Accept: application/vnd.github+json" \
  "${API_URL}/app/installations/${GITHUB_APP_INSTALLATION_ID}/access_tokens")"

# `|| true`: a response that is not JSON (a proxy's HTML error page, say) makes jq
# fail, and that should reach the message below instead of killing the script
# with a bare parse error.
token="$(printf '%s' "$response" | jq -r '.token // empty' 2>/dev/null || true)"
if [ -z "$token" ]; then
  # A failed exchange has no token to leak. Print GitHub's message, or the start
  # of the body when it is not JSON.
  message="$(printf '%s' "$response" | jq -r '.message // empty' 2>/dev/null || true)"
  printf 'token exchange failed: %s\n' "${message:-$(printf '%s' "$response" | head -c 200)}" >&2
  exit 1
fi

# World-readable on purpose: the volume is pod-local and in memory, and the
# renovate container runs as a different uid than this one.
umask 022
printf '%s' "$token" >"$TOKEN_FILE"
echo "installation token written to ${TOKEN_FILE}"
