import io
import json

from fakes import FakeCoder, make_entry, seed_in_sync, small_catalog


def write_catalog(tmp_path, entries=None):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"data": entries or small_catalog()}))
    return str(path)


def run(sync, argv, env, fake, answer="y"):
    out = io.StringIO()
    code = sync.main(
        argv,
        env,
        client_factory=lambda base, token: fake,
        confirm=lambda prompt: answer,
        out=out,
    )
    return code, out.getvalue()


ENV = {"CODER_SESSION_TOKEN": "tok", "REQUESTY_API_KEY": "key"}


def test_check_reports_drift_with_exit_1(sync, tmp_path):
    code, output = run(sync, ["check", "--catalog-file", write_catalog(tmp_path)], ENV, FakeCoder())
    assert code == 1
    assert "MISSING_MODEL (3)" in output
    assert "drift: 3 missing provider" in output


def test_check_is_clean_with_exit_0(sync, tmp_path):
    fake = FakeCoder()
    seed_in_sync(fake, sync.build_desired(small_catalog()))
    code, output = run(sync, ["check", "--catalog-file", write_catalog(tmp_path)], ENV, fake)
    assert code == 0
    assert output.strip().endswith("in sync")


def test_check_json_output(sync, tmp_path):
    code, output = run(
        sync, ["check", "--json", "--catalog-file", write_catalog(tmp_path)], ENV, FakeCoder()
    )
    payload = json.loads(output)
    assert code == 1
    assert payload["drift"] is True
    assert payload["findings"][0]["category"] == "MISSING_PROVIDER"


def test_check_never_writes(sync, tmp_path):
    fake = FakeCoder()
    run(sync, ["check", "--catalog-file", write_catalog(tmp_path)], ENV, fake)
    assert fake.calls == []


def test_missing_token_is_an_error(sync, tmp_path, capsys):
    code, _ = run(sync, ["check", "--catalog-file", write_catalog(tmp_path)], {}, FakeCoder())
    assert code == 2
    assert "CODER_SESSION_TOKEN" in capsys.readouterr().err


def test_an_empty_catalog_is_an_error(sync, tmp_path):
    code, _ = run(
        sync,
        ["check", "--catalog-file", write_catalog(tmp_path, [make_entry("a/x", tools=False)])],
        ENV,
        FakeCoder(),
    )
    assert code == 2


def test_api_failure_is_an_error(sync, tmp_path):
    class Broken(FakeCoder):
        def default_org_id(self):
            raise sync.ApiError("GET", "/api/v2/organizations", 401, "nope")

    code, _ = run(sync, ["check", "--catalog-file", write_catalog(tmp_path)], ENV, Broken())
    assert code == 2


def test_apply_then_check_is_clean(sync, tmp_path):
    fake = FakeCoder()
    catalog = write_catalog(tmp_path)
    code, output = run(sync, ["apply", "--yes", "--catalog-file", catalog], ENV, fake)
    assert code == 0
    assert "Applied." in output
    assert run(sync, ["check", "--catalog-file", catalog], ENV, fake)[0] == 0


def test_apply_asks_and_aborts_on_no(sync, tmp_path):
    fake = FakeCoder()
    code, output = run(
        sync, ["apply", "--catalog-file", write_catalog(tmp_path)], ENV, fake, answer="n"
    )
    assert code == 1
    assert "Aborted." in output
    assert fake.calls == []


def test_apply_with_nothing_to_do(sync, tmp_path):
    fake = FakeCoder()
    seed_in_sync(fake, sync.build_desired(small_catalog()))
    code, output = run(
        sync, ["apply", "--yes", "--catalog-file", write_catalog(tmp_path)], ENV, fake
    )
    assert code == 0
    assert "Nothing to apply." in output


def test_apply_without_the_requesty_key_is_an_error(sync, tmp_path, capsys):
    env = {"CODER_SESSION_TOKEN": "tok"}
    code, _ = run(
        sync, ["apply", "--yes", "--catalog-file", write_catalog(tmp_path)], env, FakeCoder()
    )
    assert code == 2
    assert "REQUESTY_API_KEY" in capsys.readouterr().err


def test_disable_orphans_flag(sync, tmp_path):
    fake = FakeCoder()
    ids = seed_in_sync(fake, sync.build_desired(small_catalog()))
    fake.create_model(
        "org-1",
        {"ai_provider_id": ids["openai-via-requesty"], "model": "openai/old", "enabled": True},
    )
    catalog = write_catalog(tmp_path)
    run(sync, ["apply", "--yes", "--catalog-file", catalog], ENV, fake)
    assert next(m for m in fake.models.values() if m["model"] == "openai/old")["enabled"] is True
    run(sync, ["apply", "--yes", "--disable-orphans", "--catalog-file", catalog], ENV, fake)
    assert next(m for m in fake.models.values() if m["model"] == "openai/old")["enabled"] is False
    assert run(sync, ["check", "--catalog-file", catalog], ENV, fake)[0] == 0


def test_check_pings_down_on_drift_and_up_when_clean(sync, tmp_path, stub):
    stub.responses[("GET", "/api/push/tok")] = (200, {"ok": True})
    env = {**ENV, "UPTIME_KUMA_PUSH_URL": f"http://127.0.0.1:{stub.server_port}/api/push/tok"}
    catalog = write_catalog(tmp_path)
    fake = FakeCoder()
    run(sync, ["check", "--catalog-file", catalog], env, fake)
    assert "status=down" in stub.requests[-1]["query"]
    assert "drift" in stub.requests[-1]["query"]
    seed_in_sync(fake, sync.build_desired(small_catalog()))
    run(sync, ["check", "--catalog-file", catalog], env, fake)
    assert "status=up" in stub.requests[-1]["query"]


def test_a_failed_heartbeat_does_not_change_the_exit_code(sync, tmp_path, stub, capsys):
    url = f"http://127.0.0.1:{stub.server_port}/api/push/tok"
    stub.shutdown()
    stub.server_close()
    code, _ = run(
        sync,
        ["check", "--catalog-file", write_catalog(tmp_path)],
        {**ENV, "UPTIME_KUMA_PUSH_URL": url},
        FakeCoder(),
    )
    assert code == 1
    assert "heartbeat failed" in capsys.readouterr().err


def test_apply_does_not_ping(sync, tmp_path, stub):
    stub.responses[("GET", "/api/push/tok")] = (200, {"ok": True})
    env = {**ENV, "UPTIME_KUMA_PUSH_URL": f"http://127.0.0.1:{stub.server_port}/api/push/tok"}
    run(sync, ["apply", "--yes", "--catalog-file", write_catalog(tmp_path)], env, FakeCoder())
    assert stub.requests == []


def test_an_unexpected_exception_exits_2_and_pings_down(sync, tmp_path, stub, capsys):
    class Exploding(FakeCoder):
        def list_providers(self):
            raise RuntimeError("boom")

    stub.responses[("GET", "/api/push/tok")] = (200, {"ok": True})
    env = {**ENV, "UPTIME_KUMA_PUSH_URL": f"http://127.0.0.1:{stub.server_port}/api/push/tok"}
    code, _ = run(sync, ["check", "--catalog-file", write_catalog(tmp_path)], env, Exploding())
    assert code == 2
    assert "unexpected RuntimeError: boom" in capsys.readouterr().err
    assert "status=down" in stub.requests[-1]["query"]
