#!/usr/bin/env python3
"""Probe Requesty directly for models that failed `requesty-coder-sync.py verify`.

`verify` sends a chat through Coder Agents, so a failure could come from
Coder's request, from the host behind Requesty, or from a slow reply. This
calls Requesty's OpenAI-compatible endpoint with no Coder in between, so it
tells those apart, and it tries the other hosts Requesty lists for the same
canonical model, so a model with a dead first-party route can move to a host
that works.

For each model it sends up to three requests (retrying once on a 429, a 5xx,
or a timeout):
  minimal  one user message
  agent    a system prompt, a user message, and one tool, no max_tokens
  capped   the agent request with max_tokens set to the registered limit
With --extra-shapes it adds:
  multi-system  two system messages before the user message (Coder may send
                more than one, and some host chat templates reject that)

Verdicts:
  WORKS_DIRECTLY   every request succeeded, so the Coder failure is specific
                   to how Coder sends the chat (or was transient): retry it
                   through `verify --model`, and if it fails again, disable it
  FAILS_DIRECTLY   Requesty or the host rejects it without Coder: disable it,
                   or switch to a working alternate host if one is listed
  PARTIAL          some requests failed: the message says which

Usage:
  export BW_SESSION=...   (bw unlock --raw; the key is read from the Bitwarden
                          item "Requesty", field "Main API key")
  or export REQUESTY_API_KEY=... to skip Bitwarden
  scripts/requesty-probe.py MODEL_ID [MODEL_ID ...] [--file ids.txt]
                            [--concurrency 4] [--timeout 120] [--out report.json]

Exit codes: 0 finished (whatever the verdicts), 2 usage or setup error.
The key is read from the environment and never printed.
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHAT_URL = "https://router.requesty.ai/v1/chat/completions"
MAX_ALTERNATES = 3
SYSTEM_PROMPT = "You are a coding agent. Use tools when they help. Be brief."
TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return the current time.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def load_sync():
    spec = importlib.util.spec_from_file_location(
        "requesty_coder_sync", HERE / "requesty-coder-sync.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["requesty_coder_sync"] = module
    spec.loader.exec_module(module)
    return module


def shorten(text, limit=200):
    return " ".join(str(text).split())[:limit]


def call(key, body, timeout):
    """One chat request. Returns (ok, http_status, seconds, message)."""
    request = urllib.request.Request(
        CHAT_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read().decode(errors="replace")
        try:
            detail = json.loads(raw)
            detail = (detail.get("error") or {}).get("message") or detail.get("message") or raw
        except ValueError:
            detail = raw
        return False, error.code, time.monotonic() - start, shorten(detail)
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        return False, None, time.monotonic() - start, f"no response: {shorten(error)}"
    except ValueError:
        return False, status, time.monotonic() - start, "the reply was not JSON"
    choice = (payload.get("choices") or [{}])[0].get("message") or {}
    if choice.get("content") or choice.get("tool_calls") or choice.get("reasoning_content"):
        return True, status, time.monotonic() - start, ""
    return False, status, time.monotonic() - start, "an empty reply"


def call_with_retry(key, body, timeout):
    result = call(key, body, timeout)
    ok, status, _, _ = result
    if not ok and (status is None or status == 429 or status >= 500):
        time.sleep(5)
        result = call(key, body, timeout)
    return result


def requests_for(model_id, max_output, extra=False):
    user = {"role": "user", "content": "Reply with the single word OK."}
    agent = {
        "model": model_id,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, user],
        "tools": [TOOL],
        "tool_choice": "auto",
    }
    shapes = {
        "minimal": {"model": model_id, "messages": [user], "max_tokens": 256},
        "agent": agent,
    }
    if max_output:
        shapes["capped"] = {**agent, "max_tokens": max_output}
    if extra:
        shapes["multi-system"] = {
            **agent,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "system", "content": "The workspace is a Linux container."},
                user,
            ],
        }
    return shapes


def probe_model(key, model_id, max_output, timeout, extra=False):
    results = {}
    for name, body in requests_for(model_id, max_output, extra).items():
        ok, status, seconds, message = call_with_retry(key, body, timeout)
        results[name] = {
            "ok": ok,
            "status": status,
            "seconds": round(seconds, 1),
            "message": message,
        }
    passed = [name for name, r in results.items() if r["ok"]]
    if len(passed) == len(results):
        verdict = "WORKS_DIRECTLY"
    elif not passed:
        verdict = "FAILS_DIRECTLY"
    else:
        verdict = "PARTIAL"
    return {"model": model_id, "verdict": verdict, "requests": results}


def summarize(result):
    parts = []
    for name, r in result["requests"].items():
        if r["ok"]:
            parts.append(f"{name} ok {r['seconds']}s")
        else:
            parts.append(f"{name} FAIL ({r['status']}) {r['message']}")
    return "; ".join(parts)


def alternates(sync, catalog, model_id, catalog_by_id):
    """Other eligible, non-retiring entries of the same canonical model."""
    entry = catalog_by_id.get(model_id)
    if entry is None:
        return []
    wanted = sync.canonical_of(entry).casefold()
    found = [
        e
        for e in catalog
        if e["id"] != model_id
        and sync.is_eligible(e)
        and not sync.is_retiring(e)
        and sync.canonical_of(e).casefold() == wanted
    ]
    found.sort(key=lambda e: (not sync.is_plain(e), e["input_price"] + e["output_price"], e["id"]))
    return found[:MAX_ALTERNATES]


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("models", nargs="*", help="Requesty model IDs to probe")
    parser.add_argument("--file", help="a file with one model ID per line")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--extra-shapes", action="store_true", help="also send the multi-system request"
    )
    parser.add_argument("--out", help="write the full report here as JSON")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    sync = load_sync()
    key = os.environ.get("REQUESTY_API_KEY", "")
    if not key:
        try:
            key = sync.bitwarden_field(sync.BITWARDEN_ITEM, sync.BITWARDEN_FIELD, os.environ)
        except sync.SyncError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    ids = list(args.models)
    if args.file:
        ids += [line.strip() for line in Path(args.file).read_text().splitlines() if line.strip()]
    ids = list(dict.fromkeys(ids))
    if not ids:
        print("error: give at least one model ID (or --file)", file=sys.stderr)
        return 2

    try:
        catalog = sync.fetch_catalog()
    except Exception as error:  # the catalog is public; a failure here is a setup problem
        print(f"error: could not fetch the Requesty catalog: {error}", file=sys.stderr)
        return 2
    by_id = {e["id"]: e for e in catalog}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        print(f"note: not in the current catalog (probing anyway): {', '.join(unknown)}")

    def max_output_of(model_id):
        entry = by_id.get(model_id, {})
        return (
            sync.MAX_OUTPUT_OVERRIDES.get(model_id)
            or int(entry.get("max_output_tokens") or 0)
            or None
        )

    def work(model_id):
        result = probe_model(
            key, model_id, max_output_of(model_id), args.timeout, args.extra_shapes
        )
        result["alternates"] = []
        if result["verdict"] != "WORKS_DIRECTLY":
            for alt in alternates(sync, catalog, model_id, by_id):
                probed = probe_model(
                    key, alt["id"], max_output_of(alt["id"]), args.timeout, args.extra_shapes
                )
                probed["price"] = (
                    f"${alt['input_price'] * 1e6:g}/${alt['output_price'] * 1e6:g} per M tokens"
                )
                result["alternates"].append(probed)
        return result

    print(f"probing {len(ids)} model(s) directly against Requesty (no Coder)...")
    counts = {}
    report = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        for result in pool.map(work, ids):
            report.append(result)
            counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1
            print(f"\n{result['verdict']:15} {result['model']}\n    {summarize(result)}")
            for alt in result["alternates"]:
                label = f"{alt['verdict']:15} {alt['model']} ({alt['price']})"
                print(f"    alternate {label}: {summarize(alt)}")
    print("\nsummary: " + ", ".join(f"{n} {v}" for v, n in sorted(counts.items())))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
        print(f"full report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
