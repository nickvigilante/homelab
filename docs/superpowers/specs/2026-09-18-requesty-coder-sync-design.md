# Requesty to Coder model sync — design

## Goal

Register the Requesty model catalog in Coder Agents so that developers can pick models grouped by model vendor, with per-token prices, and get alerted when Requesty and Coder drift apart.

Concretely:

- One Coder AI provider per model vendor, named "\<Vendor> via Requesty", each pointing at Requesty's OpenAI-compatible router.
- One dedicated "★ All free models via Requesty" provider that holds every free model, so the operator can see at a glance when a free option exists.
- Every provider shows the vendor's own logo as shipped by Requesty, not just the Requesty logo.
- Every model carries its context limit, output limit, and custom per-token prices.
- A read-only check runs daily in the cluster and reports drift to Uptime Kuma.

## Non-goals

- Managing any Coder provider or model that this tool did not create.
- Deleting models automatically.
- Managing Requesty itself (keys, routing policies, budgets).
- Modelling cache-write costs, because Requesty's catalog does not publish them.
- Setting a default model, because that stays a UI decision.

## Verified background (2026-09-18)

### Coder

- The deployment runs Coder v2.37.0 as a Flux HelmRelease in the `coder` namespace, and telemetry is enabled.
- These routes exist on the live server: `/api/v2/ai/providers`, `/api/v2/organizations/{org}/chats/models`, and `/api/experimental/ai/model-prices`.
- Providers are unique by `name` only, which must match `^[a-z0-9]+(-[a-z0-9]+)*$`, so many providers of the same type may share one base URL.
- The provider `type` decides the wire protocol, the built-in default icon, and the price-book key.
- The Models page groups and filters by provider row, labelled with the provider's `display_name`.
- The provider `icon` field is a free string that the server stores as given, and Coder's default CSP is `img-src 'self' https: data:`, so any `https://` image loads.
- Model prices are keyed by provider type and model string, in micro-dollars per million tokens, so `$3.00` per million tokens is `3000000`.
- The `openai-compat` type is deliberately excluded from pricing, so the tool never uses it.
- The price endpoint validates the whole batch before writing, and each entry must carry all four price keys, with `null` meaning unknown and `0` meaning free.
- The `coderd` Terraform provider has no price resource, and its provider and model resources are experimental, which is why this design uses a script instead of OpenTofu.

### Requesty catalog

- `GET https://router.requesty.ai/v1/models` answers unauthenticated and returned 692 entries, all with `api == chat`.
- 660 entries support tool calling, which Coder Agents needs.
- Those 660 entries collapse to 195 distinct `(model_lab, model_canonical_name)` pairs, because the ID prefix is the host rather than the vendor and most models appear under several hosts and regions.
- Regional variants (`…@eu-west-3`) are priced about 10% higher than the plain entry.
- Prices are USD per token: `input_price`, `output_price`, `cached_price`, with tiered `pricing[]` for long context.
- There is no cache-write price in the catalog.
- 12 entries are free (`input_price` and `output_price` both zero), of which 9 support tool calling, and 4 of those 9 also have a paid twin under another host.
- 63 entries carry a `retires` Unix timestamp (61 of them tool-capable), for example `poolside/laguna-m.1` on 2026-09-21, several Gemini 2.5 entries on 2026-10-16, and several OpenAI models on 2026-12-10, while the other 629 entries have no such field.
- Only two canonical names appear under more than one lab today: `kimi-k2.6` (`moonshot` and `moonshotai`) and `glm-5.2` (`zai` and `deepinfra`).
- Requesty publishes provider logos at `https://www.requesty.ai/provider_logos/v2/<name>.png`, and 15 of the 23 relevant labs match directly.

## Design

### Managed set

The tool owns providers named `<lab>-via-requesty` and `free-via-requesty`, and the models under them.
Everything else in Coder is ignored: never read for drift and never modified.

### Selection rules

The tool turns the catalog into a desired state in this order.

