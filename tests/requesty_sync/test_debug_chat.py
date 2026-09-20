"""requesty-debug-chat.py: capture the request Coder Agents sends for a chat."""

import base64
import importlib.util
import io
import json
import pathlib
import sys

import pytest
from fakes import FakeCoder, seed_in_sync, small_catalog

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "requesty-debug-chat.py"
TOKEN = "sekrit-coder-token-123"


@pytest.fixture(scope="session")
def debug(sync):
    """The script under test. It reuses the sync module the `sync` fixture loaded."""
    spec = importlib.util.spec_from_file_location("requesty_debug_chat", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["requesty_debug_chat"] = module
    spec.loader.exec_module(module)
    return module


# ---- a hand-written debug run, in the shape Coder v2.37.0 returns --------------------

SYSTEM_TEXT = "You are Coder Agents, a coding assistant. TAIL-OF-THE-SYSTEM-PROMPT"
USER_TEXT = "Reply with the single word ok. TAIL-OF-THE-USER-PROMPT"
HEADER_VALUE = "SECRET-HEADER-VALUE-7"


def b64(value):
    """Coder marshals an attempt's body bytes as base64."""
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return base64.b64encode(raw).decode()


def tool(name, strict=None, additional=None):
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    if additional is not None:
        parameters["additionalProperties"] = additional
    function = {"name": name, "description": "does " + name, "parameters": parameters}
    if strict is not None:
        function["strict"] = strict
    return {"type": "function", "function": function}


def request_body(tools=None):
    return {
        "model": "qwen/qwen-2.5-72b-instruct",
        "messages": [
            {"role": "system", "content": SYSTEM_TEXT},
            {"role": "system", "content": "The workspace is a Linux container."},
            {"role": "user", "content": [{"type": "text", "text": USER_TEXT}]},
        ],
        "tools": tools
        if tools is not None
        else [tool("read_file", strict=True, additional=False), tool("execute")],
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
        "store": True,
        "max_tokens": 4096,
    }


RESPONSE_TEXT = '{"error": {"message": "Conversation roles must alternate",\n  "code": 400}}'


def attempt(body=None, status=400, response=RESPONSE_TEXT):
    return {
        "number": 1,
        "status": "completed",
        "method": "POST",
        "url": "https://router.requesty.ai/v1/chat/completions",
        "path": "/v1/chat/completions",
        "request_headers": {
            "Authorization": "[REDACTED]",
            "User-Agent": HEADER_VALUE,
            "Content-Type": "application/json",
        },
        "request_body": b64(request_body() if body is None else body),
        "response_status": status,
        "response_headers": {"X-Request-Id": HEADER_VALUE, "Content-Type": "text/plain"},
        "response_body": b64(response.encode()),
        "duration_ms": 312,
    }


def run_detail(attempts=None, status="error", steps=None):
    if steps is None:
        steps = [
            {
                "id": "step-1",
                "step_number": 1,
                "operation": "stream",
                "status": status,
                "attempts": [attempt()] if attempts is None else attempts,
                "error": {"message": "upstream said no", "type": "provider"},
                "normalized_request": {},
            }
        ]
    return {
        "id": "run-1",
        "chat_id": "chat-1",
        "kind": "chat_turn",
        "status": status,
        "provider": "openai",
        "model": "qwen/qwen-2.5-72b-instruct",
        "started_at": "2026-09-19T10:00:00Z",
        "steps": steps,
    }


def capture(runs=None, chat=None, notes=None):
    return {
        "model": "novita/qwen/qwen-2.5-72b-instruct",
        "chat_id": "chat-1",
        "chat": chat
        or {
            "status": "error",
            "last_error": {
                "message": "Upstream returned an error.",
                "status_code": 400,
                "retryable": False,
                "kind": "generic",
                "provider": "openai",
            },
        },
        "runs": [run_detail()] if runs is None else runs,
        "notes": notes or [],
    }


def digest(debug, **kwargs):
    return "\n".join(debug.digest_capture(capture(**kwargs)))


# ---- the digest ---------------------------------------------------------------------


def test_the_digest_states_the_chat_status_and_last_error(debug):
    text = digest(debug)
    assert "status: error" in text
    assert "Upstream returned an error." in text
    assert "status_code=400" in text
    assert "retryable=false" in text


def test_the_digest_names_the_run_step_provider_and_model(debug):
    text = digest(debug)
    assert "chat_turn" in text
    assert "provider openai" in text
    assert "model qwen/qwen-2.5-72b-instruct" in text
    assert "step 1: stream, status error" in text


def test_the_digest_shows_every_top_level_key_with_its_scalar_value(debug):
    text = digest(debug)
    assert 'model: "qwen/qwen-2.5-72b-instruct"' in text
    assert "store: true" in text
    assert "stream: true" in text
    assert "max_tokens: 4096" in text
    assert 'tool_choice: "auto"' in text
    assert "stream_options: {include_usage: true}" in text
    for key in ("messages", "tools"):
        assert f"{key}:" in text


def test_the_digest_lists_message_roles_with_content_lengths(debug):
    text = digest(debug)
    assert (
        f"messages: list of 3: system({len(SYSTEM_TEXT)}), "
        f"system({len('The workspace is a Linux container.')}), user({len(USER_TEXT)})"
    ) in text


def test_the_digest_shows_only_the_first_40_characters_of_a_message(debug):
    text = digest(debug)
    assert SYSTEM_TEXT[:40] in text
    assert USER_TEXT[:40] in text
    assert "TAIL-OF-THE-SYSTEM-PROMPT" not in text
    assert "TAIL-OF-THE-USER-PROMPT" not in text


def test_the_digest_lists_tool_names_and_the_schema_keywords(debug):
    text = digest(debug)
    assert "tools: 2: read_file, execute" in text
    assert "strict: read_file" in text
    assert "additionalProperties: read_file" in text


def test_the_digest_says_when_no_tool_schema_has_the_keywords(debug):
    text = digest(debug, runs=[run_detail([attempt(request_body([tool("a"), tool("b")]))])])
    assert "tools: 2: a, b" in text
    assert "strict: none" in text
    assert "additionalProperties: none" in text


def test_the_digest_handles_a_request_with_no_tools(debug):
    body = request_body()
    del body["tools"]
    text = digest(debug, runs=[run_detail([attempt(body)])])
    assert "tools" not in text.replace("tool_choice", "")


def test_the_digest_shows_the_response_status_and_a_collapsed_body(debug):
    text = digest(debug)
    assert "-> HTTP 400" in text
    assert (
        'response body: {"error": {"message": "Conversation roles must alternate", "code": 400}}'
        in text
    )


def test_the_response_body_is_cut_to_400_characters(debug):
    body = "  ".join(["x" * 99] * 10)
    text = digest(debug, runs=[run_detail([attempt(response=body)])])
    (line,) = [ln for ln in text.splitlines() if "response body:" in ln]
    shown = line.split("response body: ", 1)[1]
    assert len(shown) == 400
    assert shown == " ".join(["x" * 99] * 10)[:400]


def test_the_digest_prints_header_names_but_never_values(debug):
    text = digest(debug)
    assert "User-Agent" in text
    assert "Content-Type" in text
    assert HEADER_VALUE not in text
    assert "REDACTED" not in text
    assert "application/json" not in text


def test_the_digest_reads_a_body_that_is_already_decoded(debug):
    plain = attempt()
    plain["request_body"] = request_body()
    plain["response_body"] = {"error": "plain object"}
    text = digest(debug, runs=[run_detail([plain])])
    assert "store: true" in text
    assert 'response body: {"error": "plain object"}' in text


def test_the_digest_marks_a_truncated_request_body(debug):
    truncated = attempt()
    truncated["request_body"] = b64(b"[TRUNCATED]")
    text = digest(debug, runs=[run_detail([truncated])])
    assert "request body: [TRUNCATED]" in text


def test_the_digest_says_when_no_debug_runs_exist(debug):
    text = digest(debug, runs=[], notes=["no debug runs were recorded"])
    assert "no debug runs were recorded" in text


def test_the_digest_shows_a_step_error_and_the_response_usage(debug):
    step = {
        "step_number": 1,
        "operation": "generate",
        "status": "completed",
        "attempts": [],
        "error": {"message": "boom " * 200},
        "normalized_response": {
            "finish_reason": "stop",
            "content": [{"type": "text", "text": "OK", "text_length": 2}],
            "usage": {"input_tokens": 12, "output_tokens": 3},
        },
        "normalized_request": {
            "messages": [{"role": "user", "parts": [{"type": "text", "text_length": 5}]}]
        },
    }
    text = digest(debug, runs=[run_detail(steps=[step])])
    assert "finish_reason=stop" in text
    assert "input_tokens=12" in text
    error_line = next(ln for ln in text.splitlines() if ln.strip().startswith("error:"))
    assert len(error_line.strip()) <= len("error: ") + 400
    assert "messages: list of 1: user(5)" in text


def test_the_digest_is_bounded_for_scalars_and_containers(debug):
    body = {
        "long": "y" * 500,
        "nested": {"a": {"b": {"c": 1}}},
        "numbers": [1, 2, 3],
        "objects": [{"k": 1, "z": 2}],
        "empty": {},
        "nothing": None,
    }
    text = digest(debug, runs=[run_detail([attempt(body)])])
    assert "y" * 81 not in text
    assert "nested: {a: <object with keys b>}" in text
    assert "numbers: <list of 3: 1, 2, 3>" in text
    assert "objects: <list of 1: keys k, z>" in text
    assert "empty: {}" in text
    assert "nothing: null" in text


def test_the_digest_marks_a_message_with_tool_calls_and_null_content(debug):
    body = request_body()
    body["messages"] = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "result", "tool_call_id": "1"},
    ]
    text = digest(debug, runs=[run_detail([attempt(body)])])
    assert "assistant(0)" in text
    assert "tool_calls" in text
    assert "tool(6)" in text


