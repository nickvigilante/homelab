#!/usr/bin/env bash
# Import selected variables from a shell env file into custom fields on a
# Bitwarden vault item (a Secure Note), as the first half of moving
# operator/IaC credentials out of ~/.homelab-opentofu.env. The second half is
# scripts/bws-bootstrap-secrets.sh, which pushes those fields into BWS.
#
# Usage:
#   ./scripts/bw-import-env.sh --item "<vault item>" [--dry-run] \
#       <env-file> <ENVVAR[=field-name]>...
#
# The field name defaults to the variable name itself, so the vault item and
# the BWS project both keep the env var names and can be rendered straight
# back into an env file (`export NAME=value`). `ENVVAR=field-name` overrides
# that for a single variable.
#
# Behavior:
#   - The env file is sourced in a subshell with `set -a`; only the variables
#     named on the command line are read from it.
#   - Each one becomes a hidden custom field (type 1) on the item. The item is
#     created if absent; if present, only MISSING fields are added.
#     A field that already has a value is reported as SKIP and never changed,
#     and existing fields are never removed. (A field that exists but is empty
#     is filled in, keeping its existing type.)
#   - An item name matching more than one vault item is a hard error, never a
#     reason to create another.
#   - Values never appear on stdout, stderr, or any command line: the merged
#     item JSON is built in a 0600 temp file and handed to `bw` on stdin.
#     Output lists field NAMES only.
#   - --dry-run unlocks the vault (it has to look at the item) but writes
#     nothing; it prints what would be created, added, or skipped.
#   - A variable that is unset or empty in the env file is an error.
#
# Variables to import for Homelab OpenTofu (TF_VAR_github_app_pem_file is a
# local file path, not a secret, so it is not imported):
#
#   TF_VAR_tailscale_oauth_client_id    TF_VAR_tailscale_oauth_client_secret
#   AWS_ACCESS_KEY_ID                   AWS_SECRET_ACCESS_KEY
#   TF_VAR_github_app_id                TF_VAR_github_app_installation_id
#   TF_VAR_github_app_pem_contents
#   TF_VAR_ci_aws_access_key_id         TF_VAR_ci_aws_secret_access_key
#
# Full run, in order:
#
#   # 1. Dry run, then the real import (prompts for the BW master password
#   #    unless BW_SESSION is already exported and unlocked).
#   ./scripts/bw-import-env.sh --item "Homelab OpenTofu" --dry-run \
#     ~/.homelab-opentofu.env \
#     TF_VAR_tailscale_oauth_client_id TF_VAR_tailscale_oauth_client_secret \
#     AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY \
#     TF_VAR_github_app_id TF_VAR_github_app_installation_id \
#     TF_VAR_github_app_pem_contents \
#     TF_VAR_ci_aws_access_key_id TF_VAR_ci_aws_secret_access_key
#   # ...then the same command without --dry-run.
#
#   # 2. Push the fields into the BWS Homelab-IaC project. Get its UUID from
#   #    `bws project list`; the homelab-bootstrap machine account needs
#   #    Read/Write on it.
#   BWS_PROJECT_ID=<Homelab-IaC project uuid> ./scripts/bws-bootstrap-secrets.sh <<'EOF'
#   TF_VAR_tailscale_oauth_client_id|Homelab OpenTofu|TF_VAR_tailscale_oauth_client_id|
#   TF_VAR_tailscale_oauth_client_secret|Homelab OpenTofu|TF_VAR_tailscale_oauth_client_secret|
#   AWS_ACCESS_KEY_ID|Homelab OpenTofu|AWS_ACCESS_KEY_ID|
#   AWS_SECRET_ACCESS_KEY|Homelab OpenTofu|AWS_SECRET_ACCESS_KEY|
#   TF_VAR_github_app_id|Homelab OpenTofu|TF_VAR_github_app_id|
#   TF_VAR_github_app_installation_id|Homelab OpenTofu|TF_VAR_github_app_installation_id|
#   TF_VAR_github_app_pem_contents|Homelab OpenTofu|TF_VAR_github_app_pem_contents|
#   TF_VAR_ci_aws_access_key_id|Homelab OpenTofu|TF_VAR_ci_aws_access_key_id|
#   TF_VAR_ci_aws_secret_access_key|Homelab OpenTofu|TF_VAR_ci_aws_secret_access_key|
#   EOF
#
# Anything that reads the whole Homelab-IaC project will see these
# credentials too, including the Storj state grant and the GitHub App key.
#
# Requires: bw (Password Manager CLI), jq, python3. Always runs `bw sync`
# after unlock -- a stale local cache returns an empty item list and would
# make an existing item look absent.

set -uo pipefail

usage() {
  cat >&2 <<USAGE
Usage: $0 --item "<vault item>" [--dry-run] <env-file> <ENVVAR[=field-name]>...

See the comment header at the top of $0 for the mapping and the full run order.
USAGE
}

ITEM=""
DRY_RUN=0
ENV_FILE=""
MAPPINGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --item)
      [ $# -ge 2 ] || {
        usage
        exit 2
      }
      ITEM="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      echo "FATAL: unknown option '$1'" >&2
      usage
      exit 2
      ;;
    *)
      if [ -z "$ENV_FILE" ]; then
        ENV_FILE="$1"
      else
        MAPPINGS+=("$1")
      fi
      shift
      ;;
  esac
done

if [ -z "$ITEM" ] || [ -z "$ENV_FILE" ] || [ "${#MAPPINGS[@]}" -eq 0 ]; then
  usage
  exit 2
