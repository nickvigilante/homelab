#!/usr/bin/env bash
# Provision (or rotate) the secrets for the requesty-sync CronJob.
#
# What it does:
#   1. Creates a 1-year Coder token for the role-less `requesty-sync` service
#      account, limited to the scopes chat_model_config:read and
#      organization:read (all Coder v2.37.0 lets a token have for this job).
#   2. Stores it, and the Uptime Kuma push URL, as hidden fields on the Bitwarden
#      item `Homelab Requesty Sync` (created if missing, updated otherwise).
#   3. Copies both into BWS as `requesty-sync-coder-token` and
#      `requesty-sync-uptime-push-url` (created if missing, updated otherwise, so
#      a rerun never makes duplicates and the ExternalSecret UUIDs stay valid).
#   4. Writes k8s/requesty-sync/external-secret.yaml with the two BWS secret IDs.
#
# The push URL is rewritten to the in-cluster Uptime Kuma address, because pods
# cannot resolve the external *.vigihome.net hostname the UI shows; only the
# token in the URL you paste is kept. To fix just the push URL later (without
# minting another Coder token), run with --push-url-only.
#
# Run it on gandalf, from a shell where the coder CLI is signed in as an admin.
# It needs bw, bws, jq, and coder. Bitwarden: export BW_SESSION first (from
# `bw unlock --raw`), or it asks for the session key. It also prompts for the
# Uptime Kuma push URL, and on a rerun offers to keep the stored one.
#
# Secrets are read without echo, never printed, and reach jq only through the
# environment. bws takes secret values as arguments (same as bws-migrate.sh),
# so they are visible to other local users of this host while it runs.
set -uo pipefail

PROJECT_ID="c167c5ba-9144-4b04-8a10-b45a01570e69"
BOOTSTRAP_ITEM="Homelab BWS Bootstrap Token"
VAULT_ITEM="Homelab Requesty Sync"
BWS_TOKEN_NAME="requesty-sync-coder-token"
BWS_PUSH_NAME="requesty-sync-uptime-push-url"
SERVICE_ACCOUNT="requesty-sync"
TOKEN_LIFETIME="8760h"
KUMA_INTERNAL="http://uptime-kuma.monitoring.svc.cluster.local:3001"
PUSH_ONLY=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXTERNAL_SECRET_FILE="${EXTERNAL_SECRET_FILE:-$ROOT/k8s/requesty-sync/external-secret.yaml}"

say() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die() {
  echo "error: $*" >&2
  exit 1
}

need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

ask_secret() {
  local reply
  read -r -s -p "$1: " reply
  echo >&2
  printf '%s' "$reply"
}

yesno() {
  local reply
  read -r -p "$1 [y/n]: " reply
  [[ $reply == [yY]* ]]
}

# field_value ITEM_JSON FIELD_NAME
field_value() { jq -r --arg f "$2" '.fields[]? | select(.name == $f) | .value // empty' <<<"$1"; }

check_prerequisites() {
  say "1. Checking prerequisites"
  local t roles
  for t in bw bws jq; do need "$t"; done
  [[ $PUSH_ONLY == 1 ]] || need coder
  if [[ -z ${BW_SESSION:-} ]]; then
    BW_SESSION="$(ask_secret 'Bitwarden session key (from: bw unlock --raw)')"
    export BW_SESSION
  fi
  bw status 2>/dev/null | jq -e '.status == "unlocked"' >/dev/null || die "the Bitwarden vault is not unlocked for this session"
  bw sync >/dev/null || die "bw sync failed"
  note "Bitwarden is unlocked and synced"
  [[ $PUSH_ONLY == 1 ]] && return 0
  roles="$(coder users show "$SERVICE_ACCOUNT" 2>&1 | grep -E '^Roles:' || true)"
  [[ -n $roles ]] || die "coder cannot show the user '$SERVICE_ACCOUNT' (is the CLI signed in as an admin, and does the account exist?)"
  note "$SERVICE_ACCOUNT $roles"
  if [[ $roles == *[Oo]wner* ]]; then
    die "$SERVICE_ACCOUNT still has the Owner role; the limited check does not need it, so remove the role in the dashboard first"
  fi
}