# ---- the client ---------------------------------------------------------------------


def debug_client(debug, stub):
    return debug.DebugClient(f"http://127.0.0.1:{stub.server_port}", TOKEN)


def test_the_client_reads_and_sets_the_admin_gate(debug, stub):
    path = "/api/v2/chats/config/debug-logging"
    stub.responses[("GET", path)] = (200, {"allow_users": True, "forced_by_deployment": False})
    stub.responses[("PUT", path)] = (204, None)
    client = debug_client(debug, stub)
    assert client.get_debug_logging() == {"allow_users": True, "forced_by_deployment": False}
    client.set_debug_logging_allow_users(False)
    put = stub.requests[-1]
    assert (put["method"], put["path"]) == ("PUT", path)
    assert json.loads(put["body"]) == {"allow_users": False}
    assert put["headers"]["coder-session-token"] == TOKEN


def test_the_client_reads_and_sets_the_users_own_toggle(debug, stub):
    path = "/api/v2/chats/config/user-debug-logging"
    reply = {"debug_logging_enabled": False, "user_toggle_allowed": True}
    stub.responses[("GET", path)] = (200, reply)
    stub.responses[("PUT", path)] = (204, None)
    client = debug_client(debug, stub)
    assert client.get_user_debug_logging() == reply
    client.set_user_debug_logging(True)
    assert json.loads(stub.requests[-1]["body"]) == {"debug_logging_enabled": True}


