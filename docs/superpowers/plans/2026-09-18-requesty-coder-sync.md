# Requesty to Coder Model Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> Tasks 1, 8 and 9 contain steps marked **(operator)**: they need the operator's credentials or browser and cannot be done by an agent alone.
> Stop at each **(operator)** step and hand the exact commands to the user.

**Goal:** Register the Requesty model catalog in Coder Agents as per-vendor providers with per-token prices and vendor logos, keep a dedicated free-model provider, and alert on drift with a daily in-cluster check.

**Architecture:** One stdlib-only Python script (`scripts/requesty-coder-sync.py`) with a read-only `check` and a confirm-then-write `apply` that never deletes.
It turns the live Requesty catalog into a desired state, compares it with the live Coder API, and reports drift by category.
A daily CronJob runs `check` from a stock `python:3.14-alpine` image with the script mounted from a ConfigMap, and the script pings an Uptime Kuma push monitor itself.

**Tech Stack:** Python 3.14 (stdlib only), pytest, ruff, Kubernetes CronJob, External Secrets Operator with Bitwarden Secrets Manager, Flux, Uptime Kuma.

**Spec:** `docs/superpowers/specs/2026-09-18-requesty-coder-sync-design.md`

## Global Constraints

- The script is stdlib-only Python that runs unchanged on the local Python 3 and in `python:3.14-alpine`, and it targets Python 3.12 syntax or newer.
- Coder is v2.37.0, and the routes used are `/api/v2/organizations`, `/api/v2/ai/providers`, `/api/v2/organizations/{org}/chats/models`, and `/api/experimental/ai/model-prices`.
- Provider names are `<lab>-via-requesty` and `free-via-requesty`, and they match `^[a-z0-9]+(-[a-z0-9]+)*$`.
- The default provider base URL is `https://router.requesty.ai/v1`, the provider type is `anthropic` for Anthropic, `google` for Google, and `openai` for every other lab and for the free provider, and `openai-compat` is never used because it cannot be priced.
- Prices are micro-dollars per million tokens, computed as `round(per_token * 1e12)`, every price entry carries all four keys, `null` means unknown, and `0` means free.
- The tool owns only providers named `<lab>-via-requesty` or `free-via-requesty` and the models under them, and it never deletes anything.
- Exit codes are 0 for in sync, 1 for drift, and 2 for an error, and a catalog with fewer than 100 eligible chat models aborts with exit 2.
- Repo rules: work in the worktree `.worktrees/requesty-coder-sync`, commit messages end with the trailer `Assisted-by: AI` and never carry a `Co-Authored-By` line, secrets never enter the repo, and BWS UUIDs carry an inline `# gitleaks:allow`.
- Repo rules for Markdown: one sentence per line, and write "and" or "&" instead of "+" in prose.
- Repo rules for Kubernetes: manifest filenames must match the kubeconform filter in `.github/workflows/lint.yml` (`*-cronjob.yaml`, `external-secret.yaml`), and YAML is formatted with `yamlfmt -conf .yamlfmt`.
- Label every command with the machine it runs on: everything in this plan runs on **gandalf** unless stated otherwise.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `scripts/requesty-coder-sync.py` | The whole tool: catalog selection, Coder client, drift detection, apply, and CLI, in that order |
| `pyproject.toml` | pytest and ruff configuration only (nothing is packaged) |
| `tests/requesty_sync/conftest.py` | Loads the hyphen-named script as a module and provides shared fixtures |
| `tests/requesty_sync/fakes.py` | Catalog entry factory, an in-memory `FakeCoder`, and a stub HTTP server |
| `tests/requesty_sync/fixtures/catalog_snapshot.json` | A 51-entry trimmed snapshot of the real Requesty catalog (already committed with this plan) |
| `tests/requesty_sync/test_*.py` | One test file per script section |
| `k8s/requesty-sync/` | CronJob, ExternalSecret, kustomization (with the ConfigMap generator), and README |
| `clusters/gandalf/requesty-sync.yaml` | The Flux Kustomization that reconciles the directory |
| `.pre-commit-config.yaml`, `.github/workflows/lint.yml` | Add ruff and an always-run pytest job |

The script is one file on purpose: it ships to the pod as a single ConfigMap key, and its five sections have one direction of dependency (selection, then client, then diff, then apply, then CLI).
Each task appends one section to the end of the file.

---

### Task 1: Smoke test the riskiest assumptions **(operator)**

This settles what the spec calls the two knobs (`NATIVE_TYPES` and `BASE_URL_BY_TYPE`) before any code depends on them, and finds the least-privileged role that can read AI configuration.

**Files:**
- Modify: `docs/superpowers/specs/2026-09-18-requesty-coder-sync-design.md` (append a `## Smoke test results` section)

**Interfaces:**
- Produces: values for `NATIVE_TYPES` and `BASE_URL_BY_TYPE` used in Task 2, and the role for the check-only token used in Task 8.

Run every command in this task in the operator's own terminal on **gandalf**, in one shell session, because the steps share variables.

- [ ] **Step 1: Load credentials without echoing them (operator, gandalf)**

```bash
read -rs -p 'Coder admin token: ' CODER_SESSION_TOKEN; echo; export CODER_SESSION_TOKEN
read -rs -p 'Requesty API key: ' REQUESTY_API_KEY; echo; export REQUESTY_API_KEY
export CODER_URL=https://coder.vigihome.net
api() { curl -sS -K <(printf 'header = "Coder-Session-Token: %s"\n' "$CODER_SESSION_TOKEN") -H 'Content-Type: application/json' "$@"; }
api "$CODER_URL/api/v2/buildinfo" | jq -r .version
```

Expected: `v2.37.0+8a148a9` (or newer).
Create the Coder token in the dashboard under Account, then Tokens, if there is none to hand.

- [ ] **Step 2: Pick one cheap, non-retiring model per type (operator, gandalf)**

```bash
CAT="$(curl -fsS https://router.requesty.ai/v1/models)"
pick() { jq -r --arg p "$1" '[.data[] | select((.id | startswith($p)) and (.id | test("[@:]") | not) and .supports_tool_calling and .retires == null and .input_price > 0)] | min_by(.output_price) | .id' <<<"$CAT"; }
ANTH="$(pick anthropic/)"; OAI="$(pick openai/)"; GOO="$(pick google/)"
echo "anthropic=$ANTH openai=$OAI google=$GOO"
```

Expected: three IDs such as `anthropic/claude-…`, `openai/gpt-…`, `google/gemini-…`.

- [ ] **Step 3: Call Requesty directly in both wire shapes (operator, gandalf)**

The OpenAI shape is what the `openai` and `google` provider types use.

```bash
for M in "$OAI" "$GOO"; do
  echo "== $M via /v1/chat/completions"
  jq -n --arg m "$M" '{model:$m, max_tokens:32, messages:[{role:"user",content:"Reply with the single word ok."}]}' \
    | curl -sS https://router.requesty.ai/v1/chat/completions \
        -K <(printf 'header = "Authorization: Bearer %s"\n' "$REQUESTY_API_KEY") \
        -H 'Content-Type: application/json' -d @- | jq -r '.choices[0].message.content // .'
done
```

The Anthropic shape is what the `anthropic` type uses.
Coder's Anthropic client most likely appends `/v1/messages` to the configured base URL, so the two URLs below tell us which base URL to configure.

```bash
for URL in https://router.requesty.ai/v1/messages https://router.requesty.ai/messages; do
  printf '%s -> ' "$URL"
  jq -n --arg m "$ANTH" '{model:$m, max_tokens:32, messages:[{role:"user",content:"Reply with the single word ok."}]}' \
    | curl -sS -o /dev/null -w '%{http_code}\n' "$URL" \
        -K <(printf 'header = "x-api-key: %s"\n' "$REQUESTY_API_KEY") \
        -H 'anthropic-version: 2023-06-01' -H 'Content-Type: application/json' -d @-
done
```

Expected: both OpenAI-shape calls print `ok`, and at least one Anthropic URL prints `200`.
Record which Anthropic URL returned `200`: if it is `/v1/messages`, the Anthropic provider base URL is `https://router.requesty.ai` (no `/v1`).

- [ ] **Step 4: Create three smoke providers and models in Coder (operator, gandalf)**

The names start with `smoke-`, so they sit outside the managed set (`*-via-requesty`).

```bash
ORG="$(api "$CODER_URL/api/v2/organizations" | jq -r '.[] | select(.is_default) | .id')"
declare -A BASE=( [anthropic]=https://router.requesty.ai/v1 [google]=https://router.requesty.ai/v1 [openai]=https://router.requesty.ai/v1 )
declare -A MODEL=( [anthropic]="$ANTH" [google]="$GOO" [openai]="$OAI" )
MODEL_IDS=()
for T in anthropic google openai; do
  PID="$(jq -n --arg t "$T" --arg b "${BASE[$T]}" '{type:$t, name:("smoke-"+$t), display_name:("Smoke "+$t), icon:("https://www.requesty.ai/provider_logos/v2/"+$t+".png"), enabled:true, base_url:$b, api_keys:[$ENV.REQUESTY_API_KEY]}' \
    | api -X POST "$CODER_URL/api/v2/ai/providers" -d @- | jq -r '.id')"
  echo "provider $T -> $PID"
  MID="$(jq -n --arg p "$PID" --arg m "${MODEL[$T]}" '{ai_provider_id:$p, model:$m, display_name:$m, enabled:true, context_limit:200000}' \
    | api -X POST "$CODER_URL/api/v2/organizations/$ORG/chats/models" -d @- | jq -r '.id')"
  echo "model $T -> $MID"; MODEL_IDS+=("$MID")
  jq -n --arg t "$T" --arg m "${MODEL[$T]}" '{prices:[{provider:$t, model:$m, input_price:1000000, output_price:5000000, cache_read_price:null, cache_write_price:null}]}' \
    | api -X POST "$CODER_URL/api/experimental/ai/model-prices" -d @-
done
```

Expected: three provider IDs and three model IDs (UUIDs), and no error `message` output.
If a call fails, the printed JSON `message` and `validations` say why; record them.

- [ ] **Step 5: Chat with each smoke model in the UI (operator, browser)**

Open Coder Agents in the dashboard, start a new chat, choose each "Smoke" model in turn, and send `Reply with the single word ok.`
Then open the AI settings Models page and check the icons in both the light and dark themes.

Record:

- Which of the three chats replied.
- For `smoke-anthropic`, whether it needed the base URL changed.
  If the Anthropic chat fails, retry with the root URL and chat again:
  `echo '{"base_url":"https://router.requesty.ai"}' | api -X PATCH "$CODER_URL/api/v2/ai/providers/smoke-anthropic" -d @-`
- Whether the three Requesty logos rendered, and whether they look acceptable on both themes.

- [ ] **Step 6: Find the least-privileged role that can read AI configuration (operator, gandalf)**

Create a user `requesty-sync` in the dashboard (Admin, Users, Create user), with the least privileged role that seems plausible, such as Auditor.
Create a token for it (check the flags with `coder tokens create --help` first; the CLI installs with `brew install coder`, and `coder login "$CODER_URL"` signs it in):

```bash
coder tokens create --user requesty-sync --name requesty-sync --lifetime 8760h
```

Store the token in a variable and probe the three read endpoints:

```bash
read -rs -p 'requesty-sync token: ' CHECK_TOKEN; echo
for P in /api/v2/ai/providers "/api/v2/organizations/$ORG/chats/models" /api/experimental/ai/model-prices; do
  printf '%s -> ' "$P"
  curl -sS -o /dev/null -w '%{http_code}\n' -K <(printf 'header = "Coder-Session-Token: %s"\n' "$CHECK_TOKEN") "$CODER_URL$P"
done
```

Expected: `200` for all three.
If any prints `403`, raise the user's role one step (for example to Organization admin) and probe again until all three print `200`, then record the role.
If nothing below Owner works, record that, and Task 8 will document the CronJob token as Owner-scoped.

- [ ] **Step 7: Clean up the smoke objects (operator, gandalf)**

```bash
for MID in "${MODEL_IDS[@]}"; do api -X DELETE "$CODER_URL/api/v2/organizations/$ORG/chats/models/$MID"; done
for T in anthropic google openai; do api -X DELETE "$CODER_URL/api/v2/ai/providers/smoke-$T"; done
api "$CODER_URL/api/v2/ai/providers" | jq -r '.[].name'
```

