"""`verify`: test every registered model through a real Agents chat."""

import io
import json

import pytest
from fakes import FakeCoder, seed_in_sync, small_catalog

TOKEN = "sekrit-coder-token-123"
ENV = {"CODER_SESSION_TOKEN": TOKEN}
MODELS = ["anthropic/claude-a", "nvidia/free-y", "openai/gpt-x"]


@pytest.fixture
def desired(sync):
    return sync.build_desired(small_catalog())


@pytest.fixture
def fake(desired):
    coder = FakeCoder()
    seed_in_sync(coder, desired)
    return coder


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"data": small_catalog()}))
    return str(path)


def never_load(args):
    raise AssertionError("verify must not load the Requesty catalog")


def verify(sync, fake, *args, answer="y", prompts=None, env=ENV):
    out = io.StringIO()

    def confirm(prompt):
        if prompts is not None:
            prompts.append(prompt)
        return answer

    code = sync.main(
        ["verify", "--poll", "0", *args],
        env,
        client_factory=lambda base, token: fake,
        load_catalog=never_load,
        confirm=confirm,
        out=out,
    )
    return code, out.getvalue()


def line_for(output, model):
    return next(line for line in output.splitlines() if model in line and "verified" not in line)


def model_named(fake, model_id):
    return next(m for m in fake.models.values() if m["model"] == model_id)


def chatted(fake):
    return sorted(chat["model"] for chat in fake.chats.values())


def add_model(fake, provider_id, model_id, enabled=True):
    return fake.create_model(
        "org-1",
        {
            "ai_provider_id": provider_id,
            "model": model_id,
            "display_name": model_id,
            "enabled": enabled,
            "context_limit": 1000,
        },
    )


def add_provider(fake, name):
    return fake.create_provider(
        {
            "type": "openai",
            "name": name,
            "display_name": name,
            "icon": "https://example.com/icon.png",
            "enabled": True,
            "base_url": "https://example.com/v1",
            "api_keys": ["k"],
        }
    )


def test_all_models_ok(sync, fake):
    code, output = verify(sync, fake)
    assert code == 0
    assert "verified 3 models: 3 ok, 0 failed, 0 inconclusive; total cost $0.0045" in output
    for model in MODELS:
        assert line_for(output, model).startswith("OK")
    assert chatted(fake) == MODELS
    assert all(chat["archived"] for chat in fake.chats.values())
    archived = [call for call in fake.calls if call[0] == "archive_chat"]
    assert sorted(chat_id for _, chat_id in archived) == sorted(fake.chats)


def test_the_probe_chat_payload(sync, fake):
    verify(sync, fake, "--model", "openai/gpt-x")
    (chat,) = fake.chats.values()
    config_id = model_named(fake, "openai/gpt-x")["id"]
    assert chat["payload"] == {
        "organization_id": "org-1",
        "model_config_id": config_id,
        "client_type": "api",
        "labels": {"probe": "requesty-sync-verify"},
        "content": [{"type": "text", "text": "Reply with the single word ok."}],
    }


def test_a_non_retryable_error_is_a_failure(sync, fake, capsys):
    fake.chat_behaviors["openai/gpt-x"] = "error"
    code, output = verify(sync, fake)
    assert code == 1
    line = line_for(output, "openai/gpt-x")
    assert line.startswith("FAIL")
    assert "Google returned an unexpected error." in line
    assert "Conversation roles must alternate" in line
    assert "HTTP 400" in line
    assert "google" in line
    assert "openai-via-requesty" in line
    assert "verified 3 models: 2 ok, 1 failed, 0 inconclusive" in output
    assert all(chat["archived"] for chat in fake.chats.values())
    assert TOKEN not in output + capsys.readouterr().err