def test_debug_runs_are_read_from_the_documented_v2_path(debug, stub):
    stub.responses[("GET", "/api/v2/chats/c1/debug/runs")] = (200, [{"id": "r1"}])
    stub.responses[("GET", "/api/v2/chats/c1/debug/runs/r1")] = (200, {"id": "r1", "steps": []})
    client = debug_client(debug, stub)
    assert client.list_debug_runs("c1") == [{"id": "r1"}]
    assert client.get_debug_run("c1", "r1") == {"id": "r1", "steps": []}
    assert [r["path"] for r in stub.requests] == [
        "/api/v2/chats/c1/debug/runs",
        "/api/v2/chats/c1/debug/runs/r1",
    ]


def test_debug_runs_fall_back_to_the_experimental_path_v2_37_serves(debug, stub):
    """In v2.37.0 the run routes are mounted only under /api/experimental."""
    stub.responses[("GET", "/api/experimental/chats/c1/debug/runs")] = (200, [{"id": "r1"}])
    stub.responses[("GET", "/api/experimental/chats/c1/debug/runs/r1")] = (200, {"id": "r1"})
    client = debug_client(debug, stub)
    assert client.list_debug_runs("c1") == [{"id": "r1"}]
    assert client.get_debug_run("c1", "r1") == {"id": "r1"}
    paths = [r["path"] for r in stub.requests]
    assert paths == [
        "/api/v2/chats/c1/debug/runs",
        "/api/experimental/chats/c1/debug/runs",
        "/api/experimental/chats/c1/debug/runs/r1",
    ]


def test_debug_runs_report_a_404_on_both_paths(debug, sync, stub):
    client = debug_client(debug, stub)
    with pytest.raises(sync.ApiError) as excinfo:
        client.list_debug_runs("c1")
    assert excinfo.value.status == 404


def test_a_null_run_list_is_empty(debug, stub):
    stub.responses[("GET", "/api/v2/chats/c1/debug/runs")] = (200, b"null")
    assert debug_client(debug, stub).list_debug_runs("c1") == []


# ---- a fake Coder with the debug endpoints ------------------------------------------


class FakeDebugCoder(FakeCoder):
    """FakeCoder plus the debug-logging endpoints, kept in memory."""

    def __init__(self, sync, allow_users=True, user_stored=True, forced=False, is_admin=True):
        super().__init__()
        self.ApiError = sync.ApiError
        self.allow_users = allow_users
        self.user_stored = user_stored  # what the user's own toggle holds
        self.forced = forced
        self.is_admin = is_admin
        self.toggle_calls = []  # ("admin" | "user", value), in order
        self.record_runs = True  # False: Coder recorded nothing for the chat
        self.response_text = RESPONSE_TEXT
        self.list_calls = 0
        self.run_error = None
        self.get_chat_error = None

    def get_debug_logging(self):
        if not self.is_admin:
            raise self.ApiError("GET", "/api/v2/chats/config/debug-logging", 404, "nope")
        return {"allow_users": self.allow_users, "forced_by_deployment": self.forced}

    def set_debug_logging_allow_users(self, allow):
        if not self.is_admin:
            raise self.ApiError("PUT", "/api/v2/chats/config/debug-logging", 403, "no")
        self.toggle_calls.append(("admin", allow))
        self.allow_users = allow

    def get_user_debug_logging(self):
        enabled = self.forced or (self.allow_users and self.user_stored)
        return {
            "debug_logging_enabled": enabled,
            "user_toggle_allowed": not self.forced and self.allow_users,
            "forced_by_deployment": self.forced,
        }

    def set_user_debug_logging(self, enabled):
        path = "/api/v2/chats/config/user-debug-logging"
        if self.forced:
            raise self.ApiError("PUT", path, 409, "forced on by deployment")
        if not self.allow_users:
            raise self.ApiError("PUT", path, 403, "an admin has not enabled it")
        self.toggle_calls.append(("user", enabled))
        self.user_stored = enabled

    def get_chat(self, chat_id):
        if self.get_chat_error:
            raise self.get_chat_error
        return super().get_chat(chat_id)

    def create_chat(self, payload):
        created = super().create_chat(payload)
        # Coder decides per turn, from the settings as they are when the chat starts.
        self.chats[created["id"]]["debug_on"] = self.get_user_debug_logging()[
            "debug_logging_enabled"
        ]
        return created

    def _runs(self, chat_id):
        if not self.record_runs:
            return []
        chat = self.chats[chat_id]
        failed = self.chat_behaviors.get(chat["model"], "ok") == "error"
        body = request_body()
        body["model"] = chat["model"]
        status = "error" if failed else "completed"
        run = run_detail(
            [attempt(body, status=400 if failed else 200, response=self.response_text)],
            status=status,
        )
        return [{**run, "id": f"run-{chat_id}", "chat_id": chat_id}]

    def list_debug_runs(self, chat_id):
        self.list_calls += 1
        if self.run_error:
            raise self.run_error
        return [{"id": r["id"], "status": r["status"]} for r in self._runs(chat_id)]

    def get_debug_run(self, chat_id, run_id):
        return next(r for r in self._runs(chat_id) if r["id"] == run_id)


def api_error(sync, status, method="GET", path="/x"):
    return sync.ApiError(method, path, status, "boom")


# ---- reading the state --------------------------------------------------------------


def test_state_when_the_admin_gate_is_off(debug, sync):
    state = debug.read_state(FakeDebugCoder(sync, allow_users=False))
    assert (state.on, state.allow_users, state.toggle_allowed, state.forced) == (
        False,
        False,
        False,
        False,
    )


