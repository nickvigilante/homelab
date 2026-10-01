#!/usr/bin/env bash
# Bootstrap machine secrets end-to-end: generate missing values, store them
# as custom fields on a Bitwarden vault item (the human-readable DR mirror
# -- see CLAUDE.md "Two vault layers"), and push them into BWS (the
# cluster-readable copy ESO syncs from). Renamed from bws-migrate.sh, which
# only did the vault-to-BWS half and required every field to already exist,
# hand-populated, on the vault item -- this also creates/edits the item and
# generates values that don't need a human to pick them.
#
# Usage:
#   ./scripts/bws-bootstrap-secrets.sh <<'EOF'
#   <bws-name>|<bw-vault-item>|<bw-field>|<generate-spec>
#   ...
#   EOF
#
# generate-spec (4th column, optional):
#   (empty)  -- field must already exist in the vault item. Use for
#              human-managed values (an API key copied from a third-party
#              dashboard) -- this is the original bws-migrate.sh behavior,
#              and existing 3-column call sites keep working unchanged.
#   hex:N    -- generate `openssl rand -hex N` (via Python's os.urandom,
#              same CSPRNG source) if the field is missing or empty; reuse
#              the existing value otherwise. Idempotent -- re-running never
#              rotates a secret you didn't ask to rotate.
#
# One vault item is created (as a Secure Note) or edited per unique
# <bw-vault-item> in the input -- all of that item's fields are computed in
# a single pass and written with a single `bw create`/`bw edit` call, so a
# multi-field item never sees a create-then-immediately-edit race. Existing
# fields, and any login/card/identity data already on the item, are left
# untouched.
#
# Example -- syncstorage-rs bootstrap (homelab#225):
#   ./scripts/bws-bootstrap-secrets.sh <<'EOF'
#   master-secret|Homelab syncstorage-rs|master-secret|hex:32
#   fxa-metrics-hash-secret|Homelab syncstorage-rs|fxa-metrics-hash-secret|hex:32
#   postgres-password|Homelab syncstorage-rs|postgres-password|hex:24
#   postgres-superuser-password|Homelab syncstorage-rs|postgres-superuser-password|hex:24
#   EOF
#
# Example -- original #135 Task 8 homepage migration (human-managed values,
# no generate-spec, item must already exist with these fields set):
#   ./scripts/bws-bootstrap-secrets.sh <<'EOF'
#   octoprint-api-key|Homelab OctoPrint|API key|
#   grafana-user|Homelab Grafana|homepage-user|
#   grafana-password|Homelab Grafana|homepage-password|
#   EOF
#
# Auth: reads BWS_ACCESS_TOKEN from BW vault item 'Homelab BWS Bootstrap
# Token' (.notes field). This is a dedicated Read/Write machine account
# (homelab-bootstrap) kept separate from the runtime flux-eso (Read-only)
# so ESO never has standing write access. Without this separation, every
# migration would require bumping flux-eso to Read/Write in the BWS web
# UI and reverting after -- prone to forgotten reverts.
#
# Target project: the `homelab` project by default. Set BWS_PROJECT_ID to push
# into another BWS project instead (e.g. Homelab-IaC for operator/IaC
# credentials -- see scripts/bw-import-env.sh). The machine account behind
# 'Homelab BWS Bootstrap Token' must have Read/Write on that project.
#
# Requires: bw (Password Manager CLI), bws (Secrets Manager CLI), jq,
# python3. Prompts for the BW master password.
#
# Output: one '<bws-name>: <uuid>' line per successful create on stdout;
# SKIP/ERROR lines on stderr. The UUIDs go into ExternalSecret manifests
# as remoteRef.key values.

set -uo pipefail

