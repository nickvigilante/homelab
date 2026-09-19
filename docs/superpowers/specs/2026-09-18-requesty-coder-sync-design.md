# Requesty to Coder model sync — design

## Goal

Register the Requesty model catalog in Coder Agents so that developers can pick models grouped by model vendor, with per-token prices, and get alerted when Requesty and Coder drift apart.

Concretely:

- One Coder AI provider per model vendor, named "\<Vendor> via Requesty", each pointing at Requesty's OpenAI-compatible router.
- One dedicated "Free models via Requesty" provider that holds every free model, so the operator can see at a glance when a free option exists.
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
- Only two canonical names appear under more than one lab today: `kimi-k2.6` (`moonshot` and `moonshotai`) and `glm-5.2` (`zai` and `deepinfra`).
- Requesty publishes provider logos at `https://www.requesty.ai/provider_logos/v2/<name>.png`, and 15 of the 23 relevant labs match directly.

## Design

### Managed set

The tool owns providers named `<lab>-via-requesty` and `free-via-requesty`, and the models under them.
Everything else in Coder is ignored: never read for drift and never modified.

### Selection rules

The tool turns the catalog into a desired state in this order.

1. **Eligible entries** are those with `api == chat` and `supports_tool_calling == true`.
1. **Free pool.** Every eligible entry with `input_price == 0` and `output_price == 0` goes to the free pool, whether or not a paid twin exists elsewhere.
   Within the pool, duplicates of the same `(lab, canonical)` collapse to one entry using the host preference below.
1. **Paid pool.** The remaining eligible entries are grouped by `(lab, canonical)` after applying the lab alias table (`moonshotai` to `moonshot`, `qwen` to `alibaba`).
   If a canonical name still appears under several labs, only the lab with the most catalog entries for it keeps it, with ties broken alphabetically.
   This handles `glm-5.2` (keep `zai`, drop `deepinfra`) and `kimi-k2.6`.
1. **Host preference** decides which entry represents a group:
   first the first-party host without a region suffix (the ID prefix equals the lab, for example `anthropic/claude-sonnet-4-5`), then the cheapest entry without a region suffix, then the cheapest entry overall, with the lexicographically smallest ID breaking ties.
1. **Skipped, reported as INFO.** Free entries without tool calling are not registered, because Agents cannot use them.
   Today these are `poolside/laguna-m.1`, `poolside/laguna-xs.2`, and `nvidia/nemotron-3.5-content-safety`.

The result today is roughly 200 models across about 23 vendor providers and the free provider.
Exact counts come from the implementation and its tests.

### Providers

| Field          | Value                                                                                          |
| -------------- | ---------------------------------------------------------------------------------------------- |
| `name`         | `<lab>-via-requesty`, or `free-via-requesty`                                                   |
| `display_name` | "\<Vendor> via Requesty" from a small proper-name table, and "Free models via Requesty"        |
| `base_url`     | `https://router.requesty.ai/v1`                                                                |
| `type`         | `anthropic` for Anthropic, `google` for Google, and `openai` for every other vendor and free   |
| `api_keys`     | The Requesty key, set on creation or with `--rotate-key`                                       |
| `enabled`      | `true`                                                                                         |
| `icon`         | See below                                                                                      |

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

| Field                            | Source                                                 |
| -------------------------------- | ------------------------------------------------------ |
| `model`                          | The Requesty ID verbatim, for example `anthropic/claude-sonnet-4-5` |
| `display_name`                   | The canonical model name                               |
| `context_limit`                  | `context_window`                                       |
| `model_config.max_output_tokens` | `max_output_tokens`                                    |
| `enabled`                        | `true`                                                 |

Everything else stays at Coder's defaults, and `is_default` is never set.

### Prices

One custom price per selected entry, keyed by the provider type and the model string.

- Micro-dollars per million tokens is `round(per_token × 1e12)`.
- `cache_read_price` comes from `cached_price`, and `cache_write_price` is `null`, so cache writes are uncosted.
- Free entries get an explicit `0` on all four prices.
- Tiered long-context pricing uses the base tier only.

### Interface

One stdlib-only Python script, `scripts/requesty-coder-sync.py`, with two subcommands.

- `check` is read-only and prints a drift report.
- `apply` computes the same diff, prints it, asks "Apply?", and then makes the changes.
- `--yes` skips the prompt, `--json` gives machine-readable output, and `--disable-orphans` sets `enabled=false` on orphaned models.
- Configuration is by environment: `CODER_URL`, `CODER_SESSION_TOKEN`, and `REQUESTY_API_KEY` for `apply` only.

### Mismatch categories