def test_state_when_only_the_users_toggle_is_off(debug, sync):
    state = debug.read_state(FakeDebugCoder(sync, user_stored=False))
    assert (state.on, state.allow_users, state.toggle_allowed) == (False, True, True)


def test_state_when_logging_is_on(debug, sync):
    assert debug.read_state(FakeDebugCoder(sync)).on is True


def test_state_when_the_deployment_forces_logging_on(debug, sync):
    state = debug.read_state(FakeDebugCoder(sync, allow_users=False, forced=True))
    assert (state.on, state.forced) == (True, True)


def test_state_for_a_token_that_cannot_read_the_admin_gate(debug, sync):
    state = debug.read_state(FakeDebugCoder(sync, is_admin=False, allow_users=False))
    assert state.allow_users is None
    assert state.on is False


def test_state_propagates_an_unexpected_error(debug, sync):
    fake = FakeDebugCoder(sync)
    fake.get_debug_logging = lambda: (_ for _ in ()).throw(api_error(sync, 500))
    with pytest.raises(sync.ApiError):
        debug.read_state(fake)


# ---- what to turn on ----------------------------------------------------------------


def test_the_hint_names_both_layers_when_the_admin_gate_is_off(debug, sync):
    text = "\n".join(debug.enable_hint(debug.read_state(FakeDebugCoder(sync, allow_users=False))))
    assert "Let users record chat debug logs" in text
    assert "Admin settings > AI > Coder Agents > Lifecycle" in text
    assert "Record debug logs for my chats" in text
    assert "Agents > Settings > General" in text
    assert "--enable" in text


def test_the_hint_names_only_the_users_toggle_when_the_gate_is_on(debug, sync):
    text = "\n".join(debug.enable_hint(debug.read_state(FakeDebugCoder(sync, user_stored=False))))
    assert "Record debug logs for my chats" in text
    assert "Let users record chat debug logs" not in text


# ---- turning it on and putting it back ----------------------------------------------


def toggles(debug, fake):
    return debug.Toggles(fake, lambda line: None)


def test_enable_sets_the_gate_and_the_toggle_and_restore_undoes_both(debug, sync):
    fake = FakeDebugCoder(sync, allow_users=False, user_stored=False)
    toggles_ = toggles(debug, fake)
    toggles_.enable(debug.read_state(fake))
    assert (fake.allow_users, fake.user_stored) == (True, True)
    assert fake.toggle_calls == [("admin", True), ("user", True)]
    fake.toggle_calls.clear()
    assert toggles_.restore() == []
    # The toggle goes back while the gate is still on (the API refuses it otherwise).
    assert fake.toggle_calls == [("user", False), ("admin", False)]
    assert (fake.allow_users, fake.user_stored) == (False, False)


def test_enable_keeps_a_stored_user_toggle_that_was_already_on(debug, sync):
    """With the gate off, the API hides the stored value, so it is read again once on."""
    fake = FakeDebugCoder(sync, allow_users=False, user_stored=True)
    toggles_ = toggles(debug, fake)
    toggles_.enable(debug.read_state(fake))
    assert fake.toggle_calls == [("admin", True)]
    fake.toggle_calls.clear()
    toggles_.restore()
    assert fake.toggle_calls == [("admin", False)]
    assert fake.user_stored is True


def test_enable_changes_only_the_users_toggle_when_the_gate_is_on(debug, sync):
    fake = FakeDebugCoder(sync, allow_users=True, user_stored=False)
    toggles_ = toggles(debug, fake)
    toggles_.enable(debug.read_state(fake))
    assert fake.toggle_calls == [("user", True)]
    fake.toggle_calls.clear()
    toggles_.restore()
    assert fake.toggle_calls == [("user", False)]
    assert fake.allow_users is True


def test_enable_changes_nothing_when_logging_is_already_on(debug, sync):
    fake = FakeDebugCoder(sync)
    toggles_ = toggles(debug, fake)
    toggles_.enable(debug.read_state(fake))
    toggles_.restore()
    assert fake.toggle_calls == []


def test_enable_changes_nothing_when_the_deployment_forces_it_on(debug, sync):
    fake = FakeDebugCoder(sync, forced=True, allow_users=False)
    toggles_ = toggles(debug, fake)
    toggles_.enable(debug.read_state(fake))
    toggles_.restore()
    assert fake.toggle_calls == []


def test_enable_says_so_when_the_token_cannot_set_the_admin_gate(debug, sync):
    fake = FakeDebugCoder(sync, allow_users=False, is_admin=False)
    with pytest.raises(sync.SyncError, match="ask an administrator"):
        toggles(debug, fake).enable(debug.read_state(fake))
    assert fake.toggle_calls == []


def test_enable_explains_a_403_on_the_users_toggle(debug, sync):
    fake = FakeDebugCoder(sync, allow_users=True, user_stored=False)
    state = debug.read_state(fake)
    fake.allow_users = False  # the admin turned the gate off in between
    with pytest.raises(sync.SyncError, match="has not enabled"):
        toggles(debug, fake).enable(state)


def test_enable_explains_a_409_on_the_users_toggle(debug, sync):
    fake = FakeDebugCoder(sync, allow_users=True, user_stored=False)
    state = debug.read_state(fake)
    fake.forced = True  # the deployment forced it on in between
    toggles(debug, fake).enable(state)  # nothing left to do, so no error


def test_a_failed_enable_still_restores_what_it_had_changed(debug, sync):
    fake = FakeDebugCoder(sync, allow_users=False, user_stored=False)
    toggles_ = toggles(debug, fake)
    state = debug.read_state(fake)
    original = fake.set_user_debug_logging
    fake.set_user_debug_logging = lambda enabled: (_ for _ in ()).throw(api_error(sync, 500, "PUT"))
    with pytest.raises(sync.ApiError):
        toggles_.enable(state)
    fake.set_user_debug_logging = original
    assert toggles_.restore() == []
    assert fake.allow_users is False