1. **Eligible entries** are those with `api == chat`, `supports_tool_calling == true`, and no `retires` date.
   Any retirement date excludes an entry, however distant, because the operator does not want models that are already scheduled to disappear.
   A canonical model still registers through any host that is not retiring.
2. **Free pool.** Every eligible entry with `input_price == 0` and `output_price == 0` goes to the free pool, whether or not a paid twin exists elsewhere.
   Within the pool, duplicates of the same `(lab, canonical)` collapse to one entry using the host preference below.
3. **Paid pool.** The remaining eligible entries are grouped by `(lab, canonical)` after applying the lab alias table (`moonshotai` to `moonshot`, `qwen` to `alibaba`).
   If a canonical name still appears under several labs, only the lab with the most catalog entries for it keeps it, with ties broken alphabetically.
   This handles `glm-5.2` (keep `zai`, drop `deepinfra`) and `kimi-k2.6`.
4. **Host preference** decides which entry represents a group.
   A _plain_ entry has neither an `@region` suffix nor a `:variant` service-tier suffix (`:flex`, `:priority`), because variants share the canonical name of the plain model but are priced differently.
   The order is: the first-party plain entry (the ID prefix equals the lab, for example `anthropic/claude-sonnet-4-5`), then the cheapest plain entry, then the cheapest entry overall, with the lexicographically smallest ID breaking ties.
   A small host alias table maps a host prefix to its lab where the two differ, today `minimaxi` to `minimax`.
5. **Skipped, reported as INFO.** Free entries without tool calling are not registered, because Agents cannot use them.
   Today these are `poolside/laguna-m.1`, `poolside/laguna-xs.2`, and `nvidia/nemotron-3.5-content-safety`.
   Models skipped for a retirement date are reported once per canonical model with the earliest date, but only when no other host keeps that model registered.
   A model that gains a `retires` date after it was registered stops being selected, so it shows up as an orphan and is disabled by `--disable-orphans`.
   When the only entry left for a model is not plain, it is still registered and reported as INFO, so the operator can see it.
   Today that is `openai/o3:flex` (a slower service tier, because the plain entry retires) and `azure/gpt-5.2-codex@eastus2` (regional-only).

The result today is roughly 200 models across about 23 vendor providers and the free provider.
Exact counts come from the implementation and its tests.

### Providers

| Field          | Value                                                                                                                                                         |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`         | `<lab>-via-requesty`, or `free-via-requesty`                                                                                                                  |
| `display_name` | "\<Vendor> via Requesty" from a small proper-name table, and "★ All free models via Requesty" (the star sorts it first in the model picker)                   |
| `base_url`     | `https://router.requesty.ai/v1`, except `https://router.requesty.ai` for the `anthropic` type, because Coder's Anthropic client appends `/v1/messages` itself |
| `type`         | `anthropic` for Anthropic, `google` for Google, and `openai` for every other vendor and free                                                                  |
| `api_keys`     | The Requesty key, set on creation or with `--rotate-key`                                                                                                      |
| `enabled`      | `true`                                                                                                                                                        |
| `icon`         | See below                                                                                                                                                     |

**Icons.**
Each vendor provider uses the vendor's own logo shipped by Requesty, `https://www.requesty.ai/provider_logos/v2/<logo>.png`.
The mapping is a static table in the script, snapshotted from Requesty's logo list.

- Direct matches: `alibaba`, `anthropic`, `deepinfra`, `deepseek`, `google`, `meta`, `mistral`, `moonshot`, `nvidia`, `openai`, `sakana`, `thinkingmachines`, `xai`, `xiaomi`, `zai`.
- Aliases: `minimax` uses `minimaxi`, `moonshotai` uses `moonshot`, and `qwen` uses `alibaba`.
- No Requesty logo exists for `bytedance`, `gryphe`, `inclusionai`, `kwaipilot`, `nousresearch`, `stepfun`, and `tencent`, so those fall back to `https://www.requesty.ai/Requesty_logo.svg`.
- The free provider also uses the Requesty logo, because it belongs to no single vendor.
- A `type: openai` provider with no icon would show the OpenAI logo, so the tool always sets an icon explicitly.

### Models

