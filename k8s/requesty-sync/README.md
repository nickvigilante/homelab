# requesty-sync

Keeps Coder Agents in step with the Requesty model catalog.

The tool is `scripts/requesty-coder-sync.py`, and its design is in `docs/superpowers/specs/2026-09-18-requesty-coder-sync-design.md`.
It has three commands: `check` reports drift, `apply` fixes it, and `verify` tests every registered model with a real Agents chat.
A daily CronJob runs a limited `check`, and you run the rest by hand.

## What it manages

- One Coder AI provider per model vendor, named `<vendor>-via-requesty`, all pointing at Requesty's router, each with the vendor's own logo as shipped by Requesty.
- One `free-via-requesty` provider that holds every free model, so you can see when a free option exists.
- One Agents model per canonical model, with its context limit and output limit.
- One custom per-token price per model, in micro-dollars per million tokens.

The tool touches only providers named `*-via-requesty` and the models under them.
It never deletes anything.
Models that Requesty lists with a retirement date are skipped.

## Pieces

| Piece                                                   | Where                                        |
| ------------------------------------------------------- | -------------------------------------------- |
| Tool                                                    | `scripts/requesty-coder-sync.py`             |
| CronJob (daily at 06:00 ET, `check --limited`)          | `requesty-sync-cronjob.yaml`                 |
| Secrets (`CODER_SESSION_TOKEN`, `UPTIME_KUMA_PUSH_URL`) | `external-secret.yaml`, sourced from BWS     |
| Alerts                                                  | The Uptime Kuma push monitor `requesty-sync` |
| Provision or rotate the secrets                         | `scripts/requesty-sync-provision.sh`         |
| Interactive smoke test of the setup                     | `scripts/requesty-smoke-test.sh`             |

## Why the daily check is limited

Coder v2.37.0 has no token scope that reads AI providers or model prices, and no role below Owner can read them.
So the CronJob holds only a narrow token: the role-less `requesty-sync` service account (a plain member, which takes no license seat) with the scopes `chat_model_config:read` and `organization:read`.
That token can read the model list, plus a descriptor for each provider, and nothing else.

`check --limited` therefore covers:

- Missing or orphaned models.
- Context and output limit changes, and a model under the wrong provider.
- Provider drift in display name, icon, enabled state, and a missing API key.

It does **not** cover prices or a provider's `base_url`, and every limited run says so with an INFO line.
Managed providers are recognized by a display name ending in "via Requesty", so a provider renamed by hand looks missing.

## The daily check

The CronJob runs `check --limited` and pings the Uptime Kuma monitor itself.
It pings `up` when everything matches, and `down` with a one-line summary on drift or on an error.
If the job dies before it can ping, the monitor goes DOWN when its 25-hour heartbeat interval expires.

The CronJob's own shell wrapper treats drift (script exit 1) as a k8s Job success, since Uptime Kuma already carries that signal.
Only a real script error (exit 2) fails the Job and trips the cluster's `KubeJobFailed` alert.
To get pinged on drift itself, attach a notification to the `requesty-sync` Uptime Kuma monitor.

To run it by hand:

```bash
kubectl -n coder create job --from=cronjob/requesty-sync test-sync-$(date +%s)
kubectl -n coder logs -f job/<the job name printed above>
```

## The full check, apply, and verify (by hand)

Run these on **gandalf**, from a fresh checkout of this repo.
They need your own unscoped admin token, which the block below creates for one hour.
`apply` also needs the Requesty API key.

```bash
cd ~/git/nickvigilante/homelab && git checkout main && git pull
export CODER_URL=https://coder.vigihome.net
export CODER_SESSION_TOKEN="$(coder tokens create --name "sync-$(date +%s)" --lifetime 1h | grep -oE '[A-Za-z0-9]{10}-[A-Za-z0-9]{22}' | head -1)"
python3 scripts/requesty-coder-sync.py check          # the full check, including prices and base URLs
```

To fix drift, run `apply`.
When it needs the Requesty key (a new provider, or `--rotate-key`), it reads it from the Bitwarden item `Requesty`, field `Main API key`.
That needs an unlocked session exported in the same shell, and setting `REQUESTY_API_KEY` yourself skips Bitwarden.