def test_restore_reports_what_it_could_not_put_back_and_keeps_going(debug, sync):
    fake = FakeDebugCoder(sync, allow_users=False, user_stored=False)
    toggles_ = toggles(debug, fake)
    toggles_.enable(debug.read_state(fake))
    original = fake.set_user_debug_logging
    fake.set_user_debug_logging = lambda enabled: (_ for _ in ()).throw(api_error(sync, 500, "PUT"))
    problems = toggles_.restore()
    fake.set_user_debug_logging = original
    assert len(problems) == 1
    assert "Record debug logs for my chats" in problems[0]
    assert fake.allow_users is False  # the gate was still put back


def test_restore_is_idempotent(debug, sync):
    fake = FakeDebugCoder(sync, allow_users=True, user_stored=False)
    toggles_ = toggles(debug, fake)
    toggles_.enable(debug.read_state(fake))
    toggles_.restore()
    fake.toggle_calls.clear()
    toggles_.restore()
    assert fake.toggle_calls == []


# ---- the whole run ------------------------------------------------------------------

MODELS = ["anthropic/claude-a", "nvidia/free-y", "openai/gpt-x"]
ENV = {"CODER_SESSION_TOKEN": TOKEN}


def make_fake(sync, **kwargs):
    fake = FakeDebugCoder(sync, **kwargs)
    seed_in_sync(fake, sync.build_desired(small_catalog()))
    return fake


def run_main(debug, fake, tmp_path, *args, env=ENV, factory=None):
    out, err = io.StringIO(), io.StringIO()
    code = debug.main(
        [*args, "--out", str(tmp_path / "cap"), "--poll", "0"],
        env,
        client_factory=factory or (lambda base, token: fake),
        out=out,
        err=err,
        sleep=lambda seconds: None,
    )
    return code, out.getvalue(), err.getvalue()


def captured_file(tmp_path, model_id):
    return tmp_path / "cap" / (model_id.replace("/", "_") + ".json")


def archived(fake):
    return sorted(cid for cid, chat in fake.chats.items() if chat["archived"])


def test_output_names_are_safe_file_names(debug):
    assert (
        debug.output_name("novita/qwen/qwen-2.5-72b-instruct")
        == "novita_qwen_qwen-2.5-72b-instruct.json"
    )
    assert debug.output_name("../../etc/passwd") == "etc_passwd.json"
    assert debug.output_name("a b:c@d") == "a_b_c_d.json"


def test_a_capture_creates_the_probe_chat_and_writes_the_file(debug, sync, tmp_path):
    fake = make_fake(sync)
    code, output, _ = run_main(debug, fake, tmp_path, "openai/gpt-x")
    assert code == 0
    (chat,) = fake.chats.values()
    config_id = next(m["id"] for m in fake.models.values() if m["model"] == "openai/gpt-x")
    assert chat["payload"] == {
        "organization_id": "org-1",
        "model_config_id": config_id,
        "client_type": "api",
        "labels": {"probe": "requesty-debug-capture"},
        "content": [{"type": "text", "text": sync.PROBE_PROMPT}],
    }
    saved = json.loads(captured_file(tmp_path, "openai/gpt-x").read_text())
    assert saved["model"] == "openai/gpt-x"
    assert saved["chat_id"] == chat["id"]
    assert saved["chat"]["status"] == "waiting"
    assert saved["runs"][0]["steps"][0]["attempts"][0]["response_status"] == 200
    assert "openai/gpt-x" in output
    assert str(captured_file(tmp_path, "openai/gpt-x")) in output


def test_the_output_file_is_private(debug, sync, tmp_path):
    run_main(debug, make_fake(sync), tmp_path, "openai/gpt-x")
    assert captured_file(tmp_path, "openai/gpt-x").stat().st_mode & 0o777 == 0o600


def test_a_failing_model_is_captured_with_its_error(debug, sync, tmp_path):
    fake = make_fake(sync)
    fake.chat_behaviors["openai/gpt-x"] = "error"
    code, output, _ = run_main(debug, fake, tmp_path, "openai/gpt-x")
    assert code == 0
    assert "chat status: error" in output
    assert "Google returned an unexpected error." in output
    assert "status_code=400" in output
    assert "-> HTTP 400" in output
    saved = json.loads(captured_file(tmp_path, "openai/gpt-x").read_text())
    assert saved["chat"]["last_error"]["status_code"] == 400
    assert archived(fake) == sorted(fake.chats)


def test_models_run_one_at_a_time_in_the_order_given(debug, sync, tmp_path):
    fake = make_fake(sync)
    code, output, _ = run_main(debug, fake, tmp_path, "openai/gpt-x", "anthropic/claude-a")
    assert code == 0
    assert [c["model"] for c in fake.chats.values()] == ["openai/gpt-x", "anthropic/claude-a"]
    assert output.index("== openai/gpt-x") < output.index("== anthropic/claude-a")


def test_model_ids_come_from_the_command_line_and_a_file(debug, sync, tmp_path):
    ids = tmp_path / "ids.txt"
    ids.write_text("nvidia/free-y\n\n  openai/gpt-x  \nopenai/gpt-x\n")
    fake = make_fake(sync)
    code, _, _ = run_main(debug, fake, tmp_path, "anthropic/claude-a", "--file", str(ids))
    assert code == 0
    assert [c["model"] for c in fake.chats.values()] == [
        "anthropic/claude-a",
        "nvidia/free-y",
        "openai/gpt-x",
    ]


