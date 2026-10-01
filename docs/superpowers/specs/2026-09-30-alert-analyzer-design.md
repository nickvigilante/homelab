# Alert analyzer design

## Purpose

When a Prometheus alert fires, a read-only agent investigates the cluster and reports what it found.
The finding reaches the operator as an Uptime Kuma alert that links to the full analysis in a Coder chat.

The agent only reads.
It holds no secrets, and nothing it says is executed.

## Background

- Coder (v2.37.0) runs Agents: the agent loop runs in the Coder control plane, and a chat can attach admin-registered MCP servers and a workspace.
  Model credentials never enter the workspace.
- `k8s/claude-mcp` already runs a read-only Kubernetes MCP server (built-in `view` role, `read_only`, Secrets denied) and a read-only Grafana MCP server (Viewer token, which also reaches Prometheus and Loki).
  Both are reachable from Coder Agents chats.
- `scripts/requesty-coder-sync.py` already drives the Chats API from Python: `POST /api/v2/chats`, status and message reads, per-chat cost, archive, and a completion test (status `waiting` with an assistant reply, or `error`).
- The restic backup CronJob and `requesty-sync` already report to Uptime Kuma push monitors.
- Alertmanager currently emails `vigihome-admin@vigiemail.com` (`k8s/kube-prometheus-stack/values.yaml`).

## Decisions made

| Question                | Decision                                                                                                   |
| ----------------------- | ---------------------------------------------------------------------------------------------------------- |
| Trigger                 | Alertmanager webhook, on alerts of severity `warning` or `critical`                                        |
| Workspace model         | One persistent read-only workspace, started per alert and stopped afterward                                |
| Template                | A new `Analyzer` template, separate from `Base` and from any future operator template                      |
| Secrets in the template | None                                                                                                       |
| Who reports             | A small orchestrator, so no reporting credential ever enters the agent's workspace                         |
| Where findings go       | An Uptime Kuma push monitor, with the Coder chat link in the message                                       |
| Issue tracking          | Out of scope. A Plane sink is a later sub-project; GitHub issues were rejected because `homelab` is public |
| Tools                   | Baked into an `images/analyzer/` image that builds on the existing base image                              |

## Architecture

```text
Alertmanager --webhook--> orchestrator --Coder API--> Coder control plane
                              |                            |  agent loop
                              |                            |--> Kubernetes MCP (read-only)
                              |                            |--> Grafana MCP (Prometheus, Loki)
                              |                            `--> Analyzer workspace (shell, repo clone)
                              `--push--> Uptime Kuma (DOWN with summary and chat link, UP on resolve)
```

### Alertmanager route

- A new child route matches `severity=~"warning|critical"` and sends to a webhook receiver pointing at the orchestrator, with `send_resolved: true` and `continue: true`.
  It sits after the existing `InfoInhibitor` and `Watchdog` routes, which stop matching, so those two never reach it.
- An alert that matches any child route skips the root route's receiver.
  So the change must also add an explicit catch-all child route to the `email` receiver after the webhook route.
  Without it, alerts matched by the webhook route would stop being emailed.
  Checking that email still arrives is part of the acceptance run.
- The change lives in `k8s/kube-prometheus-stack/values.yaml` and is applied with a pinned `helm upgrade`.

### Orchestrator

- One Deployment in a new `alert-analyzer` namespace, managed by Flux under `k8s/alert-analyzer/`.
- Standard-library Python on a stock image with the script mounted from a ConfigMap, following `requesty-sync`.
  Whether the Coder client is shared with `requesty-coder-sync.py` or copied is a plan-level choice.
- On a firing alert:
  1. Deduplicate on a chat label `alert-fingerprint`, with a cooldown (default 6 hours).
     A re-fire after a resolve is analyzed again.
  2. Respect a concurrency cap of one active chat, a queue limit, and a daily cap of 20 chats.
     A burst of distinct alerts becomes one grouped chat.
  3. Start the `alert-analyzer` workspace if it is stopped.
  4. Create a chat as the dedicated Coder user `alert-analyzer`, with `workspace_id`, `mcp_server_ids` (Kubernetes and Grafana), `model_config_id`, a read-only-analyst `system_prompt`, labels, and the alert as `content`.
  5. Poll the chat until it is `waiting` with an assistant reply, `error`, or the timeout (default 15 minutes).
  6. Parse the final message as JSON and push the result to Uptime Kuma.
  7. After 10 idle minutes with no active chat, stop the workspace.
- On a resolved alert, push UP.
  If an analysis is still running, let it finish and note that the alert resolved.
- It pushes a heartbeat to its own Uptime Kuma push monitor, so its death is visible.