```bash
export BW_SESSION="$(bw unlock --raw)"; bw sync
python3 scripts/requesty-coder-sync.py apply
```

`scripts/requesty-probe.py` reads the key the same way.

Flags for `apply`:

- `--yes` skips the confirmation prompt.
- `--disable-orphans` disables models that Requesty no longer selects, which clears their alert.
- `--rotate-key` replaces the API key on every managed provider with the Requesty key.

Run the full `check` weekly, or after Requesty changes its prices, because that is the only way to see price drift.

### verify

`verify` tests every registered model through a real Coder Agents chat, which catches models that a host rejects even though the catalog says they work (a Gemma model rejected the system prompt, for example).
It costs real money, roughly a few thousand tokens per model, and it needs the unscoped token above, so it is never run by the CronJob.

```bash
python3 scripts/requesty-coder-sync.py verify --limit 3      # a cheap first try
python3 scripts/requesty-coder-sync.py verify                # every registered model
python3 scripts/requesty-coder-sync.py verify --disable-failures
```

- A model **passes** when the chat finishes with an assistant reply.
- A model **fails** when the host returns a non-retryable error.
  The report shows Coder's message and the upstream HTTP status.
- A model is **inconclusive** on a retryable error (a rate limit, a 5xx) or a timeout, and is never disabled for that.
- `--disable-failures` disables only the failed models, after asking.
  A disabled model is skipped by later `verify` runs and never re-enabled by `apply`, and `check` lists it as "selected but disabled in Coder".
  Re-enable one by hand to test it again.
- Other options: `--provider NAME`, `--model ID`, `--limit N`, `--concurrency N`, and `--timeout SECONDS`.

## Excluded models

The catalog lists models that fail when a chat is actually sent.
`EXCLUDED_MODELS` in the script names them, each with the observed error as its reason.
An excluded model is not registered, its canonical model falls back to the next-best host, and `apply --disable-orphans` disables the copy already in Coder.
`check` lists each one as an `excluded (...)` INFO line.

Most reasons are Requesty or host outages, so retest now and then with `scripts/requesty-probe.py MODEL_ID`, which calls Requesty without Coder.
The "several system messages" entries are Coder's request shape, so retest them after a Coder upgrade (`--extra-shapes` sends that request).
To remove an entry, delete it from the table, then run `apply` and `verify --model MODEL_ID`.

## Capturing the request Coder sends

When a model fails only through Coder Agents, `verify` and `requesty-probe.py` cannot say why.
Coder's chat debug logging records the HTTP request Coder sends to the provider, and the provider's reply, for each chat turn.
`scripts/requesty-debug-chat.py` sends one probe chat through each model and reads that recording back.

```sh
export CODER_SESSION_TOKEN=...
scripts/requesty-debug-chat.py --enable novita/qwen/qwen-2.5-72b-instruct novita/deepseek/deepseek_v3
```

- Debug logging is off by default, so pass `--enable`.
  It sets the admin gate ("Let users record chat debug logs", which needs an owner or admin token) and your own toggle ("Record debug logs for my chats"), and puts both back when it finishes, on an error, and on Ctrl-C.
  Add `--keep-enabled` to leave them on.
  Without `--enable` it changes nothing, and exits 2 with the settings to turn on by hand if logging is off.
- Each model must be registered and enabled in Coder.
  One that is not is reported and skipped, so enable it in the model admin first.
  Or pass `--enable-models`, which enables a disabled model just for its chat and sets it back to disabled afterwards, on an error and on Ctrl-C too.
  It changes only the model's `enabled` field, and never touches a model that is already enabled.
  If putting one back fails it prints a warning, and `python3 scripts/requesty-coder-sync.py apply --disable-orphans` disables it again.
- Models run one at a time, and each probe chat is archived afterwards.
  Other options are `--file ids.txt` (one model ID per line), `--out DIR`, `--timeout SECONDS` and `--poll SECONDS`.
- The script prints a digest per model that is safe to paste into a bug report.
  It has the chat's final status and error, then for each run and step the provider, model, request summary and response.
  The request summary lists every top-level key with its value, the message roles with content lengths, and the tool names with whether any schema uses `strict` or `additionalProperties`.
  It shows header names but never values, and only the first 40 characters of each message.
