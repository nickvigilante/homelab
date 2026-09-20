#!/usr/bin/env python3
"""Capture the request Coder Agents really sends to an LLM provider for a chat.

`requesty-coder-sync.py verify` and `requesty-probe.py` say that a model fails, but not
why, because neither shows what Coder put on the wire. Coder v2.37.0 has chat debug
logging for this: while it is on, each chat turn is recorded with the HTTP request Coder
sent to the provider (the body, with secrets redacted) and the provider's response. This
script turns that on, sends one probe chat through each model, reads the recording back,
and prints a short digest you can paste into a bug report. The complete data goes to a
JSON file per model.

Debug logging has three layers (docs: ai-coder/agents/platform-controls/chat-debug-logging):
  1. CODER_CHAT_DEBUG_LOGGING_ENABLED on the server forces it on (nothing to change).
  2. The admin gate, "Let users record chat debug logs":
       GET/PUT /api/v2/chats/config/debug-logging          {"allow_users": bool}
  3. Your own toggle, "Record debug logs for my chats":
       GET/PUT /api/v2/chats/config/user-debug-logging     {"debug_logging_enabled": bool}
Without --enable the script only reads them, and exits 2 if logging is off, naming what
to turn on. With --enable it sets the admin gate (owner or admin token) and your own
toggle, and puts both back when it finishes, on an error, or on Ctrl-C (--keep-enabled
leaves them on). It changes nothing else.

Usage:
  export CODER_SESSION_TOKEN=...   (CODER_URL defaults to https://coder.vigihome.net)
  scripts/requesty-debug-chat.py MODEL_ID [MODEL_ID ...] [--file ids.txt] [--out DIR]
                                 [--timeout 200] [--poll 2] [--enable] [--keep-enabled]

MODEL_ID is a model as registered in Coder by the sync (the Requesty ID, for example
novita/qwen/qwen-2.5-72b-instruct). It must be enabled. Each model gets one archived chat,
one at a time, so the cost is a few cents at most. No Requesty key is needed.

The JSON in DIR (default ./chat-debug) can contain prompt text, tool output, and the
provider's replies, so treat it like conversation history. The digest never prints header
values, and shows only the length and the first 40 characters of each message. The
session token is never printed or written.

Exit codes: 0 finished, 2 usage or setup error, 130 interrupted.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import importlib.util
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = "chat-debug"
LABELS = {"probe": "requesty-debug-capture"}
PREVIEW_CHARS = 40  # of each message; the debug data can contain prompt text
SCALAR_CHARS = 80  # of a string value shown in the request summary
BODY_CHARS = 400  # of the response body
ERROR_CHARS = 400
MAX_NAMES = 40  # tool names listed
MAX_INLINE_KEYS = 8  # an object with more keys is shown as a list of key names
SETTLED = {"waiting", "error", "requires_action", "interrupting"}
RUN_RETRIES = 3  # re-reads of an empty run list, in case the rows lag the chat status
EXIT_OK, EXIT_ERROR, EXIT_INTERRUPTED = 0, 2, 130


def load_sync():
    """The sync script, which holds CoderClient. Reuses it when already loaded."""
    existing = sys.modules.get("requesty_coder_sync")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "requesty_coder_sync", HERE / "requesty-coder-sync.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["requesty_coder_sync"] = module
    spec.loader.exec_module(module)
    return module


sync = load_sync()


# ---- reading recorded bodies -----------------------------------------------------------


def shorten(text: Any, limit: int) -> str:
    """Whitespace collapsed to single spaces and cut to `limit` characters."""
    return " ".join(str(text).split())[:limit]


def cut(text: Any, limit: int) -> str:
    """Like shorten, but ends in "..." when it had to cut."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "..."