One Agents model per selected entry, in the default organization.

| Field                            | Source                                                              |
| -------------------------------- | ------------------------------------------------------------------- |
| `model`                          | The Requesty ID verbatim, for example `anthropic/claude-sonnet-4-5` |
| `display_name`                   | The canonical model name                                            |
| `context_limit`                  | `context_window`                                                    |
| `model_config.max_output_tokens` | `max_output_tokens`                                                 |
| `enabled`                        | `true`                                                              |

Everything else stays at Coder's defaults, and `is_default` is never set.

### Prices

One custom price per selected entry, keyed by the provider type and the model string.

- Micro-dollars per million tokens is `round(per_token × 1e12)`.
- `cache_read_price` comes from `cached_price`, and `cache_write_price` is `null`, so cache writes are uncosted.
- Free entries get an explicit `0` on all four prices.
- Tiered long-context pricing uses the base tier only.

### Interface

One stdlib-only Python script, `scripts/requesty-coder-sync.py`, with three subcommands.

- `check` is read-only and prints a drift report, and `check --limited` does the same using only what a narrow member-level token can read (see Limited check mode).
- `apply` computes the same diff, prints it, asks "Apply?", and then makes the changes.
- `verify` runs one real Coder Agents chat per registered model and reports which models fail (see Verify).
- `--yes` skips the prompt, `--json` gives machine-readable output, and `--disable-orphans` sets `enabled=false` on orphaned models.
- Configuration is by environment: `CODER_URL`, `CODER_SESSION_TOKEN`, and `REQUESTY_API_KEY` for `apply` only.
  When `REQUESTY_API_KEY` is unset and `BW_SESSION` is exported, `apply` reads the key from the Bitwarden item `Requesty`, field `Main API key`, and only when a change needs it.

### Mismatch categories

