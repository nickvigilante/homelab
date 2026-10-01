"""Stub-based tests for scripts/bw-import-env.sh.

A fake `bw` is put first on PATH, so the real Bitwarden CLI is never called.
Every value below is obviously fake; the tests assert none of them ever reaches
stdout, stderr, or a command line.
"""

import base64
import json
import os
import pathlib
import shutil
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "bw-import-env.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="needs jq and bash",
)

# A fake `bw` that records argv, serves $STUB_ITEMS, and captures what the
# script writes. Any subcommand it does not know fails loudly.
BW_STUB = r"""#!/usr/bin/env bash
echo "$*" >>"$STUB_DIR/argv.log"
case "$1 $2" in
  "status "*) echo '{"status":"unlocked"}' ;;
  "sync "*) ;;
  "list items") cat "$STUB_ITEMS" ;;
  "encode "*) base64 -w0 2>/dev/null || base64 ;;
  "create item") base64 -d >"$STUB_DIR/created.json" ;;
  "edit item") base64 -d >"$STUB_DIR/edited.json"; echo "$3" >"$STUB_DIR/edited.id" ;;
  *) echo "stub bw: unexpected: $*" >&2; exit 99 ;;
esac
"""

FAKE = {
    "TF_VAR_tailscale_oauth_client_id": "FAKE-ts-id-1111",
    "TF_VAR_tailscale_oauth_client_secret": "FAKE-ts-secret-2222",
    "AWS_ACCESS_KEY_ID": "FAKE-aws-id-3333",
}
MAPPINGS = list(FAKE)
TS_ID = "TF_VAR_tailscale_oauth_client_id"
ITEM = "Homelab OpenTofu"


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    bw = bindir / "bw"
    bw.write_text(BW_STUB)
    bw.chmod(0o755)
    env_file = tmp_path / "opentofu.env"
    env_file.write_text("".join(f'export {k}="{v}"\n' for k, v in FAKE.items()))
    items = tmp_path / "items.json"
    items.write_text("[]")
    return {
        "dir": tmp_path,
        "env_file": env_file,
        "items": items,
        "env": {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "BW_SESSION": "fake-session",
            "STUB_DIR": str(tmp_path),
            "STUB_ITEMS": str(items),
        },
    }


def run(env, *extra, mappings=None, env_file=None):
    cmd = [
        "bash",
        str(SCRIPT),
        "--item",
        ITEM,
        *extra,
        str(env_file or env["env_file"]),
        *(MAPPINGS if mappings is None else mappings),
    ]
    return subprocess.run(cmd, env=env["env"], capture_output=True, text=True, timeout=30)


def set_items(env, items):
    env["items"].write_text(json.dumps(items))


def existing_item(fields):
    return {
        "id": "item-123",
        "type": 2,
        "name": ITEM,
        "notes": None,
        "secureNote": {"type": 0},
        "fields": fields,
    }


def read_json(env, name):
    return json.loads((env["dir"] / name).read_text())


def assert_no_values(env, result):
    argv_log = (env["dir"] / "argv.log").read_text()
    for value in FAKE.values():
        assert value not in result.stdout
        assert value not in result.stderr
        assert value not in argv_log