def body_text(value: Any) -> str:
    """The text of a recorded body. Coder marshals the bytes as base64 in a JSON string."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    if not text.lstrip().startswith(("{", "[")):
        with contextlib.suppress(ValueError, binascii.Error):
            return base64.b64decode(text, validate=True).decode("utf-8")
    return text


def decode_body(value: Any) -> Any:
    """A recorded body as JSON when it is JSON (a dict or a list), else as text."""
    if isinstance(value, (dict, list)):
        return value
    text = body_text(value)
    try:
        return json.loads(text)
    except ValueError:
        return text


# ---- summarizing a request -------------------------------------------------------------


def scalar_text(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(cut(value, SCALAR_CHARS), ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list))


def summarize_value(value: Any, depth: int = 0) -> str:
    """A scalar's value, or a short description of a container. Never a whole nested value."""
    if isinstance(value, dict):
        if not value:
            return "{}"
        if depth == 0 and len(value) <= MAX_INLINE_KEYS:
            inner = ", ".join(f"{k}: {summarize_value(v, depth + 1)}" for k, v in value.items())
            return "{" + inner + "}"
        return "<object with keys " + ", ".join(str(k) for k in value) + ">"
    if isinstance(value, list):
        if not value:
            return "<list of 0>"
        if all(isinstance(item, dict) for item in value):
            if all("role" in item for item in value):
                return f"<list of {len(value)}: " + ", ".join(message_label(m) for m in value) + ">"
            keys = sorted({str(k) for item in value for k in item})
            return f"<list of {len(value)}: keys " + ", ".join(keys) + ">"
        shown = ", ".join(
            scalar_text(item) if is_scalar(item) else type(item).__name__ for item in value[:5]
        )
        return f"<list of {len(value)}: {shown}{', ...' if len(value) > 5 else ''}>"
    return scalar_text(value)


def content_of(message: dict[str, Any]) -> tuple[int, str]:
    """(length, text) of a message's content. Handles a string, OpenAI content parts, and
    Coder's normalized parts (which can carry only a text_length)."""
    content = message.get("content", message.get("parts"))
    if isinstance(content, str):
        return len(content), content
    if isinstance(content, list):
        length, first = 0, ""
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            size = part.get("text_length")
            if not isinstance(size, int):
                size = len(text) if isinstance(text, str) else 0
            length += size
            if not first and isinstance(text, str):
                first = text
        return length, first
    return 0, ""


def message_label(message: dict[str, Any]) -> str:
    return f"{message.get('role', '?')}({content_of(message)[0]})"


def summarize_messages(messages: list[Any]) -> list[str]:
    rows = [m for m in messages if isinstance(m, dict)]
    lines = [f"messages: list of {len(messages)}: " + ", ".join(message_label(m) for m in rows)]
    for index, message in enumerate(rows):
        length, text = content_of(message)
        line = f"  [{index}] {message.get('role', '?')} {length} chars"
        if text:
            preview = shorten(text, PREVIEW_CHARS)
            line += f": {json.dumps(preview, ensure_ascii=False)}" + (
                "..." if len(" ".join(text.split())) > PREVIEW_CHARS else ""
            )
        extras = [k for k in message if k not in ("role", "content", "parts")]
        if extras:
            line += f" (also: {', '.join(extras)})"
        lines.append(line)
    return lines


def find_values(node: Any, key: str) -> list[Any]:
    """Every value stored under `key`, at any depth."""
    found: list[Any] = []
    if isinstance(node, dict):
        for name, child in node.items():
            if name == key:
                found.append(child)
            found.extend(find_values(child, key))
    elif isinstance(node, list):
        for child in node:
            found.extend(find_values(child, key))
    return found


def tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
        if tool.get("name"):
            return str(tool["name"])
    return "?"


def summarize_tools(tools: list[Any]) -> str:
    names = [tool_name(t) for t in tools]
    shown = ", ".join(names[:MAX_NAMES]) + (", ..." if len(names) > MAX_NAMES else "")
    parts = [f"tools: {len(tools)}: {shown}" if tools else "tools: 0"]
    for keyword in ("strict", "additionalProperties"):
        hits = []
        for name, tool in zip(names, tools, strict=True):
            values = find_values(tool, keyword)
            if values:
                seen = "|".join(dict.fromkeys(json.dumps(v) for v in values))
                hits.append(f"{name}={seen}")
        parts.append(f"{keyword}: " + (", ".join(hits) if hits else "none"))
    return "; ".join(parts)