| Category                            | Meaning                                                                                                                                                                                                                                                                                                                                                              |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MISSING_PROVIDER`, `MISSING_MODEL` | In the desired state and absent in Coder                                                                                                                                                                                                                                                                                                                             |
| `PROVIDER_DRIFT`                    | Wrong `base_url`, `icon`, or `display_name`, disabled, or no API key set (key values are masked)                                                                                                                                                                                                                                                                     |
| `MODEL_DRIFT`                       | `context_limit`, `max_output_tokens`, or owning provider differs, including moves into or out of the free provider                                                                                                                                                                                                                                                   |
| `PRICE_DRIFT`                       | Custom price absent or different                                                                                                                                                                                                                                                                                                                                     |
| `ORPHAN_MODEL`                      | Enabled in Coder but no longer selected (retired, or the host preference changed); orphans that are already disabled are not reported, so the alert clears once `--disable-orphans` has run                                                                                                                                                                          |
| `INFO`                              | Not drift, worth knowing: models skipped on purpose (free models without tool calling, models with a Requesty retirement date), non-plain fallbacks, and models that are selected but disabled in Coder (for example after `--disable-orphans`, when the model later returns), which the operator may have disabled by hand and so is never re-enabled automatically |

A host change for a model appears as a `MISSING_MODEL` and an `ORPHAN_MODEL` reported together.
Drift on the free provider gets its own summary line, so a newly free model is easy to spot.

### Exit codes and guards

- Exit 0 means no drift, and exit 1 means drift.
- Exit 2 means an error such as an auth failure, an API failure, or an unreachable catalog.
- A catalog with fewer than 100 eligible chat models aborts with exit 2, so a truncated response cannot look like mass deprecation.

### Apply rules

1. Providers first, then models, then one batch price upsert.
2. It never deletes anything, and orphans are only flagged unless `--disable-orphans` is given.
3. The API key is set only on provider creation or with `--rotate-key`, and it is never compared or logged.
4. It is idempotent, so a second run shows an empty diff.

### Deployment

A new directory `k8s/requesty-sync/` in the `coder` namespace, with its own Flux Kustomization at `clusters/gandalf/requesty-sync.yaml` that depends on `infrastructure` and `coder`.

- **CronJob** at 06:00 America/New_York daily, with a pinned `python:3-alpine` image, a gandalf node selector, and `concurrencyPolicy: Forbid`, running `check --limited`.
- **Script delivery** through a kustomize `configMapGenerator` from `scripts/requesty-coder-sync.py`, so there is no custom image and the script has one source.
- **Coder URL** is the in-cluster service DNS name, confirmed at planning time.
- **Secrets:** a narrow Coder token for the `requesty-sync` service account, delivered by an ExternalSecret from BWS.
  The service account has no role (a plain member, and service accounts take no license seat), and the token is limited to the scopes `chat_model_config:read` and `organization:read` with a 1-year lifetime, so it can read the model list and nothing else.
  The CronJob never holds write access or the Requesty key, and `apply` and `verify` use the operator's own admin token by hand.
  A Todoist reminder covers rotating the token before it expires.
- **Alerting** follows the restic pattern: an Uptime Kuma push monitor `requesty-sync`, whose push URL reaches the pod as `UPTIME_KUMA_PUSH_URL` from the same ExternalSecret as the token.
  The script itself pings the monitor after `check`: exit 0 pings `up`, and drift or an error pings `down` with the summary line.

### Testing

- Unit tests with pytest for selection, host preference, alias merging, cross-lab collapse, free routing, icon mapping, and price conversion, against a trimmed snapshot of the real catalog.
- Flow tests for `check` and `apply` against a stub HTTP server, including idempotency and a test asserting that `apply` never deletes.
- A `ruff` pre-commit hook and an always-run pytest job in `lint.yml`, not path-filtered, so a required check can never be left waiting.

### Rollout

1. **Smoke test.** Create one provider per type (`anthropic`, `google`, `openai`) with one model each by hand, price them, and run an Agents chat.
   This settles the base URL and suffix for each type, whether slash-containing model IDs are accepted, whether the Requesty PNG icons load and look right on both themes, and which Coder role can read providers, models, and prices.
   If the `anthropic` type fails against Requesty, the fallback is the `openai` type for Anthropic as well.
   The script isolates the two outcomes in `NATIVE_TYPES` (which labs use a native type) and `BASE_URL_BY_TYPE` (a per-type base URL override, needed if a type appends its own `/v1`), so either fallback is a one-line change.
2. Build the tool test-first in a worktree.
3. Run `check` against the live Coder (expecting everything missing), run `apply` by hand on gandalf, then run `check` again and expect exit 0.
4. Deploy the CronJob, ExternalSecret, and Uptime Kuma monitor, trigger a manual job, and verify the heartbeat.
5. Write `k8s/requesty-sync/README.md`.
   No persistent directories are added, so the backup CronJob is untouched.

### Risks

- Protocol and base-URL compatibility per provider type, covered by the smoke test.
- Coder v2.37.0 accepts no scope that reads AI providers or model prices, and no role below Owner can read them, so the daily check runs in limited mode: it cannot see prices or a provider's `base_url`.
  Price drift is checked by hand, by running the full `check` with the operator's own token.
- Coder's experimental endpoints may change on upgrade, and the daily check fails loudly if they do.
- Requesty could move its logo URLs, which would silently break icons until the static table is refreshed.
- About 200 models is a long Models page.
- Cache-write costs are uncounted, which under-reports Anthropic prompt-caching spend.

## Decisions taken by default, open to veto

1. Free models without tool calling are skipped and reported, not registered.
2. A free model is kept in the free provider even when a paid twin stays in its vendor's provider, so the operator can see both.
3. Vendors with no Requesty logo fall back to the Requesty logo.
4. Requesty's PNG logos are used for all vendors, including Anthropic, OpenAI, and Google, in place of Coder's built-in icons.
5. Cross-lab duplicates collapse to the lab with the most entries.
6. The check runs daily rather than weekly.
7. Any `retires` date excludes an entry, even one months away.
   Today that removes nine canonical models entirely (for example `gpt-5-pro`, `o3-pro`, `deepseek-chat`, and `deepseek-reasoner`), while popular models such as `gpt-5-mini` and Gemini 2.5 Pro stay registered through hosts that are not retiring.
   If that proves too strict, a horizon (for example, skip only entries retiring within 60 days) is a small change to one function.

## Smoke test results

Run on gandalf on 2026-09-18 with `scripts/requesty-smoke-test.sh`, against the live Coder v2.37.0+8a148a9 and Requesty.

- Direct OpenAI-shape calls worked for `openai/gpt-4.1-nano` and `google/gemini-3.1-flash-lite`.
- The direct Anthropic-shape call returned 200 at `/v1/messages` and 404 at `/messages`.
- A Coder `anthropic` provider with base URL `https://router.requesty.ai` (no `/v1`) chatted successfully, so `BASE_URL_BY_TYPE` now carries that override.
- Coder `openai` and `google` providers with base URL `https://router.requesty.ai/v1` chatted successfully with first-party models.
- The third-party-hosted `bedrock/claude-haiku-4-5` chatted successfully on the `anthropic` type.
- The third-party-hosted `vertex/claude-haiku-4-5` chatted successfully on the `anthropic` type, and `vertex/gemini-2.5-flash-lite` on the `google` type, so third-party hosts work on both native types.
- `nebius/google/gemma-3-27b-it` answers correctly when called directly on Requesty, with and without tools, but fails in a Coder chat on the `google` type with "Conversation roles must alternate user/assistant/user/assistant/..." (HTTP 400).
  The upstream chat template of Gemma 3 rejects the system prompt that Coder Agents always sends, so this is a model and host limitation, not a provider-type problem, and it means Requesty's `supports_tool_calling` flag does not guarantee that a model works in Agents.
  The `google` type stays.
  It fails identically on the `openai` type (an HTTP 400 from the host, surfaced by Coder as "OpenAI returned an unexpected error"), which confirms that the provider type is not involved.
