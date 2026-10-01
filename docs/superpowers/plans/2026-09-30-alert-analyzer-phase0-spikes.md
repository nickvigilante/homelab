# Alert analyzer phase 0 spikes implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.
> Most steps need a human at a terminal on gandalf (an interactive login, a Bitwarden unlock, or a click in the Coder or Uptime Kuma UI).
> Steps marked **[operator]** must be done by the operator.
> Everything else may be run by an agent in the operator's terminal.

**Goal:** Answer, with evidence, the five open questions in phase 0 of the alert analyzer spec, so phases 1 to 3 are built on verified Coder and Uptime Kuma behavior.

**Architecture:** Five throwaway probes run from gandalf against the live Coder (`https://coder.vigihome.net`) and Uptime Kuma.
Each probe has a pass criterion and a stated fallback from the spec's design.
Nothing here is code to keep.
The one durable output is a "Spike findings" section appended to the spec.

**Tech Stack:** `curl` and `jq` for the Coder API, the `coder` CLI, `kubectl`, the Uptime Kuma web UI, and the OpenTofu wrapper in `~/git/nickvigilante/infrastructure/coder`.

**Spec:** `docs/superpowers/specs/2026-09-30-alert-analyzer-design.md`, section "Phases", phase 0.

## Global Constraints

- Coder is v2.37.0 at `https://coder.vigihome.net`, reachable only over the tailnet.
  Run everything from gandalf.
- Coder API calls authenticate with the `Coder-Session-Token` header.
  The chat API is mounted under `/api/v2` (confirmed in the v2.37.0 route table).
- Tokens never appear in a file, in the plan's commands, or on a command line (argv is visible in `ps`).
  Use the `capi` helper in task 1, which passes the header through a curl config on a file descriptor.
- Never print a secret.
  Uptime Kuma push URLs contain a token, so treat them as secrets too.
- Everything the spikes create is named with the `spike-` prefix and listed in the cleanup ledger, except the `alert-analyzer` user, which the design needs and which is kept.
- The agent must stay read-only toward the cluster.
  No step asks it to change anything.
- Do not toggle agent availability on `Base` or any other live template during a spike.
  Use the throwaway `spike-analyzer` template.
- Findings are recorded in the spec (section "Spike findings"), in the same PR that records the results.
- Shell: gandalf runs zsh.
  Do not put `<placeholder>` text in a command, because zsh reads `<...>` as a redirection.
  Use shell variables.
  Do not name a function `q`; it collides with an alias.

## Review Focus

These are the inputs and conditions the spec implies that no task would otherwise exercise.
Each has a test in the task named after it.

1. **A Kuma push monitor goes DOWN on silence.**
   A push monitor with no heartbeat inside its interval turns DOWN, so "push UP when the alert resolves" would flip back to DOWN a minute later.
   Expected: the design must keep re-pushing the current state.
   Pinned in task 6.
2. **The agent can create workspaces from any agent-enabled template.**
   Acting as `alert-analyzer`, it could provision from `Base`.
   Expected: after the controls, only `Analyzer` is visible to it.
   Pinned in task 4.
3. **MCP servers are `default_off`.**
   A chat that omits `mcp_server_ids` has no cluster tools, and a careless prompt may make the model guess instead of saying so.
   Expected: the agent states that it has no Kubernetes tool.
   Pinned in task 5.
4. **The analyzer token expires without warning.**
   An expired token would make the orchestrator fail every alert with a 401.
   Expected: the maximum lifetime is known, and the orchestrator will need an expiry check.
   Pinned in task 3.
5. **A pinned workspace owned by someone else.**
   `workspace_id` pointing at another user's workspace must be refused, or the analyzer could drive a workspace that holds secrets.
   Expected: an error, with the other workspace untouched.
   Pinned in task 5.

______________________________________________________________________

## Cleanup ledger

Fill this in as things are created.
Task 7 walks it.

| Created in task | Item                                           | Kept?                      |
| --------------- | ---------------------------------------------- | -------------------------- |
| 1               | Admin session token `spike-phase0`             | No, revoke                 |
| 3               | Template `spike-analyzer`                      | No, delete                 |
| 3               | User `alert-analyzer`                          | Yes, with no active tokens |
| 3               | Token `spike-phase0-analyzer`                  | No, revoke                 |
| 3               | Workspace `alert-analyzer/spike-ws`            | No, delete                 |
| 4, 5            | Chats labeled `purpose=spike-phase0`           | No, archive                |
| 6               | Uptime Kuma push monitor `spike-analyzer-push` | No, delete                 |

### Task 1: Environment, helpers and the state file

**Files:**

- Create (not committed): `~/spike-notes/state.env`, mode 600, holding only non-secret identifiers.
- Create (not committed): `~/spike-notes/notes.md`, free-form notes for task 7.

**Interfaces:**

- Consumes: nothing.
- Produces, in the shell: `CODER_URL`, `CODER_SESSION_TOKEN` (an admin token), the shell function `capi METHOD PATH [JSON_BODY]`, the function `as_analyzer METHOD PATH [JSON_BODY]` (defined in task 3), and `ORG` (the default organization id).
  Produces in `~/spike-notes/state.env`: `ORG`, then later `K8S_MCP_ID`, `GRAFANA_MCP_ID`, `TEMPLATE_ID`, `WS_ID`, `MODEL_ID`.