def test_a_retryable_error_is_inconclusive_and_never_disabled(sync, fake):
    fake.chat_behaviors["openai/gpt-x"] = "retryable_error"
    prompts = []
    code, output = verify(sync, fake, "--disable-failures", "--yes", prompts=prompts)
    assert code == 1
    line = line_for(output, "openai/gpt-x")
    assert line.startswith("INCONCLUSIVE")
    assert "HTTP 429" in line
    assert "verified 3 models: 2 ok, 0 failed, 1 inconclusive" in output
    assert model_named(fake, "openai/gpt-x")["enabled"] is True
    assert "Disable" not in output
    assert prompts == []
    assert all(chat["archived"] for chat in fake.chats.values())


def test_requires_action_counts_as_ok(sync, fake):
    fake.chat_behaviors["anthropic/claude-a"] = "tool"
    code, output = verify(sync, fake)
    assert code == 0
    assert line_for(output, "anthropic/claude-a").startswith("OK")
    assert "3 ok, 0 failed, 0 inconclusive" in output


def test_a_chat_that_never_finishes_is_inconclusive(sync, fake):
    fake.chat_behaviors["nvidia/free-y"] = "never"
    code, output = verify(sync, fake, "--timeout", "0.2")
    assert code == 1
    line = line_for(output, "nvidia/free-y")
    assert line.startswith("INCONCLUSIVE")
    assert "no reply within 0.2s (last status: running)" in line
    assert "2 ok, 0 failed, 1 inconclusive" in output
    assert all(chat["archived"] for chat in fake.chats.values())


def test_disable_failures_with_yes_disables_only_the_failed_model(sync, fake):
    fake.chat_behaviors["openai/gpt-x"] = "error"
    fake.chat_behaviors["nvidia/free-y"] = "retryable_error"
    prompts = []
    code, output = verify(sync, fake, "--disable-failures", "--yes", prompts=prompts)
    assert code == 1
    assert prompts == []
    assert "Disabled 1 model(s)." in output
    assert model_named(fake, "openai/gpt-x")["enabled"] is False
    assert model_named(fake, "nvidia/free-y")["enabled"] is True
    assert model_named(fake, "anthropic/claude-a")["enabled"] is True


def test_disable_failures_asks_and_a_no_disables_nothing(sync, fake):
    fake.chat_behaviors["openai/gpt-x"] = "error"
    prompts = []
    code, output = verify(sync, fake, "--disable-failures", answer="n", prompts=prompts)
    assert code == 1
    assert prompts == ["Disable 1 failed model(s)? [y/N] "]
    assert "openai/gpt-x" in output.split("verified")[1]
    assert "Disabled" not in output
    assert model_named(fake, "openai/gpt-x")["enabled"] is True
    assert not [call for call in fake.calls if call[0] == "update_model"]


def test_disable_failures_asks_and_a_yes_disables_the_failed_model(sync, fake):
    fake.chat_behaviors["openai/gpt-x"] = "error"
    prompts = []
    code, output = verify(sync, fake, "--disable-failures", answer="y", prompts=prompts)
    assert code == 1
    assert prompts == ["Disable 1 failed model(s)? [y/N] "]
    assert "Disabled 1 model(s)." in output
    assert model_named(fake, "openai/gpt-x")["enabled"] is False
    assert model_named(fake, "anthropic/claude-a")["enabled"] is True


def test_failures_are_left_alone_without_the_flag(sync, fake):
    fake.chat_behaviors["openai/gpt-x"] = "error"
    prompts = []
    verify(sync, fake, "--yes", prompts=prompts)
    assert prompts == []
    assert model_named(fake, "openai/gpt-x")["enabled"] is True


def test_check_and_apply_leave_a_disabled_model_alone(sync, fake, catalog):
    fake.chat_behaviors["openai/gpt-x"] = "error"
    verify(sync, fake, "--disable-failures", "--yes")
    out = io.StringIO()

    def main(*argv):
        out.seek(0)
        out.truncate()
        code = sync.main(
            list(argv),
            {**ENV, "REQUESTY_API_KEY": "key"},
            client_factory=lambda base, token: fake,
            confirm=lambda prompt: "y",
            out=out,
        )
        return code, out.getvalue()

    code, output = main("check", "--catalog-file", catalog)
    assert code == 0
    assert "selected but disabled in Coder: openai/gpt-x" in output
    assert output.strip().endswith("in sync")
    code, output = main("apply", "--yes", "--catalog-file", catalog)
    assert code == 0
    assert "Nothing to apply." in output
    assert model_named(fake, "openai/gpt-x")["enabled"] is False