- The Requesty logos rendered on both themes.
- Real Agents chats can be driven through the API: `POST /api/v2/chats` with a `model_config_id` and `client_type: "api"` ran to completion for seven models without a workspace, and a failing model reports its upstream status code and message in the chat's `last_error`.
  This is the basis for a `verify` command that tests every registered model through the real Agents path.
- With the smoke prices of $1 and $5 per million tokens, each chat cost between 2,175 and 6,233 micro-dollars, so a chat is a few thousand tokens, and models with no registered price report a cost of 0.
- The logo check was skipped on the last run; the logos rendered correctly on the earlier run, and the operator reports they have always displayed correctly.
- Coder returned the price list as an array, returned null prices as null, kept the key and `enabled` on a single-field provider PATCH, and replaced (did not append) the key set on an `api_keys` PATCH.
- Coder accepted an output limit above the context limit, and accepted two models with the same display name under different providers.
  The model picker keys options by model ID and groups them by provider name, so same-named twins show as separate entries.
- The default service-account role could read the model list but got 403 for AI providers and model prices, and in Coder's role tests only the Owner role can read either.
  Coder v2.37.0 refuses to issue a token with `ai_provider:read` ("invalid or unsupported API key scope"), because its curated list of external scopes has `chat_model_config:read` and `organization:read` but no AI provider or price scope, so a scoped token cannot read them.
  A token limited to `chat_model_config:read` and `organization:read` on a role-less service account can read the model list with each model's `model_config`, plus a descriptor per provider, and is otherwise empty or denied (0 users, 0 templates, 0 workspaces, 403 for audit logs, AI providers, and prices).
  The provider descriptor carries `id`, `type`, `display_name`, `icon`, `enabled`, `has_api_key`, and `available`, but not the provider's `name` or `base_url`.

## Limited check mode

`check --limited` exists because the daily CronJob cannot hold a credential that reads AI providers or prices (see Risks).
It reads only `GET /api/v2/organizations` (to find the default organization) and `GET /api/v2/organizations/{org}/chats/models`, whose response carries the models and one descriptor per provider.