Keep one terminal open for the whole plan.
If it closes, re-run the helper block below and `. ~/spike-notes/state.env`.
Tokens are never written to the state file, so mint new ones.

- [ ] **Step 1: Check the tools and the CLI version**

```bash
command -v coder curl jq kubectl
coder version | head -3
```

Expected: all four tools are found, and the CLI version is v2.37.x.
If the CLI is older or newer than the server, expect small flag differences and note them in `~/spike-notes/notes.md`.

- [ ] **Step 2: [operator] Log in as a Coder admin and mint a short-lived admin token**

```bash
export CODER_URL=https://coder.vigihome.net
coder login "$CODER_URL"
CODER_SESSION_TOKEN="$(coder tokens create --lifetime 4h --name spike-phase0)"
export CODER_SESSION_TOKEN
coder whoami
```

Expected: `coder whoami` names an owner/admin account.
If `coder tokens create --help` shows different flag names, use what it shows and note it.
The token is now only in this shell's environment.

- [ ] **Step 3: Define the `capi` helper**

It passes the token through a curl config read from a file descriptor, so it never appears in argv.

```bash
mkdir -p ~/spike-notes && chmod 700 ~/spike-notes
capi() { # usage: capi METHOD PATH [JSON_BODY]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    printf '%s' "$body" | curl -fsS -X "$method" \
      -K <(printf 'header = "Coder-Session-Token: %s"\n' "$CODER_SESSION_TOKEN") \
      -H 'Content-Type: application/json' --data-binary @- "$CODER_URL$path"
  else
    curl -fsS -X "$method" \
      -K <(printf 'header = "Coder-Session-Token: %s"\n' "$CODER_SESSION_TOKEN") \
      "$CODER_URL$path"
  fi
}
capi GET /api/v2/buildinfo | jq '{version, external_url}'
```

Expected: `version` starts with `v2.37`.
A 401 means the token is wrong or expired.

- [ ] **Step 4: Record the default organization id**

```bash
ORG="$(capi GET /api/v2/organizations | jq -r '.[] | select(.is_default) | .id')"
echo "ORG=$ORG"
printf 'ORG=%s\n' "$ORG" > ~/spike-notes/state.env && chmod 600 ~/spike-notes/state.env
```

Expected: one UUID.

- [ ] **Step 5: Snapshot the live templates, read-only**

```bash
capi GET "/api/v2/organizations/$ORG/templates" | jq -r '.[] | [.name, .id] | @tsv'
```

Expected: the list includes `Base`.
Save the output to `~/spike-notes/notes.md` as "templates before".
Nothing on these templates is changed by any task.

### Task 2: Spike A, find the registered MCP server ids

**Files:**

- Read: `~/git/nickvigilante/infrastructure/coder/mcp_servers.tf`, `coder/README.md`, `coder/tofu.sh`.
- Update: `~/spike-notes/state.env`.

**Interfaces:**

- Consumes: `capi`, `ORG` from task 1.
- Produces: `K8S_MCP_ID` and `GRAFANA_MCP_ID` (UUIDs), plus the HTTP path that lists MCP server configs, if one is found.

The MCP servers were registered by the `coderd_agents_mcp_server` resource with slugs `kubernetes` and `grafana`.
The Go SDK exposes `MCPServerConfigs(ctx, organizationID)`, so the list is organization-scoped, but its exact HTTP path is not in the public reference.
So this spike has two independent sources and checks that they agree.

- [ ] **Step 1: Read the wrapper before running it**

```bash
cd ~/git/nickvigilante/infrastructure && git checkout main && git pull --ff-only
sed -n 1,60p coder/tofu.sh
```

Expected: the wrapper mints an admin token, injects secrets from BWS, runs `tofu` with its arguments, and revokes the token.
If it does not pass arguments through to `tofu`, stop and use only source 2 below.

- [ ] **Step 2: [operator] Source 1, the OpenTofu state**

This needs a Bitwarden unlock for the wrapper, so run it yourself.

```bash
cd ~/git/nickvigilante/infrastructure/coder
./tofu.sh state show coderd_agents_mcp_server.kubernetes | grep -E '^\s*(id|slug|availability|enabled)\s'
./tofu.sh state show coderd_agents_mcp_server.grafana | grep -E '^\s*(id|slug|availability|enabled)\s'
```

Expected: an `id` for each, `slug` of `kubernetes` and `grafana`, `availability = "default_off"`, `enabled = true`.
The import ids elsewhere in this context use `organization/slug`, so the `id` attribute may be that string and not a UUID.
If it is not a UUID, source 2 provides the UUID.

- [ ] **Step 3: Source 2, probe the API for the list path**

These are all read-only GETs.
A 404 only means "not this path".

```bash
for p in \
  "/api/v2/organizations/$ORG/chats/mcp-servers" \
  "/api/v2/organizations/$ORG/chats/mcp/servers" \
  "/api/v2/organizations/$ORG/mcp/servers" \
  "/api/v2/organizations/$ORG/mcp-servers" \
  "/api/experimental/mcp/servers"; do
  code="$(curl -s -o /tmp/spike-mcp.json -w '%{http_code}' \
    -K <(printf 'header = "Coder-Session-Token: %s"\n' "$CODER_SESSION_TOKEN") "$CODER_URL$p")"
  printf '%s  %s\n' "$code" "$p"
done
```