def summarize_request(body: Any) -> list[str]:
    """Every top-level key of a request body, with scalar values but no whole messages."""
    if not isinstance(body, dict):
        return [f"({type(body).__name__}) {cut(body_text(body), SCALAR_CHARS)}"]
    lines = []
    for key, value in body.items():
        if key == "messages" and isinstance(value, list):
            lines.extend(summarize_messages(value))
        elif key == "tools" and isinstance(value, list):
            lines.append(summarize_tools(value))
        else:
            lines.append(f"{key}: {summarize_value(value)}")
    return lines


# ---- the digest ------------------------------------------------------------------------


def key_value(pairs: list[tuple[str, Any]]) -> str:
    return " ".join(
        f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in pairs if v not in (None, "")
    )


def describe_last_error(error: Any) -> str:
    if not isinstance(error, dict):
        return "none"
    head = key_value(
        [
            ("message", cut(error.get("message"), ERROR_CHARS)),
            ("status_code", error.get("status_code")),
            ("retryable", bool(error.get("retryable"))),
            ("kind", error.get("kind")),
            ("provider", error.get("provider")),
        ]
    )
    detail = error.get("detail")
    return head + (
        f" detail={json.dumps(cut(detail, ERROR_CHARS), ensure_ascii=False)}" if detail else ""
    )


def digest_attempt(attempt: dict[str, Any], pad: str) -> list[str]:
    status = attempt.get("response_status")
    outcome = f"HTTP {status}" if status else "no response"
    line = (
        f"{pad}attempt {attempt.get('number', '?')}: {attempt.get('method', '?')} "
        f"{attempt.get('path') or attempt.get('url') or '?'} -> {outcome} "
        f"({attempt.get('status', '?')}, {attempt.get('duration_ms', '?')} ms"
    )
    if attempt.get("retry_classification"):
        line += f", retry: {attempt['retry_classification']}"
    lines = [line + ")"]
    if attempt.get("error"):
        lines.append(f"{pad}  transport error: {shorten(attempt['error'], ERROR_CHARS)}")
    for label, key in (("request", "request_headers"), ("response", "response_headers")):
        names = attempt.get(key)
        if isinstance(names, dict) and names:
            lines.append(f"{pad}  {label} headers (names only): {', '.join(names)}")
    request = decode_body(attempt.get("request_body"))
    if request in (None, ""):
        lines.append(f"{pad}  request body: (not recorded)")
    elif isinstance(request, dict):
        lines.append(f"{pad}  request body:")
        lines.extend(f"{pad}    {row}" for row in summarize_request(request))
    else:
        lines.append(f"{pad}  request body: {cut(request, SCALAR_CHARS)}")
    response = " ".join(body_text(attempt.get("response_body")).split())
    if response:
        lines.append(f"{pad}  response body: {response[:BODY_CHARS]}")
        if len(response) > BODY_CHARS:
            lines.append(f"{pad}  ... {len(response) - BODY_CHARS} more characters not shown")
    else:
        lines.append(f"{pad}  response body: (empty)")
    return lines