- **Managed providers** are the descriptors whose `display_name` ends with " via Requesty", because a descriptor has no `name`.
  A desired provider is matched to a descriptor by its exact display name, so a provider renamed by hand looks missing and its models look missing too.
  A recognized descriptor with no desired provider is kept, named `slugify(display name without the suffix) + "-via-requesty"`, so its enabled models show up as orphans.
- **Managed models** are the models whose `ai_provider_id` is a managed descriptor.
- **Compared:** the same model fields as the full check (`context_limit`, `model_config.max_output_tokens`, and the owning provider), plus a provider's `display_name` (its identity), `icon`, and `enabled`, and whether it has an API key.
- **Not compared:** prices and a provider's `base_url`, which a member-level token cannot read.
  Every limited run reports the INFO line "prices and provider base URLs are not checked in limited mode".
- Categories, the summary, exit codes, and the Uptime Kuma heartbeat are the same as the full check.
- `apply` is unaffected and always uses the full read.
- The mode is chosen by the flag alone, never by detecting a permission error, so a broken token cannot silently downgrade the check.

## Verify

`verify` tests every registered model through the real Coder Agents path, using the chats API, so it catches failures that catalog metadata cannot, such as a host that rejects the system prompt.
It needs the operator's own unscoped token, because `chat:create` is not an external scope, and it runs by hand after `apply`, never in the CronJob, because each chat costs money.

- For each managed, enabled model it creates a chat with `POST /api/v2/chats` (`organization_id`, `model_config_id`, `client_type: "api"`, a `probe` label, and one text part asking for the single word ok), polls `GET /api/v2/chats/{id}`, and archives the chat with `PATCH /api/v2/chats/{id}` whether or not it worked.
- A chat **passes** when its status is `waiting` and an assistant message exists, or its status is `requires_action` (the model answered with a tool call).
- A chat **fails** when its status is `error` and `last_error.retryable` is false; the report shows the error message, detail, upstream status code, and provider.
- A chat is **inconclusive** when it errors with `retryable: true` (a rate limit or a 5xx) or does not finish within the timeout, so a transient problem never marks a good model as broken.
- The report shows each result as it arrives, the counts, and the total cost from `GET /api/v2/chats/{id}/cost`.
- Options: `--provider NAME` and `--model ID` narrow the set, `--limit N` caps it, `--concurrency N` (default 4) and `--timeout SECONDS` (default 180) tune it.
- `--disable-failures` sets `enabled=false` on the models that failed (not the inconclusive ones), after showing them and asking for confirmation unless `--yes` is given.
  `apply` never re-enables a disabled model, and `check` reports it as "selected but disabled in Coder".
- Exit codes: 0 when every model passed, 1 when any failed or was inconclusive, and 2 for an error.

## Models that fail in practice

The catalog lists models that fail when a chat is really sent.
`verify` found about one in five, and `scripts/requesty-probe.py` (which calls Requesty without Coder) separated the causes.

- `EXCLUDED_MODELS` names each unusable model with the observed error as its reason.
  An excluded model is not registered, its canonical model falls back to the next-best host, and `apply --disable-orphans` disables the copy already in Coder.
  `check` reports each one as an `excluded (...)` INFO line.
- `MAX_OUTPUT_OVERRIDES` pins output limits the hosts named in their own errors, where the catalog value was missing or too large.
- `LAB_OVERRIDES` files an entry that Requesty labels with a host under its real lab.
- Models with `supports_image_generation` are skipped by rule.
- Canonical names are compared case-blind, because Requesty spells one model differently across hosts.

The causes seen, in order of how often they occurred:

- The router returns 404, 403, 410 or 500 for a listed model, so the catalog is ahead of the hosts.
- The host's chat template rejects the several consecutive system messages that Coder v2.37.0 sends (Qwen 3.5 and 3.8, Gemma 3).
  This is Coder's request shape, tracked upstream in coder/coder#27176, so those entries are retested after a Coder upgrade.
- An Agents chat never completes although every direct request passes, seen twice each for two models.
- Requests that carry tools or a large `max_tokens` are rejected by the host.

Not yet confirmed: whether Coder's default `store: true` is what three Novita models reject.
The probe's `--extra-shapes` sends that request.
