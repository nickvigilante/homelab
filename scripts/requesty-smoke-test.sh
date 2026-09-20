#!/usr/bin/env bash
# Interactive smoke test for the Requesty to Coder sync (scripts/requesty-coder-sync.py).
#
# Settles, before anything is registered for real:
#   - whether Requesty works as a Coder AI provider of each type, and which base
#     URL the Anthropic type needs
#   - whether third-party-hosted models work on the native provider types
#   - whether the Requesty logos render
#   - whether a role-less service account with a two-scope token can read the model list
#     and nothing else (for the CronJob's limited check)
#   - the Coder API behaviours the sync relies on (see api_probes)
#
# It creates only objects named smoke-* and deletes them when it finishes, also
# on Ctrl-C. The one thing it can leave behind is the optional `requesty-sync`
# service account, which the CronJob token needs later.
#
# The chats run through the Coder Agents API, so the only browser step left is the
# logo check.
#
# Usage, on gandalf (needs curl and jq, plus the coder CLI for the optional
# token and service-account steps):
#   scripts/requesty-smoke-test.sh              the whole walkthrough
#   scripts/requesty-smoke-test.sh --role-only  only the narrow-token probe
#
# It prompts for what it needs. Set CODER_URL, CODER_SESSION_TOKEN or
# REQUESTY_API_KEY in the environment to skip those prompts. Secrets are read
# without echo and reach curl only through a config file descriptor, never on a
# command line, so they do not show up in `ps`. The results file it writes
# (./requesty-smoke-results.md) contains no secrets.
set -uo pipefail

REQUESTY_URL="${REQUESTY_URL:-https://router.requesty.ai}"
CODER_URL="${CODER_URL:-https://coder.vigihome.net}"
LOGO_BASE="https://www.requesty.ai/provider_logos/v2"
RESULTS_FILE="${SMOKE_RESULTS_FILE:-./requesty-smoke-results.md}"
CODER_SESSION_TOKEN="${CODER_SESSION_TOKEN:-}"
REQUESTY_API_KEY="${REQUESTY_API_KEY:-}"
PROMPT="Reply with the single word ok."
RUN_ID="$(date +%s)"

ORG=""
CAT=""
CLEANED=0
ROLE_ONLY=0
BODY=""
HTTP_STATUS=""
ANTH_BASES=()
MODEL_IDS=()
declare -A MODEL=()
EXTRA_KEYS=(anthropic_3p google_3p gemma_google gemma_openai)
declare -A EXTRA_TYPE=([anthropic_3p]=anthropic [google_3p]=google [gemma_google]=google [gemma_openai]=openai)
declare -A EXTRA_MODEL=()
declare -A EXTRA_DISPLAY=()
declare -A PROVIDER_ID=()
declare -A MODEL_CFG=()
CHAT_IDS=()
CHAT_RESULT=""
CHAT_COST=""
TOTAL_COST_MICROS=0
LAST_MODEL_ID=""
CHAT_TIMEOUT="${SMOKE_CHAT_TIMEOUT:-120}"
CHAT_POLL="${SMOKE_POLL_SECONDS:-2}"
declare -A RESULT=()

say() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required tool: $1" >&2
    exit 2
  }
}

# ask PROMPT [DEFAULT]: prints the reply (the prompt goes to stderr).
ask() {
  local reply
  read -r -p "$1${2:+ [$2]}: " reply
  printf '%s' "${reply:-${2:-}}"
}

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

coder_api() {
  curl -sS -K <(printf 'header = "Coder-Session-Token: %s"\n' "$CODER_SESSION_TOKEN") \
    -H 'Content-Type: application/json' "$@"
}

# coder_json METHOD PATH [BODY]: sets BODY and HTTP_STATUS (call it directly,
# not inside $(...), so the globals survive).
coder_json() {
  local method=$1 path=$2 body=${3:-} out
  if [[ -n $body ]]; then
    out="$(printf '%s' "$body" | coder_api -X "$method" "$CODER_URL$path" -d @- -w '\n%{http_code}')"
  else
    out="$(coder_api -X "$method" "$CODER_URL$path" -w '\n%{http_code}')"
  fi
  HTTP_STATUS="${out##*$'\n'}"
  BODY="${out%$'\n'*}"
}