Expected: at least one path returns 200.
Then set `MCP_LIST_PATH` to the path that did, and print only the safe fields (slug, id, availability, enabled):

```bash
MCP_LIST_PATH="/api/v2/organizations/$ORG/chats/mcp-servers"   # edit to the path that returned 200
capi GET "$MCP_LIST_PATH" | jq -r '(if type=="array" then . else (.servers // .mcp_servers // []) end)[] | [.slug, .id, .availability, .enabled] | @tsv'
```

If no candidate returns 200, discover the path in the browser: open the Coder dashboard, go to Agents, Settings, MCP servers, open the browser's network tab, reload, and read the request URL.
Record it.

- [ ] **Step 4: Save the ids**

```bash
K8S_MCP_ID="$(capi GET "$MCP_LIST_PATH" | jq -r '(if type=="array" then . else (.servers // .mcp_servers // []) end)[] | select(.slug=="kubernetes") | .id')"
GRAFANA_MCP_ID="$(capi GET "$MCP_LIST_PATH" | jq -r '(if type=="array" then . else (.servers // .mcp_servers // []) end)[] | select(.slug=="grafana") | .id')"
printf 'K8S_MCP_ID=%s\nGRAFANA_MCP_ID=%s\n' "$K8S_MCP_ID" "$GRAFANA_MCP_ID" >> ~/spike-notes/state.env
echo "$K8S_MCP_ID $GRAFANA_MCP_ID"
```

**Pass:** two UUIDs, and the list endpoint and the OpenTofu state agree on the slugs and on `default_off`.
**Partial pass:** the state gives ids but the list path is not found; the orchestrator takes the ids from configuration, which the design already allows.
**Fail:** neither source yields an id.
Fallback: pass the MCP servers in the chat as caller-supplied servers (the experimental `inline_mcp_servers` field), or attach them manually per chat in the UI for the acceptance run, and record that the orchestrator then needs a different integration.

- [ ] **Step 5: Record**

Write the list path (or "not found"), both ids, and the pass/fail in `~/spike-notes/notes.md`.

### Task 3: Spike B, a non-human user, its token, and start and stop through the API

**Files:**

- Read: `~/git/nickvigilante/homelab-dev-templates/templates/Base/` (pushed as a throwaway copy).
- Update: `~/spike-notes/state.env`, `~/spike-notes/notes.md`.

**Interfaces:**

- Consumes: `capi`, `ORG`.

- Produces: the user `alert-analyzer`, the shell variable `ANALYZER_TOKEN` (never written to disk), the function `as_analyzer`, `TEMPLATE_ID` (the throwaway template), `WS_ID` (the throwaway workspace, owned by `alert-analyzer`), and measured start and stop durations.

- [ ] **Step 1: Push a throwaway copy of `Base` as `spike-analyzer`**

`Base` has no agent restrictions yet, so a copy lets task 4 toggle agent availability without touching live templates.

```bash
cd ~/git/nickvigilante/homelab-dev-templates && git pull --ff-only
coder templates push spike-analyzer --directory templates/Base --yes
TEMPLATE_ID="$(capi GET "/api/v2/organizations/$ORG/templates" | jq -r '.[] | select(.name=="spike-analyzer") | .id')"
echo "TEMPLATE_ID=$TEMPLATE_ID" | tee -a ~/spike-notes/state.env
```

Expected: a UUID.
The template's workspaces use `Base` defaults (2 cores, 8 GB, 10 GB disk), which is acceptable for a short spike.

- [ ] **Step 2: Discover how to create a non-human user**

The reference lists `service_account` and `login_type` on `POST /api/v2/users` but does not list the `login_type` values.
Ask the CLI, which matches this server version:

```bash
coder users create --help | sed -n 1,40p
```

Record the flags it shows (look for `--service-account` and `--login-type`) in `~/spike-notes/notes.md`.

- [ ] **Step 3: Create the user, trying the simplest form first**

```bash
capi POST /api/v2/users "$(jq -n --arg org "$ORG" '{username:"alert-analyzer", name:"Alert analyzer", service_account:true, organization_ids:[$org]}')" | jq '{id, username, login_type, status, email}'
```

If the server rejects it with a message about a missing email, add a non-routable one:

```bash
capi POST /api/v2/users "$(jq -n --arg org "$ORG" '{username:"alert-analyzer", name:"Alert analyzer", email:"alert-analyzer@vigihome.net", service_account:true, organization_ids:[$org]}')" | jq '{id, username, login_type, status}'
```

If `service_account` is rejected or ignored, try `login_type` `none`:

```bash
capi POST /api/v2/users "$(jq -n --arg org "$ORG" '{username:"alert-analyzer", name:"Alert analyzer", email:"alert-analyzer@vigihome.net", login_type:"none", organization_ids:[$org]}')" | jq '{id, username, login_type, status}'
```

**Pass:** an active user that has no password and cannot log in interactively (`login_type` is not `password`).
Record which form worked, and the exact error text of any form that failed.
Confirm the role is the default member role:

```bash
capi GET /api/v2/users/alert-analyzer | jq '{username, roles, login_type, status}'
```

- [ ] **Step 4: Mint the analyzer's token and find its maximum lifetime**