| Category                                | Meaning                                                                                              |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `MISSING_PROVIDER`, `MISSING_MODEL`     | In the desired state and absent in Coder                                                             |
| `PROVIDER_DRIFT`                        | Wrong `base_url`, `icon`, or `display_name`, disabled, or no API key set (key values are masked)     |
| `MODEL_DRIFT`                           | `context_limit`, `max_output_tokens`, or owning provider differs, including moves into or out of the free provider |
| `PRICE_DRIFT`                           | Custom price absent or different                                                                     |
| `ORPHAN_MODEL`                          | In Coder but no longer selected (retired, or the host preference changed)                            |
| `INFO`                                  | Free models skipped for lacking tool calling                                                         |

A host change for a model appears as a `MISSING_MODEL` and an `ORPHAN_MODEL` reported together.
Drift on the free provider gets its own summary line, so a newly free model is easy to spot.

### Exit codes and guards

- Exit 0 means no drift, and exit 1 means drift.
- Exit 2 means an error such as an auth failure, an API failure, or an unreachable catalog.
- A catalog with fewer than 100 eligible chat models aborts with exit 2, so a truncated response cannot look like mass deprecation.

### Apply rules

1. Providers first, then models, then one batch price upsert.
1. It never deletes anything, and orphans are only flagged unless `--disable-orphans` is given.
1. The API key is set only on provider creation or with `--rotate-key`, and it is never compared or logged.
1. It is idempotent, so a second run shows an empty diff.

### Deployment

A new directory `k8s/requesty-sync/` in the `coder` namespace, with its own Flux Kustomization at `clusters/gandalf/requesty-sync.yaml` that depends on `infrastructure` and `coder`.

- **CronJob** at 06:00 America/New_York daily, with a pinned `python:3-alpine` image, a gandalf node selector, and `concurrencyPolicy: Forbid`, running `check --json`.
- **Script delivery** through a kustomize `configMapGenerator` from `scripts/requesty-coder-sync.py`, so there is no custom image and the script has one source.
- **Coder URL** is the in-cluster service DNS name, confirmed at planning time.
- **Secrets:** a check-only Coder token for a dedicated `requesty-sync` user, delivered by an ExternalSecret from BWS.
  The CronJob never holds write access or the Requesty key, and `apply` uses the operator's own admin token by hand.
- **Alerting** follows the restic pattern: an Uptime Kuma push monitor `requesty-sync`, with its URL added to the `uptime-kuma-push-urls` Secret.
  Exit 0 pings `up`, and drift or an error pings `down` with the summary line.

### Testing

- Unit tests with pytest for selection, host preference, alias merging, cross-lab collapse, free routing, icon mapping, and price conversion, against a trimmed snapshot of the real catalog.
- Flow tests for `check` and `apply` against a stub HTTP server, including idempotency and a test asserting that `apply` never deletes.
- A `ruff` pre-commit hook and an always-run pytest job in `lint.yml`, not path-filtered, so a required check can never be left waiting.

### Rollout

1. **Smoke test.** Create one provider per type (`anthropic`, `google`, `openai`) with one model each by hand, price them, and run an Agents chat.
   This settles the base URL and suffix for each type, whether slash-containing model IDs are accepted, whether the Requesty PNG icons load and look right on both themes, and which Coder role can read providers, models, and prices.
   If the `anthropic` type fails against Requesty, the fallback is the `openai` type for Anthropic as well.
1. Build the tool test-first in a worktree.
1. Run `check` against the live Coder (expecting everything missing), run `apply` by hand on gandalf, then run `check` again and expect exit 0.
1. Deploy the CronJob, ExternalSecret, and Uptime Kuma monitor, trigger a manual job, and verify the heartbeat.
1. Write `k8s/requesty-sync/README.md`.
   No persistent directories are added, so the backup CronJob is untouched.

### Risks

- Protocol and base-URL compatibility per provider type, covered by the smoke test.
- The read-only token role may not exist below Owner, in which case the CronJob token is an Owner-scoped dedicated user and the README says so.
- Coder's experimental endpoints may change on upgrade, and the daily check fails loudly if they do.
- Requesty could move its logo URLs, which would silently break icons until the static table is refreshed.
- About 200 models is a long Models page.
- Cache-write costs are uncounted, which under-reports Anthropic prompt-caching spend.

## Decisions taken by default, open to veto

1. Free models without tool calling are skipped and reported, not registered.
1. A free model is kept in the free provider even when a paid twin stays in its vendor's provider, so the operator can see both.
1. Vendors with no Requesty logo fall back to the Requesty logo.
1. Requesty's PNG logos are used for all vendors, including Anthropic, OpenAI, and Google, in place of Coder's built-in icons.
1. Cross-lab duplicates collapse to the lab with the most entries.
1. The check runs daily rather than weekly.