requesty_openai() {
  jq -n --arg m "$1" --arg p "$PROMPT" \
    '{model: $m, max_tokens: 32, messages: [{role: "user", content: $p}]}' \
    | curl -sS "$REQUESTY_URL/v1/chat/completions" \
      -K <(printf 'header = "Authorization: Bearer %s"\n' "$REQUESTY_API_KEY") \
      -H 'Content-Type: application/json' -d @-
}

requesty_anthropic_code() {
  jq -n --arg m "$2" --arg p "$PROMPT" \
    '{model: $m, max_tokens: 32, messages: [{role: "user", content: $p}]}' \
    | curl -sS -o /dev/null -w '%{http_code}' "$1" \
      -K <(printf 'header = "x-api-key: %s"\n' "$REQUESTY_API_KEY") \
      -H 'anthropic-version: 2023-06-01' -H 'Content-Type: application/json' -d @-
}

# The cheapest first-party model of a lab: plain (no @region or :tier suffix),
# tool-capable, not retiring, not free.
pick_first_party() {
  jq -r --arg lab "$1" '[.data[] | select((.id | startswith($lab + "/")) and (.id | test("[@:]") | not) and .supports_tool_calling and .retires == null and .input_price > 0)] | min_by(.output_price) | .id // empty' <<<"$CAT"
}

# The same, but hosted somewhere other than the lab itself, and limited to the
# lab's flagship family (a small open model is a poor probe for a native type).
pick_third_party() {
  jq -r --arg lab "$1" --arg family "$2" '[.data[] | select(.model_lab == $lab and (.model_canonical_name | startswith($family)) and (.id | startswith($lab + "/") | not) and (.id | test("[@:]") | not) and .supports_tool_calling and .retires == null and .input_price > 0)] | min_by(.output_price) | .id // empty' <<<"$CAT"
}

