"""Stub-based tests for scripts/lib/bw-unlock.sh and its two callers.

A fake `bw` is put first on PATH, so the real Bitwarden CLI is never called.
The fake master password lives in a file passed as BW_UNLOCK_TTY; the helper
must hand it to `bw` on stdin and must never print it.
"""

import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "lib" / "bw-unlock.sh"
IMPORT_SCRIPT = ROOT / "scripts" / "bw-import-env.sh"
BOOTSTRAP_SCRIPT = ROOT / "scripts" / "bws-bootstrap-secrets.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="needs jq and bash",
)

FAKE_PASSWORD = "FAKE-master-password-9f3a"
BASH = shutil.which("bash")  # absolute: one test narrows PATH to nothing useful

# Counts unlock calls, records what arrived on stdin (the tty file), and fails
# the first STUB_UNLOCK_FAILS attempts the way real bw does on a wrong password.
BW_STUB = r"""#!/usr/bin/env bash
echo "$*" >>"$STUB_DIR/argv.log"
case "$1" in
  status) printf '{"status":"%s"}\n' "${STUB_STATUS:-locked}" ;;
  unlock)
    n=$(( $(cat "$STUB_DIR/unlock.count" 2>/dev/null || echo 0) + 1 ))
    echo "$n" >"$STUB_DIR/unlock.count"
    cat >"$STUB_DIR/stdin.seen"
    if [ "$n" -le "${STUB_UNLOCK_FAILS:-0}" ]; then
      echo "ERROR bitwarden_crypto::keys::master_key: error=The decryption operation failed" >&2
      exit 1
    fi
    printf 'FAKE-SESSION-TOKEN'
    ;;
  sync) ;;
  list) echo '[]' ;;
  get) echo '{"notes":""}' ;;
  *) echo "stub bw: unexpected: $*" >&2; exit 99 ;;
esac
"""


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    bw = bindir / "bw"
    bw.write_text(BW_STUB)
    bw.chmod(0o755)
    tty = tmp_path / "tty"
    tty.write_text(FAKE_PASSWORD + "\n")
    base = {k: v for k, v in os.environ.items() if k != "BW_SESSION"}
    return {
        "dir": tmp_path,
        "bindir": bindir,
        "env": {
            **base,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "STUB_DIR": str(tmp_path),
            "BW_UNLOCK_TTY": str(tty),
        },
    }


def unlock_count(env):
    f = env["dir"] / "unlock.count"
    return int(f.read_text()) if f.exists() else 0


def argv_log(env):
    f = env["dir"] / "argv.log"
    return f.read_text() if f.exists() else ""


def run_helper(env, **overrides):
    e = {**env["env"], **overrides}
    script = (
        f'set -uo pipefail; . "{HELPER}"; bw_ensure_unlocked; rc=$?; '
        'echo "rc=$rc session=${BW_SESSION:-}"; exit $rc'
    )
    return subprocess.run([BASH, "-c", script], env=e, capture_output=True, text=True, timeout=30)


def assert_password_not_leaked(result):
    assert FAKE_PASSWORD not in result.stdout
    assert FAKE_PASSWORD not in result.stderr


@pytest.mark.parametrize("fails", [1, 2])
def test_succeeds_after_a_mistyped_password(env, fails):
    result = run_helper(env, STUB_UNLOCK_FAILS=str(fails))
    assert result.returncode == 0, result.stderr
    assert "session=FAKE-SESSION-TOKEN" in result.stdout
    assert unlock_count(env) == fails + 1
    for n in range(1, fails + 1):
        assert f"Unlock failed (attempt {n}/3) -- check the master password." in result.stderr
    assert_password_not_leaked(result)


def test_gives_up_after_three_failures_with_no_fourth_attempt(env):
    result = run_helper(env, STUB_UNLOCK_FAILS="99")
    assert result.returncode != 0
    assert unlock_count(env) == 3
    assert "attempt 3/3" in result.stderr
    assert "FATAL: bw unlock failed 3 times" in result.stderr
    assert "FAKE-SESSION-TOKEN" not in result.stdout
    assert_password_not_leaked(result)