- The complete data lands in `DIR/<model id, with / as _>.json` (default `./chat-debug/`), readable only by you.
  It is what Coder returned, plus a decoded copy of each request and response body.
- The JSON can contain prompt text, tool output and model replies, so treat it like conversation history.
  The session token is never printed or written.

## Reading a report

| Category                            | Meaning                                                                                                                     | Fixed by                                 |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `MISSING_PROVIDER`, `MISSING_MODEL` | In the desired state and absent in Coder                                                                                    | `apply`                                  |
| `PROVIDER_DRIFT`                    | Wrong `base_url` (full check only), `icon`, or `display_name`, disabled, or no API key                                      | `apply` (a key needs `REQUESTY_API_KEY`) |
| `MODEL_DRIFT`                       | Context or output limit differs, or the model belongs under a different provider (for example it became free)               | `apply`                                  |
| `PRICE_DRIFT`                       | Custom price absent or different (full check only)                                                                          | `apply`                                  |
| `ORPHAN_MODEL`                      | Enabled in Coder but no longer selected (retired, or its best host changed)                                                 | `apply --disable-orphans`                |
| `INFO`                              | Not drift: models skipped on purpose, non-plain fallbacks, models selected but disabled in Coder, and the limited-mode note | Nothing to fix                           |

Disabling a model in the UI is not drift, and neither is renaming it, so you can hide models you do not want.

## First-time setup

1. The `requesty-sync` service account exists and has no role.
   (`coder users create --service-account --username requesty-sync`; a role-less service account takes no license seat.)
2. In Uptime Kuma, add a monitor of type Push named `requesty-sync` with a heartbeat interval of `90000` seconds (25 hours).
   Keep its push URL for the next step.
   Paste the URL as the UI shows it: the script keeps only the token and rewrites the host to the in-cluster address (`http://uptime-kuma.monitoring.svc.cluster.local:3001`), because pods cannot resolve `*.vigihome.net`.
3. Run `scripts/requesty-sync-provision.sh` on gandalf, with Bitwarden unlocked.
   It creates the 1-year token, stores both secrets in the Bitwarden item `Homelab Requesty Sync` and in BWS, and writes `external-secret.yaml` with the two BWS IDs.
4. Commit `external-secret.yaml` with the rest of this directory, merge, and let Flux apply it (`flux reconcile kustomization requesty-sync --with-source`).
5. Run the CronJob once by hand and check that the monitor turns UP.

## Changing only the push URL

Run `scripts/requesty-sync-provision.sh --push-url-only` on gandalf.
It updates the push URL in Bitwarden and BWS without minting another Coder token, then `flux reconcile externalsecret -n coder requesty-sync-secrets` picks it up.

## Rotating the token

The Coder token lasts one year, and a Todoist reminder is set for a few weeks before it expires.
Run `scripts/requesty-sync-provision.sh` again: it creates a new token, updates the Bitwarden item and the two BWS secrets in place (so the ExternalSecret IDs do not change), and offers to keep the stored push URL.
Then run `flux reconcile externalsecret -n coder requesty-sync-secrets`, trigger the CronJob once, and revoke the old token.

If you rotated the Requesty API key at Requesty, run `apply --rotate-key` so every provider in Coder gets the new key.

## Troubleshooting

- **Exit code 2** means an error, and the message names the cause: a missing or expired token, an unreachable Requesty catalog, a catalog with fewer than 100 eligible models (refused so a truncated response cannot look like mass deprecation), or a Coder API failure.
- **The monitor is DOWN and the job log says 401 or "signed out"** means the token expired or was revoked, so rotate it.
- **A Coder API failure with status 404 or 405 after a Coder upgrade** usually means an experimental endpoint moved, because model prices live under `/api/experimental`.
- **The monitor is DOWN but the job succeeded** means drift, so read the job log.
- **A model fails `verify` with "Range of max_tokens" or "exceeds the maximum output tokens"** means the catalog's output limit is wrong for that host.
  Add the limit the error names to `MAX_OUTPUT_OVERRIDES` in the script, then run `apply` and `verify --model <id>`.
- **Icons do not load** means Requesty moved its logo files, so refresh the `LAB_LOGOS` table in the script from `https://www.requesty.ai/provider_logos/v2/`.