fi
if [ ! -r "$ENV_FILE" ]; then
  echo "FATAL: env file '$ENV_FILE' is not readable" >&2
  exit 2
fi
for m in "${MAPPINGS[@]}"; do
  if ! [[ "$m" =~ ^[A-Za-z_][A-Za-z0-9_]*(=[A-Za-z0-9._-]+)?$ ]]; then
    echo "FATAL: bad variable '$m' (expected ENVVAR or ENVVAR=field-name)" >&2
    exit 2
  fi
done

umask 077
EXISTING_ITEM_FILE="$(mktemp)"
MERGED_FILE="$(mktemp)"
ENCODED_FILE="$(mktemp)"
MERGE_SCRIPT="$(mktemp)"
trap 'rm -f "$EXISTING_ITEM_FILE" "$MERGED_FILE" "$ENCODED_FILE" "$MERGE_SCRIPT"' EXIT

# ---- bw unlock ----
if [ -n "${BW_SESSION:-}" ] && bw status 2>/dev/null | jq -e '.status == "unlocked"' >/dev/null; then
  : # Reuse the caller's already-unlocked session.
else
  # Read the master password from the TTY directly.
  if ! BW_SESSION_VAL="$(bw unlock --raw </dev/tty)"; then
    echo "FATAL: bw unlock failed (bad password or vault locked)" >&2
    exit 1
  fi
  export BW_SESSION="$BW_SESSION_VAL"
  unset BW_SESSION_VAL
fi

bw sync >/dev/null

# ---- Find the item: exactly one exact-name match, or none ----
# `bw get item <name>` fails on multiple matches, which would read as "absent"
# and create a duplicate; list and match the name exactly instead.
if ! list_json="$(bw list items --search "$ITEM")"; then
  echo "FATAL: bw list items failed" >&2
  exit 1
fi
matches="$(printf '%s' "$list_json" | jq -c --arg n "$ITEM" '[.[] | select(.name == $n)]')" || {
  echo "FATAL: could not parse bw list items output" >&2
  exit 1
}
unset list_json
match_count="$(printf '%s' "$matches" | jq 'length')"
case "$match_count" in
  0) : >"$EXISTING_ITEM_FILE" ;;
  1) printf '%s' "$matches" | jq -c '.[0]' >"$EXISTING_ITEM_FILE" ;;
  *)
    echo "FATAL: $match_count vault items are named '$ITEM'; refusing to guess" >&2
    exit 1
    ;;
esac
unset matches

# ---- Merge: Python reads the VALUES from its own environment ----
# Only variable NAMES cross argv. Values stay inside the subshell's
# environment and the Python process, and are written only to MERGED_FILE.
cat >"$MERGE_SCRIPT" <<'PYEOF'
import json
import os
import sys

item_name, existing_path, out_path, dry_run = sys.argv[1:5]
mappings = [(m.split("=", 1) + [m])[:2] for m in sys.argv[5:]]
dry_run = dry_run == "1"

values = {}
for envvar, field in mappings:
    if field in values:
        print(f"FATAL: field '{field}' is mapped more than once", file=sys.stderr)
        sys.exit(1)
    value = os.environ.get(envvar, "")
    if not value:
        print(f"FATAL: {envvar} is unset or empty in the env file", file=sys.stderr)
        sys.exit(1)
    values[field] = value

with open(existing_path) as f:
    existing_raw = f.read().strip()

exists = bool(existing_raw)
if exists:
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

fields = doc.get("fields") or []
by_name = {f["name"]: f for f in fields}

changes = 0
for field, value in values.items():
    current = by_name.get(field)
    if current and current.get("value"):
        print(f"SKIP   {field}  (already set)")
        continue
    if current:
        verb = "FILL"
        if not dry_run:
            current["value"] = value
    else:
        verb = "ADD"
        if not dry_run:
            fields.append({"name": field, "value": value, "type": 1})
    changes += 1
    print(f"{'would ' if dry_run else ''}{verb:<6} {field}")

if not exists and changes:
    print(f"{'would create' if dry_run else 'creating'} item '{item_name}'")

if changes and not dry_run:
    doc["fields"] = fields
    with open(out_path, "w") as f:
        json.dump(doc, f)
PYEOF

if ! (
  set +u
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  python3 "$MERGE_SCRIPT" "$ITEM" "$EXISTING_ITEM_FILE" "$MERGED_FILE" "$DRY_RUN" "${MAPPINGS[@]}"
); then
  echo "FATAL: nothing was written to the vault" >&2
  exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "dry run: nothing was written to the vault"
  exit 0
fi

if [ ! -s "$MERGED_FILE" ]; then
  echo "nothing to change"
  exit 0
fi

# ---- Write: JSON goes to bw on stdin, never as an argument ----
if ! bw encode <"$MERGED_FILE" >"$ENCODED_FILE"; then
  echo "FATAL: bw encode failed" >&2
  exit 1
fi
if [ -s "$EXISTING_ITEM_FILE" ]; then
  item_id="$(jq -r '.id' "$EXISTING_ITEM_FILE")"
  if ! bw edit item "$item_id" <"$ENCODED_FILE" >/dev/null; then
    echo "FATAL: bw edit item failed for '$ITEM' ($item_id)" >&2
    exit 1
  fi
else
  if ! bw create item <"$ENCODED_FILE" >/dev/null; then
    echo "FATAL: bw create item failed for '$ITEM'" >&2
    exit 1
  fi
fi

bw sync >/dev/null
unset BW_SESSION
echo "done"
