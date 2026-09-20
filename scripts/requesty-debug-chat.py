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

import base64
import binascii
import contextlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

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
