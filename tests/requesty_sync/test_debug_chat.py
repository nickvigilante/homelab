"""requesty-debug-chat.py: capture the request Coder Agents sends for a chat."""

import base64
import importlib.util
import json
import pathlib
import sys

import pytest

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