def digest_step(step: dict[str, Any], pad: str) -> list[str]:
    number, operation = step.get("step_number", "?"), step.get("operation", "?")
    lines = [f"{pad}step {number}: {operation}, status {step.get('status', '?')}"]
    attempts = [a for a in step.get("attempts") or [] if isinstance(a, dict)]
    for attempt in attempts:
        lines.extend(digest_attempt(attempt, pad + "  "))
    if not any(isinstance(decode_body(a.get("request_body")), dict) for a in attempts):
        normalized = step.get("normalized_request")
        if isinstance(normalized, dict) and normalized:
            lines.append(f"{pad}  normalized request (Coder's own view of what it sent):")
            lines.extend(f"{pad}    {row}" for row in summarize_request(normalized))
    response = step.get("normalized_response")
    if isinstance(response, dict):
        parts = [f"finish_reason={response.get('finish_reason')}"]
        content = response.get("content")
        if isinstance(content, list):
            labels = [
                f"{c.get('type', '?')}({content_of(c)[0]})" for c in content if isinstance(c, dict)
            ]
            parts.append("content: " + (", ".join(labels) or "none"))
        usage = step.get("usage") or response.get("usage")
        if isinstance(usage, dict):
            parts.append(
                "usage: " + ", ".join(f"{k}={v}" for k, v in usage.items() if is_scalar(v))
            )
        lines.append(f"{pad}  response: " + ", ".join(parts))
    error = step.get("error")
    if isinstance(error, dict) and error:
        extra = [f"{k} {error[k]}" for k in ("type", "provider_status") if error.get(k)]
        message = shorten(error.get("message") or json.dumps(error), ERROR_CHARS)
        lines.append(f"{pad}  error: {message}" + (f" ({', '.join(extra)})" if extra else ""))
    return lines


def digest_capture(result: dict[str, Any]) -> list[str]:
    """A compact digest of one model's capture, safe to paste into a bug report."""
    chat = result.get("chat") or {}
    lines = [f"== {result.get('model')} (chat {result.get('chat_id') or 'not created'})"]
    lines.append(f"  chat status: {chat.get('status', 'unknown')}")
    lines.append(f"  last_error: {describe_last_error(chat.get('last_error'))}")
    lines.extend(f"  note: {note}" for note in result.get("notes") or [])
    runs = sorted(result.get("runs") or [], key=lambda r: str(r.get("started_at", "")))
    for number, run in enumerate(runs, 1):
        lines.append(
            f"  run {number} of {len(runs)}: {run.get('kind', '?')}, "
            f"status {run.get('status', '?')}, provider {run.get('provider') or '?'}, "
            f"model {run.get('model') or '?'}"
        )
        for step in run.get("steps") or []:
            lines.extend(digest_step(step, "    "))
    return lines


# ---- the Coder client ------------------------------------------------------------------

ADMIN_GATE_PATH = "/api/v2/chats/config/debug-logging"
USER_TOGGLE_PATH = "/api/v2/chats/config/user-debug-logging"
ADMIN_GATE_NAME = "Let users record chat debug logs"
USER_TOGGLE_NAME = "Record debug logs for my chats"
# The docs give /api/v2, but Coder v2.37.0 mounts the run routes only under /api/experimental.
RUN_PREFIXES = ("/api/v2", "/api/experimental")


class DebugClient(sync.CoderClient):
    """CoderClient plus the chat debug logging endpoints."""

    _run_prefix: str | None = None

    def get_debug_logging(self) -> dict[str, Any]:
        """The admin gate: {"allow_users": bool, "forced_by_deployment": bool}."""
        return self._request_object("GET", ADMIN_GATE_PATH)

    def set_debug_logging_allow_users(self, allow: bool) -> None:
        self.request("PUT", ADMIN_GATE_PATH, {"allow_users": allow})

    def get_user_debug_logging(self) -> dict[str, Any]:
        """The caller's own state: debug_logging_enabled (effective), user_toggle_allowed,
        and forced_by_deployment."""
        return self._request_object("GET", USER_TOGGLE_PATH)

    def set_user_debug_logging(self, enabled: bool) -> None:
        self.request("PUT", USER_TOGGLE_PATH, {"debug_logging_enabled": enabled})

    def _get_run_path(self, suffix: str) -> Any:
        prefixes = (self._run_prefix,) if self._run_prefix else RUN_PREFIXES
        for index, prefix in enumerate(prefixes):
            try:
                result = self.request("GET", f"{prefix}/chats/{suffix}")
            except sync.ApiError as err:
                if err.status == 404 and index < len(prefixes) - 1:
                    continue
                raise
            self._run_prefix = prefix
            return result
        raise AssertionError("unreachable")

    def list_debug_runs(self, chat_id: str) -> list[dict[str, Any]]:
        """The chat's newest runs (up to 100), as summaries."""
        result = self._get_run_path(f"{chat_id}/debug/runs")
        if result is None:
            return []
        if not isinstance(result, list):
            raise sync.ApiError(
                "GET", "debug runs", 0, f"expected a JSON list, got {result!r}"[:200]
            )
        return result

    def get_debug_run(self, chat_id: str, run_id: str) -> dict[str, Any]:
        """One run with all its steps, attempts, and bodies."""
        result = self._get_run_path(f"{chat_id}/debug/runs/{run_id}")
        if not isinstance(result, dict):
            raise sync.ApiError(
                "GET", "debug run", 0, f"expected a JSON object, got {result!r}"[:200]
            )
        return result