create_token() {
  local out
  say "3. Creating the $TOKEN_LIFETIME token for $SERVICE_ACCOUNT"
  out="$(coder tokens create --user "$SERVICE_ACCOUNT" --name "requesty-sync-check-$(date +%Y%m%d-%H%M%S)" \
    --lifetime "$TOKEN_LIFETIME" --scope chat_model_config:read --scope organization:read 2>&1)"
  TOKEN="$(grep -oE '[A-Za-z0-9]{10}-[A-Za-z0-9]{22}' <<<"$out" | head -1)"
  if [[ -z $TOKEN ]]; then
    echo "coder tokens create did not return a token. Its output was:" >&2
    printf '%s\n' "$out" | sed -E 's/[A-Za-z0-9_-]{20,}/<redacted>/g; s/^/   /' >&2
    exit 1
  fi
  note "created (value not shown); scopes: chat_model_config:read, organization:read"
}

# normalize_push_url URL: prints the in-cluster push URL for the token in URL.
normalize_push_url() {
  local token
  token="$(sed -nE 's#^https?://[^/]+/api/push/([A-Za-z0-9]+).*$#\1#p' <<<"$1")"
  [[ -n $token ]] || return 1
  printf '%s/api/push/%s' "$KUMA_INTERNAL" "$token"
}

ask_push_url() {
  local stored="$1" url
  say "2. Uptime Kuma push URL"
  if [[ -n $stored ]] && yesno "Keep the push URL already stored in Bitwarden?"; then
    url="$stored"
  else
    url="$(ask_secret 'Push URL (from the requesty-sync push monitor)')"
  fi
  PUSH_URL="$(normalize_push_url "$url")" || die "that does not look like an Uptime Kuma push URL (expected .../api/push/<token>)"
  note "stored as the in-cluster address; only the token from the URL you gave is kept"
}

# vault_item: the item named exactly $VAULT_ITEM as JSON, or nothing. `bw get item`
# matches by substring and refuses when several items match, so list and filter.
vault_item() {
  local list matches
  list="$(bw list items --search "$VAULT_ITEM" 2>/dev/null)" || die "could not search Bitwarden"
  matches="$(jq -c --arg n "$VAULT_ITEM" '[.[] | select(.name == $n)]' <<<"$list")" || die "Bitwarden returned something that is not a list of items"
  [[ "$(jq length <<<"$matches")" -le 1 ]] || die "more than one Bitwarden item is named exactly '$VAULT_ITEM'; remove the extras"
  jq -c '.[0] // empty' <<<"$matches"
}