```bash
ANALYZER_TOKEN="$(capi POST /api/v2/users/alert-analyzer/keys/tokens '{"token_name":"spike-phase0-analyzer","lifetime":14400}' | jq -r .key)"
test -n "$ANALYZER_TOKEN" && echo "token minted (not printed)"
as_analyzer() { ( CODER_SESSION_TOKEN="$ANALYZER_TOKEN"; capi "$@" ); }
as_analyzer GET /api/v2/users/me | jq '{username, roles}'
```

Expected: `username` is `alert-analyzer`.
If minting a token for another user as an admin is refused, try the CLI form that `coder tokens create --help` shows (look for `--user`).

Now find the longest lifetime the server accepts, using a probe token that is deleted immediately:

```bash
capi POST /api/v2/users/alert-analyzer/keys/tokens '{"token_name":"spike-lifetime-probe","lifetime":31536000}' | jq 'if .key then "accepted 1 year" else . end'
coder tokens list --user alert-analyzer 2>/dev/null | head
coder tokens remove spike-lifetime-probe --user alert-analyzer 2>/dev/null || coder tokens remove spike-lifetime-probe
```

Expected: either "accepted 1 year", or an error naming the server's maximum.
If the probe token is created, the `remove` line deletes it.
If `coder tokens remove` needs different flags, use `coder tokens remove --help`, and confirm with `coder tokens list --user alert-analyzer` that only `spike-phase0-analyzer` remains.
Record the maximum lifetime, because the orchestrator needs an expiry check for it (review focus 4).

- [ ] **Step 5: Create a throwaway workspace owned by `alert-analyzer`**

The agent can only reach workspaces owned by the chat's user, so the workspace must be owned by `alert-analyzer`.

```bash
coder create --help | grep -i -E 'owner|user|template|parameter' | head
coder create alert-analyzer/spike-ws --template spike-analyzer --use-parameter-defaults --yes
```

If the `owner/name` form is not accepted, use the flag that `--help` shows for the owner.
If none exists, create it through the API instead and record that:

```bash
capi POST "/api/v2/organizations/$ORG/members/alert-analyzer/workspaces" "$(jq -n --arg t "$TEMPLATE_ID" '{name:"spike-ws", template_id:$t}')" | jq '{id, name, owner_name}'
```

Then save the id:

```bash
WS_ID="$(capi GET /api/v2/users/alert-analyzer/workspace/spike-ws | jq -r .id)"
echo "WS_ID=$WS_ID" | tee -a ~/spike-notes/state.env
```

Wait for it to run, and note how long the first build took:

```bash
until [ "$(capi GET "/api/v2/workspaces/$WS_ID" | jq -r .latest_build.status)" = running ]; do sleep 5; done
capi GET "/api/v2/workspaces/$WS_ID" | jq '{status: .latest_build.status, created: .latest_build.created_at, updated: .latest_build.updated_at}'
```

- [ ] **Step 6: Stop and start it with the analyzer's token only**

```bash
t0=$(date +%s)
as_analyzer POST "/api/v2/workspaces/$WS_ID/builds" '{"transition":"stop"}' | jq '{id, transition, status}'
until [ "$(as_analyzer GET "/api/v2/workspaces/$WS_ID" | jq -r .latest_build.status)" = stopped ]; do sleep 3; done
echo "stop took $(( $(date +%s) - t0 ))s"

t0=$(date +%s)
as_analyzer POST "/api/v2/workspaces/$WS_ID/builds" '{"transition":"start"}' | jq '{id, transition, status}'
until [ "$(as_analyzer GET "/api/v2/workspaces/$WS_ID" | jq -r '.latest_build.resources[].agents[]?.status' | head -1)" = connected ]; do sleep 3; done
echo "start to agent connected took $(( $(date +%s) - t0 ))s"
```

`POST /api/v2/workspaces/{workspace}/builds` with `transition` is confirmed in the server's route table.
The build status strings (`stopped`, `running`) are assumed from the CLI and may differ.
If a loop never ends, press Ctrl-C and print `.latest_build.status` to see the real strings, then record them.

**Pass:** both transitions succeed with only the analyzer's token, and the durations are recorded (the start duration is the orchestrator's wait budget).
**Fail:** a 403 on the builds endpoint.
Fallback: the orchestrator uses an admin-owned token with narrower `scopes` or an `allow_list`, or the agent starts the workspace itself with its `start_workspace` tool, and task 5 shows whether that works.

- [ ] **Step 7: Least-privilege probe, read-only**

The analyzer must not reach other users' workspaces.

```bash
ADMIN_NAME="$(capi GET /api/v2/users/me | jq -r .username)"
ADMIN_WS="$(capi GET "/api/v2/workspaces?q=owner:$ADMIN_NAME" | jq -r '.workspaces[0].name')"
echo "probing $ADMIN_NAME/$ADMIN_WS"
curl -s -o /dev/null -w '%{http_code}\n' -K <(printf 'header = "Coder-Session-Token: %s"\n' "$ANALYZER_TOKEN") "$CODER_URL/api/v2/users/$ADMIN_NAME/workspace/$ADMIN_WS"
```

**Pass:** 403 or 404, not 200.
Record the code.

### Task 4: Spike C, restrict what the analyzer's agent can provision

**Files:**

- Update: `~/spike-notes/notes.md`.

**Interfaces:**

- Consumes: `capi`, `as_analyzer`, `ORG`, `TEMPLATE_ID`.
- Produces: `MODEL_ID`, the name of the template-settings field that controls agent availability (if found), the list of live templates the agent can see today, and whether template ACLs are licensed.