def test_provider_narrows_the_set(sync, fake):
    code, output = verify(sync, fake, "--provider", "openai-via-requesty")
    assert code == 0
    assert chatted(fake) == ["openai/gpt-x"]
    assert "verified 1 models: 1 ok" in output


def test_model_narrows_the_set(sync, fake):
    verify(sync, fake, "--model", "nvidia/free-y")
    assert chatted(fake) == ["nvidia/free-y"]


def test_limit_caps_the_set_in_provider_then_model_order(sync, fake):
    verify(sync, fake, "--limit", "2")
    assert chatted(fake) == ["anthropic/claude-a", "nvidia/free-y"]


def test_disabled_models_are_skipped(sync, fake):
    model_named(fake, "openai/gpt-x")["enabled"] = False
    code, output = verify(sync, fake)
    assert code == 0
    assert chatted(fake) == ["anthropic/claude-a", "nvidia/free-y"]
    assert "verified 2 models" in output


def test_models_of_unmanaged_providers_are_skipped(sync, fake):
    other = add_provider(fake, "personal-openai")
    add_model(fake, other["id"], "gpt-personal")
    verify(sync, fake)
    assert chatted(fake) == MODELS


def test_nothing_selected_is_a_clean_exit(sync, fake):
    code, output = verify(sync, fake, "--model", "no/such-model")
    assert code == 0
    assert output.strip() == "No models to verify."
    assert fake.chats == {}


def test_a_chat_that_cannot_be_created_is_inconclusive(sync, fake):
    class Refusing(FakeCoder):
        def create_chat(self, payload):
            raise sync.ApiError("POST", "/api/v2/chats", 500, "boom")

    refusing = Refusing()
    seed_in_sync(refusing, sync.build_desired(small_catalog()))
    code, output = verify(sync, refusing, "--model", "openai/gpt-x")
    assert code == 1
    line = line_for(output, "openai/gpt-x")
    assert line.startswith("INCONCLUSIVE")
    assert "POST /api/v2/chats failed (500): boom" in line


def test_concurrency_runs_every_model(sync, fake):
    provider_id = next(iter(fake.providers))
    for n in range(5):
        add_model(fake, provider_id, f"extra/model-{n}")
    code, output = verify(sync, fake, "--concurrency", "4")
    assert code == 0
    assert "verified 8 models: 8 ok, 0 failed, 0 inconclusive; total cost $0.0120" in output
    assert len(fake.chats) == 8
    assert all(chat["archived"] for chat in fake.chats.values())


def test_a_concurrency_below_one_is_rejected(sync, fake):
    with pytest.raises(SystemExit) as excinfo:
        verify(sync, fake, "--concurrency", "0")
    assert excinfo.value.code == 2


def test_a_missing_token_is_an_error(sync, fake, capsys):
    code, _ = verify(sync, fake, env={})
    assert code == 2
    assert "CODER_SESSION_TOKEN" in capsys.readouterr().err
    assert fake.chats == {}


def test_verify_never_sends_a_heartbeat(sync, fake, stub):
    stub.responses[("GET", "/api/push/tok")] = (200, {"ok": True})
    push = {"UPTIME_KUMA_PUSH_URL": f"http://127.0.0.1:{stub.server_port}/api/push/tok"}
    assert verify(sync, fake, env={**ENV, **push})[0] == 0
    fake.chat_behaviors["openai/gpt-x"] = "error"
    assert verify(sync, fake, env={**ENV, **push})[0] == 1
    assert verify(sync, fake, env=push)[0] == 2
    assert stub.requests == []