PROJECT_ID="${BWS_PROJECT_ID:-c167c5ba-9144-4b04-8a10-b45a01570e69}"
if ! [[ "$PROJECT_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  echo "FATAL: BWS_PROJECT_ID is not a UUID" >&2
  exit 2
fi
BOOTSTRAP_ITEM="Homelab BWS Bootstrap Token"

if [ -t 0 ]; then
  cat >&2 <<USAGE
ERROR: expected pipe-separated tuples on stdin.

Usage:
  $0 <<'EOF'
  <bws-name>|<bw-item>|<bw-field>|<generate-spec>
  ...
  EOF

See the comment header at the top of $0 for details.
USAGE
  exit 2
fi

trim() {
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  printf '%s' "$v"
}

# mktemp defaults to mode 0600 -- these may hold vault item JSON, which can
# include already-set secret VALUES for fields other callers populated by
# hand. Never pass that JSON as a command-line argument to a child process
# (argv is visible to other local users via `ps`); pass file paths instead
# and let Python read the content directly.
TUPLES_FILE="$(mktemp)"
EXISTING_ITEM_FILE="$(mktemp)"
MERGE_SCRIPT="$(mktemp)"
trap 'rm -f "$TUPLES_FILE" "$EXISTING_ITEM_FILE" "$MERGE_SCRIPT"' EXIT

# ---- Parse stdin into a validated TSV: item<TAB>field<TAB>spec<TAB>name ----
processed=0
while IFS='|' read -r raw_name raw_item raw_field raw_spec || [ -n "${raw_name:-}" ]; do
  name="$(trim "${raw_name:-}")"
  # Skip blank lines and comments
  [ -z "$name" ] && continue
  case "$name" in \#*) continue ;; esac
  item="$(trim "${raw_item:-}")"
  field="$(trim "${raw_field:-}")"
  spec="$(trim "${raw_spec:-}")"
  if [ -z "$item" ] || [ -z "$field" ]; then
    echo "SKIP  malformed line for '$name' (missing item or field)" >&2
    continue
  fi
  # `|`, not tab: bash `read` squeezes RUNS of any IFS whitespace char
  # (tab/space/newline) together and silently drops empty fields between
  # them, which would eat an intentionally-empty spec column. `|` is not
  # whitespace, so empty fields between two `|`s are preserved correctly
  # (verified: see the first stdin-parsing loop above, which relies on
  # the same property for IFS='|').
  printf '%s|%s|%s|%s\n' "$item" "$field" "$spec" "$name" >>"$TUPLES_FILE"
  processed=$((processed + 1))
done

if [ "$processed" -eq 0 ]; then
  echo "WARN: no tuples processed" >&2
  exit 3
fi

# ---- bw unlock ----
if [ -n "${BW_SESSION:-}" ] && bw status 2>/dev/null | jq -e '.status == "unlocked"' >/dev/null; then
  : # Reuse the caller's already-unlocked session. This avoids a stdin
  # conflict when tuples are piped in via heredoc: `bw unlock` would
  # otherwise read the heredoc as the master password and fail. To
  # use, the caller exports BW_SESSION before invoking the script.
else
  # Read the master password from the TTY directly, not the script's
  # stdin (which may be a heredoc of tuples).
  if ! BW_SESSION_VAL="$(bw unlock --raw </dev/tty)"; then
    echo "FATAL: bw unlock failed (bad password or vault locked)" >&2
    exit 1
  fi
  export BW_SESSION="$BW_SESSION_VAL"
  unset BW_SESSION_VAL
fi

bw sync >/dev/null

BWS_TOKEN_VAL="$(bw get item "$BOOTSTRAP_ITEM" | jq -r '.notes')"
if [ -z "$BWS_TOKEN_VAL" ] || [ "$BWS_TOKEN_VAL" = "null" ]; then
  echo "FATAL: bootstrap access token not found in BW vault item '$BOOTSTRAP_ITEM' (.notes field)" >&2
  unset BW_SESSION
  exit 1
fi
export BWS_ACCESS_TOKEN="$BWS_TOKEN_VAL"
unset BWS_TOKEN_VAL

# ---- Step 1: ensure every vault item has every requested field ----
# One item at a time: fetch it (if it exists), compute the merged field
# set in Python (generating values for fields with a spec that are
# missing or empty; never touching a field that already has a value),
# then a single create-or-edit call.
cat >"$MERGE_SCRIPT" <<'PYEOF'
import sys, json, os

item_name = sys.argv[1]
existing_path = sys.argv[2]
fields_path = sys.argv[3]

with open(fields_path) as f:
    fields_needed = [line.rstrip("\n").split("\t") for line in f if line.strip()]

existing_raw = ""
if existing_path:
    with open(existing_path) as f:
        existing_raw = f.read().strip()

if existing_raw:
    doc = json.loads(existing_raw)
else:
    doc = {
        "organizationId": None,
        "folderId": None,
        "type": 2,
        "name": item_name,
        "notes": None,
        "favorite": False,
        "secureNote": {"type": 0},
        "fields": [],
    }

existing_fields = {f["name"]: f for f in (doc.get("fields") or [])}

for field_name, spec in fields_needed:
    current = existing_fields.get(field_name)
    if current and current.get("value"):
        continue  # already set -- never overwrite
    if not spec:
        print(
            f"FATAL: field '{field_name}' missing on '{item_name}' and no generate-spec given",
            file=sys.stderr,
        )
        sys.exit(1)
    if spec.startswith("hex:"):
        value = os.urandom(int(spec[len("hex:"):])).hex()
    else:
        print(f"FATAL: unknown generate-spec '{spec}'", file=sys.stderr)
        sys.exit(1)
    existing_fields[field_name] = {"name": field_name, "value": value, "type": 0}

doc["fields"] = list(existing_fields.values())
print(json.dumps(doc))
PYEOF

while IFS='|' read -r item; do
  existing_json="$(bw get item "$item" 2>/dev/null)"
  if [ -n "$existing_json" ] && [ "$existing_json" != "null" ]; then
    printf '%s' "$existing_json" >"$EXISTING_ITEM_FILE"
  else
    : >"$EXISTING_ITEM_FILE"
    existing_json=""
  fi

  item_fields_file="$(mktemp)"
  # awk splits the `|`-delimited TUPLES_FILE; the output it writes is
  # tab-separated, but that file is only ever parsed by Python's explicit
  # str.split("\t") below, never by bash `read`, so no squeeze risk there.
  awk -F'|' -v it="$item" '$1==it {print $2"\t"$3}' "$TUPLES_FILE" >"$item_fields_file"

  merged_json="$(python3 "$MERGE_SCRIPT" "$item" "$EXISTING_ITEM_FILE" "$item_fields_file")"
  merge_status=$?
  rm -f "$item_fields_file"
  if [ "$merge_status" -ne 0 ]; then
    echo "FATAL: field generation failed for '$item'" >&2
    exit 1
  fi

  encoded="$(printf '%s' "$merged_json" | bw encode)"
  if [ -n "$existing_json" ]; then
    item_id="$(printf '%s' "$existing_json" | jq -r '.id')"
    if ! bw edit item "$item_id" "$encoded" >/dev/null; then
      echo "FATAL: bw edit item failed for '$item' ($item_id)" >&2
      exit 1
    fi
  else
    if ! bw create item "$encoded" >/dev/null; then
      echo "FATAL: bw create item failed for '$item'" >&2
      exit 1
    fi
  fi
done < <(cut -d'|' -f1 "$TUPLES_FILE" | sort -u)

bw sync >/dev/null

# ---- Step 2: push every field's (now-guaranteed-present) value into BWS ----
migrate_one() {
  local name="$1" item="$2" field="$3" value bws_out id
  value="$(bw get item "$item" | jq -r --arg f "$field" '.fields[]?|select(.name==$f)|.value')"
  if [ -z "$value" ] || [ "$value" = "null" ]; then
    echo "SKIP  $name  (empty value at '$item' / '$field' -- generation may have failed)" >&2
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

while IFS='|' read -r item field spec name; do
  migrate_one "$name" "$item" "$field"
done <"$TUPLES_FILE"

unset BW_SESSION BWS_ACCESS_TOKEN
unset -f migrate_one trim