### Analyzer template and image

- `homelab-dev-templates/templates/Analyzer/` and `images/analyzer/`.
- The image builds on the existing base image and adds `kubectl`, `helm`, `flux`, `promtool`, `yq` and `jq`.
  It is published by the same GHCR CI and pinned by short SHA in the template.
- The workspace is persistent, pinned to amd64, with 1 core, 2 GB and a small home volume.
  It has no user-facing parameters.
- A startup script clones or pulls the public `homelab` repo, so the agent can compare manifests with live state.
- Autostop after 30 idle minutes is a backstop.
  Auto-delete is off.

### Access and containment

- **In-cluster identity:** ServiceAccount `analyzer` in the `coder` namespace, bound to the built-in `view` ClusterRole.
  Secrets are not readable.
  `view` still allows `pods/log`, so log contents can reach model context.
- **Network:** a NetworkPolicy limits the workspace pod to DNS, the API server, Loki, Prometheus, Grafana and `github.com:443`.
  The orchestrator namespace is default-deny, admitting only Alertmanager (ingress) and Coder and Uptime Kuma (egress).
- **Coder identity:** the `alert-analyzer` user can use the `Analyzer` template only.
  Without that, an agent could create workspaces from other templates.
- **Secrets:** none in the workspace.
  The orchestrator holds a Coder token for the `alert-analyzer` user and the Uptime Kuma push URLs.
  They live in the BWS `homelab` project and reach the pod through ESO, like other cluster secrets.
  `Homelab-IaC` credentials are never involved.

### Report contract

The agent's final message is a JSON object:

```json
{
  "summary": "one line, at most 200 characters",
  "severity": "info | warning | critical",
  "probable_cause": "text",
  "evidence": ["text"],
  "suggested_actions": ["text, advice only"],
  "confidence": "low | medium | high"
}
```

The orchestrator accepts nothing else, truncates fields, strips control characters, and puts only the summary and the Coder chat link into the Uptime Kuma message.
Anything in `suggested_actions` is advice for a person.
Nothing is executed.

## Failure handling

- The analyzer never gates alerting.
  The email route is untouched.
- If Coder is unreachable, the workspace will not start, or the model errors, the orchestrator retries with backoff and then pushes "analysis unavailable".
- On timeout it pushes "timed out" with the chat link.
- On invalid JSON it asks once for a reformat, then pushes "see chat".
- Alert text and log lines are untrusted input.
  The system prompt says so, the agent has read-only tools and no secrets, and the orchestrator never acts on the agent's text.
- Model spend is bounded by the daily chat cap, the per-chat timeout, and a per-chat cost check through the chat cost endpoint.

## Testing

- **Unit tests** (pytest, in the style of `tests/requesty_sync/`) against fake Coder, Alertmanager and Uptime Kuma servers.
  They cover JSON parsing, sanitization, dedupe, cooldown, concurrency, the queue limit, timeouts and resolved handling.
- **Image smoke test** that the tools are present, plus the existing template-validate CI.
- **Live acceptance:** break a workload in a scratch namespace.
  Pass means the alert fires, the chat runs, Uptime Kuma goes DOWN with a link, the alert resolves, Uptime Kuma goes UP, and the workspace stops.
  The same alert must also still arrive by email.

## Phases

0. **Spikes**, each a small probe that reports an answer.
   - Create a chat with `workspace_id` set to a stopped workspace.
   - Create a non-human Coder user and token, and start and stop a workspace through the API with it.
   - Restrict that user to the `Analyzer` template.
   - Find the registered MCP server IDs for `mcp_server_ids`.
   - Confirm the Uptime Kuma push monitors accept `status=down` with a message of the intended length.
1. **Image and template**, with the ServiceAccount, RBAC and NetworkPolicy.
2. **Orchestrator** and its tests, with its ExternalSecrets and Flux wiring.
3. **Alertmanager route**, the Uptime Kuma monitors, and the live acceptance run.

## Not in scope

- A Plane issue sink.
  Plane is its own sub-project with its own spec.
- An ephemeral per-alert workspace.
- An operator template that holds `Homelab-IaC` credentials, for `ansible` and `tofu`.
- Letting the agent change anything in the cluster.
- Deciding whether `Base` gets its tools from the image or the dotfiles Brewfile.

## Open items for the plan

- Share the Coder client with `requesty-coder-sync.py` or copy it.
- Which `model_config_id` the analyzer uses, and its reasoning effort.
- Exact Uptime Kuma monitor layout: one monitor for all findings, or one per alert class.
- Cooldown, daily cap and idle-stop defaults are starting values to tune.