def test_no_model_ids_is_a_usage_error(debug, sync, tmp_path):
    fake = make_fake(sync)
    code, _, err = run_main(debug, fake, tmp_path)
    assert code == 2
    assert "MODEL_ID" in err
    assert fake.chats == {}


def test_a_missing_file_is_a_usage_error(debug, sync, tmp_path):
    code, _, err = run_main(debug, make_fake(sync), tmp_path, "--file", str(tmp_path / "nope"))
    assert code == 2
    assert "nope" in err


def test_a_missing_session_token_is_a_setup_error(debug, sync, tmp_path):
    fake = make_fake(sync)
    code, _, err = run_main(debug, fake, tmp_path, "openai/gpt-x", env={})
    assert code == 2
    assert "CODER_SESSION_TOKEN" in err


def test_the_url_defaults_to_the_sync_scripts(debug, sync, tmp_path):
    seen = []

    def factory(base, token):
        seen.append((base, token))
        return make_fake(sync)

    run_main(debug, None, tmp_path, "openai/gpt-x", factory=factory)
    run_main(
        debug,
        None,
        tmp_path,
        "openai/gpt-x",
        env={**ENV, "CODER_URL": "https://coder.example"},
        factory=factory,
    )
    assert seen == [(sync.DEFAULT_CODER_URL, TOKEN), ("https://coder.example", TOKEN)]


def test_an_unusable_output_directory_is_a_setup_error(debug, sync, tmp_path):
    (tmp_path / "cap").write_text("a file, not a directory")
    fake = make_fake(sync)
    code, _, err = run_main(debug, fake, tmp_path, "openai/gpt-x")
    assert code == 2
    assert "cap" in err
    assert fake.chats == {}


# ---- unknown and disabled models ----------------------------------------------------


def test_a_disabled_model_is_reported_and_skipped(debug, sync, tmp_path):
    fake = make_fake(sync)
    next(m for m in fake.models.values() if m["model"] == "openai/gpt-x")["enabled"] = False
    code, output, _ = run_main(debug, fake, tmp_path, "openai/gpt-x", "nvidia/free-y")
    assert code == 0
    assert "openai/gpt-x" in output
    assert "disabled" in output
    assert "enable it in the model admin first" in output
    assert [c["model"] for c in fake.chats.values()] == ["nvidia/free-y"]
    assert not captured_file(tmp_path, "openai/gpt-x").exists()
    assert captured_file(tmp_path, "nvidia/free-y").exists()


def test_an_unknown_model_is_reported_and_skipped(debug, sync, tmp_path):
    fake = make_fake(sync)
    code, output, _ = run_main(debug, fake, tmp_path, "acme/nope", "nvidia/free-y")
    assert code == 0
    assert "acme/nope" in output
    assert "unknown" in output
    assert "enable it in the model admin first" in output
    assert [c["model"] for c in fake.chats.values()] == ["nvidia/free-y"]


def test_nothing_to_capture_is_an_error_and_changes_nothing(debug, sync, tmp_path):
    fake = make_fake(sync, allow_users=False, user_stored=False)
    code, output, err = run_main(debug, fake, tmp_path, "acme/nope", "--enable")
    assert code == 2
    assert "acme/nope" in output
    assert "nothing to capture" in err
    assert fake.toggle_calls == []
    assert fake.chats == {}


# ---- the preflight ------------------------------------------------------------------


def test_without_enable_it_refuses_when_the_admin_gate_is_off(debug, sync, tmp_path):
    fake = make_fake(sync, allow_users=False, user_stored=False)
    code, _, err = run_main(debug, fake, tmp_path, "openai/gpt-x")
    assert code == 2
    assert "Let users record chat debug logs" in err
    assert "Record debug logs for my chats" in err
    assert "--enable" in err
    assert fake.toggle_calls == []
    assert fake.chats == {}
    assert not (tmp_path / "cap").exists() or not list((tmp_path / "cap").iterdir())


def test_without_enable_it_names_only_the_users_toggle_when_the_gate_is_on(debug, sync, tmp_path):
    fake = make_fake(sync, allow_users=True, user_stored=False)
    code, _, err = run_main(debug, fake, tmp_path, "openai/gpt-x")
    assert code == 2
    assert "Record debug logs for my chats" in err
    assert "Let users record chat debug logs" not in err
    assert fake.toggle_calls == []
    assert fake.chats == {}


def test_without_enable_it_never_changes_a_setting_that_is_already_on(debug, sync, tmp_path):
    fake = make_fake(sync)
    code, _, _ = run_main(debug, fake, tmp_path, "openai/gpt-x")
    assert code == 0
    assert fake.toggle_calls == []


def test_enable_turns_logging_on_for_the_chats_and_puts_the_settings_back(debug, sync, tmp_path):
    fake = make_fake(sync, allow_users=False, user_stored=False)
    code, output, _ = run_main(debug, fake, tmp_path, "openai/gpt-x", "nvidia/free-y", "--enable")
    assert code == 0
    assert all(chat["debug_on"] for chat in fake.chats.values())
    assert (fake.allow_users, fake.user_stored) == (False, False)
    assert fake.toggle_calls == [
        ("admin", True),
        ("user", True),
        ("user", False),
        ("admin", False),
    ]
    assert "put back" in output


def test_enable_restores_the_settings_after_a_failing_model(debug, sync, tmp_path):
    fake = make_fake(sync, allow_users=True, user_stored=False)
    fake.chat_behaviors["openai/gpt-x"] = "error"
    code, _, _ = run_main(debug, fake, tmp_path, "openai/gpt-x", "--enable")
    assert code == 0
    assert (fake.allow_users, fake.user_stored) == (True, False)
    assert fake.toggle_calls == [("user", True), ("user", False)]