Expected: no `smoke-` names remain.
The three custom price rows stay behind in the price table and are harmless, because `apply` overwrites them with identical values for the same models.

- [ ] **Step 8: Record the outcomes in the spec and commit**

Append this section to the spec, filling in the observed values, and commit.

```markdown
## Smoke test results

- Direct OpenAI-shape calls: worked or failed, with the models used.
- Direct Anthropic-shape URL that returned 200: `/v1/messages` or `/messages`.
- Coder `anthropic` provider base URL that worked: the URL.
- Coder `google` provider worked: yes or no.
- Requesty logos rendered on both themes: yes or no.
- Least-privileged role that can read providers, models, and prices: the role.
```

```bash
cd ~/git/nickvigilante/homelab/.worktrees/requesty-coder-sync
git add docs/superpowers/specs/2026-09-18-requesty-coder-sync-design.md
git commit -m "docs(coder): record the Requesty smoke test results" -m "Assisted-by: AI"
```

Carry the outcomes into Task 2 as follows.

- If the Anthropic base URL that worked is not `https://router.requesty.ai/v1`, set `BASE_URL_BY_TYPE = {"anthropic": "<the URL that worked>"}` in Task 2.
- If the `anthropic` type could not be made to work at all, remove `"anthropic"` from `NATIVE_TYPES`, so Anthropic uses the `openai` type.
- If the `google` type failed, remove `"google"` from `NATIVE_TYPES` in the same way.

---

### Task 2: Scaffold, tooling, and shared test support

**Files:**
- Create: `pyproject.toml`
- Create: `tests/requesty_sync/conftest.py`
- Create: `tests/requesty_sync/fakes.py`
- Create: `tests/requesty_sync/test_scaffold.py`
- Create: `scripts/requesty-coder-sync.py` (the header: imports, constants, exceptions, dataclasses)
- Modify: `.pre-commit-config.yaml`
- Modify: `.github/workflows/lint.yml`

**Interfaces:**
- Produces: `sync` (a session fixture returning the loaded script module), `relaxed_guard` (autouse, sets `MIN_ELIGIBLE_MODELS` to 1), `stub` (a stub HTTP server with `.requests`, `.responses`, `.server_port`), and from `fakes.py`: `make_entry`, `small_catalog`, `FakeCoder`, `seed_in_sync`, `start_stub`.
- Produces: in the script, `SyncError`, `ApiError(method, path, status, body)`, `DesiredProvider`, `DesiredModel`, `Desired`, `Prices`, the constants (`REQUESTY_MODELS_URL`, `REQUESTY_BASE_URL`, `LOGO_BASE_URL`, `FALLBACK_ICON`, `DEFAULT_CODER_URL`, `PROVIDER_SUFFIX`, `FREE_PROVIDER_NAME`, `MIN_ELIGIBLE_MODELS`, `USER_AGENT`, `EXIT_OK`, `EXIT_DRIFT`, `EXIT_ERROR`, `NATIVE_TYPES`, `BASE_URL_BY_TYPE`, `LAB_ALIASES`, `LAB_LOGOS`, `LAB_NAMES`).

- [ ] **Step 1: Create the worktree checkout state**

The worktree and branch `requesty-coder-sync` already exist and hold the spec.
Confirm you are in it and up to date with `main`.

```bash
cd ~/git/nickvigilante/homelab/.worktrees/requesty-coder-sync
git status -sb
git fetch origin && git log --oneline -1 origin/main
```

Expected: branch `requesty-coder-sync`, clean tree.

- [ ] **Step 2: Write the tooling config and the failing scaffold test**

`pyproject.toml`:

```toml
# Tool configuration only; nothing here is packaged or installed.
[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM"]
```

`tests/requesty_sync/conftest.py`:

```python
import importlib.util
import pathlib
import sys

import pytest
from fakes import start_stub

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "requesty-coder-sync.py"


@pytest.fixture(scope="session")
def sync():
    spec = importlib.util.spec_from_file_location("requesty_coder_sync", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["requesty_coder_sync"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def relaxed_guard(monkeypatch, sync):
    """Small test catalogs would trip the truncated-catalog guard."""
    monkeypatch.setattr(sync, "MIN_ELIGIBLE_MODELS", 1)


@pytest.fixture
def stub():
    server = start_stub()
    yield server
    server.shutdown()
    server.server_close()
```

`tests/requesty_sync/fakes.py`:

```python
"""Test doubles: catalog entries, an in-memory Coder, and a stub HTTP server."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_entry(
    model_id,
    lab="acme",
    canonical=None,
    inp=1e-6,
    out=2e-6,
    cached=None,
    ctx=100_000,
    maxout=8_000,
    tools=True,
    retires=None,
):
    entry = {
        "id": model_id,
        "api": "chat",
        "model_lab": lab,
        # Region (@) and service-tier (:) suffixes share the plain canonical name.
        "model_canonical_name": canonical or re.split(r"[@:]", model_id.rsplit("/", 1)[-1])[0],
        "input_price": inp,
        "output_price": out,
        "cached_price": cached,
        "context_window": ctx,
        "max_output_tokens": maxout,
        "supports_tool_calling": tools,
    }
    if retires is not None:
        entry["retires"] = retires
    return entry


def small_catalog():
    """A paid Anthropic model, a paid OpenAI model, and a free NVIDIA model."""
    return [
        make_entry(
            "anthropic/claude-a",
            "anthropic",
            inp=3e-6,
            out=15e-6,
            cached=3e-7,
            ctx=200_000,
            maxout=64_000,
        ),
        make_entry("openai/gpt-x", "openai", inp=1e-6, out=4e-6, ctx=400_000, maxout=128_000),
        make_entry("nvidia/free-y", "nvidia", inp=0, out=0, ctx=131_072, maxout=0),
    ]


class FakeCoder:
    """Same interface as CoderClient, kept in memory. It has no delete methods."""

    def __init__(self):
        self.providers = {}
        self.models = {}
        self.prices = {}
        self.calls = []
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}-{self._n}"

    def default_org_id(self):
        return "org-1"

    def list_providers(self):
        return [dict(p) for p in self.providers.values()]

    def create_provider(self, payload):
        provider_id = self._id("prov")
        keys = [{"id": self._id("key"), "masked": "****"} for _ in payload.get("api_keys", [])]
        provider = {
            **{k: v for k, v in payload.items() if k != "api_keys"},
            "id": provider_id,
            "api_keys": keys,
        }
        self.providers[provider_id] = provider
        self.calls.append(("create_provider", payload["name"]))
        return dict(provider)

    def update_provider(self, provider_id, payload):
        provider = self.providers[provider_id]
        for key, value in payload.items():
            if key == "api_keys":
                provider["api_keys"] = [{"id": self._id("key"), "masked": "****"} for _ in value]
            else:
                provider[key] = value
        self.calls.append(("update_provider", provider_id))

    def list_models(self, org_id):
        return [dict(m) for m in self.models.values()]

    def create_model(self, org_id, payload):
        model_id = self._id("model")
        model = {**payload, "id": model_id, "organization_id": org_id, "is_default": False}
        self.models[model_id] = model
        self.calls.append(("create_model", payload["model"]))
        return dict(model)

    def update_model(self, org_id, model_id, payload):
        self.models[model_id].update(payload)
        self.calls.append(("update_model", model_id))

    def list_custom_prices(self):
        return [dict(p) for p in self.prices.values()]

    def upsert_prices(self, prices):
        for price in prices:
            self.prices[(price["provider"], price["model"])] = {**price, "source": "custom"}
        self.calls.append(("upsert_prices", len(prices)))


def seed_in_sync(fake, desired):
    """Populate a FakeCoder so that it exactly matches `desired`."""
    ids = {}
    for name, provider in desired.providers.items():
        created = fake.create_provider(
            {
                "type": provider.type,
                "name": name,
                "display_name": provider.display_name,
                "icon": provider.icon,
                "enabled": True,
                "base_url": provider.base_url,
                "api_keys": ["secret-key"],
            }
        )
        ids[name] = created["id"]
    for model in desired.models.values():
        payload = {
            "ai_provider_id": ids[model.provider],
            "model": model.model,
            "display_name": model.display_name,
            "enabled": True,
            "context_limit": model.context_limit,
        }
        if model.max_output_tokens:
            payload["model_config"] = {"max_output_tokens": model.max_output_tokens}
        fake.create_model("org-1", payload)
        first, second, third, fourth = model.prices
        fake.upsert_prices(
            [
                {
                    "provider": model.provider_type,
                    "model": model.model,
                    "input_price": first,
                    "output_price": second,
                    "cache_read_price": third,
                    "cache_write_price": fourth,
                }
            ]
        )
    fake.calls.clear()
    return ids


class _Handler(BaseHTTPRequestHandler):
    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        path = self.path.split("?")[0]
        self.server.requests.append(
            {
                "method": self.command,
                "path": path,
                "query": self.path,
                "headers": headers,
                "body": body,
            }
        )
        status, payload = self.server.responses.get(
            (self.command, path), (404, {"message": "not found"})
        )
        raw = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PATCH = _handle

    def log_message(self, *args):
        pass


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.responses = {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
```

`tests/requesty_sync/test_scaffold.py`:

```python
def test_module_constants(sync):
    assert sync.FREE_PROVIDER_NAME == "free-via-requesty"
    assert sync.REQUESTY_BASE_URL == "https://router.requesty.ai/v1"
    assert (sync.EXIT_OK, sync.EXIT_DRIFT, sync.EXIT_ERROR) == (0, 1, 2)


def test_api_error_carries_status(sync):
    error = sync.ApiError("GET", "/x", 500, "boom")
    assert error.status == 500
    assert "GET /x failed (500): boom" in str(error)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run --with pytest pytest -q`
Expected: FAIL with `FileNotFoundError` for `scripts/requesty-coder-sync.py` (the `sync` fixture cannot load the script yet).

- [ ] **Step 4: Create the script header**

Create `scripts/requesty-coder-sync.py` with this content.
Apply the Task 1 outcomes to `NATIVE_TYPES` and `BASE_URL_BY_TYPE` before saving, exactly as Task 1 Step 8 describes.

```python
#!/usr/bin/env python3
"""Sync the Requesty model catalog into Coder Agents.

Subcommands:
  check  report drift between Requesty and Coder (read-only)
  apply  make Coder match Requesty (creates and updates, never deletes)

Environment:
  CODER_URL             Coder base URL (default https://coder.vigihome.net)
  CODER_SESSION_TOKEN   Coder API token
  REQUESTY_API_KEY      Requesty key, needed by apply to create providers
  UPTIME_KUMA_PUSH_URL  optional push monitor URL, pinged after check

Exit codes: 0 in sync, 1 drift, 2 error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

REQUESTY_MODELS_URL = "https://router.requesty.ai/v1/models"
REQUESTY_BASE_URL = "https://router.requesty.ai/v1"
LOGO_BASE_URL = "https://www.requesty.ai/provider_logos/v2/"
FALLBACK_ICON = "https://www.requesty.ai/Requesty_logo.svg"
DEFAULT_CODER_URL = "https://coder.vigihome.net"
PROVIDER_SUFFIX = "-via-requesty"
FREE_PROVIDER_NAME = "free" + PROVIDER_SUFFIX
MIN_ELIGIBLE_MODELS = 100
USER_AGENT = "requesty-coder-sync/1.0"

EXIT_OK, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2

# Smoke-test outcomes live here, so a fallback is a one-line edit.
# NATIVE_TYPES maps a lab to the Coder provider type that speaks its protocol;
# every other lab uses "openai". BASE_URL_BY_TYPE overrides the base URL for a
# provider type whose client appends its own "/v1".
NATIVE_TYPES = {"anthropic": "anthropic", "google": "google"}
BASE_URL_BY_TYPE: dict[str, str] = {}

LAB_ALIASES = {"moonshotai": "moonshot", "qwen": "alibaba"}

# Snapshot of https://www.requesty.ai/provider_logos/v2/<logo>.png
LAB_LOGOS = {
    "alibaba": "alibaba",
    "anthropic": "anthropic",
    "deepinfra": "deepinfra",
    "deepseek": "deepseek",
    "google": "google",
    "meta": "meta",
    "minimax": "minimaxi",
    "mistral": "mistral",
    "moonshot": "moonshot",
    "nvidia": "nvidia",
    "openai": "openai",
    "sakana": "sakana",
    "thinkingmachines": "thinkingmachines",
    "xai": "xai",
    "xiaomi": "xiaomi",
    "zai": "zai",
}

LAB_NAMES = {
    "alibaba": "Alibaba",
    "anthropic": "Anthropic",
    "bytedance": "ByteDance",
    "deepinfra": "DeepInfra",
    "deepseek": "DeepSeek",
    "google": "Google",
    "gryphe": "Gryphe",
    "inclusionai": "inclusionAI",
    "kwaipilot": "Kwaipilot",
    "meta": "Meta",
    "minimax": "MiniMax",
    "mistral": "Mistral",
    "moonshot": "Moonshot AI",
    "nousresearch": "Nous Research",
    "nvidia": "NVIDIA",
    "openai": "OpenAI",
    "sakana": "Sakana AI",
    "stepfun": "StepFun",
    "tencent": "Tencent",
    "thinkingmachines": "Thinking Machines",
    "xai": "xAI",
    "xiaomi": "Xiaomi",
    "zai": "Z.ai",
}

# input, output, cache read, cache write; micro-dollars per million tokens.
Prices = tuple[int | None, int | None, int | None, int | None]


class SyncError(Exception):
    """A fatal problem; the CLI exits with code 2."""


class ApiError(SyncError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} failed ({status or 'no response'}): {body}")
        self.status = status


@dataclass(frozen=True)
class DesiredProvider:
    name: str
    display_name: str
    type: str
    base_url: str
    icon: str


@dataclass(frozen=True)
class DesiredModel:
    provider: str
    provider_type: str
    model: str
    display_name: str
    context_limit: int
    max_output_tokens: int | None
    prices: Prices


@dataclass
class Desired:
    providers: dict[str, DesiredProvider]
    models: dict[str, DesiredModel]  # keyed by Requesty model ID
    info: list[str]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run --with pytest pytest -q`
