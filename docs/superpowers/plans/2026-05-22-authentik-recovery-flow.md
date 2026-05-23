# Authentik recovery flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the email-based password recovery flow for `homelab-users` (akadmin excluded), with rate-limit + reputation abuse controls and a documented akadmin operator runbook.

**Architecture:** One Helm values.yaml edit overrides the `AUTHENTIK_EMAIL__FROM` display name. Everything else is UI configuration against the live Authentik (2026.2.2) — group, three policies, one stage edit, two flow bindings — captured in the README so a from-scratch rebuild without the postgres restore can replay the clicks. The PR carries the values.yaml change, two new README sections (recovery + akadmin runbook), and the audit doc resolution.

**Tech Stack:** Authentik 2026.2.2 (ghcr.io/goauthentik/server), Helm (authentik chart), Forward Email SMTP relay (already wired in PR #56), Bitwarden CLI (for akadmin runbook).

**Spec reference:** `docs/superpowers/specs/2026-05-20-authentik-recovery-flow-design.md`

**Issue:** #62 (closes when this lands)

**Branch:** `authentik-recovery-flow` (already created off main, spec already committed as `c30baa1`)

**Commands run from:** The operator's laptop unless tagged otherwise. `kubectl` context must be the homelab cluster. Authentik UI is `https://authentik.vigihome.net`, signed in as akadmin.

---

## File / state structure

| File / object | Status | Owner |
|---|---|---|
| `k8s/authentik/values.yaml:97-101` | Modify | Task 1 |
| `k8s/authentik/README.md` (new "Self-service password recovery" + "akadmin recovery — last-resort paths" sections) | Modify | Task 10 |
| `audits/tier-1-authentik.md` (flip finding 5-i to Resolved, clean Open follow-ups) | Modify | Task 11 |
| Authentik group `homelab-users` | Verify (likely exists; create if missing) | Task 3 |
| Authentik policy `recovery-allowed-group` | Create | Task 4 |
| Authentik policy `recovery-rate-limit` | Create | Task 5 |
| Authentik policy `recovery-reputation` | Create | Task 6 |
| Authentik stage `default-email-recovery` (edit subject + token_expiry) | Modify | Task 7 |
| Authentik flow `default-recovery-flow` (bind 3 policies) | Modify | Task 8 |
| Authentik flow `default-authentication-flow` (set recovery_flow) | Modify | Task 9 — **live cutover** |

---

## Execution ordering rationale

UI clicks must happen in this order to avoid a window where the "Forgot password?" link is visible without the abuse controls bound:

1. Create policies (Tasks 4-6) — exist but bound to nothing
2. Configure stage (Task 7) — token expiry + subject
3. Bind policies to recovery flow (Task 8) — abuse controls in place
4. Bind recovery flow to auth flow (Task 9) — **link goes live here**
5. Test (Tasks 13-17) — verify abuse controls actually fire

The Helm change (Task 1 file edit + Task 2 apply) is independent of the UI work and can run in parallel with Tasks 3-8. It does need to land before testing (Task 13's email shows the new From: header).

---

## Task 1: Add display-name override to AUTHENTIK_EMAIL__FROM

**Files:**
- Modify: `k8s/authentik/values.yaml:73-101`

Current state: `AUTHENTIK_EMAIL__FROM` pulls from the `smtp-relay` Secret's `smtp-username` field — same value as `AUTHENTIK_EMAIL__USERNAME` (the bare address `noreply@vigihome.net`). The change separates the SMTP envelope sender (kept as the bare address, Forward Email requires it) from the From: display name (a literal `vigihome auth <noreply@vigihome.net>`).

- [ ] **Step 1: Update the SMTP comment block**

Replace lines 73-76 of `k8s/authentik/values.yaml`:

```yaml
    # The SMTP username also serves as the From address — Forward Email
    # ties auth to a verified sender at the configured domain, and we
    # reuse it for FROM rather than tracking a separate field.
```

with:

```yaml
    # SMTP envelope sender (USERNAME) is the bare address — Forward Email
    # ties SMTP AUTH to a verified sender at the configured domain.
    # From: display name (FROM) is set independently as a literal below
    # so recovery emails arrive as "vigihome auth <noreply@vigihome.net>"
    # rather than the default "authentik <…>".
```

- [ ] **Step 2: Replace the FROM env entry with a literal value**

Replace lines 97-101 of `k8s/authentik/values.yaml`:

```yaml
    - name: AUTHENTIK_EMAIL__FROM
      valueFrom:
        secretKeyRef:
          name: smtp-relay
          key: smtp-username
```

with:

```yaml
    - name: AUTHENTIK_EMAIL__FROM
      value: "vigihome auth <noreply@vigihome.net>"
```

- [ ] **Step 3: Render the chart locally and diff against the live ConfigMap/Deployment**

Run from the repo root:

```bash
helm template authentik authentik/authentik \
  --version "$(helm list -n auth -f '^authentik$' -o json | jq -r '.[0].chart' | sed 's/^authentik-//')" \
  -n auth -f k8s/authentik/values.yaml \
  --show-only templates/server-deployment.yaml \
  | grep -A1 AUTHENTIK_EMAIL__FROM
```

Expected output:

```
            - name: AUTHENTIK_EMAIL__FROM
              value: vigihome auth <noreply@vigihome.net>
```

If the rendered YAML still shows a `valueFrom: secretKeyRef`, the edit didn't land — re-check Step 2.

- [ ] **Step 4: Commit**

```bash
git add k8s/authentik/values.yaml
git commit -m "Authentik: override AUTHENTIK_EMAIL__FROM display name (#62)

Separate the SMTP envelope sender (USERNAME, bare address) from the
From: display name (FROM, literal). Recovery emails now arrive as
'vigihome auth <noreply@vigihome.net>' instead of the default
'authentik <noreply@vigihome.net>'."
```

---

## Task 2: Apply the Helm change to the live cluster

**Files:** None (cluster state only)

- [ ] **Step 1: Pull latest chart into the helm cache (in case the user's local cache is stale)**

```bash
helm repo update authentik
```

Expected: `...Successfully got an update from the "authentik" chart repository`.

- [ ] **Step 2: Apply the chart upgrade — PIN THE VERSION**

⚠ **An unpinned `helm upgrade authentik authentik/authentik` pulls the
latest chart and silently bumps the Authentik + bundled postgres
images.** On 2026-05-23 that drifted 2026.2.2 → 2026.5.0 and the new
postgres image `ImagePullBackOff`'d, taking the `auth` namespace down
(recovered via `helm rollback`). This is a values-only change, so pin
to the currently-installed chart version:

```bash
CHART_VER="$(helm list -n auth -f '^authentik$' -o json | jq -r '.[0].chart' | sed 's/^authentik-//')"
echo "pinning to chart $CHART_VER"   # expect 2026.2.2
helm upgrade authentik authentik/authentik --version "$CHART_VER" -n auth -f k8s/authentik/values.yaml
```

Expected: `Release "authentik" has been upgraded. Happy Helming!` and the revision number increments. The server Deployment rolls one pod (env-var-only change, no image move).

- [ ] **Step 3: Wait for rollout**

```bash
kubectl -n auth rollout status deployment/authentik-server --timeout=120s
kubectl -n auth rollout status deployment/authentik-worker --timeout=120s
```

Expected: both `deployment "..." successfully rolled out`.

- [ ] **Step 4: Verify the new env var is live in the running pod**

```bash
kubectl -n auth exec deployment/authentik-server -- printenv AUTHENTIK_EMAIL__FROM
```

Expected exact output:

```
vigihome auth <noreply@vigihome.net>
```

If the output is just the bare address, the rollout hasn't picked up the new values — re-run Step 2 or check `helm history authentik -n auth`.

---

## Task 3: Confirm (or create) the homelab-users group

**Files:** None (Authentik UI / postgres)

The `homelab-users` group is referenced from existing policy bindings on Coder, HA, and other downstream applications (per audit finding 4d, `audits/tier-1-authentik.md:62`). It almost certainly already exists, but verify before depending on it.

- [ ] **Step 1: Check the group exists via API**

```bash
# Get a session cookie or use the bootstrap token. Bootstrap token is
# easiest from the operator laptop:
TOKEN="$(bw get item 'Homelab Authentik' \
  | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/core/groups/?name=homelab-users' \
  | jq -r '.results[] | "\(.pk)\t\(.name)\t\(.users | length) members"'
```

Expected: one line showing the group's PK, name, and user count (likely 2-5 members today).

- [ ] **Step 2 (only if Step 1 returned nothing): Create the group via UI**

Navigate: Admin interface → Directory → Groups → "Create"
- Name: `homelab-users`
- Is superuser: OFF
- Parent: (leave blank)
- Save.

Then add each non-admin human user via the group's Users tab. (akadmin stays out — it's in `akadmins`, not here.)

Re-run Step 1 to confirm.

- [ ] **Step 3: Capture the group's PK for later cross-reference**

Note the PK from Step 1 in a scratchpad — useful if you need to grep logs later. Not committed anywhere.

---

## Task 4: Create the recovery-allowed-group policy

**Files:** None (Authentik UI / postgres)

A Group Membership policy gates the entire recovery flow on membership in `homelab-users`. akadmin (in `akadmins`) is naturally excluded.

- [ ] **Step 1: Create the policy**

Navigate: Admin interface → Customization → Policies → "Create" → "Group Membership Policy"
- Name: `recovery-allowed-group`
- Group: `homelab-users`
- **Negate result:** OFF (unchecked)
- **Execution logging:** ON (checked) — leave on for the first month, turn off after; useful during the testing phase to confirm the policy is firing
- Save.

- [ ] **Step 2: Verify via API**

```bash
TOKEN="$(bw get item 'Homelab Authentik' \
  | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/policies/group_membership/?name=recovery-allowed-group' \
  | jq '.results[] | {name, group_obj: .group_obj.name, negate, execution_logging}'
```

Expected:

```json
{
  "name": "recovery-allowed-group",
  "group_obj": "homelab-users",
  "negate": false,
  "execution_logging": true
}
```

---

## Task 5: Create the recovery-rate-limit policy

**Files:** None (Authentik UI / postgres)

Authentik 2026.2 exposes Rate Limit Policy as a first-class primitive (verified by the spec's version check).

- [ ] **Step 1: Create the policy**

Navigate: Admin interface → Customization → Policies → "Create" → "Rate Limit Policy"
- Name: `recovery-rate-limit`
- Rate: `3`
- Per: `3600` (seconds — 1 hour)
- Key (context expression): `request.context.remote_ip`
- Save.

If the UI presents the Per field as a duration string rather than seconds, use `hours=1` instead of `3600`.

- [ ] **Step 2: Verify via API**

```bash
TOKEN="$(bw get item 'Homelab Authentik' \
  | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/policies/rate_limit/?name=recovery-rate-limit' \
  | jq '.results[] | {name, rate, per, key}'
```

Expected:

```json
{
  "name": "recovery-rate-limit",
  "rate": 3,
  "per": 3600,
  "key": "request.context.remote_ip"
}
```

If the `rate_limit` endpoint 404s, your Authentik version may have renamed it — confirm by running `curl -sSf -H "Authorization: Bearer $TOKEN" 'https://authentik.vigihome.net/api/v3/policies/' | jq '.results | unique_by(.component) | .[].component'` and pick the closest match.

---

## Task 6: Create the recovery-reputation policy

**Files:** None (Authentik UI / postgres)

Reputation policy denies the recovery flow when `ak_reputation_score < -3` for the requesting IP or username.

**⚠ Important semantic:** Authentik reputation policies pass (`passing=true`) when reputation is **bad** (below threshold). Bind this policy to a deny path with `negate=OFF`. See memory: feedback_authentik_reputation_policy_semantics.

- [ ] **Step 1: Create the policy**

Navigate: Admin interface → Customization → Policies → "Create" → "Reputation Policy"
- Name: `recovery-reputation`
- Threshold: `-3`
- Check IP: ON (checked)
- Check Username: ON (checked)
- **Negate result:** OFF (unchecked)
- Save.

- [ ] **Step 2: Verify via API**

```bash
TOKEN="$(bw get item 'Homelab Authentik' \
  | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/policies/reputation/?name=recovery-reputation' \
  | jq '.results[] | {name, threshold, check_ip, check_username}'
```

Expected:

```json
{
  "name": "recovery-reputation",
  "threshold": -3,
  "check_ip": true,
  "check_username": true
}
```

---

## Task 7: Configure default-email-recovery stage

**Files:** None (Authentik UI / postgres)

The built-in `default-email-recovery` stage handles the actual email send + token. Tweak the subject and the token expiry.

- [ ] **Step 1: Edit the stage**

Navigate: Admin interface → Flows & Stages → Stages → click `default-email-recovery` → "Edit"
- Subject: `vigihome.net — password reset requested`
- Token expiry: `hours=1`
- Template: leave on the default (`email/password_reset.html`)
- From address: leave blank (falls back to `AUTHENTIK_EMAIL__FROM`)
- Save.

- [ ] **Step 2: Verify via API**

```bash
TOKEN="$(bw get item 'Homelab Authentik' \
  | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/stages/email/?name=default-email-recovery' \
  | jq '.results[] | {name, subject, token_expiry, template}'
```

Expected:

```json
{
  "name": "default-email-recovery",
  "subject": "vigihome.net — password reset requested",
  "token_expiry": "hours=1",
  "template": "email/password_reset.html"
}
```

If `token_expiry` shows as `"60"` or `"3600"` (seconds) rather than a duration string, your Authentik version stores it as seconds — that's also fine; 3600s = 1 hour.

---

## Task 8: Bind the three policies to default-recovery-flow

**Files:** None (Authentik UI / postgres)

Bind policies in this order so the cheapest checks run first (group membership is in-memory; reputation needs a postgres lookup; rate limit needs a counter increment).

- [ ] **Step 1: Open the recovery flow's policy bindings**

Navigate: Admin interface → Flows & Stages → Flows → click `default-recovery-flow` → "Policy / Group / User Bindings" tab.

- [ ] **Step 2: Bind recovery-allowed-group at order 10**

Click "Create binding"
- Policy: `recovery-allowed-group`
- Order: `10`
- Negate: OFF
- Enabled: ON
- Timeout: 30 (default)
- Save.

- [ ] **Step 3: Bind recovery-reputation at order 20**

Click "Create binding"
- Policy: `recovery-reputation`
- Order: `20`
- Negate: OFF
- Enabled: ON
- Timeout: 30 (default)
- Save.

- [ ] **Step 4: Bind recovery-rate-limit at order 30**

Click "Create binding"
- Policy: `recovery-rate-limit`
- Order: `30`
- Negate: OFF
- Enabled: ON
- Timeout: 30 (default)
- Save.

- [ ] **Step 5: Verify all three bindings via API**

```bash
TOKEN="$(bw get item 'Homelab Authentik' \
  | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

# Find the flow's pk
FLOW_PK="$(curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/flows/instances/?slug=default-recovery-flow' \
  | jq -r '.results[0].pk')"

curl -sSf -H "Authorization: Bearer $TOKEN" \
  "https://authentik.vigihome.net/api/v3/policies/bindings/?target=$FLOW_PK" \
  | jq '.results | sort_by(.order) | .[] | {order, policy_obj: .policy_obj.name, negate, enabled}'
```

Expected three objects in order 10/20/30 with negate=false, enabled=true, names matching the three policies.

---

## Task 9: ⚠ LIVE CUTOVER — bind default-recovery-flow to default-authentication-flow

**Files:** None (Authentik UI / postgres)

This is the moment the "Forgot password?" link appears on the login page for all users. Once this binding is in place, `homelab-users` members can immediately request recovery emails (gated by Tasks 4-8). Before flipping, sanity-check Tasks 4-8 are all in place via the API queries from each task.

- [ ] **Step 1: Confirm preconditions**

```bash
TOKEN="$(bw get item 'Homelab Authentik' \
  | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

echo "--- policies that should exist (3 lines expected) ---"
for name in recovery-allowed-group recovery-rate-limit recovery-reputation; do
  curl -sSf -H "Authorization: Bearer $TOKEN" \
    "https://authentik.vigihome.net/api/v3/policies/all/?name=$name" \
    | jq -r '.results[] | "\(.name)\t\(.component)"'
done

echo "--- bindings on default-recovery-flow (3 lines expected) ---"
FLOW_PK="$(curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/flows/instances/?slug=default-recovery-flow' \
  | jq -r '.results[0].pk')"
curl -sSf -H "Authorization: Bearer $TOKEN" \
  "https://authentik.vigihome.net/api/v3/policies/bindings/?target=$FLOW_PK" \
  | jq -r '.results | sort_by(.order) | .[] | "\(.order)\t\(.policy_obj.name)"'

echo "--- stage settings ---"
curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/stages/email/?name=default-email-recovery' \
  | jq -r '.results[] | "\(.subject)\t\(.token_expiry)"'
```

All three sections must show the expected output. If any line is missing, fix that task first.

- [ ] **Step 2: Bind the recovery flow on the authentication flow**

Navigate: Admin interface → Flows & Stages → Flows → click `default-authentication-flow` → "Edit"
- **Recovery flow:** select `default-recovery-flow` from the dropdown
- Leave all other fields unchanged
- Save.

- [ ] **Step 3: Verify the link appears on the login page**

Open a fresh incognito window. Visit `https://authentik.vigihome.net/`. Expected: the login page now shows a "Forgot password?" link below the username field. **Do not click it yet** — formal tests are Tasks 13-17.

- [ ] **Step 4: Verify via API**

```bash
TOKEN="$(bw get item 'Homelab Authentik' \
  | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/flows/instances/?slug=default-authentication-flow' \
  | jq '.results[0] | {slug, recovery_flow_obj: .recovery_flow_obj.slug}'
```

Expected:

```json
{
  "slug": "default-authentication-flow",
  "recovery_flow_obj": "default-recovery-flow"
}
```

---

## Task 10: Add README sections (self-service recovery + akadmin runbook)

**Files:**
- Modify: `k8s/authentik/README.md` (append two sections at the end of "Day-to-day operations" section, before "Pointer to Tier-2" if present)

Determine the right insertion point: the new sections belong under "Day-to-day operations" (line 134 onwards). After the existing "Promoting an OIDC user to owner/admin in a downstream" section (ends around line 296), append the two new sections.

- [ ] **Step 1: Append the self-service recovery section**

Append to `k8s/authentik/README.md`:

```markdown

### Self-service password recovery (homelab-users only)

`homelab-users` members can reset their own password via the "Forgot
password?" link on `https://authentik.vigihome.net/`. The flow is
**not** available to akadmin or any other admin account — see the
"akadmin recovery — last-resort paths" section below for the operator
runbook.

**How it's wired (all UI-managed, lives in postgres):**

| Object | Where | Settings |
|---|---|---|
| Group `homelab-users` | Directory → Groups | Membership gates which users see the recovery link |
| Policy `recovery-allowed-group` | Customization → Policies (Group Membership) | Group: `homelab-users`, negate: OFF |
| Policy `recovery-rate-limit` | Customization → Policies (Rate Limit) | 3 requests / 3600s, key: `request.context.remote_ip` |
| Policy `recovery-reputation` | Customization → Policies (Reputation) | Threshold: -3, check IP + username, negate: OFF |
| Stage `default-email-recovery` | Flows & Stages → Stages | Subject: `vigihome.net — password reset requested`, token_expiry: `hours=1` |
| Flow `default-recovery-flow` | Flows & Stages → Flows | 3 policy bindings (orders 10/20/30) on the 3 policies above |
| Flow `default-authentication-flow` | Flows & Stages → Flows | `recovery_flow` field set to `default-recovery-flow` |
| Helm env `AUTHENTIK_EMAIL__FROM` | `k8s/authentik/values.yaml` | `"vigihome auth <noreply@vigihome.net>"` (display name) |

**Abuse posture:**

- Group membership: non-`homelab-users` accounts (including akadmin)
  cannot proceed past the identification stage. The on-screen response
  is generic ("if an account exists…") — same response for valid and
  invalid identifiers, so the flow doesn't leak account existence.
- Rate limit: 3 emails per hour per requesting IP. Fourth attempt
  shows a generic "try again later" message and sends no email.
- Reputation: after roughly 3 failed logins in a short window, the
  requesting IP's reputation score drops below -3 and the recovery
  flow refuses to advance for that IP (same generic response).

**Why these settings (full rationale):**

See `docs/superpowers/specs/2026-05-20-authentik-recovery-flow-design.md`.

**Postgres dependency:**

All of the above (except the Helm env var) lives in the `authentik`
postgres database. The nightly restic backup captures the postgres
PVC, so a worst-case rebuild restores everything. A from-scratch
rebuild **without** the postgres restore (e.g., if `AUTHENTIK_SECRET_KEY`
is lost and the DB has to be re-initialized) means redoing the UI
clicks documented above. This is captured by issue #104 (Blueprints
migration), which would let the same config land as YAML applied by
the Authentik worker at startup.

**Reapply on rebuild (in order):**

1. `homelab-users` group exists (likely re-created automatically by
   the OIDC enrollment property mapping).
2. Create the three policies above.
3. Edit the `default-email-recovery` stage (subject + token_expiry).
4. Bind the three policies to `default-recovery-flow` at orders
   10/20/30.
5. Set `default-authentication-flow.recovery_flow` to
   `default-recovery-flow`.

### akadmin recovery — last-resort paths

akadmin is the break-glass account and is deliberately excluded from
the self-service recovery flow above. When akadmin's password is lost
or needs rotating, use one of these two paths.

<!-- TODO: mirror this entire section to the Outline wiki when #92 lands -->

**Path A — postgres-direct via `ak shell`** (preferred when ssh +
kubectl access to the cluster are available, which is the common case
from gandalf or the operator laptop on tailnet):

```bash
# On the laptop (kubectl context = homelab):

# 1. Sanity-check akadmin exists
kubectl -n auth exec -it statefulset/authentik-postgresql -- \
  bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U authentik -d authentik \
    -c "SELECT id, username, email FROM authentik_core_user WHERE username = '"'"'akadmin'"'"';"'

# 2. Reset the password via the management shell (Django hashes it
#    before writing, so the literal here is plaintext)
kubectl -n auth exec -it deployment/authentik-server -- \
  ak shell -c "from authentik.core.models import User; \
    u = User.objects.get(username='akadmin'); \
    u.set_password('REPLACE_WITH_NEW_PASSWORD'); \
    u.save()"

# 3. Update Bitwarden item 'Homelab Authentik' with the new password
```

**Path B — bootstrap-token via the Authentik API** (for when shell
access is harder than API access, but the bootstrap token cached in
Bitwarden is available):

```bash
# On any machine with curl + bw + jq + tailnet access to vigihome:

# 1. Unlock Bitwarden and pull the bootstrap token
BW_SESSION="$(bw unlock --raw)"
TOKEN="$(bw get item 'Homelab Authentik' \
  | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

# 2. Look up akadmin's primary key
AKADMIN_PK="$(curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/core/users/?username=akadmin' \
  | jq -r '.results[0].pk')"

# 3. Reset the password
curl -sSf -H "Authorization: Bearer $TOKEN" \
  -X POST "https://authentik.vigihome.net/api/v3/core/users/$AKADMIN_PK/set_password/" \
  -H 'Content-Type: application/json' \
  -d '{"password":"REPLACE_WITH_NEW_PASSWORD"}'

# 4. Update Bitwarden item 'Homelab Authentik' with the new password
```

**Bootstrap token rotation:** the bootstrap token is admin-equivalent.
If used during recovery (Path B), rotate it afterward — edit
`AUTHENTIK_BOOTSTRAP_TOKEN` in chart values and `helm upgrade authentik
-n auth -f k8s/authentik/values.yaml`. Update Bitwarden in lockstep.

**Testing the recovery flow:**

The five end-to-end scenarios documented in
`docs/superpowers/specs/2026-05-20-authentik-recovery-flow-design.md`
(Testing section) are the regression checklist. Re-run them whenever
the recovery flow is touched (chart upgrade, Authentik upgrade,
policy change).
```

- [ ] **Step 2: Commit the README**

```bash
git add k8s/authentik/README.md
git commit -m "Authentik: README — recovery flow + akadmin runbook (#62)

Two new sections under Day-to-day operations:
- Self-service password recovery — wiring table, abuse posture, and
  the reapply-on-rebuild checklist for the UI-managed config.
- akadmin recovery — last-resort paths — Path A (postgres-direct via
  ak shell) and Path B (bootstrap-token API). Marked for Outline
  wiki mirror when #92 lands."
```

---

## Task 11: Update the audit doc

**Files:**
- Modify: `audits/tier-1-authentik.md:64-71` (Check 5 — self-service flows)
- Modify: `audits/tier-1-authentik.md:94-99` (Open follow-ups)

- [ ] **Step 1: Flip Check 5 finding 5-i to resolved**

Replace lines 64-71 of `audits/tier-1-authentik.md`:

```markdown
### Check 5 — self-service flows

- The default Authentik recovery flow assumes outbound email exists,
  and at audit time none did. Tracked as finding 5-i (email-based
  recovery). **Unblocked** by PR #56 (SMTP wiring); the flow itself
  is now tracked as #62 — it needs user
  sign-off on the exact UX (who can recover what, lockout window,
  rate limits) before it lands.
```

with:

```markdown
### Check 5 — self-service flows

- The default Authentik recovery flow assumes outbound email exists,
  and at audit time none did. Tracked as finding 5-i (email-based
  recovery). **Resolved** by PR #56 (SMTP wiring) +
  PR #<BACKFILL_AFTER_PR_CREATE> (flow binding, abuse policies,
  akadmin runbook). Email-based self-recovery is enabled for
  `homelab-users` (akadmin out of scope per blast-radius decision);
  abuse controls are a 3/hr per-IP rate limit plus a reputation deny
  at score < -3. See `k8s/authentik/README.md` for the wiring and
  `docs/superpowers/specs/2026-05-20-authentik-recovery-flow-design.md`
  for the design rationale.
```

The `#<BACKFILL_AFTER_PR_CREATE>` placeholder is filled in by Task 12 Step 4 below — leave it as-is for now.

- [ ] **Step 2: Clean the Open follow-ups section**

Replace lines 94-99 of `audits/tier-1-authentik.md`:

```markdown
## Open follow-ups

Tracked as GitHub issues in this repo, not described inline. Items
currently open:

- **Email-based recovery flow** (finding 5-i) — #62
```

with:

```markdown
## Open follow-ups

Tracked as GitHub issues in this repo, not described inline. Items
currently open:

_None — all Tier-1 audit findings resolved as of 2026-05-22._
```

- [ ] **Step 3: Commit (leaves the PR backfill placeholder intentionally; Task 12 fixes it)**

```bash
git add audits/tier-1-authentik.md
git commit -m "Audit: mark Tier-1 finding 5-i Resolved (#62)

Recovery flow lands as the Helm + UI config wired in the previous
two commits. All Tier-1 audit findings are now resolved; the Open
follow-ups section is empty. PR number in the finding's reference
is backfilled after PR create."
```

---

## Task 12: Open the PR

**Files:** None (GitHub state)

- [ ] **Step 1: Push the branch**

```bash
git push -u origin authentik-recovery-flow
```

Expected: `* [new branch]      authentik-recovery-flow -> authentik-recovery-flow` plus tracking confirmation.

- [ ] **Step 2: Create the PR**

```bash
gh pr create --repo nickvigilante/homelab \
  --title "Authentik: email-based password recovery flow (#62)" \
  --body "$(cat <<'EOF'
## Summary

- Email-based password self-recovery enabled for `homelab-users`; akadmin deliberately excluded (blast-radius reduction) with a documented operator runbook (Path A postgres-direct + Path B bootstrap-token).
- Abuse controls: 3/hr per-IP rate limit + reputation deny < -3. No CAPTCHA (avoids external dep).
- Repo changes are limited to one Helm values.yaml line (`AUTHENTIK_EMAIL__FROM` display-name override), two new `k8s/authentik/README.md` sections, and the audit doc resolution. Everything else is UI configuration against the live cluster, captured step-by-step in the README so a from-scratch rebuild can replay the clicks.

**Closes #62.** Tier-1 audit finding 5-i moves to Resolved; the audit's Open follow-ups section is now empty.

**Design:** docs/superpowers/specs/2026-05-20-authentik-recovery-flow-design.md
**Plan:** docs/superpowers/plans/2026-05-22-authentik-recovery-flow.md

## Test plan

Five end-to-end scenarios, run against the live cluster after the Helm upgrade and the final UI binding (Task 9 in the plan):

- [ ] **Negative: akadmin** — "Forgot password?" → enter `akadmin` → generic "if an account exists" response, no email sent (verify in Forward Email log)
- [ ] **Positive: homelab-users member** — email arrives within 30s with subject `vigihome.net — password reset requested` and From: `vigihome auth <noreply@vigihome.net>`; link works; password resets; sign-in works with the new password
- [ ] **Rate limit** — 3 successful recovery attempts from the same IP, 4th in the same hour → generic "try again later", no fourth email
- [ ] **Reputation** — 3 wrong-password sign-in attempts to drop ak_reputation_score ≤ -3, then attempt recovery → generic refusal, no email
- [ ] **Token expiry** — start recovery, wait 65 minutes, click the email link → "token expired" error
EOF
)"
```

Expected: a PR URL on stdout.

- [ ] **Step 3: Capture the PR number**

```bash
PR_NUM="$(gh pr view --json number -q .number)"
echo "PR: #$PR_NUM"
```

- [ ] **Step 4: Backfill the PR number in the audit doc**

```bash
sed -i "s|#<BACKFILL_AFTER_PR_CREATE>|#$PR_NUM|" audits/tier-1-authentik.md
grep -n "Resolved.* PR #" audits/tier-1-authentik.md | grep -i 5-i || \
  grep -B1 -A6 "Check 5 — self-service" audits/tier-1-authentik.md
```

Verify the placeholder is gone — should see e.g. `PR #62-fix` or whatever number the PR got. If grep still finds the placeholder, sed didn't match — adjust.

- [ ] **Step 5: Commit + push the backfill**

```bash
git add audits/tier-1-authentik.md
git commit -m "Audit: backfill PR number for finding 5-i"
git push
```

The PR auto-updates with the new commit.

---

## Task 13: E2E test 1 — negative akadmin

**Files:** None (live cluster test)

- [ ] **Step 1: Open a fresh incognito window**

Navigate to `https://authentik.vigihome.net/`. Confirm trusted cert (Let's Encrypt E7 issuer) and that the "Forgot password?" link is visible on the login form.

- [ ] **Step 2: Trigger recovery for akadmin**

Click "Forgot password?". Enter `akadmin` in the identifier field. Click Continue.

- [ ] **Step 3: Verify the generic response**

Expected on-screen: the same "if an account exists" message that a non-existent username produces. **Do not** look for an explicit "denied" message — that would leak that akadmin exists.

- [ ] **Step 4: Verify no email was sent**

Sign in to the Forward Email dashboard (https://forwardemail.net/) → Logs. Filter for the last 5 minutes. Expected: **no entry** for `akadmin` or for the operator's recorded akadmin email address.

If an email **was** sent, the group-membership policy isn't bound correctly — revisit Task 8 Step 2.

- [ ] **Step 5: Record the result**

In the PR test plan, check off the "Negative: akadmin" box (use the gh CLI or the UI):

```bash
gh pr edit "$PR_NUM" --body-file <(gh pr view "$PR_NUM" --json body -q .body \
  | sed 's|- \[ \] \*\*Negative: akadmin\*\*|- [x] **Negative: akadmin**|')
```

---

## Task 14: E2E test 2 — positive homelab-users member

**Files:** None (live cluster test)

- [ ] **Step 1: Pick a homelab-users test user**

Pick one of the user's normal accounts (not akadmin). Confirm they're in `homelab-users` and their recorded email is reachable (e.g., the operator's personal address).

- [ ] **Step 2: Trigger recovery**

Fresh incognito → `https://authentik.vigihome.net/` → "Forgot password?" → enter the test user's identifier → Continue.

Expected on-screen: generic "if an account exists" message.

- [ ] **Step 3: Verify the email arrives**

Wait up to 30 seconds. Check the test user's inbox.

Expected:
- From: `vigihome auth <noreply@vigihome.net>`
- Subject: `vigihome.net — password reset requested`
- Body contains a recovery link (the Authentik default template)

If the From: shows just `noreply@vigihome.net` without the display name, the Helm rollout didn't pick up the new env var — revisit Task 2 Step 4.

- [ ] **Step 4: Click the link**

Open the recovery link from the email. Expected: the Authentik password change prompt.

- [ ] **Step 5: Reset the password**

Set a new password. Save. Expected: redirect to the post-recovery success page.

- [ ] **Step 6: Sign in with the new password**

Sign out of the success page. From a fresh login form, enter the test user's identifier + the new password. Expected: successful sign-in.

- [ ] **Step 7: Restore the original password (optional but tidy)**

If you don't want the test user's password permanently changed, run the postgres-direct path from `k8s/authentik/README.md` (akadmin runbook Path A) but for the test user instead of akadmin, setting the password back to whatever it was. Or just leave it changed and update Bitwarden.

- [ ] **Step 8: Record the result**

```bash
gh pr edit "$PR_NUM" --body-file <(gh pr view "$PR_NUM" --json body -q .body \
  | sed 's|- \[ \] \*\*Positive: homelab-users member\*\*|- [x] **Positive: homelab-users member**|')
```

---

## Task 15: E2E test 3 — rate limit

**Files:** None (live cluster test)

- [ ] **Step 1: Trigger 3 recovery emails from the same source**

From the same incognito window as Task 14, request recovery 3 times. Use the same test user each time so the test user's mailbox accumulates the proof.

Expected: 3 emails arrive (within ~30s of each request). Each shows the generic "if an account exists" on-screen response.

- [ ] **Step 2: Attempt a 4th in the same hour**

Within the same hour, request recovery a 4th time.

Expected on-screen: generic "try again later" message. **Verify in Forward Email logs that no 4th email was sent** — that's the actual signal the rate limit fired.

If the 4th email **was** sent, the rate-limit policy isn't bound correctly — revisit Task 8 Step 4.

- [ ] **Step 3: Wait out the rate-limit window before the next test**

If proceeding immediately to Task 16 (Reputation), use a different source IP (e.g., tailnet vs LAN) since the IP is also part of the rate-limit key. Otherwise wait 1 hour.

- [ ] **Step 4: Record the result**

```bash
gh pr edit "$PR_NUM" --body-file <(gh pr view "$PR_NUM" --json body -q .body \
  | sed 's|- \[ \] \*\*Rate limit\*\*|- [x] **Rate limit**|')
```

---

## Task 16: E2E test 4 — reputation

**Files:** None (live cluster test)

This test drops the reputation score by attempting bad sign-ins, then attempts recovery from the same IP. Run from a fresh IP that doesn't already have bad reputation.

- [ ] **Step 1: Trigger reputation drop**

Fresh incognito (or different network so the IP differs from prior tests). Navigate to `https://authentik.vigihome.net/`. Attempt to sign in 3 times with a wrong password against any account (e.g., `nickv` + `wrongpassword`).

- [ ] **Step 2: Verify reputation in admin UI**

Sign in to Authentik as akadmin in a separate browser. Navigate: Admin interface → Events → Reputation. Find the recent entry for the IP used in Step 1. Expected: score ≤ -3.

If the score is higher than -3, repeat Step 1 a few more times.

- [ ] **Step 3: Attempt recovery from the bad-reputation source**

Return to the incognito window from Step 1. Click "Forgot password?". Enter any valid identifier. Click Continue.

Expected on-screen: generic "if an account exists" response. **Verify in Forward Email logs that no email was sent.**

If an email **was** sent, the reputation policy isn't bound correctly — revisit Task 8 Step 3.

- [ ] **Step 4: Clear the test reputation entry**

Admin interface → Events → Reputation → find the entry for the test IP → delete. (Avoids polluting future tests.)

- [ ] **Step 5: Record the result**

```bash
gh pr edit "$PR_NUM" --body-file <(gh pr view "$PR_NUM" --json body -q .body \
  | sed 's|- \[ \] \*\*Reputation\*\*|- [x] **Reputation**|')
```

---

## Task 17: E2E test 5 — token expiry

**Files:** None (live cluster test)

Slow test — requires 65 minutes of wall-clock time. Run last so it doesn't block the other tests.

- [ ] **Step 1: Trigger recovery for a homelab-users test user**

Same as Task 14 Step 2. Confirm an email arrives.

- [ ] **Step 2: Note the timestamp and the recovery link URL**

Copy the link from the email body. Note the current time.

- [ ] **Step 3: Wait 65 minutes**

Don't click the link. Don't reset anything. Do other work.

- [ ] **Step 4: Click the link after 65 minutes**

Open the recovery link in a fresh incognito.

Expected: a "token expired" error page (Authentik's default expiry-rejection screen). The password change prompt should NOT appear.

If the link still works after 65 minutes, the `token_expiry` setting on the email stage didn't take — revisit Task 7 Step 2.

- [ ] **Step 5: Record the result**

```bash
gh pr edit "$PR_NUM" --body-file <(gh pr view "$PR_NUM" --json body -q .body \
  | sed 's|- \[ \] \*\*Token expiry\*\*|- [x] **Token expiry**|')
```

---

## Task 18: Merge + close #62

**Files:** None (GitHub state)

- [ ] **Step 1: Verify all 5 test checkboxes are ticked in the PR body**

```bash
gh pr view "$PR_NUM" --json body -q .body | grep -E '^- \[[ x]\]'
```

Expected: 5 lines, all `- [x]`. If any are still `- [ ]`, finish that test first.

- [ ] **Step 2: Verify CI passes**

```bash
gh pr checks "$PR_NUM"
```

Expected: all checks pass (kubeconform + yamllint via `.github/workflows/lint.yml`). If they fail, fix and push.

- [ ] **Step 3: Squash-merge**

```bash
gh pr merge "$PR_NUM" --squash --delete-branch
```

Expected: `✓ Squashed and merged pull request #$PR_NUM` and `✓ Deleted branch authentik-recovery-flow`.

- [ ] **Step 4: Confirm #62 auto-closed**

```bash
gh issue view 62 --json state -q .state
```

Expected: `CLOSED`. If the issue is still OPEN, the `Closes #62` in the PR body didn't trigger auto-close — close manually:

```bash
gh issue close 62 --comment "Landed in PR #$PR_NUM."
```

- [ ] **Step 5: Pull the squash commit onto local main**

```bash
git checkout main && git pull --ff-only
```

Expected: fast-forward to the squash-merge commit. The local feature branch is already gone (Step 3 `--delete-branch`).

---

## Self-review notes (don't execute — just for the worker's awareness)

The plan implements every spec section:

| Spec section | Implementing task(s) |
|---|---|
| Goal / Approach | Tasks 4-9 (full UI configuration) |
| `values.yaml` change | Tasks 1-2 |
| UI changes (1-7) | Tasks 3-9 |
| Abuse posture | Tasks 4-6 + 8 (policies + bindings) + 15-16 (validation) |
| akadmin runbook | Task 10 (README) |
| Documentation | Tasks 10-11 |
| Testing (5 scenarios) | Tasks 13-17 |
| Related issues (#62 close, #104, #92) | Task 11 (audit doc), Task 12 (PR body), Task 18 (#62 auto-close) |

No placeholders that aren't intentional (`REPLACE_WITH_NEW_PASSWORD` is intentionally not a real secret in the README runbook; `<BACKFILL_AFTER_PR_CREATE>` is explicitly fixed by Task 12 Step 4).