def test_an_unexpected_exception_exits_2(sync, fake, capsys):
    class Exploding(FakeCoder):
        def get_chat(self, chat_id):
            raise RuntimeError("boom")

    exploding = Exploding()
    seed_in_sync(exploding, sync.build_desired(small_catalog()))
    code, _ = verify(sync, exploding)
    assert code == 2
    assert "unexpected RuntimeError: boom" in capsys.readouterr().err
    assert all(chat["archived"] for chat in exploding.chats.values())


# ---- verify_model, driven by a scripted client -----------------------------


class Scripted:
    """A client whose get_chat walks through a list of chat states."""

    def __init__(self, states, messages=None, cost=None, archive_error=None):
        self.states = list(states)
        self.messages = messages or []
        self.cost = cost
        self.archive_error = archive_error
        self.created = []
        self.archived = []
        self.polls = 0

    def create_chat(self, payload):
        self.created.append(payload)
        return {"id": "chat-1", "status": "running"}

    def get_chat(self, chat_id):
        self.polls += 1
        state = self.states[min(self.polls, len(self.states)) - 1]
        if isinstance(state, Exception):
            raise state
        return {"id": chat_id, **state}

    def get_chat_messages(self, chat_id):
        return {"messages": self.messages, "queued_messages": [], "has_more": False}

    def get_chat_cost(self, chat_id):
        if isinstance(self.cost, Exception):
            raise self.cost
        return self.cost or {}

    def archive_chat(self, chat_id):
        self.archived.append(chat_id)
        if self.archive_error:
            raise self.archive_error