cleanup() {
  local id name
  CLEANED=1
  [[ -n $ORG && -n $CODER_SESSION_TOKEN ]] || return 0
  # Nothing was created (for example in --role-only mode), so there is nothing to clean up.
  ((${#CHAT_IDS[@]} + ${#MODEL_IDS[@]} + ${#PROVIDER_ID[@]} > 0)) || return 0
  say "Cleaning up the smoke-* objects"
  for id in "${CHAT_IDS[@]}"; do
    coder_api -X PATCH "$CODER_URL/api/v2/chats/$id" -d '{"archived": true}' >/dev/null
  done
  for id in "${MODEL_IDS[@]}"; do
    coder_api -X DELETE "$CODER_URL/api/v2/organizations/$ORG/chats/models/$id" >/dev/null
  done
  for name in "${!PROVIDER_ID[@]}"; do
    coder_api -X DELETE "$CODER_URL/api/v2/ai/providers/smoke-$name" >/dev/null
  done
  note "Providers left named smoke-*: $(coder_api "$CODER_URL/api/v2/ai/providers" | jq -r '[.[].name | select(startswith("smoke-"))] | length')"
}

on_exit() {
  [[ $CLEANED == 1 ]] || cleanup
}

get_credentials() {
  local version out
  say "1. Credentials"
  CODER_URL="$(ask 'Coder URL' "$CODER_URL")"
  if [[ -z $CODER_SESSION_TOKEN ]]; then
    CODER_SESSION_TOKEN="$(ask_secret 'Coder admin token (press Enter to create a 2h one with the coder CLI)')"
  fi
  if [[ -z $CODER_SESSION_TOKEN ]]; then
    need coder
    note "Creating a 2h token with the coder CLI (needs 'coder login $CODER_URL' as an admin)"
    out="$(CODER_URL="$CODER_URL" coder tokens create --name "requesty-smoke-$RUN_ID" --lifetime 2h 2>&1)"
    CODER_SESSION_TOKEN="$(grep -oE '[A-Za-z0-9]{10}-[A-Za-z0-9]{22}' <<<"$out" | head -1)"
    if [[ -z $CODER_SESSION_TOKEN ]]; then
      echo "coder tokens create did not return a token. Its output was:" >&2
      printf '%s\n' "$out" | sed 's/^/   /' >&2
      echo "If it says you are signed out or the session expired, run: coder login $CODER_URL" >&2
      exit 2
    fi
  fi
  [[ -n $CODER_SESSION_TOKEN ]] || {
    echo "no Coder token" >&2
    exit 2
  }
  # The role probe never talks to Requesty, so it does not need the key.
  if [[ $ROLE_ONLY != 1 ]]; then
    [[ -n $REQUESTY_API_KEY ]] || REQUESTY_API_KEY="$(ask_secret 'Requesty API key')"
    [[ -n $REQUESTY_API_KEY ]] || {
      echo "no Requesty API key" >&2
      exit 2
    }
  fi
  export CODER_URL CODER_SESSION_TOKEN REQUESTY_API_KEY
  coder_json GET /api/v2/buildinfo
  version="$(jq -r '.version // empty' <<<"$BODY" 2>/dev/null)"
  [[ -n $version ]] || {
    echo "cannot reach Coder at $CODER_URL (HTTP ${HTTP_STATUS:-none})" >&2
    exit 2
  }
  RESULT[coder_version]=$version
  note "Coder $version"
  coder_json GET /api/v2/organizations
  ORG="$(jq -r '.[] | select(.is_default) | .id' <<<"$BODY" 2>/dev/null)"
  [[ -n $ORG ]] || {
    echo "no default organization: is the token valid and an admin's?" >&2
    exit 2
  }
}

pick_models() {
  local type key gemma=nebius/google/gemma-3-27b-it
  say "2. Picking one cheap model per provider type from the public catalog"
  CAT="$(curl -fsS "$REQUESTY_URL/v1/models")" || {
    echo "cannot fetch the Requesty catalog" >&2
    exit 2
  }
  for type in anthropic openai google; do
    MODEL[$type]="$(pick_first_party "$type")"
    [[ -n ${MODEL[$type]} ]] || {
      echo "no suitable $type model in the catalog" >&2
      exit 2
    }
    note "$type -> ${MODEL[$type]}"
  done
  EXTRA_MODEL[anthropic_3p]="$(pick_third_party anthropic claude)"
  EXTRA_MODEL[google_3p]="$(pick_third_party google gemini)"
  # A model that failed in Coder's google type on the first real run: try it on
  # the google type again and on the openai type, to tell the two apart.
  EXTRA_MODEL[gemma_google]="$(jq -r --arg id "$gemma" '.data[] | select(.id == $id) | .id' <<<"$CAT")"
  EXTRA_MODEL[gemma_openai]="${EXTRA_MODEL[gemma_google]}"
  for key in "${EXTRA_KEYS[@]}"; do
    note "extra $key (${EXTRA_TYPE[$key]} type) -> ${EXTRA_MODEL[$key]:-none found}"
  done
}

direct_calls() {
  local type resp v1 root
  say "3. Calling Requesty directly (Coder is not involved)"
  for type in openai google; do
    resp="$(requesty_openai "${MODEL[$type]}")"
    if jq -e '.choices[0].message.content | select(type == "string" and length > 0)' <<<"$resp" >/dev/null 2>&1; then
      RESULT[direct_$type]="worked"
    else
      RESULT[direct_$type]="FAILED: $(jq -c '.error // .' <<<"$resp" 2>/dev/null | head -c 200)"
    fi
    note "OpenAI shape, $type model ${MODEL[$type]}: ${RESULT[direct_$type]}"
  done
  v1="$(requesty_anthropic_code "$REQUESTY_URL/v1/messages" "${MODEL[anthropic]}")"
  root="$(requesty_anthropic_code "$REQUESTY_URL/messages" "${MODEL[anthropic]}")"
  RESULT[direct_v1_messages]=$v1
  RESULT[direct_messages]=$root
  note "Anthropic shape, $REQUESTY_URL/v1/messages -> HTTP $v1"
  note "Anthropic shape, $REQUESTY_URL/messages -> HTTP $root"
  # Coder's Anthropic client most likely appends /v1/messages to the base URL,
  # so try the root first.
  [[ $v1 == 200 ]] && ANTH_BASES+=("$REQUESTY_URL")
  [[ $root == 200 ]] && ANTH_BASES+=("$REQUESTY_URL/v1")
  ((${#ANTH_BASES[@]} > 0)) || note "Neither Anthropic URL answered 200, so the anthropic type will be skipped."
}

create_provider() {
  local type=$1 base=$2 body id
  body="$(jq -n --arg t "$type" --arg b "$base" --arg logo "$LOGO_BASE/$type.png" \
    '{type: $t, name: ("smoke-" + $t), display_name: ("Smoke " + $t), icon: $logo, enabled: true, base_url: $b, api_keys: [$ENV.REQUESTY_API_KEY]}')"
  coder_json POST /api/v2/ai/providers "$body"
  id="$(jq -r '.id // empty' <<<"$BODY" 2>/dev/null)"
  if [[ -z $id ]]; then
    RESULT[create_$type]="FAILED (HTTP $HTTP_STATUS): $(jq -c . <<<"$BODY" 2>/dev/null | head -c 300)"
    note "provider smoke-$type: ${RESULT[create_$type]}"
    return 1
  fi
  PROVIDER_ID[$type]=$id
  note "provider smoke-$type -> $id (base $base)"
}

# create_model PROVIDER_TYPE MODEL DISPLAY_NAME [CONTEXT_LIMIT] [MAX_OUTPUT]
# Returns 0 and records the model ID on success; on failure prints Coder's error.
create_model() {
  local type=$1 model=$2 display=$3 ctx=${4:-200000} maxout=${5:-} body id
  body="$(jq -n --arg p "${PROVIDER_ID[$type]}" --arg m "$model" --arg d "$display" \
    --argjson c "$ctx" --arg o "$maxout" \
    '{ai_provider_id: $p, model: $m, display_name: $d, enabled: true, context_limit: $c} + (if $o == "" then {} else {model_config: {max_output_tokens: ($o | tonumber)}} end)')"
  coder_json POST "/api/v2/organizations/$ORG/chats/models" "$body"
  id="$(jq -r '.id // empty' <<<"$BODY" 2>/dev/null)"
  if [[ -z $id ]]; then
    note "model $model FAILED (HTTP $HTTP_STATUS): $(jq -c . <<<"$BODY" 2>/dev/null | head -c 300)"
    return 1
  fi
  MODEL_IDS+=("$id")
  LAST_MODEL_ID=$id
  note "model $model -> $id"
}

create_smoke_objects() {
  local type base
  say "4. Creating smoke providers, models and prices in Coder"
  for type in openai google anthropic; do
    base="$REQUESTY_URL/v1"
    if [[ $type == anthropic ]]; then
      base="${ANTH_BASES[0]:-}"
      [[ -n $base ]] || continue
    fi
    create_provider "$type" "$base" || continue
    create_model "$type" "${MODEL[$type]}" "${MODEL[$type]}" || continue
    MODEL_CFG[$type]=$LAST_MODEL_ID
    coder_json POST /api/experimental/ai/model-prices \
      "$(jq -n --arg t "$type" --arg m "${MODEL[$type]}" '{prices: [{provider: $t, model: $m, input_price: 1000000, output_price: 5000000, cache_read_price: null, cache_write_price: null}]}')"
    [[ $HTTP_STATUS == 204 ]] || note "price upsert for $type: HTTP $HTTP_STATUS $(head -c 300 <<<"$BODY")"
  done
}

# Third-party-hosted models on the native types, a duplicate display name across
# providers, and a model whose output limit exceeds its context limit. These
# are the shapes the real sync will register.
extra_models() {
  local key type
  say "5. Extra models: third-party hosts, a duplicate display name, limits"
  for key in "${EXTRA_KEYS[@]}"; do
    type=${EXTRA_TYPE[$key]}
    [[ -n ${EXTRA_MODEL[$key]:-} && -n ${PROVIDER_ID[$type]:-} ]] || continue
    # The display name says which model and type, so the picker shows which is which.
    EXTRA_DISPLAY[$key]="${EXTRA_MODEL[$key]} ($type type)"
    if create_model "$type" "${EXTRA_MODEL[$key]}" "${EXTRA_DISPLAY[$key]}"; then
      MODEL_CFG[$key]=$LAST_MODEL_ID
      RESULT[extra_created_$key]=yes
    else
      RESULT[extra_created_$key]=no
    fi
  done
  # A duplicate display name across providers, on a model nobody chats with: the
  # sync registers free and paid twins under the same name.
  if [[ -n ${PROVIDER_ID[google]:-} && -n ${PROVIDER_ID[openai]:-} ]]; then
    if create_model google "smoke/duplicate-probe" "${MODEL[openai]}"; then
      RESULT[duplicate_display_names]="accepted across providers"
    else
      RESULT[duplicate_display_names]="REJECTED (see the error above)"
    fi
  fi
  if [[ -n ${PROVIDER_ID[openai]:-} ]]; then
    if create_model openai "smoke/limits-probe" "smoke-limits-probe" 16384 40960; then
      RESULT[limits_mismatch]="accepted (max_output_tokens above context_limit)"
    else
      RESULT[limits_mismatch]="REJECTED (see the error above)"
    fi
  fi
}

api_probes() {
  say "6. API probes the sync relies on"
  coder_json GET "/api/experimental/ai/model-prices?source=custom"
  RESULT[prices_list_type]="$(jq -r 'type' <<<"$BODY" 2>/dev/null || echo 'not JSON')"
  note "GET model-prices?source=custom returns a JSON ${RESULT[prices_list_type]} (the sync expects: array)"
  RESULT[price_nulls]="$(jq -r --arg m "${MODEL[openai]}" '[.[] | select(.provider == "openai" and .model == $m)] | if length == 0 then "price row missing" elif (.[0].cache_read_price == null and .[0].cache_write_price == null) then "null prices come back as null" else "null prices CHANGED: \(.[0] | [.cache_read_price, .cache_write_price] | tostring)" end' <<<"$BODY" 2>/dev/null || echo 'could not read')"
  note "price nulls round trip: ${RESULT[price_nulls]}"
  [[ -n ${PROVIDER_ID[openai]:-} ]] || return 0
  coder_json PATCH /api/v2/ai/providers/smoke-openai '{"display_name": "Smoke openai (patched)"}'
  coder_json GET /api/v2/ai/providers/smoke-openai
  RESULT[patch_partial]="$(jq -r 'if ((.api_keys | length) == 1 and .enabled == true) then "partial PATCH kept the key and enabled" else "PATCH CHANGED other fields: keys=\(.api_keys | length) enabled=\(.enabled)" end' <<<"$BODY" 2>/dev/null || echo 'could not read')"
  note "provider PATCH with one field: ${RESULT[patch_partial]}"
  local keybody
  keybody="$(jq -n '{api_keys: [{api_key: $ENV.REQUESTY_API_KEY}]}')"
  coder_json PATCH /api/v2/ai/providers/smoke-openai "$keybody"
  coder_json PATCH /api/v2/ai/providers/smoke-openai "$keybody"
  coder_json GET /api/v2/ai/providers/smoke-openai
  RESULT[key_patch]="$(jq -r 'if (.api_keys | length) == 1 then "replaces the key set" else "APPENDS (\(.api_keys | length) keys after two rotations)" end' <<<"$BODY" 2>/dev/null || echo 'could not read')"
  note "api_keys PATCH sent twice: ${RESULT[key_patch]}"
}

# api_chat CONFIG_ID: runs one real Coder Agents chat against that model config
# through the API, and sets CHAT_RESULT ("ok", or "FAILED: ..." with Coder's own
# error) and CHAT_COST (micro-dollars). A chat counts as working when its turn
# finished (waiting, or requires_action for a tool call) and an assistant message
# exists.
api_chat() {
  local cfg=$1 body chat_id status="" waited=0 replies
  CHAT_RESULT=""
  CHAT_COST=""
  body="$(jq -n --arg org "$ORG" --arg m "$cfg" --arg p "$PROMPT" --arg run "$RUN_ID" \
    '{organization_id: $org, model_config_id: $m, client_type: "api", labels: {probe: ("requesty-smoke-" + $run)}, content: [{type: "text", text: $p}]}')"
  coder_json POST /api/v2/chats "$body"
  chat_id="$(jq -r '.id // empty' <<<"$BODY" 2>/dev/null)"
  if [[ -z $chat_id ]]; then
    CHAT_RESULT="FAILED: could not create the chat (HTTP $HTTP_STATUS): $(jq -c . <<<"$BODY" 2>/dev/null | head -c 300)"
    return 1
  fi
  CHAT_IDS+=("$chat_id")
  while ((waited < CHAT_TIMEOUT)); do
    sleep "$CHAT_POLL"
    waited=$((waited + CHAT_POLL))
    coder_json GET "/api/v2/chats/$chat_id"
    status="$(jq -r '.status // empty' <<<"$BODY" 2>/dev/null)"
    if [[ $status == error ]]; then
      CHAT_RESULT="FAILED: $(jq -r '.last_error | "\(.message // "unknown error")\(if .detail then " - " + .detail else "" end) (upstream HTTP \(.status_code // "?"), \(.provider // "?"))"' <<<"$BODY" 2>/dev/null)"
      return 1
    fi
    if [[ $status == waiting || $status == requires_action ]]; then
      coder_json GET "/api/v2/chats/$chat_id/messages"
      replies="$(jq -r '[.messages[]? | select(.role == "assistant")] | length' <<<"$BODY" 2>/dev/null)"
      if [[ ${replies:-0} -gt 0 || $status == requires_action ]]; then
        CHAT_RESULT=ok
        coder_json GET "/api/v2/chats/$chat_id/cost"
        CHAT_COST="$(jq -r '.total_cost_micros // empty' <<<"$BODY" 2>/dev/null)"
        return 0
      fi
    fi
  done
  CHAT_RESULT="FAILED: no reply within ${CHAT_TIMEOUT}s (last status: ${status:-none})"
  return 1
}

# chat_and_report KEY CONFIG_ID LABEL: one chat, printed and stored under RESULT[chat_KEY].
chat_and_report() {
  api_chat "$2" || true
  RESULT[chat_$1]=$CHAT_RESULT
  RESULT[cost_$1]=$CHAT_COST
  TOTAL_COST_MICROS=$((TOTAL_COST_MICROS + ${CHAT_COST:-0}))
  note "$3: $CHAT_RESULT${CHAT_COST:+ (cost $CHAT_COST micro-dollars)}"
}

agent_chats() {
  local type key base i
  say "7. Real Coder Agents chats, through the API"
  note "Each model gets one chat: '$PROMPT' (waits up to ${CHAT_TIMEOUT}s per model)."
  for type in openai google; do
    [[ -n ${MODEL_CFG[$type]:-} ]] || continue
    chat_and_report "$type" "${MODEL_CFG[$type]}" "${MODEL[$type]} (smoke $type)"
  done
  RESULT[chat_anthropic]="not run"
  RESULT[anthropic_base]=none
  if [[ -n ${MODEL_CFG[anthropic]:-} ]]; then
    for i in "${!ANTH_BASES[@]}"; do
      base=${ANTH_BASES[$i]}
      if ((i > 0)); then
        note "Retrying the Anthropic provider with base URL $base"
        coder_json PATCH /api/v2/ai/providers/smoke-anthropic "$(jq -n --arg b "$base" '{base_url: $b}')"
      fi
      chat_and_report anthropic "${MODEL_CFG[anthropic]}" "${MODEL[anthropic]} (smoke anthropic, base $base)"
      if [[ ${RESULT[chat_anthropic]} == ok ]]; then
        RESULT[anthropic_base]=$base
        break
      fi
    done
  fi
  for key in "${EXTRA_KEYS[@]}"; do
    [[ -n ${MODEL_CFG[$key]:-} ]] || continue
    chat_and_report "extra_$key" "${MODEL_CFG[$key]}" "${EXTRA_MODEL[$key]} (${EXTRA_TYPE[$key]} type)"
  done
  note "Total cost of these chats: $TOTAL_COST_MICROS micro-dollars"
}

logo_check() {
  say "8. Logos (the one thing that needs your eyes)"
  note "Open the AI settings Models page at $CODER_URL while the smoke providers exist."
  if yesno "Do the three Requesty logos render in both light and dark themes?"; then
    RESULT[logos]=yes
  else
    RESULT[logos]=no
  fi
}

role_probe() {
  local out check_token p code ok=yes roles seen
  say "9. Narrow token for the CronJob's limited check (optional)"
  note "Coder v2.37.0 issues no scope that reads AI providers or prices, so the daily check runs"
  note "in limited mode with a role-less service account and a token limited to two read scopes."
  if ! yesno "Probe this now?"; then
    RESULT[scoped_token]="not probed"
    return 0
  fi
  need coder
  if yesno "Create the 'requesty-sync' service account with the coder CLI now (say n if it exists)?"; then
    coder users create --service-account --username requesty-sync || note "coder users create failed; continue if the user already exists"
  fi
  roles="$(coder users show requesty-sync 2>&1 | grep -E '^Roles:' || true)"
  note "requesty-sync ${roles:-roles: unknown}"
  if [[ $roles == *[Oo]wner* ]]; then
    note "It still has the Owner role. The limited check does not need it; remove it in the dashboard."
  fi
  out="$(coder tokens create --user requesty-sync --name "requesty-smoke-check-$RUN_ID" --lifetime 1h \
    --scope chat_model_config:read --scope organization:read 2>&1)"
  check_token="$(grep -oE '[A-Za-z0-9]{10}-[A-Za-z0-9]{22}' <<<"$out" | head -1)"
  if [[ -z $check_token ]]; then
    note "could not create a narrow token for requesty-sync:"
    printf '%s\n' "$out" | sed 's/^/     /'
    RESULT[scoped_token]="not probed (token creation failed)"
    return 0
  fi
  note "Reads the limited check needs (should be 200):"
  for p in /api/v2/organizations "/api/v2/organizations/$ORG/chats/models"; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' -K <(printf 'header = "Coder-Session-Token: %s"\n' "$check_token") "$CODER_URL$p")"
    note "  GET $p -> HTTP $code"
    [[ $code == 200 ]] || ok=no
  done
  note "Reads it must NOT have (should be 403):"
  for p in /api/v2/ai/providers /api/experimental/ai/model-prices; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' -K <(printf 'header = "Coder-Session-Token: %s"\n' "$check_token") "$CODER_URL$p")"
    note "  GET $p -> HTTP $code"
    [[ $code == 403 ]] || ok=no
  done
  seen="$(curl -sS -K <(printf 'header = "Coder-Session-Token: %s"\n' "$check_token") "$CODER_URL/api/v2/users" | jq -r '.count // "?"' 2>/dev/null)"
  note "Users the token can see (should be 0; Coder returns an empty list, not an error): $seen"
  [[ $seen == 0 ]] || ok=no
  note "Writes (should be 403):"
  for p in /api/v2/ai/providers /api/experimental/ai/model-prices; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' \
      -K <(printf 'header = "Coder-Session-Token: %s"\n' "$check_token") "$CODER_URL$p")"
    note "  POST $p -> HTTP $code"
    [[ $code == 403 ]] || ok=no
  done
  if [[ $ok == yes ]]; then
    RESULT[scoped_token]="works: reads the model list, is denied AI providers, prices, and writes, and sees no users"
  else
    RESULT[scoped_token]="did NOT behave as expected (see the codes above)"
  fi
}

report() {
  local type key recommend=""
  local anth_base=${RESULT[anthropic_base]:-none}
  if [[ $anth_base != none && $anth_base != "$REQUESTY_URL/v1" ]]; then
    recommend+="  BASE_URL_BY_TYPE = {\"anthropic\": \"$anth_base\"} (already set in the header when this is https://router.requesty.ai)"$'\n'
  fi
  if [[ ${RESULT[chat_anthropic]:-not run} != ok ]]; then
    recommend+="  NATIVE_TYPES: remove \"anthropic\" (the anthropic type did not work)"$'\n'
  fi
  if [[ ${RESULT[chat_google]:-not run} != ok ]]; then
    recommend+="  NATIVE_TYPES: remove \"google\" (the google type did not work)"$'\n'
  fi
  if [[ ${RESULT[chat_extra_gemma_google]:-ok} == FAILED* && ${RESULT[chat_extra_gemma_openai]:-} == ok ]]; then
    recommend+="  NATIVE_TYPES: remove \"google\" (gemma failed on the google type but worked on the openai type)"$'\n'
  fi
  [[ -n $recommend ]] || recommend="  none: the defaults in the script header are right"$'\n'
  say "Results"
  {
    echo "## Smoke test results"
    echo
    echo "- Coder version: ${RESULT[coder_version]:-?}"
    echo "- Direct OpenAI-shape call, openai model (${MODEL[openai]:-?}): ${RESULT[direct_openai]:-?}"
    echo "- Direct OpenAI-shape call, google model (${MODEL[google]:-?}): ${RESULT[direct_google]:-?}"
    echo "- Direct Anthropic-shape call: /v1/messages returned ${RESULT[direct_v1_messages]:-?}, /messages returned ${RESULT[direct_messages]:-?}"
    echo "- Coder chat, openai type: ${RESULT[chat_openai]:-not run}"
    echo "- Coder chat, google type: ${RESULT[chat_google]:-not run}"
    echo "- Coder chat, anthropic type: ${RESULT[chat_anthropic]:-not run} (base URL that worked: $anth_base)"
    for key in "${EXTRA_KEYS[@]}"; do
      echo "- Coder chat, ${EXTRA_MODEL[$key]:-none} on the ${EXTRA_TYPE[$key]} type: ${RESULT[chat_extra_$key]:-not run}"
    done
    echo "- Requesty logos rendered on both themes: ${RESULT[logos]:-not run}"
    echo "- Cost of all the chats: $TOTAL_COST_MICROS micro-dollars (divide by 1,000,000 for dollars)"
    echo "- Duplicate display names across providers: ${RESULT[duplicate_display_names]:-not run}"
    echo "- Output limit above context limit: ${RESULT[limits_mismatch]:-not run}"
    echo "- Price list endpoint returns: ${RESULT[prices_list_type]:-?}"
    echo "- Null prices round trip: ${RESULT[price_nulls]:-?}"
    echo "- Provider PATCH with a single field: ${RESULT[patch_partial]:-?}"
    echo "- API key PATCH sent twice: ${RESULT[key_patch]:-?}"
    echo "- Narrow token for the limited check: ${RESULT[scoped_token]:-not probed}"
    for type in openai google anthropic; do
      [[ -z ${RESULT[create_$type]:-} ]] || echo "- Creating the $type provider: ${RESULT[create_$type]}"
    done
    echo
    echo "Edits to the header of scripts/requesty-coder-sync.py:"
    printf '%s' "$recommend"
  } | tee "$RESULTS_FILE"
  note "Saved to $RESULTS_FILE"
}

main() {
  need curl
  need jq
  trap on_exit EXIT
  trap 'exit 130' INT TERM
  [[ ${1:-} == --role-only ]] && ROLE_ONLY=1
  get_credentials
  if [[ ${1:-} == --role-only ]]; then
    role_probe
    say "Result"
    echo "- Narrow token for the limited check: ${RESULT[scoped_token]:-not probed}"
    return 0
  fi
  pick_models
  direct_calls
  create_smoke_objects
  extra_models
  api_probes
  agent_chats
  logo_check
  role_probe
  cleanup
  report
}

main "$@"
