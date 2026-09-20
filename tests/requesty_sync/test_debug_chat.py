"""requesty-debug-chat.py: capture the request Coder Agents sends for a chat."""

import base64
import importlib.util
import json
import pathlib
import sys

import pytest
from fakes import FakeCoder

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
        self.runs = {}  # chat id -> list of run details
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

    def list_debug_runs(self, chat_id):
        if self.run_error:
            raise self.run_error
        return [{"id": r["id"]} for r in self.runs.get(chat_id, [])]

    def get_debug_run(self, chat_id, run_id):
        return next(r for r in self.runs[chat_id] if r["id"] == run_id)


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