def test_enable_restores_the_settings_when_a_model_raises(debug, sync, tmp_path):
    fake = make_fake(sync, allow_users=False, user_stored=False)
    fake.run_error = RuntimeError("boom")
    code, output, err = run_main(debug, fake, tmp_path, "openai/gpt-x", "nvidia/free-y", "--enable")
    assert code == 0
    assert (fake.allow_users, fake.user_stored) == (False, False)
    assert "boom" in output + err
    assert len(fake.chats) == 2  # the second model still ran
    assert archived(fake) == sorted(fake.chats)


def test_enable_restores_the_settings_on_ctrl_c(debug, sync, tmp_path):
    fake = make_fake(sync, allow_users=False, user_stored=False)
    fake.get_chat_error = KeyboardInterrupt()
    code, output, err = run_main(debug, fake, tmp_path, "openai/gpt-x", "--enable")
    assert code == 130
    assert "nterrupted" in output + err
    assert (fake.allow_users, fake.user_stored) == (False, False)
    assert archived(fake) == sorted(fake.chats)
    assert len(fake.chats) == 1


def test_enable_restores_the_settings_when_creating_a_chat_raises(debug, sync, tmp_path):
    fake = make_fake(sync, allow_users=True, user_stored=False)
    original = fake.create_chat

    def boom(payload):
        original(payload)
        raise RuntimeError("chat exploded")

    fake.create_chat = boom
    code, output, err = run_main(debug, fake, tmp_path, "openai/gpt-x", "--enable")
    assert code == 0  # the one model failed; the run finished
    assert "chat exploded" in output + err
    assert (fake.allow_users, fake.user_stored) == (True, False)


def test_keep_enabled_leaves_the_settings_on_and_says_how_to_undo_it(debug, sync, tmp_path):
    fake = make_fake(sync, allow_users=False, user_stored=False)
    code, output, _ = run_main(debug, fake, tmp_path, "openai/gpt-x", "--enable", "--keep-enabled")
    assert code == 0
    assert (fake.allow_users, fake.user_stored) == (True, True)
    assert fake.toggle_calls == [("admin", True), ("user", True)]
    assert "left on" in output
    assert "PUT /api/v2/chats/config/user-debug-logging" in output


def test_a_setting_that_cannot_be_put_back_is_a_loud_warning(debug, sync, tmp_path):
    fake = make_fake(sync, allow_users=True, user_stored=False)
    original = fake.set_user_debug_logging
    calls = []

    def flaky(enabled):
        calls.append(enabled)
        if not enabled:
            raise api_error(sync, 500, "PUT")
        original(enabled)

    fake.set_user_debug_logging = flaky
    code, _, err = run_main(debug, fake, tmp_path, "openai/gpt-x", "--enable")
    assert code == 0
    assert "WARNING" in err
    assert "Record debug logs for my chats" in err
    assert calls == [True, False]


def test_enable_with_a_token_that_cannot_set_the_gate_is_a_setup_error(debug, sync, tmp_path):
    fake = make_fake(sync, allow_users=False, user_stored=False, is_admin=False)
    code, _, err = run_main(debug, fake, tmp_path, "openai/gpt-x", "--enable")
    assert code == 2
    assert "ask an administrator" in err
    assert fake.chats == {}


# ---- the chat is archived on every path ---------------------------------------------


def test_the_chat_is_archived_after_a_good_run(debug, sync, tmp_path):
    fake = make_fake(sync)
    run_main(debug, fake, tmp_path, *MODELS)
    assert len(fake.chats) == 3
    assert archived(fake) == sorted(fake.chats)


def test_the_chat_is_archived_when_it_never_settles(debug, sync, tmp_path):
    fake = make_fake(sync)
    fake.chat_behaviors["openai/gpt-x"] = "never"
    code, output, _ = run_main(debug, fake, tmp_path, "openai/gpt-x", "--timeout", "0")
    assert code == 0
    assert "had not settled" in output
    assert "chat status: running" in output
    assert archived(fake) == sorted(fake.chats)
    assert captured_file(tmp_path, "openai/gpt-x").exists()


def test_the_chat_is_archived_when_reading_it_fails(debug, sync, tmp_path):
    fake = make_fake(sync)
    fake.get_chat_error = api_error(sync, 500)
    code, output, _ = run_main(debug, fake, tmp_path, "openai/gpt-x")
    assert code == 0
    assert "500" in output
    assert archived(fake) == sorted(fake.chats)


def test_the_chat_is_archived_when_the_debug_runs_cannot_be_read(debug, sync, tmp_path):
    fake = make_fake(sync)
    fake.run_error = api_error(sync, 404)
    code, output, _ = run_main(debug, fake, tmp_path, "openai/gpt-x")
    assert code == 0
    assert "could not read the debug runs" in output
    assert archived(fake) == sorted(fake.chats)
    saved = json.loads(captured_file(tmp_path, "openai/gpt-x").read_text())
    assert saved["runs"] == []
    assert any("404" in note for note in saved["notes"])


def test_an_archive_failure_changes_nothing(debug, sync, tmp_path):
    fake = make_fake(sync)
    fake.archive_chat = lambda chat_id: (_ for _ in ()).throw(api_error(sync, 500, "PATCH"))
    code, _, _ = run_main(debug, fake, tmp_path, "openai/gpt-x")
    assert code == 0


def test_a_chat_that_cannot_be_created_is_reported(debug, sync, tmp_path):
    fake = make_fake(sync)
    fake.create_chat = lambda payload: (_ for _ in ()).throw(api_error(sync, 500, "POST"))
    code, output, _ = run_main(debug, fake, tmp_path, "openai/gpt-x", "nvidia/free-y")
    assert code == 0
    assert "could not create the chat" in output
    assert output.count("could not create the chat") == 2  # and it went on to the next model


