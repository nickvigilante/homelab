import json
import stat

import pytest
from fakes import FakeCoder, seed_in_sync, small_catalog
from test_cli import run, write_catalog

TOKEN_ONLY = {"CODER_SESSION_TOKEN": "tok"}
WITH_BW = {**TOKEN_ONLY, "BW_SESSION": "sess"}


def fake_bw(tmp_path, monkeypatch, body, exit_code=0):
    """Put a `bw` on PATH that prints BODY (and logs its arguments)."""
    log = tmp_path / "bw.log"
    script = tmp_path / "bw"
    script.write_text(
        f'#!/bin/sh\necho "$@" >> {log}\n'
        f"if [ \"$1\" = get ]; then cat <<'EOF'\n{body}\nEOF\nexit {exit_code}\nfi\nexit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    return log


def item(*fields):
    return json.dumps({"fields": [{"name": n, "value": v} for n, v in fields]})


def test_the_key_is_read_from_the_requesty_item(sync, tmp_path, monkeypatch):
    log = fake_bw(tmp_path, monkeypatch, item(("Other", "x"), ("Main API key", "sk-123")))
    assert sync.bitwarden_field("Requesty", "Main API key", {"BW_SESSION": "sess"}) == "sk-123"
    assert "get item Requesty" in log.read_text()


def test_a_missing_field_is_an_error_that_does_not_echo_values(sync, tmp_path, monkeypatch):
    fake_bw(tmp_path, monkeypatch, item(("Other", "secret-value")))
    with pytest.raises(sync.SyncError, match="Main API key") as err:
        sync.bitwarden_field("Requesty", "Main API key", {"BW_SESSION": "sess"})
    assert "secret-value" not in str(err.value)


def test_a_failing_bw_is_an_error(sync, tmp_path, monkeypatch):
    fake_bw(tmp_path, monkeypatch, "Not found.", exit_code=1)
    with pytest.raises(sync.SyncError, match="Bitwarden"):
        sync.bitwarden_field("Requesty", "Main API key", {"BW_SESSION": "sess"})


def test_bitwarden_needs_an_unlocked_session(sync):
    with pytest.raises(sync.SyncError, match="BW_SESSION"):
        sync.bitwarden_field("Requesty", "Main API key", {})


def test_apply_falls_back_to_bitwarden_when_it_needs_a_key(sync, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "bitwarden_field", lambda *a: calls.append(a) or "sk-bw")
    fake = FakeCoder()
    code, _ = run(
        sync, ["apply", "--yes", "--catalog-file", write_catalog(tmp_path)], WITH_BW, fake
    )
    assert code == 0
    assert calls == [("Requesty", "Main API key", WITH_BW)]
    assert ("create_provider", "anthropic-via-requesty") in fake.calls


def test_the_env_key_wins_and_bitwarden_is_not_touched(sync, tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "bitwarden_field", lambda *a: pytest.fail("bw was called"))
    env = {**WITH_BW, "REQUESTY_API_KEY": "sk-env"}
    code, _ = run(
        sync, ["apply", "--yes", "--catalog-file", write_catalog(tmp_path)], env, FakeCoder()
    )
    assert code == 0


def test_bitwarden_is_not_touched_when_no_key_is_needed(sync, tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "bitwarden_field", lambda *a: pytest.fail("bw was called"))
    fake = FakeCoder()
    seed_in_sync(fake, sync.build_desired(small_catalog()))
    code, output = run(
        sync, ["apply", "--yes", "--catalog-file", write_catalog(tmp_path)], WITH_BW, fake
    )
    assert code == 0
    assert "Nothing to apply." in output


def test_check_never_reads_bitwarden(sync, tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "bitwarden_field", lambda *a: pytest.fail("bw was called"))
    code, _ = run(sync, ["check", "--catalog-file", write_catalog(tmp_path)], WITH_BW, FakeCoder())
    assert code == 1


def test_apply_with_neither_key_names_both_sources(sync, tmp_path, capsys):
    code, _ = run(
        sync, ["apply", "--yes", "--catalog-file", write_catalog(tmp_path)], TOKEN_ONLY, FakeCoder()
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "REQUESTY_API_KEY" in err
    assert "BW_SESSION" in err