class Clock:
    """A fake clock: sleeping advances it, and nothing waits in real time."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


ROW = {"id": "cfg-1", "model": "openai/gpt-x", "ai_provider_id": "prov-1", "enabled": True}


def probe(sync, client, timeout=10, poll=2, clock=None):
    clock = clock or Clock()
    return sync.verify_model(
        client, "org-1", ROW, "openai-via-requesty", timeout, poll, clock.sleep, clock.clock
    )


def test_verify_model_times_out_on_the_injected_clock(sync):
    client = Scripted([{"status": "running"}])
    clock = Clock()
    result = probe(sync, client, timeout=10, poll=2, clock=clock)
    assert result.outcome == "inconclusive"
    assert result.detail == "no reply within 10s (last status: running)"
    assert clock.now >= 10
    assert set(clock.sleeps) == {2}
    assert client.archived == ["chat-1"]


def test_verify_model_keeps_polling_a_waiting_chat_with_no_assistant_reply(sync):
    assistant = {"role": "assistant"}
    client = Scripted([{"status": "running"}, {"status": "waiting"}], messages=[assistant])
    assert probe(sync, client).outcome == "ok"
    silent = Scripted([{"status": "waiting"}], messages=[{"role": "user"}])
    result = probe(sync, silent, timeout=6)
    assert result.outcome == "inconclusive"
    assert "last status: waiting" in result.detail
    assert silent.archived == ["chat-1"]


def test_verify_model_reports_the_cost_of_an_ok_chat(sync):
    client = Scripted(
        [{"status": "requires_action"}], cost={"total_cost_micros": 2500, "request_count": 1}
    )
    result = probe(sync, client)
    assert (result.outcome, result.cost_micros) == ("ok", 2500)
    assert (result.model, result.provider, result.config_id) == (
        "openai/gpt-x",
        "openai-via-requesty",
        "cfg-1",
    )


def test_verify_model_treats_an_unreadable_cost_as_zero(sync):
    client = Scripted(
        [{"status": "requires_action"}], cost=sync.ApiError("GET", "/cost", 500, "nope")
    )
    result = probe(sync, client)
    assert (result.outcome, result.cost_micros) == ("ok", 0)
    assert client.archived == ["chat-1"]


def test_verify_model_formats_an_error_without_a_detail_or_status(sync):
    client = Scripted([{"status": "error", "last_error": {"message": "Bad thing."}}])
    result = probe(sync, client)
    assert result.outcome == "failed"
    assert result.detail == "Bad thing. (upstream HTTP ?, ?)"


def test_verify_model_archives_even_when_polling_fails(sync):
    client = Scripted([sync.ApiError("GET", "/api/v2/chats/chat-1", 502, "bad gateway")])
    result = probe(sync, client)
    assert result.outcome == "inconclusive"
    assert "bad gateway" in result.detail
    assert client.archived == ["chat-1"]


def test_verify_model_archives_even_when_polling_raises_unexpectedly(sync):
    client = Scripted([RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        probe(sync, client)
    assert client.archived == ["chat-1"]


def test_an_archive_failure_never_changes_the_outcome(sync):
    ok = Scripted([{"status": "requires_action"}], archive_error=RuntimeError("nope"))
    assert probe(sync, ok).outcome == "ok"
    failed = Scripted(
        [{"status": "error", "last_error": {"message": "x", "retryable": False}}],
        archive_error=sync.ApiError("PATCH", "/api/v2/chats/chat-1", 500, "nope"),
    )
    assert probe(sync, failed).outcome == "failed"


def test_verify_model_is_inconclusive_when_the_created_chat_has_no_id(sync):
    client = Scripted([{"status": "requires_action"}])
    client.create_chat = lambda payload: {}
    result = probe(sync, client)
    assert result.outcome == "inconclusive"
    assert "no id" in result.detail
    assert client.polls == 0
    assert client.archived == []


def test_verify_model_sends_the_probe_payload(sync):
    client = Scripted([{"status": "requires_action"}])
    probe(sync, client)
    assert client.created == [
        {
            "organization_id": "org-1",
            "model_config_id": "cfg-1",
            "client_type": "api",
            "labels": {"probe": "requesty-sync-verify"},
            "content": [{"type": "text", "text": "Reply with the single word ok."}],
        }
    ]


# ---- CoderClient chat methods ----------------------------------------------


def client_for(sync, stub):
    return sync.CoderClient(f"http://127.0.0.1:{stub.server_port}", "tok")


def test_create_chat_posts_the_payload(sync, stub):
    stub.responses[("POST", "/api/v2/chats")] = (201, {"id": "chat-1", "status": "running"})
    chat = client_for(sync, stub).create_chat({"model_config_id": "cfg-1"})
    assert chat == {"id": "chat-1", "status": "running"}
    request = stub.requests[0]
    assert request["method"] == "POST"
    assert json.loads(request["body"]) == {"model_config_id": "cfg-1"}
    assert request["headers"]["coder-session-token"] == "tok"


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        ("get_chat", "", {"id": "chat-1", "status": "waiting"}),
        ("get_chat_messages", "/messages", {"messages": [], "has_more": False}),
        ("get_chat_cost", "/cost", {"total_cost_micros": 7}),
    ],
)
def test_chat_reads_use_get(sync, stub, method, suffix, payload):
    stub.responses[("GET", f"/api/v2/chats/chat-1{suffix}")] = (200, payload)
    assert getattr(client_for(sync, stub), method)("chat-1") == payload
    assert stub.requests[0]["method"] == "GET"
    assert stub.requests[0]["path"] == f"/api/v2/chats/chat-1{suffix}"
    assert stub.requests[0]["body"] == b""


@pytest.mark.parametrize("status", [200, 204])
def test_archive_chat_patches_archived_true(sync, stub, status):
    stub.responses[("PATCH", "/api/v2/chats/chat-1")] = (status, None)
    assert client_for(sync, stub).archive_chat("chat-1") is None
    request = stub.requests[0]
    assert request["method"] == "PATCH"
    assert json.loads(request["body"]) == {"archived": True}


def test_a_chat_read_that_is_not_an_object_is_an_api_error(sync, stub):
    stub.responses[("GET", "/api/v2/chats/chat-1")] = (200, [])
    with pytest.raises(sync.ApiError, match="expected a JSON object"):
        client_for(sync, stub).get_chat("chat-1")