store_in_bitwarden() {
  local existing="$1" item id
  say "4. Storing in Bitwarden ($VAULT_ITEM)"
  if [[ -n $existing ]]; then
    id="$(jq -r '.id' <<<"$existing")"
    item="$(T="${TOKEN:-}" U="$PUSH_URL" jq '
      def setfield($n; $v): (.fields // []) as $f
        | if any($f[]; .name == $n) then (.fields = [$f[] | if .name == $n then .value = $v else . end])
          else (.fields = $f + [{name: $n, value: $v, type: 1}]) end;
      (if ($ENV.T // "") != "" then setfield("coder-token"; $ENV.T) else . end)
      | setfield("uptime-kuma-push-url"; $ENV.U)' <<<"$existing")"
    bw encode <<<"$item" | bw edit item "$id" >/dev/null || die "could not update the Bitwarden item"
    note "updated the existing item"
  else
    [[ $PUSH_ONLY == 1 ]] && die "there is no '$VAULT_ITEM' item to update; run without --push-url-only first"
    item="$(bw get template item | T="$TOKEN" U="$PUSH_URL" N="$VAULT_ITEM" jq '
      .type = 2 | .secureNote = {type: 0} | .name = $ENV.N
      | .notes = "Secrets for the requesty-sync CronJob (k8s/requesty-sync). The Coder token lasts one year: rotate it with scripts/requesty-sync-provision.sh."
      | .fields = [{name: "coder-token", value: $ENV.T, type: 1}, {name: "uptime-kuma-push-url", value: $ENV.U, type: 1}]')"
    bw encode <<<"$item" | bw create item >/dev/null || die "could not create the Bitwarden item"
    note "created the item"
  fi
}

# upsert_bws NAME VALUE: prints the secret's UUID.
upsert_bws() {
  local name="$1" value="$2" id out
  id="$(jq -r --arg k "$name" '[.[] | select(.key == $k)][0].id // empty' <<<"$BWS_LIST")"
  if [[ -n $id ]]; then
    bws secret edit --value "$value" "$id" >/dev/null 2>&1 || die "could not update BWS secret $name"
    note "updated $name" >&2
  else
    out="$(bws secret create "$name" "$value" "$PROJECT_ID" 2>&1)" || die "could not create BWS secret $name"
    id="$(jq -r '.id // empty' <<<"$out")"
    [[ -n $id ]] || die "created $name but could not read its id"
    note "created $name" >&2
  fi
  printf '%s' "$id"
}

push_to_bws() {
  local boot
  say "5. Copying both into BWS"
  boot="$(bw get item "$BOOTSTRAP_ITEM" | jq -r '.notes // empty')"
  [[ -n $boot ]] || die "no access token in the Bitwarden item '$BOOTSTRAP_ITEM' (.notes)"
  export BWS_ACCESS_TOKEN="$boot"
  BWS_LIST="$(bws secret list "$PROJECT_ID" 2>/dev/null)" || die "could not list BWS secrets"
  if [[ -n ${TOKEN:-} ]]; then
    TOKEN_ID="$(upsert_bws "$BWS_TOKEN_NAME" "$TOKEN")" || exit 1
  fi
  PUSH_ID="$(upsert_bws "$BWS_PUSH_NAME" "$PUSH_URL")" || exit 1
  unset BWS_ACCESS_TOKEN
}

write_external_secret() {
  say "6. Writing $EXTERNAL_SECRET_FILE"
  mkdir -p "$(dirname "$EXTERNAL_SECRET_FILE")"
  cat >"$EXTERNAL_SECRET_FILE" <<EOF
# Credentials for the requesty-sync CronJob, sourced from BWS. Both keys are
# read via \`envFrom: secretRef:\`, so the key names are the env var names:
#
#   - CODER_SESSION_TOKEN: a 1-year token for the role-less \`requesty-sync\`
#     Coder service account, limited to the scopes chat_model_config:read and
#     organization:read. It can read the model list and nothing else. The
#     Requesty API key is deliberately NOT here; the check needs only the
#     public catalog.
#   - UPTIME_KUMA_PUSH_URL: push URL (no query string) of the \`requesty-sync\`
#     Uptime Kuma push monitor.
#
# Source of truth is the Bitwarden item \`$VAULT_ITEM\`; BWS holds the
# cluster-readable copies. Rotate with scripts/requesty-sync-provision.sh.
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: requesty-sync-secrets
  namespace: coder
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: bitwarden
    kind: ClusterSecretStore
  target:
    name: requesty-sync-secrets
    creationPolicy: Owner
  data:
    - secretKey: CODER_SESSION_TOKEN
      remoteRef:
        key: $TOKEN_ID # gitleaks:allow
    - secretKey: UPTIME_KUMA_PUSH_URL
      remoteRef:
        key: $PUSH_ID # gitleaks:allow
EOF
  note "wrote it with the two BWS secret IDs (identifiers, not credentials)"
}

main() {
  local existing stored=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --push-url-only) PUSH_ONLY=1 ;;
      *) die "unknown option: $1 (only --push-url-only is supported)" ;;
    esac
    shift
  done
  check_prerequisites
  existing="$(vault_item)"
  stored="$(field_value "$existing" uptime-kuma-push-url)"
  # Validate the URL before minting a token, so a typo leaves no unused token behind.
  ask_push_url "$stored"
  if [[ $PUSH_ONLY != 1 ]]; then
    create_token
  fi
  store_in_bitwarden "$existing"
  push_to_bws
  if [[ $PUSH_ONLY != 1 ]]; then
    write_external_secret
  fi
  say "Done"
  if [[ $PUSH_ONLY == 1 ]]; then
    note "Updated only the push URL. Run: flux reconcile externalsecret -n coder requesty-sync-secrets"
  else
    note "Token expires in about a year; a Todoist reminder covers the rotation."
    note "Next: review and commit k8s/requesty-sync/external-secret.yaml, then continue with the manifests."
  fi
  unset TOKEN PUSH_URL BW_SESSION
}

main "$@"
