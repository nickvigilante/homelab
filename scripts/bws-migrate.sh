#!/usr/bin/env bash
# Migrate values from the Bitwarden password vault into BWS as secrets
# in the homelab project. Reads pipe-separated tuples from stdin.
#
# Usage:
#   ./scripts/bws-migrate.sh <<'EOF'
#   <bws-name>|<bw-vault-item>|<bw-field>
#   ...
#   EOF
#
# Each line is one secret to migrate:
#   - <bws-name>      -- the name BWS will store this under (lowercase-kebab).
#   - <bw-vault-item> -- the Password Manager item name (e.g. 'Homelab Grafana').
#   - <bw-field>      -- the custom-field name within that item.
#
# Comments (lines starting with #) and blank lines are ignored.
#
# Example -- the original #135 Task 8 homepage migration:
#   ./scripts/bws-migrate.sh <<'EOF'
#   octoprint-api-key|Homelab OctoPrint|API key
#   grafana-user|Homelab Grafana|homepage-user
#   grafana-password|Homelab Grafana|homepage-password
#   EOF
#
# Auth: reads BWS_ACCESS_TOKEN from BW vault item 'Homelab BWS Bootstrap
# Token' (.notes field). This is a dedicated Read/Write machine account
# (homelab-bootstrap) kept separate from the runtime flux-eso (Read-only)
# so ESO never has standing write access. Without this separation, every
# migration would require bumping flux-eso to Read/Write in the BWS web
# UI and reverting after -- prone to forgotten reverts.
#
# Requires: bw (Password Manager CLI), bws (Secrets Manager CLI), jq.
# Prompts for the BW master password.
#
# Output: one '<bws-name>: <uuid>' line per successful create on stdout;
# SKIP/ERROR lines on stderr. The UUIDs go into ExternalSecret manifests
# as remoteRef.key values.

set -uo pipefail

PROJECT_ID="c167c5ba-9144-4b04-8a10-b45a01570e69"
BOOTSTRAP_ITEM="Homelab BWS Bootstrap Token"

if [ -t 0 ]; then
  cat >&2 <<USAGE
ERROR: expected pipe-separated tuples on stdin.

Usage:
  $0 <<'EOF'
  <bws-name>|<bw-item>|<bw-field>
  ...
  EOF

See the comment header at the top of $0 for details.
USAGE
  exit 2
fi

if ! BW_SESSION_VAL="$(bw unlock --raw)"; then
  echo "FATAL: bw unlock failed (bad password or vault locked)" >&2
  exit 1
fi
export BW_SESSION="$BW_SESSION_VAL"
unset BW_SESSION_VAL

bw sync >/dev/null

BWS_TOKEN_VAL="$(bw get item "$BOOTSTRAP_ITEM" | jq -r '.notes')"
if [ -z "$BWS_TOKEN_VAL" ] || [ "$BWS_TOKEN_VAL" = "null" ]; then
  echo "FATAL: bootstrap access token not found in BW vault item '$BOOTSTRAP_ITEM' (.notes field)" >&2
  unset BW_SESSION
  exit 1
fi
export BWS_ACCESS_TOKEN="$BWS_TOKEN_VAL"
unset BWS_TOKEN_VAL

trim() {
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  printf '%s' "$v"
}

migrate_one() {
  local name="$1" item="$2" field="$3" value bws_out id
  value="$(bw get item "$item" | jq -r --arg f "$field" '.fields[]?|select(.name==$f)|.value')"
  if [ -z "$value" ] || [ "$value" = "null" ]; then
    echo "SKIP  $name  (empty value at '$item' / '$field')" >&2
    return
  fi
  if bws_out="$(bws secret create "$name" "$value" "$PROJECT_ID" 2>&1)"; then
    id="$(echo "$bws_out" | jq -r '.id // empty' 2>/dev/null)"
    if [ -n "$id" ]; then
      printf '%-30s %s\n' "$name:" "$id"
    else
      echo "WARN  $name  (created but couldn't parse id) raw: $bws_out" >&2
    fi
  else
    echo "ERROR $name: $bws_out" >&2
  fi
}

processed=0
while IFS='|' read -r raw_name raw_item raw_field || [ -n "${raw_name:-}" ]; do
  name="$(trim "${raw_name:-}")"
  # Skip blank lines and comments
  [ -z "$name" ] && continue
  case "$name" in \#*) continue ;; esac
  item="$(trim "${raw_item:-}")"
  field="$(trim "${raw_field:-}")"
  if [ -z "$item" ] || [ -z "$field" ]; then
    echo "SKIP  malformed line for '$name' (missing item or field)" >&2
    continue
  fi
  migrate_one "$name" "$item" "$field"
  processed=$((processed + 1))
done

unset BW_SESSION BWS_ACCESS_TOKEN
unset -f migrate_one trim

if [ "$processed" -eq 0 ]; then
  echo "WARN: no tuples processed" >&2
  exit 3
fi