There are two controls:
the per-template switch "Allow Coder Agents to create workspaces using this template" (Agents, Settings, Manage Agents, Templates, or the template's settings page), which hides a template from the agent's `list_templates`, `read_template` and `create_workspace` tools,
and template ACLs, which may need a paid license.

- [ ] **Step 1: Check the license for template permissions, read-only**

```bash
capi GET /api/v2/entitlements | jq '{has_license, features: (.features | with_entries(select(.key | test("template|rbac|group"; "i"))))}'
```

Expected: either `has_license: false` and no template features, or entitled features.
Record the output.
If template ACLs are not entitled, skip step 2 and rely on the agent switch.

- [ ] **Step 2: Probe the template ACL, only if licensed**

```bash
capi GET "/api/v2/templates/$TEMPLATE_ID/acl" | jq 'keys'
```

The public reference does not document this endpoint.
A 404 or a license error means it is not available.
Record the result.
If it works, record the field names for granting a user the `use` role and removing the Everyone group.

- [ ] **Step 3: Pick a cheap model for the probe chats**

```bash
capi GET "/api/v2/organizations/$ORG/chats/models" | jq -r '.models[] | [.id, .provider, .model] | @tsv' | head -30
```

Choose a small, inexpensive model from the list and set `MODEL_ID` to its id.
Do not guess the id.

```bash
MODEL_ID="paste the id here, from the list above"
echo "MODEL_ID=$MODEL_ID" >> ~/spike-notes/state.env
```

- [ ] **Step 4: Find the template-settings field behind the switch, with a diff**

```bash
capi GET "/api/v2/templates/$TEMPLATE_ID" | jq -S . > ~/spike-notes/template-before.json
```

**[operator]** In the Coder UI, open the template `spike-analyzer`, then its settings page, and turn **off** "Allow Coder Agents to create workspaces using this template".
Then:

```bash
capi GET "/api/v2/templates/$TEMPLATE_ID" | jq -S . > ~/spike-notes/template-after-off.json
diff ~/spike-notes/template-before.json ~/spike-notes/template-after-off.json
```

Expected: one or two changed keys.
That key is the field the switch uses; record its name and values.
If the diff is empty, the setting lives elsewhere; check the Agents, Settings, Templates tab's network request and record it.

- [ ] **Step 5: Test what the agent can see, with the switch off and then on**

Create a chat as `alert-analyzer` with no workspace and a harmless prompt.
Run it once with the switch off:

```bash
CHAT_OFF="$(as_analyzer POST /api/v2/chats "$(jq -n --arg org "$ORG" --arg m "$MODEL_ID" '{organization_id:$org, model_config_id:$m, client_type:"api", labels:{purpose:"spike-phase0"}, content:[{type:"text", text:"List the template names you are able to use, one per line. Do not create or start anything. If you can use none, say so."}]}')" | jq -r .id)"
until s="$(as_analyzer GET "/api/v2/chats/$CHAT_OFF" | jq -r .status)"; [ "$s" = waiting ] || [ "$s" = error ] || [ "$s" = requires_action ]; do sleep 3; done
echo "status: $s"
as_analyzer GET "/api/v2/chats/$CHAT_OFF/messages" | jq -r '.messages[] | select(.role=="assistant") | .content' | head -40
```

The message content shape may be a string or a list of parts.
If the last command prints nothing useful, run it without the `select` and `.content` filter and look at the keys, then adjust the filter and record the real shape.

**[operator]** Turn the switch back **on** for `spike-analyzer`, then repeat the same chat as `CHAT_ON`, with the same prompt.

**Pass:** with the switch off, `spike-analyzer` is not listed.
With it on, it is listed.
Also record every other template name the agent lists.
Those are the live templates that must have the switch turned off before the analyzer is enabled (review focus 2).
**Fail:** the template is listed even with the switch off.
Fallback: give `alert-analyzer` access to nothing but the `Analyzer` template with a template ACL if licensed, and otherwise accept the residual risk and have the orchestrator pin the workspace so the agent never needs to create one.

- [ ] **Step 6: Archive the probe chats**

```bash
for c in "$CHAT_OFF" "$CHAT_ON"; do as_analyzer PATCH "/api/v2/chats/$c" '{"archived":true}' >/dev/null; done
```

### Task 5: Spike D, a chat pinned to a stopped workspace, with the MCP servers attached

**Files:**

- Update: `~/spike-notes/notes.md`.

**Interfaces:**

- Consumes: `as_analyzer`, `capi`, `ORG`, `MODEL_ID`, `WS_ID`, `K8S_MCP_ID`, `GRAFANA_MCP_ID`.
- Produces: whether `workspace_id` on a stopped workspace works, who starts the workspace, the message shape that carries tool calls, a per-chat cost, and a clean verdict on review focus items 3 and 5.

The completion test below is the one `scripts/requesty-coder-sync.py` already uses: status `waiting` with an assistant reply, or `error`.

- [ ] **Step 1: Stop the workspace and confirm**

```bash
as_analyzer POST "/api/v2/workspaces/$WS_ID/builds" '{"transition":"stop"}' >/dev/null
until [ "$(as_analyzer GET "/api/v2/workspaces/$WS_ID" | jq -r .latest_build.status)" = stopped ]; do sleep 3; done
echo stopped
```

- [ ] **Step 2: Create the pinned chat with both MCP servers**

```bash
PROMPT='Do exactly two things. First, use your Kubernetes tool to count the namespaces in the cluster. Second, run the shell command hostname in the workspace. Reply with one line in the form: namespaces=N hostname=NAME. If you do not have a tool for either step, say which one is missing and do not guess.'
CHAT_PIN="$(as_analyzer POST /api/v2/chats "$(jq -n --arg org "$ORG" --arg m "$MODEL_ID" --arg ws "$WS_ID" --arg k "$K8S_MCP_ID" --arg g "$GRAFANA_MCP_ID" --arg p "$PROMPT" '{organization_id:$org, model_config_id:$m, client_type:"api", workspace_id:$ws, mcp_server_ids:[$k,$g], labels:{purpose:"spike-phase0"}, content:[{type:"text", text:$p}]}')" | jq -r .id)"
echo "CHAT_PIN=$CHAT_PIN"
```

If the request itself fails, the error text is the answer to "does `workspace_id` accept a stopped workspace?".
Record it verbatim.

- [ ] **Step 3: Watch the chat and the workspace together**

```bash
for i in $(seq 1 120); do
  c="$(as_analyzer GET "/api/v2/chats/$CHAT_PIN" | jq -r .status)"
  w="$(as_analyzer GET "/api/v2/workspaces/$WS_ID" | jq -r '.latest_build.status + "/" + .latest_build.transition')"
  printf '%3ss  chat=%s  workspace=%s\n' "$((i*5))" "$c" "$w"
  [ "$c" = waiting ] || [ "$c" = error ] || [ "$c" = requires_action ] && break
  sleep 5
done
as_analyzer GET "/api/v2/chats/$CHAT_PIN" | jq '{status, last_error}'
```

Expected observations to record: whether the workspace left `stopped` without anyone calling the builds endpoint, how long that took, and the chat's final status.

- [ ] **Step 4: Read what the agent did**

```bash
as_analyzer GET "/api/v2/chats/$CHAT_PIN/messages" | jq -r '.messages[] | [.role, (.content | tostring | .[0:300])] | @tsv'
```

The shape of tool calls is not documented.
If this prints too little, dump one message with `jq '.messages[2]'` and read the keys.
Record the names of the tools the agent called (for example a Kubernetes tool, an `execute` tool, `start_workspace`).

**Pass criteria, each recorded separately:**

- P1: the chat reaches `waiting` with an assistant reply that contains a namespace count and a hostname.
- P2: the workspace was started either automatically by the chat or by the agent's own `start_workspace` call, and no admin or orchestrator action was needed.
- P3: the agent used the Kubernetes tool and the in-workspace shell.

Failure branches and fallbacks:

- The request is refused for a stopped workspace, or the chat errors.
  Fallback (already in the design): the orchestrator starts the workspace first (task 3 shows how long that takes), waits for the agent to connect, then creates the chat.

- The workspace starts but the shell tool fails.
  Record the error and treat the agent-connection wait as part of the orchestrator's start step.

- `workspace_id` is ignored and the agent creates its own workspace.
  Fallback: the spec's "ephemeral workspace" or "MCP only" alternatives become the plan.

- [ ] **Step 5: Control run without `mcp_server_ids` (review focus 3)**

Use a running workspace so only the MCP difference matters:

```bash
CHAT_NOMCP="$(as_analyzer POST /api/v2/chats "$(jq -n --arg org "$ORG" --arg m "$MODEL_ID" --arg ws "$WS_ID" --arg p "$PROMPT" '{organization_id:$org, model_config_id:$m, client_type:"api", workspace_id:$ws, labels:{purpose:"spike-phase0"}, content:[{type:"text", text:$p}]}')" | jq -r .id)"
for i in $(seq 1 60); do s="$(as_analyzer GET "/api/v2/chats/$CHAT_NOMCP" | jq -r .status)"; [ "$s" = waiting ] || [ "$s" = error ] && break; sleep 5; done
as_analyzer GET "/api/v2/chats/$CHAT_NOMCP/messages" | jq -r '.messages[] | select(.role=="assistant") | (.content | tostring | .[0:400])' | tail -3
```

**Pass:** the answer says the Kubernetes tool is missing and does not invent a count.
Record the answer.
If it invents a number, the system prompt in the design must forbid guessing, and the orchestrator must verify the report's evidence before posting.

- [ ] **Step 6: Ownership probe (review focus 5)**

Pin a chat to a workspace owned by someone else, using the admin workspace found in task 3 step 7:

```bash
ADMIN_WS_ID="$(capi GET "/api/v2/workspaces?q=owner:$ADMIN_NAME" | jq -r '.workspaces[0].id')"
as_analyzer POST /api/v2/chats "$(jq -n --arg org "$ORG" --arg m "$MODEL_ID" --arg ws "$ADMIN_WS_ID" '{organization_id:$org, model_config_id:$m, client_type:"api", workspace_id:$ws, labels:{purpose:"spike-phase0"}, content:[{type:"text", text:"Reply with the single word ok."}]}')" | jq '.id // .'
```

**Pass:** an error such as 403 or 404, or a refusal in the response.
If a chat is created, archive it at once, and record that pinning is not ownership-checked, which makes the "analyzer user only" restriction the only guard.

- [ ] **Step 7: Per-chat cost, then archive**

```bash
for c in "$CHAT_PIN" "$CHAT_NOMCP"; do printf '%s ' "$c"; as_analyzer GET "/api/v2/chats/$c/cost" | jq -c .; done
for c in "$CHAT_PIN" "$CHAT_NOMCP"; do as_analyzer PATCH "/api/v2/chats/$c" '{"archived":true}' >/dev/null; done
```

Record the costs.
They calibrate the daily chat cap in the design.

### Task 6: Spike E, an Uptime Kuma push monitor

**Files:**

- Update: `~/spike-notes/notes.md`.

**Interfaces:**

- Consumes: nothing from earlier tasks.
- Produces: the maximum useful `msg` length, the exact response bodies, the notification behavior on repeated DOWN pushes, what happens on silence, the longest interval the monitor allows, and a recommendation on keepalive.

The existing helper `push_heartbeat` in `scripts/requesty-coder-sync.py` truncates `msg` to 200 characters, which is a conservative choice and not a measured limit.
The design's message is a summary of up to 200 characters plus a Coder chat link, so the real limit matters.

- [ ] **Step 1: [operator] Create a throwaway push monitor**

In Uptime Kuma (`https://uptime.vigihome.net`, log in with the native admin from Bitwarden), add a monitor:
type **Push**, name `spike-analyzer-push`, heartbeat interval **60** seconds, retries **0**.
Attach the same email notification the backup monitors use, so notification counts can be observed.
Save, and copy the push URL.

- [ ] **Step 2: Load the URL without echoing it, and define `kpush`**

```bash
printf 'Push URL: '; stty -echo; read -r KUMA_PUSH_URL; stty echo; printf '\n'
export KUMA_PUSH_URL
kpush() { # usage: kpush STATUS MSG  -- the URL stays out of argv
  curl -sS -G -K <(printf 'url = "%s"\n' "$KUMA_PUSH_URL") \
    --data-urlencode "status=$1" --data-urlencode "msg=$2" --data-urlencode "ping="
  printf '\n'
}
kpush up "spike: baseline up"
```

Expected response body: `{"ok":true}`.
In the UI the monitor is UP with that message.

- [ ] **Step 3: Message length**

Push DOWN with messages of increasing length.
Each message starts with its own length, so truncation is visible in the UI.

```bash
for n in 100 200 300 500 1000 2000 4000; do
  m="$(printf '%s ' "len=$n"; head -c "$n" /dev/zero | tr '\0' 'x')"
  printf '%s: ' "$n"; kpush down "$m"
  sleep 2
done
```

**[operator]** For each length, open the monitor's page in the UI and note whether the full message is shown or truncated, and where.
**Pass:** a message of at least 300 characters is stored intact (the design needs about 260).
Record the point at which it truncates or the push is rejected.
If the server returns an error for the long ones (for example a 414 or 400), record the status code and body.

- [ ] **Step 4: Error responses**

```bash
curl -sS -o /dev/null -w 'missing token -> %{http_code}\n' "https://uptime.vigihome.net/api/push/does-not-exist?status=up"
kpush sideways "spike: invalid status value"
```

Record both responses.
The orchestrator must treat a non-200 or a body without `"ok":true` as a failed push.

- [ ] **Step 5: Repeated DOWN pushes and notifications (review focus 1, first half)**

```bash
for i in 1 2 3 4 5; do kpush down "spike: repeated down $i"; sleep 5; done
kpush up "spike: recovered"
```

**[operator]** Count the emails: expect one DOWN notification (not five), and one recovery notification.
Count the heartbeat rows in the UI.
Record both counts.

- [ ] **Step 6: Silence turns the monitor DOWN (review focus 1, second half)**

```bash
kpush up "spike: up, then silence"
sleep 150
```

**[operator]** Look at the monitor in the UI.
Expected: after the 60 second interval passes with no push, the monitor goes DOWN with a "no heartbeat" style message, even though the last push was UP.
Record the message text and how long it took.

This confirms that "push UP once on resolve" is not enough.
The orchestrator must keep re-pushing the current state, UP or DOWN with the finding, at a period shorter than the interval, for as long as it runs.

- [ ] **Step 7: Longest interval the monitor allows**

**[operator]** Edit `spike-analyzer-push` and try to set the heartbeat interval to 86400 seconds (24 hours), then to 2073600.
Record the largest value the form accepts.
Then set it back to 60.
A long interval removes the need for frequent keepalives but delays detection of a dead orchestrator, so record both options for the design.

- [ ] **Step 8: Delete the throwaway monitor**

**[operator]** Delete `spike-analyzer-push` in the UI.
Then clear the variable in the shell:

```bash
unset KUMA_PUSH_URL
```

**Pass overall:** the messages fit, the responses are understood, repeated DOWN pushes notify once, and the silence behavior is recorded.
**Fail:** the message limit is below 100 characters.
Fallback: the Uptime Kuma message carries only the alert name and a short link, and the summary lives in the Coder chat.

### Task 7: Record the findings, update the spec, clean up

**Files:**

- Modify: `docs/superpowers/specs/2026-09-30-alert-analyzer-design.md` (append "Spike findings", update "Phases" and "Open items for the plan").
- Read: `~/spike-notes/notes.md`.

**Interfaces:**

- Consumes: everything recorded in tasks 2 to 6.

- Produces: a spec that states what is verified, what changed in the design, and which fallbacks were taken.

- [ ] **Step 1: Walk the cleanup ledger**

```bash
coder delete alert-analyzer/spike-ws --yes
coder templates delete spike-analyzer --yes
coder tokens remove spike-phase0-analyzer --user alert-analyzer 2>/dev/null || echo "check: coder tokens list --user alert-analyzer"
coder tokens list --user alert-analyzer
coder tokens remove spike-phase0
unset ANALYZER_TOKEN CODER_SESSION_TOKEN
```

The workspace goes first because a template cannot be deleted while a workspace still uses it.
If a command's flags differ, use its `--help`.
Expected afterwards: no `spike-*` template, no `spike-ws`, and no active tokens for `alert-analyzer`.
Confirm in the Coder UI that no chat labeled `purpose=spike-phase0` is left unarchived.
Delete the notes that contain no durable value:

```bash
rm -f ~/spike-notes/template-before.json ~/spike-notes/template-after-off.json /tmp/spike-mcp.json
```

- [ ] **Step 2: Write the "Spike findings" section in the spec**

Append this table to the spec and fill every cell from `~/spike-notes/notes.md`, with the exact evidence and the design impact:

```markdown
## Spike findings

Run on YYYY-MM-DD against Coder v2.37.0 and Uptime Kuma, from gandalf.

| Spike                                     | Result            | Evidence                                   | Design impact |
| ----------------------------------------- | ----------------- | ------------------------------------------ | ------------- |
| A. MCP server ids                         | pass, partial, fail | list path, both ids, agreement with state |               |
| B. non-human user, token, start and stop  |                   | user form that worked, max token lifetime, stop and start durations |   |
| C. restrict the agent's templates         |                   | licensed or not, field name, templates the agent could see |      |
| D. chat pinned to a stopped workspace     |                   | who started it, tool names, cost per chat  |               |
| E. Uptime Kuma push                       |                   | message limit, responses, silence behavior, max interval |  |
```

Replace `YYYY-MM-DD` with the actual date, and write real results into every cell before committing.
Do not leave the table with empty cells.

- [ ] **Step 3: Update the design where a spike changed it**

For each of these, edit the spec's body and say why:

- **Keepalive:** the orchestrator re-pushes its current state to the findings monitor every half interval, and a silent monitor is itself the orchestrator-is-dead signal.
  Decide whether that replaces the separate heartbeat monitor, and change the "Orchestrator" and "Failure handling" sections to match.

- **Message budget:** set the summary-plus-link limit to the measured limit.

- **Workspace start:** state whether the orchestrator starts the workspace or the chat does, and the wait budget from task 3.

- **Template restriction:** state which control is used and the list of live templates whose agent switch must be turned off.

- **Token lifetime:** state the maximum, the chosen lifetime, and the expiry check.

- [ ] **Step 4: Update "Phases" and "Open items for the plan"**

Mark phase 0 done in "Phases".
Remove the open items that the spikes answered.
Keep the ones they did not.

- [ ] **Step 5: Lint, commit and open a PR**

```bash
cd ~/git/nickvigilante/homelab
git fetch -q && git worktree add -q .worktrees/spec-phase0-findings -b spec-phase0-findings origin/main
cd .worktrees/spec-phase0-findings
# edit docs/superpowers/specs/2026-09-30-alert-analyzer-design.md here (steps 2 to 4)
pre-commit run --files docs/superpowers/specs/2026-09-30-alert-analyzer-design.md
git add docs/superpowers/specs/2026-09-30-alert-analyzer-design.md
git commit -F - <<'EOF'
docs(spec): record the alert analyzer phase 0 spike findings

Phase 0 probes answered the open Coder and Uptime Kuma questions. Record the
results, the design changes they forced, and the fallbacks taken.

Assisted-by: AI
EOF
git push -u origin spec-phase0-findings
gh pr create --base main --head spec-phase0-findings --title "docs(spec): record the alert analyzer phase 0 spike findings" --body-file ~/spike-notes/pr-body.md
```

Write `~/spike-notes/pr-body.md` first, using the repo's PR template (Summary, Before merge, Test plan) and ending with a `---` rule and the line `🤖 Built with AI assistance.`
Do not start phase 1 until the operator has read the findings.

## Self-review

- **Spec coverage:** the five phase 0 bullets map to tasks 5 (chat with a stopped workspace), 3 (user, token, start and stop), 4 (restrict the user to the `Analyzer` template), 2 (MCP server ids) and 6 (Uptime Kuma push).
  Task 7 updates the spec's open items.
- **Placeholders:** the commands use shell variables, not angle brackets.
  Three steps ask the operator to paste a value read from the preceding output (`MCP_LIST_PATH`, `MODEL_ID`, and the findings table), and each says exactly where the value comes from.
- **Names:** `capi`, `as_analyzer`, `ORG`, `TEMPLATE_ID`, `WS_ID`, `MODEL_ID`, `K8S_MCP_ID`, `GRAFANA_MCP_ID`, `KUMA_PUSH_URL` and `kpush` are defined once and used consistently.
- **Unconfirmed in the public documentation, with a probe in the step:** the list path for MCP server configs, the accepted `login_type` values and whether `service_account` needs an email, whether an admin can mint a token for another user, the template ACL endpoint and its license, the name of the setting field behind the agent switch, the exact build status strings, and the shape of chat message content.
  The build creation endpoint, the user creation endpoint, and the chat routes under `/api/v2` are confirmed in the v2.37.0 route table.