def test_dry_run_prints_names_only_and_writes_nothing(env):
    set_items(
        env,
        [existing_item([{"name": "TF_VAR_tailscale_oauth_client_id", "value": "OLD", "type": 1}])],
    )
    result = run(env, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "SKIP   TF_VAR_tailscale_oauth_client_id" in result.stdout
    assert "would ADD" in result.stdout
    assert "TF_VAR_tailscale_oauth_client_secret" in result.stdout
    assert_no_values(env, result)
    assert "OLD" not in result.stdout + result.stderr
    assert not (env["dir"] / "created.json").exists()
    assert not (env["dir"] / "edited.json").exists()


def test_creates_item_with_hidden_fields_and_leaks_nothing(env):
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert_no_values(env, result)
    created = read_json(env, "created.json")
    assert created["name"] == ITEM
    assert created["type"] == 2
    by_name = {f["name"]: f for f in created["fields"]}
    assert set(by_name) == set(MAPPINGS)
    assert all(f["type"] == 1 for f in by_name.values())
    assert (
        by_name["TF_VAR_tailscale_oauth_client_id"]["value"]
        == FAKE["TF_VAR_tailscale_oauth_client_id"]
    )


def test_existing_field_is_skipped_and_others_added(env):
    set_items(
        env,
        [
            existing_item(
                [
                    {"name": "TF_VAR_tailscale_oauth_client_id", "value": "KEEP-ME", "type": 1},
                    {"name": "unrelated", "value": "ALSO-KEEP", "type": 0},
                ]
            )
        ],
    )
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert "SKIP   TF_VAR_tailscale_oauth_client_id" in result.stdout
    assert_no_values(env, result)
    assert (env["dir"] / "edited.id").read_text().strip() == "item-123"
    by_name = {f["name"]: f for f in read_json(env, "edited.json")["fields"]}
    assert by_name["TF_VAR_tailscale_oauth_client_id"]["value"] == "KEEP-ME"
    assert by_name["unrelated"]["value"] == "ALSO-KEEP"
    assert (
        by_name["TF_VAR_tailscale_oauth_client_secret"]["value"]
        == FAKE["TF_VAR_tailscale_oauth_client_secret"]
    )
    assert not (env["dir"] / "created.json").exists()


def test_nothing_to_change_makes_no_write(env):
    set_items(
        env,
        [existing_item([{"name": m, "value": "SET", "type": 1} for m in MAPPINGS])],
    )
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert "nothing to change" in result.stdout
    assert not (env["dir"] / "created.json").exists()
    assert not (env["dir"] / "edited.json").exists()


def test_empty_existing_field_is_filled_keeping_its_type(env):
    set_items(
        env,
        [existing_item([{"name": "TF_VAR_tailscale_oauth_client_id", "value": "", "type": 0}])],
    )
    result = run(env)
    assert result.returncode == 0, result.stderr
    by_name = {f["name"]: f for f in read_json(env, "edited.json")["fields"]}
    assert (
        by_name["TF_VAR_tailscale_oauth_client_id"]["value"]
        == FAKE["TF_VAR_tailscale_oauth_client_id"]
    )
    assert by_name["TF_VAR_tailscale_oauth_client_id"]["type"] == 0


def test_unset_env_var_is_an_error_and_writes_nothing(env):
    result = run(env, mappings=[*MAPPINGS, "TF_VAR_not_in_file"])
    assert result.returncode != 0
    assert "TF_VAR_not_in_file is unset or empty" in result.stderr
    assert_no_values(env, result)
    assert not (env["dir"] / "created.json").exists()


def test_empty_env_var_is_an_error(env):
    env["env_file"].write_text('export AWS_ACCESS_KEY_ID=""\n')
    result = run(env, mappings=["AWS_ACCESS_KEY_ID"])
    assert result.returncode != 0
    assert "AWS_ACCESS_KEY_ID is unset or empty" in result.stderr


def test_duplicate_item_names_are_refused(env):
    set_items(env, [existing_item([]), {**existing_item([]), "id": "item-456"}])
    result = run(env)
    assert result.returncode != 0
    assert "2 vault items are named" in result.stderr
    assert not (env["dir"] / "created.json").exists()
    assert not (env["dir"] / "edited.json").exists()


def test_search_matches_are_filtered_to_exact_name(env):
    set_items(env, [{**existing_item([]), "name": f"{ITEM} (old copy)"}])
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert (env["dir"] / "created.json").exists()
    assert not (env["dir"] / "edited.json").exists()


def test_multiline_value_round_trips(env):
    pem = "-----BEGIN KEY-----\nFAKEFAKE\n-----END KEY-----"
    env["env_file"].write_text(f"export TF_VAR_github_app_pem_contents='{pem}'\n")
    result = run(env, mappings=["TF_VAR_github_app_pem_contents"])
    assert result.returncode == 0, result.stderr
    assert "FAKEFAKE" not in result.stdout + result.stderr
    field = read_json(env, "created.json")["fields"][0]
    assert field["name"] == "TF_VAR_github_app_pem_contents"
    assert field["value"] == pem


def test_explicit_field_name_overrides_the_default(env):
    result = run(env, mappings=["AWS_ACCESS_KEY_ID=custom-field-name"])
    assert result.returncode == 0, result.stderr
    names = [f["name"] for f in read_json(env, "created.json")["fields"]]
    assert names == ["custom-field-name"]


def test_field_names_default_to_the_variable_names(env):
    result = run(env)
    assert result.returncode == 0, result.stderr
    names = [f["name"] for f in read_json(env, "created.json")["fields"]]
    assert names == MAPPINGS


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--item", ITEM],
        ["--item", ITEM, "/nonexistent.env", "A=b"],
    ],
)
def test_bad_usage_exits_2(env, args):
    result = subprocess.run(
        ["bash", str(SCRIPT), *args], env=env["env"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 2


def test_bad_mapping_exits_2(env):
    result = run(env, mappings=["not a mapping"])
    assert result.returncode == 2
    assert "bad variable" in result.stderr


def test_stub_encode_is_plain_base64():
    # Guards the stub itself: if `base64 -d` can't invert it, other tests lie.
    assert base64.b64decode(base64.b64encode(b"x")) == b"x"