# ---- debug logging state ---------------------------------------------------------------


@dataclass
class State:
    allow_users: bool | None  # the admin gate; None when the token cannot read it
    toggle_allowed: bool  # the gate is on and the deployment does not force logging
    user_enabled: bool  # effective for the caller's chats (includes the deployment override)
    forced: bool  # CODER_CHAT_DEBUG_LOGGING_ENABLED

    @property
    def on(self) -> bool:
        return self.user_enabled


def read_state(client: Any) -> State:
    """What is on now. A token that cannot read the admin gate (Coder answers 403 or 404 to a
    non-admin) still shows the caller's effective state from its own endpoint."""
    allow_users: bool | None = None
    forced = False
    try:
        admin = client.get_debug_logging()
        allow_users = bool(admin.get("allow_users"))
        forced = bool(admin.get("forced_by_deployment"))
    except sync.ApiError as err:
        if err.status not in (403, 404):
            raise
    user = client.get_user_debug_logging()
    forced = forced or bool(user.get("forced_by_deployment"))
    return State(
        allow_users=allow_users,
        toggle_allowed=bool(user.get("user_toggle_allowed")),
        user_enabled=bool(user.get("debug_logging_enabled")),
        forced=forced,
    )


def enable_hint(state: State) -> list[str]:
    """What to turn on, for a person to do by hand."""
    lines = ["Chat debug logging is off, so Coder would record nothing to capture. Turn on:"]
    step = 1
    if not state.toggle_allowed:
        lines.append(
            f"  {step}. The admin gate, as an admin: "
            f'Admin settings > AI > Coder Agents > Lifecycle > "{ADMIN_GATE_NAME}"'
        )
        lines.append(f'     (PUT {ADMIN_GATE_PATH} {{"allow_users": true}})')
        step += 1
    lines.append(f'  {step}. Your own toggle: Agents > Settings > General > "{USER_TOGGLE_NAME}"')
    lines.append(f'     (PUT {USER_TOGGLE_PATH} {{"debug_logging_enabled": true}})')
    lines.append(
        "Then run this again. Or pass --enable, and this script sets them and puts them back "
        "when it finishes."
    )
    return lines