Expected: `2 passed`.

- [ ] **Step 6: Add the ruff hook to pre-commit**

Look up the current pin and hook IDs, then add the block to `.pre-commit-config.yaml` directly above the `- repo: local` block.

```bash
gh api repos/astral-sh/ruff-pre-commit/releases/latest --jq .tag_name
```

Expected: `v0.16.8` or newer.
Use that tag as `rev`.

```yaml
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.8
    hooks:
      - id: ruff-check
      - id: ruff-format
```

Run: `pre-commit run --files pyproject.toml scripts/requesty-coder-sync.py tests/requesty_sync/*.py .pre-commit-config.yaml`
Expected: all hooks pass, possibly after ruff or mdformat rewrote a file.
If hooks modified files, review the diff and run the command again until it passes.

- [ ] **Step 7: Add an always-run pytest job to CI**

Append this job under `jobs:` in `.github/workflows/lint.yml`, after `kubeconform`.
It has no path filter on purpose, so a required check can never be left waiting.

```yaml
  python-tests:
    name: pytest (requesty-coder-sync)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.14"
      - run: pip install pytest
      - run: pytest -q
```

Run: `yamlfmt -conf .yamlfmt -lint .github/workflows/lint.yml && yamllint -c .github/yamllint.yml .github/workflows/lint.yml`
Expected: no output.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml scripts/requesty-coder-sync.py tests .pre-commit-config.yaml .github/workflows/lint.yml
git commit -m "build(coder): scaffold the Requesty sync tool and its test tooling" -m "Assisted-by: AI"
```

---

### Task 3: Catalog selection

Turns the Requesty catalog into the desired state: eligibility, retirement filter, free pool, host preference, cross-lab collapse, providers, icons, and prices.

**Files:**
- Modify: `scripts/requesty-coder-sync.py` (append the selection section)
- Create: `tests/requesty_sync/test_selection.py`
- Already present: `tests/requesty_sync/fixtures/catalog_snapshot.json`

**Interfaces:**
- Consumes: the Task 2 constants and dataclasses.
- Produces: `is_eligible(entry)`, `is_retiring(entry)`, `retire_date(timestamp) -> str`, `is_free(entry)`, `lab_of(entry)`, `canonical_of(entry)`, `is_plain(entry)`, `host_of(entry)`, `pick(entries, lab)`, `select(entries) -> (chosen, notes)`, `slugify(text)`, `micro(per_token)`, `icon_for(lab)`, `lab_provider(lab) -> DesiredProvider`, `free_provider() -> DesiredProvider`, and `build_desired(entries) -> Desired`.

- [ ] **Step 1: Confirm the fixture is present**

Run: `jq '.data | length' tests/requesty_sync/fixtures/catalog_snapshot.json`
Expected: `51`.

- [ ] **Step 2: Write the failing tests**

`tests/requesty_sync/test_selection.py`:

```python
import json
import pathlib

import pytest
from fakes import make_entry

SNAPSHOT = pathlib.Path(__file__).parent / "fixtures" / "catalog_snapshot.json"
LOGOS = "https://www.requesty.ai/provider_logos/v2/"
FALLBACK = "https://www.requesty.ai/Requesty_logo.svg"


@pytest.fixture
def snap(sync):
    return sync.build_desired(json.loads(SNAPSHOT.read_text())["data"])


def by_display_name(desired, name):
    return [m for m in desired.models.values() if m.display_name == name]


# ---- real-catalog snapshot -------------------------------------------------


def test_snapshot_picks_the_first_party_entry(snap):
    model = snap.models["anthropic/claude-sonnet-4-5"]
    assert model.provider == "anthropic-via-requesty"
    assert model.provider_type == "anthropic"
    assert model.context_limit == 1_000_000
    assert model.max_output_tokens == 64_000
    assert model.prices == (3_000_000, 15_000_000, 300_000, None)
    assert "bedrock/claude-sonnet-4-5" not in snap.models


def test_snapshot_ignores_regions_and_service_tiers(snap):
    gpt5 = by_display_name(snap, "gpt-5")
    assert [m.model for m in gpt5] == ["openai/gpt-5"]
    assert gpt5[0].provider == "openai-via-requesty"
    assert gpt5[0].provider_type == "openai"
    assert gpt5[0].prices[:2] == (1_250_000, 10_000_000)


def test_snapshot_merges_the_moonshotai_alias(snap):
    kimi = by_display_name(snap, "kimi-k2.6")
    assert [m.model for m in kimi] == ["moonshot/kimi-k2.6"]
    assert kimi[0].provider == "moonshot-via-requesty"


def test_snapshot_collapses_a_model_spanning_labs(snap):
    glm = by_display_name(snap, "glm-5.2")
    assert [m.model for m in glm] == ["zai/glm-5.2"]
    assert glm[0].provider == "zai-via-requesty"
    assert "collapsed glm-5.2: kept lab zai, dropped lab deepinfra" in snap.info


def test_snapshot_gives_free_models_their_own_provider(snap):
    free = snap.providers["free-via-requesty"]
    assert free.type == "openai"
    assert free.display_name == "Free models via Requesty"
    assert free.icon == FALLBACK
    for model_id in (
        "google/gemma-4-31b-it",
        "nvidia/nemotron-3-nano-30b-a3b",
        "mistral/leanstral-1-5",
    ):
        assert snap.models[model_id].provider == "free-via-requesty"
        assert snap.models[model_id].prices == (0, 0, 0, 0)


def test_snapshot_keeps_the_paid_twin_of_a_free_model(snap):
    assert snap.models["deepinfra/google/gemma-4-31B-it"].provider == "google-via-requesty"
    assert snap.providers["google-via-requesty"].type == "google"
    twin = snap.models["deepinfra/nvidia/Nemotron-3-Nano-30B-A3B"]
    assert twin.provider == "nvidia-via-requesty"


def test_snapshot_skips_free_models_without_tool_calling(snap):
    assert "poolside/laguna-m.1" not in snap.models
    assert "skipped (free, no tool calling): poolside/laguna-m.1" in snap.info


# ---- host preference -------------------------------------------------------


def test_first_party_plain_entry_beats_cheaper_hosts(sync):
    desired = sync.build_desired(
        [
            make_entry("bedrock/foo", inp=1e-6, out=1e-6),
            make_entry("acme/foo", inp=2e-6, out=2e-6),
            make_entry("acme/foo:flex", inp=1e-7, out=1e-7),
        ]
    )
    assert list(desired.models) == ["acme/foo"]


def test_cheapest_plain_entry_when_no_first_party(sync):
    desired = sync.build_desired(
        [
            make_entry("a/foo", inp=3e-6, out=0),
            make_entry("b/foo@eu", inp=1e-6, out=0),
            make_entry("c/foo", inp=2e-6, out=0),
        ]
    )
    assert list(desired.models) == ["c/foo"]


def test_cheapest_overall_when_nothing_is_plain(sync):
    desired = sync.build_desired(
        [make_entry("x/foo@eu", inp=5e-6, out=0), make_entry("y/foo@us", inp=2e-6, out=0)]
    )
    assert list(desired.models) == ["y/foo@us"]


def test_cross_lab_tie_goes_to_the_alphabetically_first_lab(sync):
    desired = sync.build_desired(
        [
            make_entry("zeta/x", lab="zeta", canonical="x"),
            make_entry("alpha/x", lab="alpha", canonical="x"),
        ]
    )
    assert list(desired.models) == ["alpha/x"]
    assert desired.models["alpha/x"].provider == "alpha-via-requesty"


def test_qwen_is_an_alias_of_alibaba(sync):
    desired = sync.build_desired([make_entry("alibaba/qwen3", lab="qwen")])
    assert desired.models["alibaba/qwen3"].provider == "alibaba-via-requesty"


# ---- eligibility -----------------------------------------------------------


def test_entries_without_tool_calling_or_prices_or_context_are_ineligible(sync):
    entries = [
        make_entry("a/no-tools", tools=False),
        make_entry("a/no-price", inp=None),
        make_entry("a/no-context", ctx=0),
        make_entry("a/ok"),
    ]
    assert list(sync.build_desired(entries).models) == ["a/ok"]


def test_a_missing_price_is_not_free(sync):
    assert sync.is_free(make_entry("a/z", inp=0, out=0))
    assert not sync.is_free(make_entry("a/z", inp=None, out=0))
    assert not sync.is_free(make_entry("a/z", inp=0, out=1e-6))


def test_truncated_catalog_is_refused(sync, monkeypatch):
    monkeypatch.setattr(sync, "MIN_ELIGIBLE_MODELS", 100)
    with pytest.raises(sync.SyncError, match="only 1 eligible"):
        sync.build_desired([make_entry("a/ok")])


# ---- retirement ------------------------------------------------------------

OCT_16 = 1_792_108_800  # 2026-10-16T00:00:00Z


def test_retiring_entries_are_skipped_and_reported(sync):
    desired = sync.build_desired([make_entry("a/keep"), make_entry("a/going", retires=OCT_16)])
    assert list(desired.models) == ["a/keep"]
    assert "skipped (retires 2026-10-16): going" in desired.info


def test_another_host_keeps_a_retiring_model_registered(sync):
    desired = sync.build_desired(
        [
            make_entry("acme/foo", canonical="foo", retires=OCT_16),
            make_entry("other/foo", canonical="foo"),
        ]
    )
    assert list(desired.models) == ["other/foo"]
    assert not any("retires" in line for line in desired.info)


def test_the_earliest_retirement_date_is_reported(sync):
    desired = sync.build_desired(
        [
            make_entry("a/old", canonical="old", retires=OCT_16),
            make_entry("b/old", canonical="old", retires=OCT_16 - 86_400),
            make_entry("a/keep"),
        ]
    )
    assert "skipped (retires 2026-10-15): old" in desired.info


def test_a_retiring_free_model_is_skipped_too(sync):
    desired = sync.build_desired(
        [make_entry("a/free", inp=0, out=0, retires=OCT_16), make_entry("a/keep")]
    )
    assert "free-via-requesty" not in desired.providers


# ---- providers, prices, icons ----------------------------------------------


@pytest.mark.parametrize(
    ("lab", "expected"),
    [
        ("anthropic", LOGOS + "anthropic.png"),
        ("moonshot", LOGOS + "moonshot.png"),
        ("minimax", LOGOS + "minimaxi.png"),
        ("gryphe", FALLBACK),
    ],
)
def test_icon_for(sync, lab, expected):
    assert sync.icon_for(lab) == expected


@pytest.mark.parametrize(
    ("lab", "provider_type"),
    [("anthropic", "anthropic"), ("google", "google"), ("mistral", "openai")],
)
def test_provider_type(sync, lab, provider_type):
    assert sync.lab_provider(lab).type == provider_type