def test_attempt_limit_is_configurable(env):
    result = run_helper(env, STUB_UNLOCK_FAILS="99", BW_UNLOCK_ATTEMPTS="2")
    assert result.returncode != 0
    assert unlock_count(env) == 2


def test_reuses_an_unlocked_session_without_prompting(env):
    result = run_helper(env, BW_SESSION="existing-session", STUB_STATUS="unlocked")
    assert result.returncode == 0, result.stderr
    assert "session=existing-session" in result.stdout
    assert unlock_count(env) == 0
    assert_password_not_leaked(result)


def test_a_stale_session_is_not_reused(env):
    """BW_SESSION set but `bw status` says locked: prompt, don't trust it."""
    result = run_helper(env, BW_SESSION="stale-session", STUB_STATUS="locked")
    assert result.returncode == 0, result.stderr
    assert unlock_count(env) == 1
    assert "session=FAKE-SESSION-TOKEN" in result.stdout


def test_fails_fast_when_not_logged_in(env):
    result = run_helper(env, STUB_STATUS="unauthenticated", STUB_UNLOCK_FAILS="99")
    assert result.returncode != 0
    assert unlock_count(env) == 0
    assert "bw login" in result.stderr
    assert_password_not_leaked(result)


def test_fails_fast_when_bw_is_missing(env):
    (env["bindir"] / "bw").unlink()
    jq = shutil.which("jq")
    nobw = env["dir"] / "nobw"
    nobw.mkdir()
    (nobw / "jq").symlink_to(jq)
    result = run_helper(env, PATH=str(nobw))
    assert result.returncode != 0
    assert "bw (Bitwarden CLI) not found" in result.stderr


def test_the_password_reaches_bw_on_stdin_and_nowhere_else(env):
    result = run_helper(env)
    assert result.returncode == 0, result.stderr
    assert (env["dir"] / "stdin.seen").read_text().strip() == FAKE_PASSWORD
    assert FAKE_PASSWORD not in argv_log(env)
    assert_password_not_leaked(result)


# ---- the two callers actually use the helper ----


def run_import(env, **overrides):
    env_file = env["dir"] / "x.env"
    env_file.write_text('export FAKE_VAR="FAKE-value-1"\n')
    return subprocess.run(
        [BASH, str(IMPORT_SCRIPT), "--item", "Test Item", "--dry-run", str(env_file), "FAKE_VAR"],
        env={**env["env"], **overrides},
        capture_output=True,
        text=True,
        timeout=30,
    )


def run_bootstrap(env, **overrides):
    return subprocess.run(
        [BASH, str(BOOTSTRAP_SCRIPT)],
        input="fake-name|Fake Item|fake-field|\n",
        env={**env["env"], "BWS_PROJECT_ID": "3f691960-335a-4bb3-a2f9-b4ca00e86906", **overrides},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_import_script_retries_then_proceeds(env):
    result = run_import(env, STUB_UNLOCK_FAILS="1")
    assert result.returncode == 0, result.stderr
    assert unlock_count(env) == 2
    assert "list items" in argv_log(env)
    assert_password_not_leaked(result)


def test_import_script_stops_after_three_failures_before_touching_the_vault(env):
    result = run_import(env, STUB_UNLOCK_FAILS="99")
    assert result.returncode != 0
    assert unlock_count(env) == 3
    assert "list items" not in argv_log(env)
    assert "sync" not in argv_log(env)


def test_bootstrap_script_retries_then_gets_past_unlock(env):
    result = run_bootstrap(env, STUB_UNLOCK_FAILS="1")
    assert unlock_count(env) == 2
    # The stub vault has an empty token note, so it stops right after unlock,
    # which proves the retry let it through.
    assert "bootstrap access token not found" in result.stderr
    assert_password_not_leaked(result)


def test_bootstrap_script_stops_after_three_failures_before_touching_the_vault(env):
    result = run_bootstrap(env, STUB_UNLOCK_FAILS="99")
    assert result.returncode != 0
    assert unlock_count(env) == 3
    assert "get item" not in argv_log(env)
    assert "sync" not in argv_log(env)