class Toggles:
    """Turns debug logging on for the caller, and remembers how to put every change back."""

    def __init__(self, client: Any, log: Callable[[str], None]) -> None:
        self.client = client
        self.log = log
        self.undo: list[tuple[str, Callable[[], None]]] = []

    def enable(self, state: State) -> None:
        if state.on:
            self.log("debug logging is already on; leaving the settings as they are")
            return
        if not state.toggle_allowed:
            self._turn_on_admin_gate()
            state = read_state(self.client)  # with the gate on, the stored toggle shows
            if state.on:
                return
        try:
            self.client.set_user_debug_logging(True)
        except sync.ApiError as err:
            if err.status == 409:  # the deployment forced logging on since we looked
                return
            if err.status == 403:
                raise sync.SyncError(
                    f'Coder refused the personal toggle ("{USER_TOGGLE_NAME}"): an administrator '
                    f'has not enabled "{ADMIN_GATE_NAME}"'
                ) from err
            raise
        self.undo.append(
            (
                f'your toggle "{USER_TOGGLE_NAME}" (PUT {USER_TOGGLE_PATH} '
                '{"debug_logging_enabled": false})',
                lambda: self.client.set_user_debug_logging(False),
            )
        )
        self.log(f'turned on your toggle "{USER_TOGGLE_NAME}"')

    def _turn_on_admin_gate(self) -> None:
        try:
            self.client.set_debug_logging_allow_users(True)
        except sync.ApiError as err:
            if err.status in (403, 404):
                raise sync.SyncError(
                    f'the admin gate "{ADMIN_GATE_NAME}" is off, and this token cannot turn it on '
                    "(it needs an owner or another role that can update the deployment "
                    "configuration): ask an administrator to turn it on, or use an admin's token"
                ) from err
            raise
        self.undo.append(
            (
                f'the admin gate "{ADMIN_GATE_NAME}" (PUT {ADMIN_GATE_PATH} '
                '{"allow_users": false})',
                lambda: self.client.set_debug_logging_allow_users(False),
            )
        )
        self.log(f'turned on the admin gate "{ADMIN_GATE_NAME}"')

    def restore(self) -> list[str]:
        """Puts back what enable() changed, newest first, and returns what it could not."""
        problems = []
        while self.undo:
            what, revert = self.undo.pop()
            try:
                revert()
            except Exception as err:  # keep going: one failure must not strand the others
                problems.append(f"could not put back {what}: {err}")
            else:
                self.log(f"put back {what.split(' (PUT')[0]}")
        return problems


# ---- capturing one model ---------------------------------------------------------------

CHAT_FIELDS = ("status", "last_error", "created_at", "updated_at", "model_config_id")


def output_name(model_id: str) -> str:
    """A file name for a model ID that cannot escape the output directory."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model_id).strip("._") + ".json"


def wait_for_chat(
    client: Any,
    chat_id: str,
    timeout: float,
    poll: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    notes: list[str],
) -> dict[str, Any]:
    """Polls until the chat leaves running (or pending) or the timeout passes."""
    deadline = clock() + timeout
    while True:
        try:
            chat = client.get_chat(chat_id)
        except sync.ApiError as err:
            notes.append(f"could not read the chat: {err}")
            return {}
        if chat.get("status") in SETTLED:
            return chat
        if clock() >= deadline:
            notes.append(
                f"the chat had not settled after {timeout:g}s (last status: "
                f"{chat.get('status')}); this is what Coder had recorded by then"
            )
            return chat
        sleep(poll)


def fetch_runs(
    client: Any,
    chat_id: str,
    settled: bool,
    poll: float,
    sleep: Callable[[float], None],
    notes: list[str],
) -> list[dict[str, Any]]:
    """The chat's debug runs with their steps. Coder can lag the chat status, so an empty list
    (or a run still in progress after the chat settled) is read again a few times."""
    runs: list[dict[str, Any]] = []
    for attempt in range(RUN_RETRIES + 1):
        try:
            summaries = client.list_debug_runs(chat_id)
            runs = [client.get_debug_run(chat_id, summary["id"]) for summary in summaries]
        except sync.ApiError as err:
            notes.append(f"could not read the debug runs: {err}")
            return []
        pending = not runs or (settled and any(r.get("status") == "in_progress" for r in runs))
        if not pending or attempt == RUN_RETRIES:
            break
        sleep(poll)
    if not runs:
        notes.append(
            "no debug runs were recorded for this chat: check that debug logging was on when "
            "the chat started (Coder decides at the start of each turn)"
        )
    return runs


def capture_model(
    client: Any,
    org_id: str,
    model_id: str,
    row: dict[str, Any],
    provider_name: str,
    timeout: float,
    poll: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Sends one probe chat through the model and reads back what Coder recorded for it.
    The chat is archived on every path (and an archive failure changes nothing)."""
    notes: list[str] = []
    result: dict[str, Any] = {
        "model": model_id,
        "provider": provider_name,
        "model_config_id": row["id"],
        "chat_id": None,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "chat": {},
        "runs": [],
        "notes": notes,
    }
    try:
        chat = client.create_chat(
            {
                "organization_id": org_id,
                "model_config_id": row["id"],
                "client_type": "api",
                "labels": dict(LABELS),
                "content": [{"type": "text", "text": sync.PROBE_PROMPT}],
            }
        )
    except sync.ApiError as err:
        notes.append(f"could not create the chat: {err}")
        return result
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if not chat_id:
        notes.append("could not create the chat: Coder returned no chat id")
        return result
    result["chat_id"] = chat_id
    try:
        final = wait_for_chat(client, chat_id, timeout, poll, sleep, clock, notes)
        result["chat"] = {k: final[k] for k in CHAT_FIELDS if k in final}
        result["runs"] = fetch_runs(
            client, chat_id, bool(final.get("status") in SETTLED), poll, sleep, notes
        )
    finally:
        with contextlib.suppress(Exception):  # best effort: never changes the outcome
            client.archive_chat(chat_id)
    return result