def test_no_debug_runs_is_reported_after_a_few_re_reads(debug, sync, tmp_path):
    fake = make_fake(sync)
    fake.record_runs = False
    code, output, _ = run_main(debug, fake, tmp_path, "openai/gpt-x")
    assert code == 0
    assert "no debug runs" in output
    assert fake.list_calls == 1 + debug.RUN_RETRIES
    assert archived(fake) == sorted(fake.chats)


# ---- the token ----------------------------------------------------------------------


def test_the_session_token_is_never_printed_or_written(debug, sync, tmp_path):
    fake = make_fake(sync)
    fake.response_text = f"echoed the token {TOKEN} back"
    fake.chat_behaviors["openai/gpt-x"] = "error"
    code, output, err = run_main(debug, fake, tmp_path, "openai/gpt-x", "nvidia/free-y")
    assert code == 0
    assert "echoed the token" in output
    for text in (output, err):
        assert TOKEN not in text
    for path in (tmp_path / "cap").iterdir():
        assert TOKEN not in path.read_text()
    assert "<token>" in captured_file(tmp_path, "openai/gpt-x").read_text()


def test_the_file_keeps_the_raw_bodies_and_adds_decoded_copies(debug, sync, tmp_path):
    fake = make_fake(sync)
    run_main(debug, fake, tmp_path, "openai/gpt-x")
    saved = json.loads(captured_file(tmp_path, "openai/gpt-x").read_text())
    attempt_ = saved["runs"][0]["steps"][0]["attempts"][0]
    assert base64.b64decode(attempt_["request_body"]).startswith(b"{")
    assert attempt_["request_body_decoded"]["model"] == "openai/gpt-x"
    assert attempt_["request_body_decoded"]["messages"][0]["role"] == "system"
    assert attempt_["response_body_decoded"] == json.loads(RESPONSE_TEXT)


def test_the_token_is_scrubbed_from_base64_bodies_too(debug):
    body = b64(f"before {TOKEN} after".encode())
    cleaned = debug.scrub_tree({"a": [{"response_body": body}], "b": f"x{TOKEN}"}, TOKEN)
    assert TOKEN not in json.dumps(cleaned)
    assert base64.b64decode(cleaned["a"][0]["response_body"]).decode() == "before <token> after"
    assert cleaned["b"] == "x<token>"


def test_a_whole_run_over_http_against_a_stub_coder(debug, sync, stub, tmp_path):
    """The real client, the v2.37.0 route layout (runs only under /api/experimental)."""
    r = stub.responses
    r[("GET", "/api/v2/organizations")] = (200, [{"id": "org-1", "is_default": True}])
    r[("GET", "/api/v2/ai/providers")] = (
        200,
        [{"id": "p1", "name": "openai-via-requesty", "type": "openai"}],
    )
    r[("GET", "/api/v2/organizations/org-1/chats/models")] = (
        200,
        {
            "models": [
                {"id": "m1", "ai_provider_id": "p1", "model": "openai/gpt-x", "enabled": True}
            ],
            "providers": [],
            "unsupported_providers": [],
        },
    )
    r[("GET", "/api/experimental/ai/model-prices")] = (200, [])
    r[("GET", "/api/v2/chats/config/debug-logging")] = (
        200,
        {"allow_users": False, "forced_by_deployment": False},
    )
    r[("PUT", "/api/v2/chats/config/debug-logging")] = (204, None)
    r[("GET", "/api/v2/chats/config/user-debug-logging")] = (
        200,
        {
            "debug_logging_enabled": False,
            "user_toggle_allowed": False,
            "forced_by_deployment": False,
        },
    )
    r[("PUT", "/api/v2/chats/config/user-debug-logging")] = (204, None)
    r[("POST", "/api/v2/chats")] = (201, {"id": "chat-9", "status": "pending"})
    r[("GET", "/api/v2/chats/chat-9")] = (200, {"id": "chat-9", "status": "waiting"})
    r[("PATCH", "/api/v2/chats/chat-9")] = (200, {})
    r[("GET", "/api/experimental/chats/chat-9/debug/runs")] = (
        200,
        [{"id": "run-9", "status": "completed"}],
    )
    r[("GET", "/api/experimental/chats/chat-9/debug/runs/run-9")] = (200, run_detail())

    out, err = io.StringIO(), io.StringIO()
    code = debug.main(
        ["openai/gpt-x", "--enable", "--out", str(tmp_path), "--poll", "0"],
        {"CODER_SESSION_TOKEN": TOKEN, "CODER_URL": f"http://127.0.0.1:{stub.server_port}"},
        out=out,
        err=err,
        sleep=lambda seconds: None,
    )
    assert code == 0, err.getvalue()
    puts = [(q["path"], json.loads(q["body"])) for q in stub.requests if q["method"] == "PUT"]
    assert puts == [
        ("/api/v2/chats/config/debug-logging", {"allow_users": True}),
        ("/api/v2/chats/config/user-debug-logging", {"debug_logging_enabled": True}),
        ("/api/v2/chats/config/user-debug-logging", {"debug_logging_enabled": False}),
        ("/api/v2/chats/config/debug-logging", {"allow_users": False}),
    ]
    assert json.loads(next(q for q in stub.requests if q["method"] == "PATCH")["body"]) == {
        "archived": True
    }
    assert (tmp_path / "openai_gpt-x.json").exists()
    assert "-> HTTP 400" in out.getvalue()
    assert all(q["headers"]["coder-session-token"] == TOKEN for q in stub.requests)