def test_provider_naming(sync):
    provider = sync.lab_provider("thinkingmachines")
    assert provider.name == "thinkingmachines-via-requesty"
    assert provider.display_name == "Thinking Machines via Requesty"
    assert sync.lab_provider("brand-new").display_name == "Brand-New via Requesty"


def test_price_conversion(sync):
    assert sync.micro(1.25e-6) == 1_250_000
    assert sync.micro(3.3000000000000003e-06) == 3_300_000
    assert sync.micro(0) == 0
    assert sync.micro(None) is None


def test_zero_max_output_tokens_means_unknown(sync):
    desired = sync.build_desired([make_entry("a/x", maxout=0)])
    assert desired.models["a/x"].max_output_tokens is None


def test_a_non_plain_fallback_is_reported(sync):
    desired = sync.build_desired(
        [make_entry("acme/foo:flex", canonical="foo"), make_entry("acme/bar")]
    )
    assert set(desired.models) == {"acme/foo:flex", "acme/bar"}
    assert "only a non-plain entry is available: acme/foo:flex" in desired.info
    assert not any("acme/bar" in line for line in desired.info)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --with pytest pytest tests/requesty_sync/test_selection.py -q`
Expected: FAIL, with `AttributeError: module 'requesty_coder_sync' has no attribute 'build_desired'` (or a sibling name) in every test.

- [ ] **Step 4: Append the selection section**

Append this to the end of `scripts/requesty-coder-sync.py`, after two blank lines.

```python
# ---- catalog selection -----------------------------------------------------


def is_eligible(entry: dict[str, Any]) -> bool:
    return (
        entry.get("api") == "chat"
        and entry.get("supports_tool_calling") is True
        and entry.get("input_price") is not None
        and entry.get("output_price") is not None
        and (entry.get("context_window") or 0) > 0
    )


def is_retiring(entry: dict[str, Any]) -> bool:
    """Requesty lists a retirement date (Unix seconds) for this entry."""
    return entry.get("retires") is not None


def retire_date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat()


def is_free(entry: dict[str, Any]) -> bool:
    # An explicit zero, never a missing price.
    return entry.get("input_price") == 0 and entry.get("output_price") == 0


def lab_of(entry: dict[str, Any]) -> str:
    lab = entry.get("model_lab") or "unknown"
    return LAB_ALIASES.get(lab, lab)


def canonical_of(entry: dict[str, Any]) -> str:
    return entry.get("model_canonical_name") or entry["id"]


def is_plain(entry: dict[str, Any]) -> bool:
    """No @region suffix and no :variant (service tier) suffix."""
    tail = entry["id"].rsplit("/", 1)[-1]
    return "@" not in tail and ":" not in tail


def host_of(entry: dict[str, Any]) -> str:
    return entry["id"].split("/", 1)[0]


def pick(entries: list[dict[str, Any]], lab: str) -> dict[str, Any]:
    """First-party plain entry, else cheapest plain, else cheapest overall."""

    def rank(entry: dict[str, Any]) -> tuple[bool, bool, float, str]:
        plain = is_plain(entry)
        first_party = plain and host_of(entry) == lab
        price = entry["input_price"] + entry["output_price"]
        return (not first_party, not plain, price, entry["id"])

    return min(entries, key=rank)