def scrub_tree(node: Any, secret: str) -> Any:
    """A copy of `node` without the secret, also inside base64 bodies (Coder stores bodies
    as base64, so a plain text replace would miss it there)."""
    if isinstance(node, dict):
        return {key: scrub_tree(value, secret) for key, value in node.items()}
    if isinstance(node, list):
        return [scrub_tree(value, secret) for value in node]
    if not isinstance(node, str) or not secret:
        return node
    if secret in node:
        return node.replace(secret, "<token>")
    with contextlib.suppress(ValueError, binascii.Error):
        decoded = base64.b64decode(node, validate=True).decode("utf-8")
        if secret in decoded:
            return base64.b64encode(decoded.replace(secret, "<token>").encode()).decode()
    return node


def add_decoded_bodies(result: dict[str, Any]) -> None:
    """Puts a readable copy of each recorded body next to the raw base64 one."""
    for run in result.get("runs") or []:
        for step in run.get("steps") or []:
            for attempt in step.get("attempts") or []:
                for key in ("request_body", "response_body"):
                    if attempt.get(key):
                        attempt[f"{key}_decoded"] = decode_body(attempt[key])


def write_capture(out_dir: Path, result: dict[str, Any], token: str) -> Path:
    """Writes the capture as JSON, readable only by you (it can hold prompt text). It is what
    Coder returned, plus a decoded copy of each request and response body."""
    path = out_dir / output_name(result["model"])
    clean = scrub_tree(result, token)
    add_decoded_bodies(clean)
    text = json.dumps(clean, indent=2, ensure_ascii=False) + "\n"
    if token:
        text = text.replace(token, "<token>")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)
    return path


# ---- the command line ------------------------------------------------------------------


