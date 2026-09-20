# Limited Check Mode and Verify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `check --limited` (a daily check that works with a narrow member-level Coder token) and `verify` (one real Coder Agents chat per registered model) to `scripts/requesty-coder-sync.py`.

**Architecture:** Both features extend the existing single-file tool and reuse its `CoderClient`, `Live`, `compute_diff`, and `main` structure.
Limited mode builds a `Live` from the model list response alone.
Verify drives the chats API with a thread pool and reports pass, fail, or inconclusive per model.

**Tech Stack:** Python 3.14 (stdlib only), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-18-requesty-coder-sync-design.md` (sections "Limited check mode" and "Verify", plus the revised token and risk statements).

## Global Constraints

- The script stays stdlib-only Python that runs unchanged on the local Python 3 and in `python:3.14-alpine`.
- Work in the worktree `/home/nickv/git/nickvigilante/homelab/.worktrees/requesty-coder-sync` on branch `requesty-coder-sync`, never push, and never `cd` to the main checkout.
- Commit messages end with the trailer `Assisted-by: AI` (two `-m` flags), never carry a `Co-Authored-By` line, and never name an AI model or vendor.
- Exit codes are 0 for success, 1 for drift (or, for verify, a failed or inconclusive model), and 2 for an error.
- Nothing in `check` may write to Coder, and nothing in `apply` or `verify` may delete anything.
- Secrets (tokens, keys, push URLs) are never printed or logged, including in error messages.
- Tests run with `uv run --no-project --with pytest pytest -q` (this form creates no lock file), and lint with `uvx ruff format scripts tests && uvx ruff check scripts tests`.
- The suite has 87 passing tests before this plan starts, and every task must leave the whole suite green with pristine output.
- Delete any stray untracked `uv.lock` before committing, and make sure `git status --porcelain` prints nothing when you report.
- Follow TDD: write the tests first, run them and record the RED failure, then write the code and run them GREEN.
- Existing test support lives in `tests/requesty_sync/`: `conftest.py` (fixtures `sync`, `stub`, and an autouse `relaxed_guard`), and `fakes.py` (`make_entry`, `small_catalog`, `FakeCoder`, `seed_in_sync`, `start_stub`).
  Read them before writing tests.
- The Markdown rule for any docs you touch is one sentence per line.

______________________________________________________________________

### Task 1: Limited check mode

**Files:**

- Modify: `scripts/requesty-coder-sync.py`
- Modify: `tests/requesty_sync/fakes.py`
- Create: `tests/requesty_sync/test_limited.py`

**Interfaces:**

- Produces: `CoderClient.list_models_response(org_id) -> dict`, `Live.limited: bool`, `load_live_limited(client, desired) -> Live`, `LIMITED_SUFFIX`, the `check --limited` flag, and `FakeCoder.list_models_response`.

**Background you need.**
The daily CronJob has only a narrow Coder token (scopes `chat_model_config:read` and `organization:read`, on an account with no role).
That token can call `GET /api/v2/organizations` and `GET /api/v2/organizations/{org}/chats/models`, and gets 403 for `/api/v2/ai/providers` and `/api/experimental/ai/model-prices`.
The models response is `{"models": [...], "providers": [...], "unsupported_providers": [...]}`.
Each model row has `id`, `ai_provider_id`, `model`, `display_name`, `enabled`, `context_limit`, and `model_config` (which carries `max_output_tokens` when set).
Each provider descriptor has exactly `id`, `type`, `display_name`, `icon`, `enabled`, `has_api_key`, `has_effective_api_key`, `has_user_api_key`, `allow_user_api_key`, and `available`; it has **no** `name` and **no** `base_url`.
Read `load_live`, `Live`, `compute_diff`, `provider_changes`, `run`, `build_parser`, and `CoderClient` in the script before you start.

**Required behavior** (from the spec section "Limited check mode"):

1. `CoderClient.list_models_response(org_id)` returns the whole models response dict and raises `ApiError` (like the existing `_get_list` guard) when the body is not a JSON object whose `models` and `providers` are lists.
   Rewrite `list_models` to call it and return `["models"]`, keeping its behavior and its existing tests unchanged.
2. `Live` gains a field `limited: bool = False`.
3. Add `LIMITED_SUFFIX = " via Requesty"`.
4. Add `load_live_limited(client, desired) -> Live`:
   - `org_id = client.default_org_id()`, then `client.list_models_response(org_id)`.
   - Managed descriptors are those whose `display_name` ends with `LIMITED_SUFFIX`.
   - Name each one: if its `display_name` equals a desired provider's display name, use that desired provider's name; otherwise use `slugify(display_name.removesuffix(LIMITED_SUFFIX)) + PROVIDER_SUFFIX`.
   - Build `providers` as a dict keyed by that name, with values shaped like the full-mode provider dicts that `compute_diff` reads: `id`, `display_name`, `icon`, `enabled`, and `api_keys` (a one-element list when `has_api_key` is true, otherwise an empty list; only its truthiness is used), plus `type`.
   - `models` are the model rows whose `ai_provider_id` is a managed descriptor's `id`.
   - `prices` is an empty dict, and the returned `Live` has `limited=True`.
   - Wrap wrong-shaped responses (`KeyError`, `TypeError`, `AttributeError`) into `SyncError("unexpected response shape from Coder: ...")`, the way `load_live` does.
5. `compute_diff` (and `provider_changes`, via a new keyword argument `limited: bool = False`) must, when `live.limited` is true:
   - not compare `base_url`;
   - skip the whole price section, and append exactly one `Finding(INFO, "prices and provider base URLs are not checked in limited mode")`.
     Everything else, including `MISSING_PROVIDER`, `PROVIDER_DRIFT` (icon, display name, enabled, missing key), `MODEL_DRIFT` (`context_limit`, `max_output_tokens`, owning provider), `MISSING_MODEL`, `ORPHAN_MODEL`, and the "selected but disabled" INFO line, behaves as in full mode.
     Full mode (`live.limited` false) must behave exactly as before.
6. CLI: `check` gets a `--limited` flag (not `apply`, which stays full).
   In `run`, `check --limited` calls `load_live_limited(client, desired)` and every other path keeps calling `load_live(client)`.
   `--limited` is chosen only by the flag; never fall back to it on a 403.
7. In `FakeCoder` (tests/requesty_sync/fakes.py) add `list_models_response(org_id)` returning `{"models": [...], "providers": [descriptor, ...], "unsupported_providers": []}`, where each descriptor is built from the fake's stored providers with exactly the descriptor fields listed above (`has_api_key` is `bool(api_keys)`, `available` is true) and **no `name` or `base_url`**.
   Give `FakeCoder` a `reads` list that records the names of the read methods called (`list_providers`, `list_custom_prices`, `list_models_response`), so tests can assert which endpoints a mode touched.

**Tests to write** (`tests/requesty_sync/test_limited.py`), each as its own test function, using `FakeCoder`, `seed_in_sync`, `small_catalog`, and `sync.main(...)` the way `test_cli.py` does:

- A fully seeded fake has no drift under `check --limited`; exit code 0; the report contains the "not checked in limited mode" INFO line; the summary is "in sync".

- `check --limited` never calls `list_providers` or `list_custom_prices` (assert on `fake.reads`), and full `check` still does.

- Providers and models of unrelated providers (a fake provider named `openai` with display name "OpenAI") are ignored.

- A missing model gives `MISSING_MODEL`; a wrong `context_limit` gives `MODEL_DRIFT`; a wrong `max_output_tokens` inside `model_config` gives `MODEL_DRIFT`.

- A wrong icon, a disabled provider, and a provider with no API key each give `PROVIDER_DRIFT`.

- A wrong `base_url` on a provider is **not** drift under `--limited` but **is** drift in full mode.

- A missing custom price is **not** drift under `--limited` but **is** in full mode.

- A provider whose display name was changed by hand (so it no longer matches) yields `MISSING_PROVIDER`, and its models yield `MISSING_MODEL`.

- A recognized descriptor with no desired provider (an extra fake provider with display name "Retired Lab via Requesty" holding an enabled model) yields an `ORPHAN_MODEL` whose provider name is `retired-lab-via-requesty`.

- A model that is selected but disabled produces the existing "selected but disabled in Coder" INFO line, and no drift, in limited mode.

- `check --limited --json` includes the limited-mode INFO finding and `"drift": false` for an in-sync fake.

- `apply --limited` is rejected by argparse with exit code 2 (`SystemExit`).

- `load_live_limited` turns a wrong-shaped response into `SyncError` (use the `stub` HTTP server fixture with a `CoderClient`, or a small fake client object).

- `list_models_response` raises `ApiError` for a JSON list body and for an object with a non-list `models` (use the `stub` fixture, like `test_client.py`).

- [ ] **Step 1: Read the existing code and tests** named above.

- [ ] **Step 2: Write the tests and the `FakeCoder` additions, run them, and record the RED output.**

- [ ] **Step 3: Implement the behavior**, then run the new tests GREEN.

- [ ] **Step 4: Run the whole suite and lint.**

- [ ] **Step 5: Commit** as one commit with subject `feat(coder): add a limited check mode for narrow tokens`, a short body explaining why (a member-level token cannot read AI providers or prices), and the trailer.

______________________________________________________________________

### Task 2: The verify command

**Files:**

- Modify: `scripts/requesty-coder-sync.py`
- Modify: `tests/requesty_sync/fakes.py`
- Create: `tests/requesty_sync/test_verify.py`

**Interfaces:**

- Consumes: `CoderClient`, `Live`, `load_live`, `SyncError`, `ApiError`, and `main`.
- Produces: `CoderClient.create_chat`, `get_chat`, `get_chat_messages`, `get_chat_cost`, `archive_chat`; `VerifyResult`; `verify_model(...)`; `select_models_to_verify(...)`; `run_verify(...)`; the `verify` subcommand; and chat support in `FakeCoder`.

**Background you need.**
The Coder chats API (v2.37.0) works like this, verified against a live server:

- `POST /api/v2/chats` with `{"organization_id", "model_config_id", "client_type": "api", "labels": {...}, "content": [{"type": "text", "text": "..."}]}` returns 201 and a chat object with an `id` and a `status`.
  No workspace is needed.
- `GET /api/v2/chats/{id}` returns the chat.
  `status` is one of `running`, `waiting`, `error`, `requires_action`, `interrupting`.
  When `status` is `error`, `last_error` is an object with `message`, optional `detail`, optional `kind`, optional `provider`, `retryable` (bool), and optional `status_code` (the upstream HTTP status).
- `GET /api/v2/chats/{id}/messages` returns `{"messages": [{"role": "user"|"assistant"|"system"|"tool", ...}], "queued_messages": [], "has_more": false}`.
- `GET /api/v2/chats/{id}/cost` returns `{"chat_id", "total_cost_micros", "request_count", "unpriced_request_count"}`.
- `PATCH /api/v2/chats/{id}` with `{"archived": true}` archives a chat.
  A model that the upstream host rejects shows up as `status: error`, for example `last_error = {"message": "Google returned an unexpected error.", "detail": "Conversation roles must alternate user/assistant/user/assistant/...", "status_code": 400, "provider": "google", "retryable": false}`.
  Read `CoderClient`, `Live`, `load_live`, `run`, `main`, `build_parser`, and `apply_changes` in the script first, and follow their style (dataclasses, injected clients so tests can use `FakeCoder`, `SyncError` for fatal problems).

**Required behavior** (from the spec section "Verify"):

1. `CoderClient` methods: `create_chat(payload) -> dict` (POST `/api/v2/chats`), `get_chat(chat_id) -> dict`, `get_chat_messages(chat_id) -> dict`, `get_chat_cost(chat_id) -> dict`, and `archive_chat(chat_id) -> None` (PATCH with `{"archived": true}`).
2. `@dataclass VerifyResult` with `model: str`, `provider: str`, `config_id: str`, `outcome: str` (`"ok"`, `"failed"`, or `"inconclusive"`), `detail: str = ""`, and `cost_micros: int = 0`.
3. `verify_model(client, org_id, model_row, provider_name, timeout, poll, sleep=time.sleep, clock=time.monotonic) -> VerifyResult`:
   - Create the chat with the payload above, a `labels` value `{"probe": "requesty-sync-verify"}`, and one text part `"Reply with the single word ok."`.
   - If creating the chat raises `ApiError`, return `inconclusive` with the error text as `detail`.
   - Poll `get_chat` every `poll` seconds (using the injected `sleep` and `clock`) until `timeout` seconds have passed:
     - `status == "error"`: if `last_error.retryable` is true, return `inconclusive`; otherwise return `failed`, with `detail` formatted as `"<message>[ - <detail>] (upstream HTTP <status_code or ?>, <provider or ?>)"`.
     - `status == "requires_action"`: `ok`.
     - `status == "waiting"`: fetch the messages, and if at least one has `role == "assistant"` return `ok`; otherwise keep polling.
     - anything else (`running`, `interrupting`): keep polling.
   - On `ok`, read the cost and set `cost_micros` from `total_cost_micros` (0 if absent or the call fails).
   - On timeout return `inconclusive` with `detail` `"no reply within <timeout>s (last status: <status>)"`.
   - Always try to archive the chat in a `finally` block, and ignore any error from archiving (it must never change the outcome or raise).
4. `select_models_to_verify(live, provider=None, model=None, limit=None) -> list[tuple[dict, str]]`:
   - Uses `live.models` (already limited to managed providers) and `live.providers` to map `ai_provider_id` to a provider name.
   - Keeps only enabled models, optionally narrowed to a provider name (`--provider`) and an exact model string (`--model`), sorted by `(provider name, model)`, then cut to `limit`.
5. `run_verify(args, env, client_factory, confirm, out) -> int`:
   - Requires `CODER_SESSION_TOKEN` (raise `SyncError` if missing) and uses `CODER_URL` like `run` does; it does **not** fetch the Requesty catalog or need `REQUESTY_API_KEY`.
   - Loads the live state with the existing full `load_live(client)`, selects the models, and runs `verify_model` for each with a `concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency)`.
   - Prints one line per model as each finishes (in the main thread, using `as_completed`): `OK`, `FAIL`, or `INCONCLUSIVE`, then the model and provider, then the detail; for OK include the cost.
   - Ends with a summary line `verified N models: X ok, Y failed, Z inconclusive; total cost $D` where `D` is the summed `cost_micros` divided by 1,000,000 with 4 decimals.
   - With `--disable-failures` and at least one `failed` result: list those models, ask `confirm("Disable N failed model(s)? [y/N] ")` unless `--yes`, then call `client.update_model(org_id, config_id, {"enabled": False})` for each failed model (never the inconclusive ones), and print how many were disabled.
   - Returns 0 when every result is `ok`, and 1 otherwise.
   - If no models are selected, print `No models to verify.` and return 0.
6. CLI: a `verify` subcommand with `--provider NAME`, `--model ID`, `--limit N` (int), `--concurrency N` (int, default 4, at least 1), `--timeout SECONDS` (float, default 180), `--poll SECONDS` (float, default 2), `--disable-failures`, and `--yes`.
   `verify` does not take `--catalog-file`.
   In `run`, handle `verify` before any catalog loading and return `run_verify(...)` (with a summary string for `main`).
   `main` must not send an Uptime Kuma heartbeat for `verify` (it already only pings for `check`; keep it that way), and its catch-all still maps an unexpected exception to exit 2.
7. `FakeCoder` chat support: `create_chat(payload)` returns `{"id": ..., "status": "running"}` and stores the chat with its `model_config_id`; `get_chat(id)` returns `running` on the first poll and the final state on the next, chosen per model by a dict `fake.chat_behaviors` mapping the model string to one of `"ok"` (default), `"error"` (non-retryable, with a `last_error` like the example above), `"retryable_error"` (same with `retryable: true` and status code 429), `"tool"` (`requires_action`), or `"never"` (stays `running`); `get_chat_messages(id)` returns an assistant message for an ok chat; `get_chat_cost(id)` returns `{"total_cost_micros": fake.chat_cost}` with `chat_cost` defaulting to 1500; `archive_chat(id)` marks the chat archived and records `("archive_chat", id)` in `fake.calls`.
   The fake must be safe to call from several threads (use a lock around id generation and the chats dict).
   Look up the model string for a chat through the fake's stored models.

**Tests to write** (`tests/requesty_sync/test_verify.py`), each its own function, using `FakeCoder` seeded with `seed_in_sync` and `sync.main(["verify", ...], env, client_factory=..., confirm=..., out=...)` the way `test_cli.py` does, with `--poll 0` so tests are fast:

- All models ok: exit 0, the summary line counts them, the total cost is `n * 1500` micro-dollars formatted in dollars, and every chat was archived.

- A non-retryable error is reported as `FAIL` with the message, detail, `HTTP 400`, and provider in the line; exit 1; the chat is still archived.

- A retryable error is reported as `INCONCLUSIVE`, not `FAIL`, and is **not** disabled by `--disable-failures`.

- `requires_action` counts as ok.

- A chat that never finishes is `INCONCLUSIVE` after a short `--timeout` (for example 0.2 with `--poll 0`).

- `--disable-failures --yes` disables only the failed model (its `enabled` becomes false in the fake), and the other models stay enabled; without `--yes` a "n" answer disables nothing and a "y" answer disables the failed model.

- After `--disable-failures`, `check` (full mode, against the same catalog) reports no drift and the INFO line "selected but disabled in Coder: <model>", and `apply --yes` does not re-enable it.

- `--provider`, `--model`, and `--limit` narrow the set; disabled models and models of unmanaged providers are skipped; `No models to verify.` and exit 0 when nothing is selected.

- Creating a chat that raises `ApiError` (make a subclass of `FakeCoder` whose `create_chat` raises) gives `INCONCLUSIVE` with the error text.

- `--concurrency 4` with eight models returns eight results.

- A missing `CODER_SESSION_TOKEN` exits 2, and `verify` never sends a heartbeat (set `UPTIME_KUMA_PUSH_URL` to the `stub` fixture URL and assert it received no requests).

- `verify_model` unit test with injected `sleep` and `clock` for the timeout path, so it does not wait in real time.

- `CoderClient` methods use the right verbs and paths (use the `stub` HTTP server fixture, like `test_client.py`), including `archive_chat` sending `{"archived": true}` and tolerating an empty or 204 response.

- [ ] **Step 1: Read the existing code and tests** named above.

- [ ] **Step 2: Write the tests and the `FakeCoder` chat support, run them, and record the RED output.**

- [ ] **Step 3: Implement the behavior**, then run the new tests GREEN.

- [ ] **Step 4: Run the whole suite and lint.**

- [ ] **Step 5: Commit** as one commit with subject `feat(coder): add verify, which tests each model through a real Agents chat`, a short body, and the trailer.