def select(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """One entry per canonical model. A canonical name that spans several labs
    stays with the lab holding the most entries (ties go alphabetically)."""
    by_canonical: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for entry in entries:
        labs = by_canonical.setdefault(canonical_of(entry), {})
        labs.setdefault(lab_of(entry), []).append(entry)
    chosen: list[dict[str, Any]] = []
    notes: list[str] = []
    for canonical in sorted(by_canonical):
        labs = by_canonical[canonical]
        counts = {lab: len(items) for lab, items in labs.items()}
        winner = max(sorted(counts), key=counts.__getitem__)
        for lab in sorted(counts):
            if lab != winner:
                notes.append(f"collapsed {canonical}: kept lab {winner}, dropped lab {lab}")
        chosen.append(pick(labs[winner], winner))
    return chosen, notes


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def micro(per_token: float | None) -> int | None:
    """USD per token to micro-dollars per million tokens."""
    if per_token is None:
        return None
    return round(per_token * 1e12)


def icon_for(lab: str) -> str:
    logo = LAB_LOGOS.get(lab)
    return f"{LOGO_BASE_URL}{logo}.png" if logo else FALLBACK_ICON


def lab_provider(lab: str) -> DesiredProvider:
    provider_type = NATIVE_TYPES.get(lab, "openai")
    return DesiredProvider(
        name=slugify(lab) + PROVIDER_SUFFIX,
        display_name=f"{LAB_NAMES.get(lab, lab.title())} via Requesty",
        type=provider_type,
        base_url=BASE_URL_BY_TYPE.get(provider_type, REQUESTY_BASE_URL),
        icon=icon_for(lab),
    )


def free_provider() -> DesiredProvider:
    return DesiredProvider(
        name=FREE_PROVIDER_NAME,
        display_name="Free models via Requesty",
        type="openai",
        base_url=BASE_URL_BY_TYPE.get("openai", REQUESTY_BASE_URL),
        icon=FALLBACK_ICON,
    )


def build_desired(entries: list[dict[str, Any]]) -> Desired:
    candidates = [e for e in entries if is_eligible(e)]
    eligible = [e for e in candidates if not is_retiring(e)]
    if len(eligible) < MIN_ELIGIBLE_MODELS:
        raise SyncError(
            f"catalog has only {len(eligible)} eligible chat models "
            f"(minimum {MIN_ELIGIBLE_MODELS}); refusing to continue"
        )
    info = sorted(
        f"skipped (free, no tool calling): {e['id']}"
        for e in entries
        if e.get("api") == "chat" and e.get("supports_tool_calling") is not True and is_free(e)
    )
    desired = Desired(providers={}, models={}, info=info)
    pools = (
        (True, [e for e in eligible if is_free(e)]),
        (False, [e for e in eligible if not is_free(e)]),
    )
    for free, pool in pools:
        chosen, notes = select(pool)
        desired.info.extend(notes)
        for entry in chosen:
            provider = free_provider() if free else lab_provider(lab_of(entry))
            if not is_plain(entry):
                desired.info.append(f"only a non-plain entry is available: {entry['id']}")
            desired.providers.setdefault(provider.name, provider)
            prices: Prices = (
                (0, 0, 0, 0)
                if free
                else (
                    micro(entry["input_price"]),
                    micro(entry["output_price"]),
                    micro(entry.get("cached_price")),
                    None,
                )
            )
            desired.models[entry["id"]] = DesiredModel(
                provider=provider.name,
                provider_type=provider.type,
                model=entry["id"],
                display_name=canonical_of(entry),
                context_limit=int(entry["context_window"]),
                max_output_tokens=int(entry.get("max_output_tokens") or 0) or None,
                prices=prices,
            )
    registered = {m.display_name for m in desired.models.values()}
    retiring: dict[str, float] = {}
    for entry in candidates:
        name = canonical_of(entry)
        if is_retiring(entry) and name not in registered:
            retiring[name] = min(retiring.get(name, entry["retires"]), entry["retires"])
    desired.info.extend(
        f"skipped (retires {retire_date(timestamp)}): {name}"
        for name, timestamp in sorted(retiring.items())
    )
    return desired
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --with pytest pytest -q`
Expected: `32 passed` (2 scaffold and 30 selection).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format scripts tests && uvx ruff check scripts tests
git add scripts/requesty-coder-sync.py tests
git commit -m "feat(coder): select and price Requesty models for Coder" -m "Assisted-by: AI"
```

---

### Task 4: Catalog fetch and Coder API client

**Files:**
- Modify: `scripts/requesty-coder-sync.py` (append the client section)
- Create: `tests/requesty_sync/test_client.py`

**Interfaces:**
- Consumes: `SyncError`, `ApiError`, `USER_AGENT`, `REQUESTY_MODELS_URL`, `PROVIDER_SUFFIX`.
- Produces: `fetch_catalog(url=REQUESTY_MODELS_URL, timeout=60.0) -> list[dict]`, `CoderClient(base_url, token, timeout=30.0)` with `request`, `default_org_id`, `list_providers`, `create_provider(payload) -> dict`, `update_provider(provider_id, payload)`, `list_models(org_id)`, `create_model(org_id, payload) -> dict`, `update_model(org_id, model_id, payload)`, `list_custom_prices`, `upsert_prices(prices)`, plus `Live(org_id, providers, models, prices)`, `is_managed_name(name)`, and `load_live(client) -> Live`.

- [ ] **Step 1: Write the failing tests**

`tests/requesty_sync/test_client.py`:

```python
import pytest


def client_for(sync, stub):
    return sync.CoderClient(f"http://127.0.0.1:{stub.server_port}", "tok")


def test_default_org_sends_the_token(sync, stub):
    stub.responses[("GET", "/api/v2/organizations")] = (
        200,
        [{"id": "org-1", "is_default": False}, {"id": "org-2", "is_default": True}],
    )
    assert client_for(sync, stub).default_org_id() == "org-2"
    assert stub.requests[0]["headers"]["coder-session-token"] == "tok"


def test_no_default_org_is_an_error(sync, stub):
    stub.responses[("GET", "/api/v2/organizations")] = (200, [{"id": "o", "is_default": False}])
    with pytest.raises(sync.SyncError, match="no default organization"):
        client_for(sync, stub).default_org_id()


def test_list_models_unwraps_the_response(sync, stub):
    stub.responses[("GET", "/api/v2/organizations/org-1/chats/models")] = (
        200,
        {"models": [{"id": "m1"}], "providers": [], "unsupported_providers": []},
    )
    assert client_for(sync, stub).list_models("org-1") == [{"id": "m1"}]


def test_prices_are_listed_from_the_custom_source(sync, stub):
    stub.responses[("GET", "/api/experimental/ai/model-prices")] = (200, [])
    client_for(sync, stub).list_custom_prices()
    assert stub.requests[0]["query"].endswith("?source=custom")


def test_post_sends_json_and_tolerates_no_content(sync, stub):
    stub.responses[("POST", "/api/experimental/ai/model-prices")] = (204, None)
    client_for(sync, stub).upsert_prices([{"provider": "openai", "model": "m"}])
    request = stub.requests[0]
    assert request["headers"]["content-type"] == "application/json"
    assert b'"prices"' in request["body"]


def test_http_errors_become_api_errors(sync, stub):
    stub.responses[("GET", "/api/v2/ai/providers")] = (403, {"message": "forbidden"})
    with pytest.raises(sync.ApiError) as excinfo:
        client_for(sync, stub).list_providers()
    assert excinfo.value.status == 403
    assert "forbidden" in str(excinfo.value)


def test_unreachable_server_becomes_an_api_error(sync, stub):
    client = client_for(sync, stub)
    stub.shutdown()
    stub.server_close()
    with pytest.raises(sync.ApiError) as excinfo:
        client.list_providers()
    assert excinfo.value.status == 0


def test_fetch_catalog_returns_the_data_list(sync, stub):
    stub.responses[("GET", "/v1/models")] = (200, {"object": "list", "data": [{"id": "a/b"}]})
    url = f"http://127.0.0.1:{stub.server_port}/v1/models"
    assert sync.fetch_catalog(url) == [{"id": "a/b"}]


def test_fetch_catalog_rejects_a_malformed_response(sync, stub):
    stub.responses[("GET", "/v1/models")] = (200, {"oops": True})
    with pytest.raises(sync.SyncError, match="no 'data' list"):
        sync.fetch_catalog(f"http://127.0.0.1:{stub.server_port}/v1/models")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest pytest tests/requesty_sync/test_client.py -q`
Expected: FAIL with `AttributeError` on `CoderClient` or `fetch_catalog`.

- [ ] **Step 3: Append the client section**

```python
# ---- catalog and Coder API -------------------------------------------------


def fetch_catalog(url: str = REQUESTY_MODELS_URL, timeout: float = 60.0) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError) as err:
        raise SyncError(f"fetch {url}: {err}") from err
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise SyncError(f"fetch {url}: response has no 'data' list")
    return data


class CoderClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Coder-Session-Token": self.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:500]
            raise ApiError(method, path, err.code, detail) from err
        except OSError as err:
            raise ApiError(method, path, 0, str(err)) from err
        return json.loads(raw) if raw else None

    def default_org_id(self) -> str:
        for org in self.request("GET", "/api/v2/organizations"):
            if org.get("is_default"):
                return org["id"]
        raise SyncError("Coder has no default organization")

    def list_providers(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v2/ai/providers")

    def create_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/v2/ai/providers", payload)

    def update_provider(self, provider_id: str, payload: dict[str, Any]) -> None:
        self.request("PATCH", f"/api/v2/ai/providers/{provider_id}", payload)

    def list_models(self, org_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v2/organizations/{org_id}/chats/models")["models"]

    def create_model(self, org_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/v2/organizations/{org_id}/chats/models", payload)

    def update_model(self, org_id: str, model_id: str, payload: dict[str, Any]) -> None:
        self.request("PATCH", f"/api/v2/organizations/{org_id}/chats/models/{model_id}", payload)

    def list_custom_prices(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/experimental/ai/model-prices?source=custom")

    def upsert_prices(self, prices: list[dict[str, Any]]) -> None:
        self.request("POST", "/api/experimental/ai/model-prices", {"prices": prices})


@dataclass
class Live:
    org_id: str
    providers: dict[str, dict[str, Any]]  # managed providers by name
    models: list[dict[str, Any]]  # models under managed providers
    prices: dict[tuple[str, str], dict[str, Any]]  # custom prices by (type, model)


def is_managed_name(name: str) -> bool:
    return name.endswith(PROVIDER_SUFFIX)


def load_live(client: CoderClient) -> Live:
    org_id = client.default_org_id()
    providers = {p["name"]: p for p in client.list_providers() if is_managed_name(p["name"])}
    managed_ids = {p["id"] for p in providers.values()}
    models = [m for m in client.list_models(org_id) if m["ai_provider_id"] in managed_ids]
    prices = {(p["provider"], p["model"]): p for p in client.list_custom_prices()}
    return Live(org_id=org_id, providers=providers, models=models, prices=prices)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --with pytest pytest -q`
Expected: `41 passed`.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format scripts tests && uvx ruff check scripts tests
git add scripts/requesty-coder-sync.py tests
git commit -m "feat(coder): add the Requesty catalog fetch and Coder API client" -m "Assisted-by: AI"
```

---

### Task 5: Drift detection

**Files:**
- Modify: `scripts/requesty-coder-sync.py` (append the drift section)
- Create: `tests/requesty_sync/test_diff.py`

**Interfaces:**
- Consumes: `Desired`, `DesiredProvider`, `DesiredModel`, `Live`, `FREE_PROVIDER_NAME`, and the test doubles `FakeCoder`, `seed_in_sync`, `small_catalog`.
- Produces: the category constants (`MISSING_PROVIDER`, `MISSING_MODEL`, `PROVIDER_DRIFT`, `MODEL_DRIFT`, `PRICE_DRIFT`, `ORPHAN_MODEL`, `INFO`, `DRIFT_CATEGORIES`), `Finding(category, subject, detail="", provider="", action=None)`, `compute_diff(desired, live) -> list[Finding]`, `has_drift(findings) -> bool`, `summarize(findings) -> str`, and `format_report(findings, summary) -> str`.
- The `Finding.action` dictionaries, consumed by Task 6: `{"op": "create_provider", "provider": DesiredProvider}`, `{"op": "update_provider", "id", "name", "payload"}`, `{"op": "set_key", "id", "name"}`, `{"op": "create_model", "model": DesiredModel}`, `{"op": "update_model", "id", "payload", "move_to"}`, `{"op": "disable_model", "id"}`, and `{"op": "upsert_price", "price"}`.

- [ ] **Step 1: Write the failing tests**

`tests/requesty_sync/test_diff.py`:

```python
from collections import Counter

import pytest
from fakes import FakeCoder, make_entry, seed_in_sync, small_catalog


@pytest.fixture
def desired(sync):
    return sync.build_desired(small_catalog())


@pytest.fixture
def fake(desired):
    coder = FakeCoder()
    seed_in_sync(coder, desired)
    return coder


def diff(sync, desired, fake):
    return sync.compute_diff(desired, sync.load_live(fake))


def only(findings, category):
    return [f for f in findings if f.category == category]


def test_a_fresh_coder_reports_everything_missing(sync, desired):
    findings = diff(sync, desired, FakeCoder())
    counts = Counter(f.category for f in findings)
    assert counts[sync.MISSING_PROVIDER] == 3
    assert counts[sync.MISSING_MODEL] == 3
    assert counts[sync.PRICE_DRIFT] == 3


def test_a_synced_coder_has_no_findings(sync, desired, fake):
    assert diff(sync, desired, fake) == []


def test_provider_icon_drift(sync, desired, fake):
    provider = next(p for p in fake.providers.values() if p["name"] == "openai-via-requesty")
    provider["icon"] = "https://example.com/wrong.png"
    (finding,) = diff(sync, desired, fake)
    assert finding.category == sync.PROVIDER_DRIFT
    assert finding.action["payload"] == {"icon": desired.providers["openai-via-requesty"].icon}


def test_disabled_provider_and_missing_key(sync, desired, fake):
    provider = next(p for p in fake.providers.values() if p["name"] == "openai-via-requesty")
    provider["enabled"] = False
    provider["api_keys"] = []
    findings = only(diff(sync, desired, fake), sync.PROVIDER_DRIFT)
    assert {f.action["op"] for f in findings} == {"update_provider", "set_key"}


def test_model_context_drift(sync, desired, fake):
    model = next(m for m in fake.models.values() if m["model"] == "openai/gpt-x")
    model["context_limit"] = 1
    (finding,) = diff(sync, desired, fake)
    assert finding.category == sync.MODEL_DRIFT
    assert finding.action["payload"] == {"context_limit": 400_000}
    assert finding.action["move_to"] is None


def test_max_output_tokens_drift_keeps_operator_tuning(sync, desired, fake):
    model = next(m for m in fake.models.values() if m["model"] == "openai/gpt-x")
    model["model_config"] = {"temperature": 0.2, "max_output_tokens": 1}
    (finding,) = diff(sync, desired, fake)
    assert finding.action["payload"]["model_config"] == {
        "temperature": 0.2,
        "max_output_tokens": 128_000,
    }


def test_enabled_and_display_name_are_not_drift(sync, desired, fake):
    model = next(m for m in fake.models.values() if m["model"] == "openai/gpt-x")
    model["enabled"] = False
    model["display_name"] = "My favourite"
    assert diff(sync, desired, fake) == []


def test_a_model_that_becomes_free_moves_to_the_free_provider(sync, fake):
    catalog = small_catalog()
    catalog[1] = make_entry("openai/gpt-x", "openai", inp=0, out=0, ctx=400_000, maxout=128_000)
    desired = sync.build_desired(catalog)
    findings = diff(sync, desired, fake)
    (moved,) = only(findings, sync.MODEL_DRIFT)
    assert moved.action["move_to"] == "free-via-requesty"
    assert only(findings, sync.PRICE_DRIFT)[0].action["price"]["input_price"] == 0


def test_price_drift(sync, desired, fake):
    del fake.prices[("openai", "openai/gpt-x")]
    fake.prices[("anthropic", "anthropic/claude-a")]["output_price"] = 1
    absent, changed = only(diff(sync, desired, fake), sync.PRICE_DRIFT)
    assert changed.detail == "absent" or absent.detail == "absent"
    assert {absent.detail == "absent", changed.detail == "absent"} == {True, False}


def test_enabled_orphans_are_reported_and_disabled_ones_are_not(sync, desired, fake):
    provider_id = next(
        p["id"] for p in fake.providers.values() if p["name"] == "openai-via-requesty"
    )
    fake.create_model(
        "org-1", {"ai_provider_id": provider_id, "model": "openai/old", "enabled": True}
    )
    fake.create_model(
        "org-1", {"ai_provider_id": provider_id, "model": "openai/older", "enabled": False}
    )
    (orphan,) = only(diff(sync, desired, fake), sync.ORPHAN_MODEL)
    assert orphan.subject == "openai/old"
    assert orphan.action["op"] == "disable_model"


def test_unmanaged_providers_and_models_are_invisible(sync, desired, fake):
    other = fake.create_provider(
        {
            "name": "openai",
            "type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_keys": ["k"],
        }
    )
    fake.create_model("org-1", {"ai_provider_id": other["id"], "model": "gpt-5", "enabled": True})
    assert diff(sync, desired, fake) == []


def test_info_lines_are_not_drift(sync):
    desired = sync.build_desired(
        small_catalog() + [make_entry("poolside/lag", "poolside", inp=0, out=0, tools=False)]
    )
    coder = FakeCoder()
    seed_in_sync(coder, desired)
    findings = diff(sync, desired, coder)
    assert [f.category for f in findings] == [sync.INFO]
    assert not sync.has_drift(findings)
    assert sync.summarize(findings) == "in sync"


def test_summary_counts_and_flags_new_free_models(sync, desired):
    findings = diff(sync, desired, FakeCoder())
    assert sync.summarize(findings) == (
        "drift: 3 missing provider, 3 missing model, 3 price drift; 1 new free model"
    )


def test_report_groups_findings_by_category(sync, desired):
    findings = diff(sync, desired, FakeCoder())
    report = sync.format_report(findings, sync.summarize(findings))
    assert "MISSING_PROVIDER (3)" in report
    assert "  openai/gpt-x: create under openai-via-requesty" in report
    assert report.endswith(sync.summarize(findings))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest pytest tests/requesty_sync/test_diff.py -q`
Expected: FAIL with `AttributeError` on `compute_diff` or a category constant.

- [ ] **Step 3: Append the drift section**

```python
# ---- drift detection -------------------------------------------------------

MISSING_PROVIDER = "MISSING_PROVIDER"
MISSING_MODEL = "MISSING_MODEL"
PROVIDER_DRIFT = "PROVIDER_DRIFT"
MODEL_DRIFT = "MODEL_DRIFT"
PRICE_DRIFT = "PRICE_DRIFT"
ORPHAN_MODEL = "ORPHAN_MODEL"
INFO = "INFO"
DRIFT_CATEGORIES = (
    MISSING_PROVIDER,
    MISSING_MODEL,
    PROVIDER_DRIFT,
    MODEL_DRIFT,
    PRICE_DRIFT,
    ORPHAN_MODEL,
)


@dataclass
class Finding:
    category: str
    subject: str
    detail: str = ""
    provider: str = ""
    action: dict[str, Any] | None = None


def provider_changes(want: DesiredProvider, have: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for field in ("base_url", "icon", "display_name"):
        if have.get(field) != getattr(want, field):
            changes[field] = getattr(want, field)
    if have.get("enabled") is not True:
        changes["enabled"] = True
    return changes


def model_changes(
    want: DesiredModel, have: dict[str, Any], provider_names: dict[str, str]
) -> tuple[dict[str, Any], list[str], str | None]:
    """Returns the PATCH payload, human-readable notes, and a provider to move to."""
    payload: dict[str, Any] = {}
    notes: list[str] = []
    move_to = None
    current = provider_names.get(have["ai_provider_id"])
    if current != want.provider:
        move_to = want.provider
        notes.append(f"provider {current} -> {want.provider}")
    if have.get("context_limit") != want.context_limit:
        payload["context_limit"] = want.context_limit
        notes.append(f"context_limit {have.get('context_limit')} -> {want.context_limit}")
    config = have.get("model_config") or {}
    if (
        want.max_output_tokens is not None
        and config.get("max_output_tokens") != want.max_output_tokens
    ):
        # Merge, so tuning an operator set in the UI (temperature, ...) survives.
        payload["model_config"] = {**config, "max_output_tokens": want.max_output_tokens}
        notes.append(
            f"max_output_tokens {config.get('max_output_tokens')} -> {want.max_output_tokens}"
        )
    return payload, notes, move_to


def price_payload(want: DesiredModel) -> dict[str, Any]:
    input_price, output_price, cache_read, cache_write = want.prices
    return {
        "provider": want.provider_type,
        "model": want.model,
        "input_price": input_price,
        "output_price": output_price,
        "cache_read_price": cache_read,
        "cache_write_price": cache_write,
    }


def compute_diff(desired: Desired, live: Live) -> list[Finding]:
    findings: list[Finding] = []

    for name in sorted(desired.providers):
        want = desired.providers[name]
        have = live.providers.get(name)
        if have is None:
            detail = f"create ({want.type}, {want.base_url})"
            action = {"op": "create_provider", "provider": want}
            findings.append(Finding(MISSING_PROVIDER, name, detail, name, action))
            continue
        changes = provider_changes(want, have)
        if changes:
            detail = ", ".join(f"{k}: {have.get(k)!r} -> {v!r}" for k, v in changes.items())
            action = {"op": "update_provider", "id": have["id"], "name": name, "payload": changes}
            findings.append(Finding(PROVIDER_DRIFT, name, detail, name, action))
        if not have.get("api_keys"):
            action = {"op": "set_key", "id": have["id"], "name": name}
            findings.append(Finding(PROVIDER_DRIFT, name, "no API key configured", name, action))

    provider_names = {p["id"]: name for name, p in live.providers.items()}
    live_by_model = {m["model"]: m for m in live.models}

    for model_id in sorted(desired.models):
        want = desired.models[model_id]
        have = live_by_model.get(model_id)
        if have is None:
            detail = f"create under {want.provider}"
            action = {"op": "create_model", "model": want}
            findings.append(Finding(MISSING_MODEL, model_id, detail, want.provider, action))
            continue
        payload, notes, move_to = model_changes(want, have, provider_names)
        if notes:
            action = {
                "op": "update_model",
                "id": have["id"],
                "payload": payload,
                "move_to": move_to,
            }
            findings.append(Finding(MODEL_DRIFT, model_id, "; ".join(notes), want.provider, action))

    for have in sorted(live.models, key=lambda m: m["model"]):
        if have["model"] not in desired.models and have.get("enabled"):
            owner = provider_names.get(have["ai_provider_id"], "")
            action = {"op": "disable_model", "id": have["id"]}
            detail = f"no longer selected (under {owner})"
            findings.append(Finding(ORPHAN_MODEL, have["model"], detail, owner, action))

    for model_id in sorted(desired.models):
        want = desired.models[model_id]
        have = live.prices.get((want.provider_type, model_id))
        current = (
            None
            if have is None
            else (
                have.get("input_price"),
                have.get("output_price"),
                have.get("cache_read_price"),
                have.get("cache_write_price"),
            )
        )
        if current != want.prices:
            detail = "absent" if current is None else f"{current} -> {want.prices}"
            action = {"op": "upsert_price", "price": price_payload(want)}
            findings.append(Finding(PRICE_DRIFT, model_id, detail, want.provider, action))

    findings.extend(Finding(INFO, line) for line in desired.info)
    return findings


def has_drift(findings: list[Finding]) -> bool:
    return any(f.category != INFO for f in findings)


def summarize(findings: list[Finding]) -> str:
    drift = [f for f in findings if f.category != INFO]
    if not drift:
        return "in sync"
    counts = Counter(f.category for f in drift)
    parts = [f"{counts[c]} {c.lower().replace('_', ' ')}" for c in DRIFT_CATEGORIES if counts[c]]
    text = "drift: " + ", ".join(parts)
    new_free = sum(
        1 for f in drift if f.category == MISSING_MODEL and f.provider == FREE_PROVIDER_NAME
    )
    if new_free:
        text += f"; {new_free} new free model{'s' if new_free != 1 else ''}"
    return text


def format_report(findings: list[Finding], summary: str) -> str:
    lines: list[str] = []
    for category in (*DRIFT_CATEGORIES, INFO):
        group = [f for f in findings if f.category == category]
        if not group:
            continue
        lines.append(f"{category} ({len(group)})")
        lines.extend(f"  {f.subject}: {f.detail}" if f.detail else f"  {f.subject}" for f in group)
        lines.append("")
    lines.append(summary)
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --with pytest pytest -q`
Expected: `55 passed`.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format scripts tests && uvx ruff check scripts tests
git add scripts/requesty-coder-sync.py tests
git commit -m "feat(coder): detect drift between Requesty and Coder" -m "Assisted-by: AI"
```

---

### Task 6: Apply

**Files:**
- Modify: `scripts/requesty-coder-sync.py` (append the apply section)
- Create: `tests/requesty_sync/test_apply.py`

**Interfaces:**
- Consumes: `CoderClient` (or `FakeCoder`), `Live`, `Finding`, `SyncError`.
- Produces: `apply_changes(client, live, findings, api_key, rotate_key, log)`, which runs the findings' actions in order (providers, keys, models, orphan disables, then one price batch), raises `SyncError` before any call when a key is needed but missing, and never deletes.

- [ ] **Step 1: Write the failing tests**

`tests/requesty_sync/test_apply.py`:

```python
import pytest
from fakes import FakeCoder, make_entry, seed_in_sync, small_catalog

ALLOWED_OPS = {
    "create_provider",
    "update_provider",
    "create_model",
    "update_model",
    "upsert_prices",
}


@pytest.fixture
def desired(sync):
    return sync.build_desired(small_catalog())


def plan(sync, desired, fake):
    live = sync.load_live(fake)
    return live, sync.compute_diff(desired, live)


def test_apply_builds_everything_and_is_idempotent(sync, desired):
    fake = FakeCoder()
    live, findings = plan(sync, desired, fake)
    log = []
    sync.apply_changes(fake, live, findings, "secret-key", False, log.append)
    assert len(fake.providers) == 3
    assert len(fake.models) == 3
    assert all(len(p["api_keys"]) == 1 for p in fake.providers.values())
    assert sync.compute_diff(desired, sync.load_live(fake)) == []
    assert not any("secret-key" in line for line in log)


def test_apply_sets_prices_in_micro_dollars(sync, desired):
    fake = FakeCoder()
    live, findings = plan(sync, desired, fake)
    sync.apply_changes(fake, live, findings, "k", False, lambda _: None)
    price = fake.prices[("anthropic", "anthropic/claude-a")]
    assert (price["input_price"], price["output_price"]) == (3_000_000, 15_000_000)
    assert price["cache_read_price"] == 300_000
    assert price["cache_write_price"] is None
    free = fake.prices[("openai", "nvidia/free-y")]
    assert (free["input_price"], free["cache_write_price"]) == (0, 0)


def test_apply_needs_the_key_before_touching_anything(sync, desired):
    fake = FakeCoder()
    live, findings = plan(sync, desired, fake)
    with pytest.raises(sync.SyncError, match="REQUESTY_API_KEY"):
        sync.apply_changes(fake, live, findings, None, False, lambda _: None)
    assert fake.calls == []


def test_apply_without_provider_changes_needs_no_key(sync, desired):
    fake = FakeCoder()
    seed_in_sync(fake, desired)
    del fake.prices[("openai", "openai/gpt-x")]
    live, findings = plan(sync, desired, fake)
    sync.apply_changes(fake, live, findings, None, False, lambda _: None)
    assert ("openai", "openai/gpt-x") in fake.prices


def test_apply_moves_a_model_to_the_free_provider(sync):
    fake = FakeCoder()
    ids = seed_in_sync(fake, sync.build_desired(small_catalog()))
    catalog = small_catalog()
    catalog[1] = make_entry("openai/gpt-x", "openai", inp=0, out=0, ctx=400_000, maxout=128_000)
    desired = sync.build_desired(catalog)
    live, findings = plan(sync, desired, fake)
    sync.apply_changes(fake, live, findings, "k", False, lambda _: None)
    model = next(m for m in fake.models.values() if m["model"] == "openai/gpt-x")
    assert model["ai_provider_id"] == ids["free-via-requesty"]
    assert fake.prices[("openai", "openai/gpt-x")]["input_price"] == 0


def test_apply_never_deletes(sync, desired):
    fake = FakeCoder()
    ids = seed_in_sync(fake, desired)
    fake.create_model(
        "org-1",
        {"ai_provider_id": ids["openai-via-requesty"], "model": "openai/old", "enabled": True},
    )
    fake.calls.clear()
    live, findings = plan(sync, desired, fake)
    sync.apply_changes(fake, live, findings, "k", False, lambda _: None)
    assert len(fake.models) == 4
    assert {op for op, _ in fake.calls} <= ALLOWED_OPS
    orphan = next(m for m in fake.models.values() if m["model"] == "openai/old")
    assert orphan["enabled"] is False


def test_rotate_key_replaces_every_provider_key(sync, desired):
    fake = FakeCoder()
    seed_in_sync(fake, desired)
    before = {p["id"]: p["api_keys"][0]["id"] for p in fake.providers.values()}
    live, findings = plan(sync, desired, fake)
    sync.apply_changes(fake, live, findings, "new-key", True, lambda _: None)
    after = {p["id"]: p["api_keys"][0]["id"] for p in fake.providers.values()}
    assert all(after[i] != before[i] for i in before)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest pytest tests/requesty_sync/test_apply.py -q`
Expected: FAIL with `AttributeError: module 'requesty_coder_sync' has no attribute 'apply_changes'`.

- [ ] **Step 3: Append the apply section**

```python
# ---- apply -----------------------------------------------------------------


def apply_changes(
    client: CoderClient,
    live: Live,
    findings: list[Finding],
    api_key: str | None,
    rotate_key: bool,
    log: Callable[[str], None],
) -> None:
    """Runs the findings' actions in dependency order. Never deletes anything."""
    actions = [f.action for f in findings if f.action]

    def of(op: str) -> list[dict[str, Any]]:
        return [a for a in actions if a["op"] == op]

    needs_key = rotate_key or bool(of("create_provider")) or bool(of("set_key"))
    if needs_key and not api_key:
        raise SyncError("REQUESTY_API_KEY is required for the pending changes")

    provider_ids = {name: p["id"] for name, p in live.providers.items()}
    names_by_id = {p["id"]: name for name, p in live.providers.items()}

    for action in of("create_provider"):
        provider = action["provider"]
        created = client.create_provider(
            {
                "type": provider.type,
                "name": provider.name,
                "display_name": provider.display_name,
                "icon": provider.icon,
                "enabled": True,
                "base_url": provider.base_url,
                "api_keys": [api_key],
            }
        )
        provider_ids[provider.name] = created["id"]
        log(f"created provider {provider.name}")

    for action in of("update_provider"):
        client.update_provider(action["id"], action["payload"])
        log(f"updated provider {action['name']}")

    rekey = {a["id"] for a in of("set_key")}
    if rotate_key:
        rekey |= set(names_by_id)
    for provider_id in sorted(rekey):
        client.update_provider(provider_id, {"api_keys": [{"api_key": api_key}]})
        log(f"set API key on provider {names_by_id[provider_id]}")

    created_models = 0
    for action in of("create_model"):
        model = action["model"]
        payload: dict[str, Any] = {
            "ai_provider_id": provider_ids[model.provider],
            "model": model.model,
            "display_name": model.display_name,
            "enabled": True,
            "context_limit": model.context_limit,
        }
        if model.max_output_tokens:
            payload["model_config"] = {"max_output_tokens": model.max_output_tokens}
        client.create_model(live.org_id, payload)
        created_models += 1
    if created_models:
        log(f"created {created_models} model(s)")

    updated_models = 0
    for action in of("update_model"):
        payload = dict(action["payload"])
        if action["move_to"]:
            payload["ai_provider_id"] = provider_ids[action["move_to"]]
        client.update_model(live.org_id, action["id"], payload)
        updated_models += 1
    if updated_models:
        log(f"updated {updated_models} model(s)")

    disabled = of("disable_model")
    for action in disabled:
        client.update_model(live.org_id, action["id"], {"enabled": False})
    if disabled:
        log(f"disabled {len(disabled)} orphaned model(s)")

    prices = [a["price"] for a in of("upsert_price")]
    if prices:
        client.upsert_prices(prices)
        log(f"upserted {len(prices)} price(s)")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --with pytest pytest -q`
Expected: `62 passed`.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format scripts tests && uvx ruff check scripts tests
git add scripts/requesty-coder-sync.py tests
git commit -m "feat(coder): apply Requesty catalog changes to Coder without deleting" -m "Assisted-by: AI"
```

---

### Task 7: Command line, reporting, and heartbeat

**Files:**
- Modify: `scripts/requesty-coder-sync.py` (append the CLI section)
- Create: `tests/requesty_sync/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 3 to 6.
- Produces: `build_parser()`, `emit(findings, summary, as_json, out)`, `push_heartbeat(url, status, message)`, `default_catalog_loader(args)`, `run_apply(...)`, `run(...)`, and `main(argv=None, env=None, *, client_factory=CoderClient, load_catalog=default_catalog_loader, confirm=input, out=None) -> int`.
- The command line: `requesty-coder-sync.py check [--json] [--catalog-file PATH]` and `requesty-coder-sync.py apply [--yes] [--disable-orphans] [--rotate-key] [--catalog-file PATH]`, configured by `CODER_URL`, `CODER_SESSION_TOKEN`, `REQUESTY_API_KEY`, and `UPTIME_KUMA_PUSH_URL`.

- [ ] **Step 1: Write the failing tests**

`tests/requesty_sync/test_cli.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest pytest tests/requesty_sync/test_cli.py -q`
Expected: FAIL with `AttributeError: module 'requesty_coder_sync' has no attribute 'main'`.

- [ ] **Step 3: Append the CLI section**

```python
# ---- command line ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--catalog-file", help="read the Requesty catalog from this JSON file, not the network"
    )
    parser = argparse.ArgumentParser(
        prog="requesty-coder-sync.py", description="Sync the Requesty catalog into Coder Agents."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", parents=[common], help="report drift (read-only)")
    check.add_argument("--json", action="store_true", help="machine-readable output")
    apply = commands.add_parser("apply", parents=[common], help="make Coder match Requesty")
    apply.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    apply.add_argument(
        "--disable-orphans",
        action="store_true",
        help="disable models that Requesty no longer selects (never deletes)",
    )
    apply.add_argument(
        "--rotate-key",
        action="store_true",
        help="replace the API key on every managed provider with REQUESTY_API_KEY",
    )
    return parser


def emit(findings: list[Finding], summary: str, as_json: bool, out: TextIO) -> None:
    if as_json:
        payload = {
            "summary": summary,
            "drift": has_drift(findings),
            "findings": [
                {
                    "category": f.category,
                    "subject": f.subject,
                    "detail": f.detail,
                    "provider": f.provider,
                }
                for f in findings
            ],
        }
        json.dump(payload, out, indent=2)
        out.write("\n")
    else:
        out.write(format_report(findings, summary) + "\n")


def push_heartbeat(url: str, status: str, message: str) -> None:
    query = urllib.parse.urlencode({"status": status, "msg": message[:200], "ping": ""})
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(f"{url}{separator}{query}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except OSError as err:
        print(f"warning: heartbeat failed: {err}", file=sys.stderr)


def default_catalog_loader(args: argparse.Namespace) -> list[dict[str, Any]]:
    if not args.catalog_file:
        return fetch_catalog()
    with open(args.catalog_file, encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload["data"] if isinstance(payload, dict) else payload


def run_apply(
    args: argparse.Namespace,
    env: Any,
    client: CoderClient,
    desired: Desired,
    live: Live,
    findings: list[Finding],
    confirm: Callable[[str], str],
    out: TextIO,
) -> int:
    todo = [
        f for f in findings if f.action and (f.category != ORPHAN_MODEL or args.disable_orphans)
    ]
    if not todo and not args.rotate_key:
        out.write("Nothing to apply.\n")
        return EXIT_OK
    if not args.yes and confirm("Apply? [y/N] ").strip().lower() not in ("y", "yes"):
        out.write("Aborted.\n")
        return EXIT_DRIFT
    apply_changes(
        client,
        live,
        todo,
        env.get("REQUESTY_API_KEY"),
        args.rotate_key,
        lambda m: out.write(m + "\n"),
    )
    remaining = [
        f
        for f in compute_diff(desired, load_live(client))
        if f.category not in (INFO, ORPHAN_MODEL)
    ]
    if remaining:
        out.write("Drift remains after apply:\n")
        out.write(format_report(remaining, summarize(remaining)) + "\n")
        return EXIT_DRIFT
    out.write("Applied.\n")
    return EXIT_OK


def run(
    args: argparse.Namespace,
    env: Any,
    client_factory: Callable[[str, str], Any],
    load_catalog: Callable[[argparse.Namespace], list[dict[str, Any]]],
    confirm: Callable[[str], str],
    out: TextIO,
) -> tuple[int, str]:
    token = env.get("CODER_SESSION_TOKEN")
    if not token:
        raise SyncError("CODER_SESSION_TOKEN is not set")
    desired = build_desired(load_catalog(args))
    client = client_factory(env.get("CODER_URL", DEFAULT_CODER_URL), token)
    live = load_live(client)
    findings = compute_diff(desired, live)
    summary = summarize(findings)
    if args.command == "check":
        emit(findings, summary, args.json, out)
        return (EXIT_DRIFT if has_drift(findings) else EXIT_OK), summary
    emit(findings, summary, False, out)
    return run_apply(args, env, client, desired, live, findings, confirm, out), summary


def main(
    argv: list[str] | None = None,
    env: Any = None,
    *,
    client_factory: Callable[[str, str], Any] = CoderClient,
    load_catalog: Callable[[argparse.Namespace], list[dict[str, Any]]] = default_catalog_loader,
    confirm: Callable[[str], str] = input,
    out: TextIO | None = None,
) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    args = build_parser().parse_args(argv)
    try:
        code, summary = run(args, env, client_factory, load_catalog, confirm, out)
    except SyncError as err:
        print(f"error: {err}", file=sys.stderr)
        code, summary = EXIT_ERROR, f"error: {err}"
    push_url = env.get("UPTIME_KUMA_PUSH_URL")
    if args.command == "check" and push_url:
        push_heartbeat(push_url, "up" if code == EXIT_OK else "down", summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --with pytest pytest -q`
Expected: `77 passed`.

- [ ] **Step 5: Try it against the real catalog without touching Coder**

Run: `curl -fsS https://router.requesty.ai/v1/models > /tmp/catalog.json && python3 - <<'EOF'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("sync", "scripts/requesty-coder-sync.py")
sync = importlib.util.module_from_spec(spec); sys.modules["sync"] = sync; spec.loader.exec_module(sync)
desired = sync.build_desired(json.load(open("/tmp/catalog.json"))["data"])
print(len(desired.providers), "providers,", len(desired.models), "models")
print("free:", sorted(k for k, v in desired.models.items() if v.provider == sync.FREE_PROVIDER_NAME))
EOF`

Expected: roughly `24 providers, 189 models`, and a `free:` list of about nine models (exact numbers move as Requesty's catalog changes).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format scripts tests && uvx ruff check scripts tests
git add scripts/requesty-coder-sync.py tests
git commit -m "feat(coder): add the sync command line, report, and Uptime Kuma heartbeat" -m "Assisted-by: AI"
```

---

### Task 8: In-cluster CronJob, secrets, and README

**Files:**
- Create: `k8s/requesty-sync/kustomization.yaml`
- Create: `k8s/requesty-sync/requesty-sync-cronjob.yaml`
- Create: `k8s/requesty-sync/external-secret.yaml`
- Create: `k8s/requesty-sync/README.md`
- Create: `clusters/gandalf/requesty-sync.yaml`

**Interfaces:**
- Consumes: the script from Tasks 2 to 7, the check-only token role from Task 1, and the existing `bitwarden` ClusterSecretStore.
- Produces: a Secret `coder/requesty-sync-secrets` with keys `CODER_SESSION_TOKEN` and `UPTIME_KUMA_PUSH_URL`, and a CronJob `coder/requesty-sync`.

- [ ] **Step 1: Create the Uptime Kuma push monitor (operator, browser)**

In Uptime Kuma, add a monitor of type Push named `requesty-sync`, with a heartbeat interval of `90000` seconds (25 hours, so one missed daily run flips it to DOWN).
Copy the token part of its push URL.
Check the in-cluster URL format the backup job uses, without printing the token:

```bash
export KUBECONFIG=~/.kube/config
kubectl -n backup get secret uptime-kuma-push-urls -o json | jq -r '.data.UPTIME_KUMA_PUSH_BACKUP_URL | @base64d' | sed -E 's#(/api/push/).*#\1<token>#'
```

Expected: an in-cluster URL such as `http://uptime-kuma.monitoring.svc.cluster.local:3001/api/push/<token>`.
The URL to store for this monitor is that same host with the new monitor's token, and no query string.

- [ ] **Step 2: Store the two secrets in Bitwarden and BWS (operator, gandalf)**

Create a Bitwarden vault item named `Homelab Requesty Sync` with two custom fields: `coder-token` (the check-only token from Task 1 Step 6) and `uptime-kuma-push-url` (the URL from Step 1).
Then push both into BWS with the repo's script.

```bash
cd ~/git/nickvigilante/homelab
export BW_SESSION="$(bw unlock --raw)"; bw sync
./scripts/bws-migrate.sh <<'EOF'
requesty-sync-coder-token|Homelab Requesty Sync|coder-token
requesty-sync-uptime-push-url|Homelab Requesty Sync|uptime-kuma-push-url
EOF
```

Expected: the script reports both secrets created.

- [ ] **Step 3: Look up the BWS secret IDs and write the ExternalSecret (gandalf)**

```bash
cd ~/git/nickvigilante/homelab/.worktrees/requesty-coder-sync
export BW_SESSION="${BW_SESSION:-$(bw unlock --raw)}"; bw sync
export BWS_ACCESS_TOKEN="$(bw get item 'Homelab BWS Bootstrap Token' | jq -r '.notes')"
TOKEN_ID="$(bws secret list | jq -r '.[] | select(.key == "requesty-sync-coder-token") | .id')"
PUSH_ID="$(bws secret list | jq -r '.[] | select(.key == "requesty-sync-uptime-push-url") | .id')"
unset BWS_ACCESS_TOKEN BW_SESSION
echo "token id: $TOKEN_ID  push id: $PUSH_ID"
mkdir -p k8s/requesty-sync
```

Expected: two UUIDs, neither empty.
Write the file with them substituted in (the IDs are identifiers, not credentials, and carry the repo's `gitleaks:allow` marker):

```bash
cat > k8s/requesty-sync/external-secret.yaml <<EOF
# Credentials for the requesty-sync CronJob, sourced from BWS. Both keys are
# read via \`envFrom: secretRef:\`, so the key names are the env var names:
#
#   - CODER_SESSION_TOKEN: API token of the dedicated \`requesty-sync\` Coder
#     user. Check-only: it can list providers, models, and prices but the
#     CronJob never writes. The Requesty API key is deliberately NOT here;
#     \`check\` needs only the public catalog.
#   - UPTIME_KUMA_PUSH_URL: push URL (no query string) of the \`requesty-sync\`
#     Uptime Kuma push monitor.
#
# Source of truth is the Bitwarden item \`Homelab Requesty Sync\`; BWS holds
# the cluster-readable copies, same pattern as Homelab Restic Repository.
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: requesty-sync-secrets
  namespace: coder
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: bitwarden
    kind: ClusterSecretStore
  target:
    name: requesty-sync-secrets
    creationPolicy: Owner
  data:
    - secretKey: CODER_SESSION_TOKEN
      remoteRef:
        key: ${TOKEN_ID} # gitleaks:allow
    - secretKey: UPTIME_KUMA_PUSH_URL
      remoteRef:
        key: ${PUSH_ID} # gitleaks:allow
EOF
```

- [ ] **Step 4: Write the remaining manifests**

`k8s/requesty-sync/kustomization.yaml`:

```yaml
# The script ships to the pod as a ConfigMap generated from scripts/, so
# there is no custom image and the script has a single source. The
# generated name carries a content hash, so editing the script rolls the
# next CronJob run onto the new version. The cross-directory file reference
# needs LoadRestrictionsNone, which Flux uses by default.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - external-secret.yaml
  - requesty-sync-cronjob.yaml
configMapGenerator:
  - name: requesty-sync-script
    namespace: coder
    files:
      - requesty-coder-sync.py=../../scripts/requesty-coder-sync.py
```

`k8s/requesty-sync/requesty-sync-cronjob.yaml`:

```yaml
# Daily read-only drift check between the Requesty model catalog and the
# providers, models, and prices registered in Coder Agents.
#
# It only ever runs `check`: it lists what Coder holds and compares it with
# the live Requesty catalog. Fixing drift is a manual `apply` (see
# README.md), so nothing that affects spend changes without a human.
#
# Failure signal: the script pings the Uptime Kuma push monitor
# `requesty-sync` itself (UPTIME_KUMA_PUSH_URL), `up` when in sync and
# `down` with a one-line summary on drift or error. If the job dies before
# it can ping, the monitor goes DOWN when its heartbeat interval expires.
#
# Manual trigger (e.g. to test):
#   kubectl -n coder create job --from=cronjob/requesty-sync test-sync-$(date +%s)
apiVersion: batch/v1
kind: CronJob
metadata:
  name: requesty-sync
  namespace: coder
spec:
  schedule: "0 6 * * *"
  timeZone: America/New_York
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 0
      activeDeadlineSeconds: 300
      template:
        spec:
          restartPolicy: Never
          nodeSelector:
            kubernetes.io/hostname: gandalf
          containers:
            - name: sync
              image: python:3.14-alpine
              command:
                - python
                - /scripts/requesty-coder-sync.py
                - check
              env:
                # In-cluster service DNS: pods cannot resolve *.vigihome.net
                # the way LAN clients do (see CLAUDE.md "DNS pattern").
                - name: CODER_URL
                  value: http://coder.coder.svc.cluster.local
                - name: PYTHONDONTWRITEBYTECODE
                  value: "1"
              envFrom:
                - secretRef:
                    name: requesty-sync-secrets
              resources:
                requests:
                  cpu: 50m
                  memory: 64Mi
                limits:
                  memory: 256Mi
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                runAsNonRoot: true
                runAsUser: 65534
                capabilities:
                  drop: [ALL]
              volumeMounts:
                - name: script
                  mountPath: /scripts
                  readOnly: true
          volumes:
            - name: script
              configMap:
                name: requesty-sync-script
```

`clusters/gandalf/requesty-sync.yaml`:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: requesty-sync
  namespace: flux-system
spec:
  interval: 10m
  path: ./k8s/requesty-sync
  # Flux owns everything in this directory, so pruning is safe.
  prune: true
  wait: true
  timeout: 3m
  sourceRef:
    kind: GitRepository
    name: flux-system
  # The ExternalSecret needs ESO and the ClusterSecretStore (infrastructure),
  # and the CronJob lives in the coder namespace (coder).
  dependsOn:
    - name: infrastructure
    - name: coder
```

- [ ] **Step 5: Write the README**

`k8s/requesty-sync/README.md`:

```markdown
# requesty-sync

Keeps Coder Agents in step with the Requesty model catalog.

The tool is `scripts/requesty-coder-sync.py`, and its design is in `docs/superpowers/specs/2026-09-18-requesty-coder-sync-design.md`.
A daily CronJob runs its read-only `check`, and you run `apply` by hand when it reports drift.

## What it manages

- One Coder AI provider per model vendor, named `<vendor>-via-requesty`, all pointing at Requesty's router, each with the vendor's own logo as shipped by Requesty.
- One `free-via-requesty` provider that holds every free model, so you can see when a free option exists.
- One Agents model per canonical model, with its context limit and output limit.
- One custom per-token price per model, in micro-dollars per million tokens.

The tool touches only providers named `*-via-requesty` and the models under them.
It never deletes anything.
Models that Requesty lists with a retirement date are skipped.

## Pieces

| Piece | Where |
| --- | --- |
| Tool | `scripts/requesty-coder-sync.py` |
| CronJob (daily at 06:00 ET, `check` only) | `requesty-sync-cronjob.yaml` |
| Secrets (`CODER_SESSION_TOKEN`, `UPTIME_KUMA_PUSH_URL`) | `external-secret.yaml`, sourced from BWS |
| Alerts | The Uptime Kuma push monitor `requesty-sync` |

The CronJob's Coder token belongs to the dedicated `requesty-sync` user, whose role is the least privileged one that can read AI configuration (recorded in the smoke test results in the design spec).
The CronJob never holds the Requesty API key, because `check` only needs the public catalog.

## The daily check

The CronJob runs `check`, which compares the live Requesty catalog with Coder and pings the Uptime Kuma monitor itself.
It pings `up` when everything matches, and `down` with a one-line summary on drift or on an error.
If the job dies before it can ping, the monitor goes DOWN when its 25-hour heartbeat interval expires.

To run it by hand:

```bash
kubectl -n coder create job --from=cronjob/requesty-sync test-sync-$(date +%s)
kubectl -n coder logs -f job/<the job name printed above>
```

## Fixing drift

Run this on **gandalf**, from a fresh checkout of this repo.
`apply` is the only thing that writes to Coder, and it asks before it does.

```bash
cd ~/git/nickvigilante/homelab && git checkout main && git pull
export CODER_URL=https://coder.vigihome.net
read -rs -p 'Coder admin token: ' CODER_SESSION_TOKEN; echo; export CODER_SESSION_TOKEN
read -rs -p 'Requesty API key: ' REQUESTY_API_KEY; echo; export REQUESTY_API_KEY
python3 scripts/requesty-coder-sync.py check
python3 scripts/requesty-coder-sync.py apply
```

Flags for `apply`:

- `--yes` skips the confirmation prompt.
- `--disable-orphans` disables models that Requesty no longer selects, which clears their alert.
- `--rotate-key` replaces the API key on every managed provider with `REQUESTY_API_KEY`.

## Reading a report

| Category | Meaning | Fixed by |
| --- | --- | --- |
| `MISSING_PROVIDER`, `MISSING_MODEL` | In the desired state and absent in Coder | `apply` |
| `PROVIDER_DRIFT` | Wrong `base_url`, `icon`, or `display_name`, disabled, or no API key | `apply` (a key needs `REQUESTY_API_KEY`) |
| `MODEL_DRIFT` | Context or output limit differs, or the model belongs under a different provider (for example it became free) | `apply` |
| `PRICE_DRIFT` | Custom price absent or different | `apply` |
| `ORPHAN_MODEL` | Enabled in Coder but no longer selected (retired, or its best host changed) | `apply --disable-orphans` |
| `INFO` | Skipped on purpose: free models without tool calling, models with a retirement date, and non-plain fallbacks | Nothing to fix |

Disabling a model in the UI is not drift, and neither is renaming it, so you can hide models you do not want.

## Troubleshooting

- **Exit code 2** means an error, and the message names the cause: a missing token, an unreachable Requesty catalog, a catalog with fewer than 100 eligible models (refused so a truncated response cannot look like mass deprecation), or a Coder API failure.
- **A Coder API failure with status 404 or 405 after a Coder upgrade** usually means an experimental endpoint moved, because model prices live under `/api/experimental`.
- **The monitor is DOWN but the job succeeded** means drift, so read the job log.
- **Icons do not load** means Requesty moved its logo files, so refresh the `LAB_LOGOS` table in the script from `https://www.requesty.ai/provider_logos/v2/`.
```

If Task 1 found that nothing below Owner can read AI configuration, replace the "Token" bullet's role text with a sentence saying the `requesty-sync` user is Owner-scoped and why.

- [ ] **Step 6: Validate the manifests locally**

```bash
yamlfmt -conf .yamlfmt -lint k8s/requesty-sync clusters/gandalf/requesty-sync.yaml && echo "yamlfmt ok"
yamllint -c .github/yamllint.yml k8s/requesty-sync clusters/gandalf/requesty-sync.yaml && echo "yamllint ok"
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s/requesty-sync | grep -E '^kind:|name: requesty-sync-script'
kubeconform -summary -strict -schema-location default -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' k8s/requesty-sync/requesty-sync-cronjob.yaml k8s/requesty-sync/external-secret.yaml
```

Expected: `yamlfmt ok` and `yamllint ok`, three kinds (`ConfigMap`, `CronJob`, `ExternalSecret`) with the ConfigMap name carrying a hash suffix that also appears in the CronJob's volume, and `Valid: 2, Invalid: 0`.
If `kubeconform` is not installed locally, CI runs it, so run `pre-commit run --all-files` instead and rely on CI for that check.

- [ ] **Step 7: Commit**

```bash
git add k8s/requesty-sync clusters/gandalf/requesty-sync.yaml
git commit -m "feat(coder): run the Requesty drift check daily in the cluster" -m "Assisted-by: AI"
```

---

### Task 9: Roll out and verify **(operator)**

**Files:**
- No new files.

**Interfaces:**
- Consumes: everything above.
- Produces: Coder populated with the catalog, a green daily check, and a PR that Flux applies after merge.

- [ ] **Step 1: Dry run against the live Coder (operator, gandalf)**

```bash
cd ~/git/nickvigilante/homelab/.worktrees/requesty-coder-sync
export CODER_URL=https://coder.vigihome.net
read -rs -p 'Coder admin token: ' CODER_SESSION_TOKEN; echo; export CODER_SESSION_TOKEN
python3 scripts/requesty-coder-sync.py check; echo "exit=$?"
```

Expected: `exit=1`, a report listing about 24 `MISSING_PROVIDER`, about 189 `MISSING_MODEL`, and about 189 `PRICE_DRIFT` entries, an INFO section, and a summary line ending in `new free models`.
Read the INFO lines: they list what was skipped and why (retiring models, free models without tool calling, and any non-plain fallback).

- [ ] **Step 2: Apply (operator, gandalf)**

```bash
read -rs -p 'Requesty API key: ' REQUESTY_API_KEY; echo; export REQUESTY_API_KEY
python3 scripts/requesty-coder-sync.py apply
```

The tool prints the same report, asks `Apply? [y/N]`, and on `y` prints one line per stage.
Expected: `created provider …` for each provider, `created N model(s)`, `upserted N price(s)`, and finally `Applied.`
If it prints `Drift remains after apply:`, read what remains and stop: it means Coder did not persist something the tool sent.

- [ ] **Step 3: Verify idempotency (operator, gandalf)**

```bash
python3 scripts/requesty-coder-sync.py check; echo "exit=$?"
```

Expected: `exit=0` and `in sync`.

- [ ] **Step 4: Verify in the UI (operator, browser)**

Open the AI settings Models page.
Expected: one provider group per vendor plus "Free models via Requesty", each with the Requesty logo for that vendor, and about 189 models in total.
Start a chat with one paid model and one free model to confirm both work.

- [ ] **Step 5: Open the PR (gandalf)**

```bash
git push -u origin requesty-coder-sync
gh pr create --base main --head requesty-coder-sync --title "feat(coder): sync Requesty models into Coder Agents" --body-file - <<'EOF'
## Summary

Adds a tool that registers the Requesty catalog in Coder Agents as one provider per vendor, with per-token prices, plus a dedicated free-model provider.
A daily CronJob runs its read-only drift check and reports to Uptime Kuma.

## Before merge

- [x] No secrets in the diff — the ExternalSecret carries BWS identifiers only, marked `gitleaks:allow`.
- [ ] New persistent dirs — n/a, nothing persistent.
- [ ] Host-level changes — n/a.
- [ ] SPOF impact — n/a, no new login path.
- [ ] DNS — n/a.
- [x] README walks through the setup end to end (`k8s/requesty-sync/README.md`).

## Notes

- `apply` was run by hand from gandalf before this PR, so Coder is already populated.
- The CronJob only ever runs `check`, and it holds a check-only Coder token, never the Requesty key.

## Test plan

- [x] `pytest -q` passes (77 tests).
- [x] `check` against live Coder returns exit 0 after `apply`.
- [ ] After merge, `flux reconcile kustomization requesty-sync --with-source` reports Ready.
- [ ] A manual job run pings the Uptime Kuma monitor `up`.

---
🤖 Built with AI assistance.
EOF
```

Expected: a PR URL.
Wait for the `lint` workflow to pass before merging.

- [ ] **Step 6: After merge, reconcile and trigger a manual run (operator, gandalf)**

```bash
export KUBECONFIG=~/.kube/config
flux reconcile kustomization requesty-sync --with-source
kubectl -n coder get externalsecret requesty-sync-secrets
kubectl -n coder create job --from=cronjob/requesty-sync test-sync-$(date +%s)
kubectl -n coder logs -f job/$(kubectl -n coder get jobs -o name | grep test-sync | tail -1 | cut -d/ -f2)
```

Expected: the ExternalSecret shows `SecretSynced`, and the job log ends with `in sync`.

- [ ] **Step 7: Verify the heartbeat and the failure path (operator, browser and gandalf)**

The Uptime Kuma monitor `requesty-sync` should show UP after the manual run.
To see the DOWN path once, disable one managed model in the UI, then run `python3 scripts/requesty-coder-sync.py check` and confirm the model is **not** reported (disabling is not drift), then change one model's context limit in the UI, rerun `check`, and confirm a `MODEL_DRIFT` line and exit 1.
Run `apply` to restore it, and confirm `check` returns to exit 0.

- [ ] **Step 8: Done**

Confirm the daily run at 06:00 America/New_York the next morning by checking the monitor's history.