class Scrubbed:
    """A stream that never lets a secret through, whatever the server echoes back."""

    def __init__(self, stream: TextIO, secret: str) -> None:
        self.stream = stream
        self.secret = secret

    def write(self, text: str) -> int:
        self.stream.write(text.replace(self.secret, "<token>") if self.secret else text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()


def resolve_models(
    live: Any, ids: list[str]
) -> tuple[list[tuple[str, dict[str, Any], str]], list[str]]:
    """The enabled managed models to capture, and a report line for each one that cannot be."""
    runnable: list[tuple[str, dict[str, Any], str]] = []
    skipped: list[str] = []
    for model_id in ids:
        chosen = sync.select_models_to_verify(live, model=model_id)
        if chosen:
            row, provider = chosen[0]
            runnable.append((model_id, row, provider))
        elif any(m.get("model") == model_id for m in live.models):
            skipped.append(f"{model_id}: disabled in Coder; enable it in the model admin first")
        else:
            skipped.append(
                f"{model_id}: unknown to Coder (no managed provider has this model); register it "
                "with `requesty-coder-sync.py apply` if it is in the Requesty catalog, then "
                "enable it in the model admin first"
            )
    return runnable, skipped


def model_ids(args: argparse.Namespace) -> list[str]:
    ids = list(args.models)
    if args.file:
        try:
            lines = Path(args.file).read_text().splitlines()
        except OSError as err:
            raise sync.SyncError(f"cannot read --file {args.file}: {err}") from err
        ids += [line.strip() for line in lines if line.strip()]
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise sync.SyncError("give at least one MODEL_ID (or --file)")
    return ids


def run(
    args: argparse.Namespace,
    env: Any,
    client_factory: Callable[[str, str], Any],
    out: TextIO,
    err: TextIO,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    token = env.get("CODER_SESSION_TOKEN")
    if not token:
        raise sync.SyncError("CODER_SESSION_TOKEN is not set")
    ids = model_ids(args)
    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise sync.SyncError(f"cannot use --out {out_dir}: {error}") from error

    client = client_factory(env.get("CODER_URL", sync.DEFAULT_CODER_URL), token)
    live = sync.load_live(client)
    runnable, skipped = resolve_models(live, ids)
    for line in skipped:
        out.write(f"skipped {line}\n")
    if not runnable:
        raise sync.SyncError("nothing to capture: none of the models can be used")

    state = read_state(client)
    if not state.on and not args.enable:
        err.write("\n".join(enable_hint(state)) + "\n")
        return EXIT_ERROR

    def log(line: str) -> None:
        out.write(line + "\n")
        out.flush()

    toggles = Toggles(client, log)
    captured = 0
    try:
        if args.enable:
            toggles.enable(state)
        for model_id, row, provider in runnable:
            log(f"capturing {model_id} (provider {provider}) ...")
            try:
                result = capture_model(
                    client,
                    live.org_id,
                    model_id,
                    row,
                    provider,
                    args.timeout,
                    args.poll,
                    sleep,
                    clock,
                )
                path = write_capture(out_dir, result, token)
            except Exception as error:  # one model must not stop the rest
                log(f"capture failed for {model_id}: {type(error).__name__}: {error}")
                continue
            captured += 1
            out.write("\n".join(digest_capture(result)) + "\n")
            log(f"saved: {path}\n")
    finally:
        if args.keep_enabled:
            for what, _ in toggles.undo:
                log(f"left on (--keep-enabled); to undo it: {what}")
        else:
            for problem in toggles.restore():
                err.write(f"WARNING: {problem}\n")
    log(f"captured {captured} of {len(runnable)} model(s) into {out_dir}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="requesty-debug-chat.py", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("models", nargs="*", metavar="MODEL_ID", help="a model to capture")
    parser.add_argument("--file", help="a file with one model ID per line")
    parser.add_argument(
        "--out", default=DEFAULT_OUT, help="directory for the JSON files (default ./chat-debug)"
    )
    parser.add_argument(
        "--timeout",
        type=sync.non_negative_float,
        default=200,
        help="seconds to wait for each chat to settle (default 200)",
    )
    parser.add_argument(
        "--poll",
        type=sync.non_negative_float,
        default=2,
        help="seconds between checks (default 2)",
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="turn debug logging on (the admin gate and your own toggle) and put it back after",
    )
    parser.add_argument(
        "--keep-enabled", action="store_true", help="with --enable, leave debug logging on"
    )
    return parser


def main(
    argv: list[str] | None = None,
    env: Any = None,
    *,
    client_factory: Callable[[str, str], Any] = DebugClient,
    out: TextIO | None = None,
    err: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    env = os.environ if env is None else env
    token = env.get("CODER_SESSION_TOKEN") or ""
    out = Scrubbed(sys.stdout if out is None else out, token)
    err = Scrubbed(sys.stderr if err is None else err, token)
    args = build_parser().parse_args(argv)
    try:
        return run(args, env, client_factory, out, err, sleep, clock)
    except KeyboardInterrupt:
        err.write("Interrupted; the chat was archived and the settings were put back.\n")
        return EXIT_INTERRUPTED
    except sync.SyncError as error:
        err.write(f"error: {error}\n")
    except Exception as error:
        err.write(f"error: unexpected {type(error).__name__}: {error}\n")
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
