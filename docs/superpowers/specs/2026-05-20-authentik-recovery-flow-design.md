# Tier-1 Authentik audit 5-i: email-based password recovery flow

**Date:** 2026-05-20
**Audit reference:** `audits/tier-1-authentik.md`, finding 5-i
**Issue:** #62
**Status:** Implemented 2026-05-25 — **with deviations.** This spec is
retained for design rationale only; the **as-built source of truth is
`k8s/authentik/README.md`**. Notably: Authentik ships no recovery flow
(it was built from scratch, not "unbound" as stated below); there are no
"Group Membership" / "Rate Limit" policy types (group gating is a single
Expression policy on `pending_user`, bound to every post-identification
stage); and the rate-limit and reputation abuse controls — including any
rate-limit test scenario below — were dropped (deferred; see the audit
doc's "Things deliberately not done").

## Problem

Tier-1 Authentik audit finding 5-i (open follow-up):

> Authentik ships a `default-recovery-flow` but it is unbound. Users
> who forget their password have no self-service path; the operator
> has to reset passwords by hand via `ak shell` or the admin UI.

The SMTP relay piece landed in PR #56 (Forward Email, `noreply@vigihome.net`
sender, chart references `smtp-relay` Secret), so the transport is in
place. What's missing is the flow configuration: bind a recovery flow
to the authentication flow, decide who's eligible, decide the abuse
posture, and document the operator-side last-resort path for the one
user (akadmin) deliberately excluded.

## Goal

Enable email-based password self-recovery for normal homelab users,
while keeping akadmin out of scope (akadmin keeps a documented
operator runbook for catastrophic recovery). Configure Authentik
abuse controls so the recovery email path cannot be turned into a
mail bomb or used to enumerate accounts.

## Non-goals

- **akadmin self-service recovery.** Deliberately excluded — reduces
  blast radius. Recovery for akadmin is a documented operator
  procedure (Path A / Path B below), not an email flow.
- **CAPTCHA.** Skipped deliberately to avoid adding a Google reCAPTCHA
  or hCaptcha external dependency. Rate-limit + reputation policies
  cover the realistic threat for an internal-only host.
- **Blueprints migration.** All recovery flow config lives in
  Authentik's postgres DB (captured by the nightly restic backup of
  the `authentik` PVC). Migrating UI-managed Authentik state to
  Blueprints is filed as #104; this work lands UI-configured and
  will be absorbed into the Blueprints migration later.
- **MFA recovery.** Out of scope — Authentik has a separate MFA
  recovery code system; this spec only covers password recovery.
- **Public exposure decisions.** vigihome.net stays tailnet-only (a
  separate Tier-2 audit item). The recovery flow is reachable from
  the LAN and tailnet only.

## Approach

Bind the built-in `default-recovery-flow` to the existing
`default-authentication-flow` via the flow's `recovery_flow` field.
Gate the binding with a Group Membership policy so only members of
`homelab-users` see the "Forgot password?" link and can complete the
recovery. Attach two policies to the recovery flow itself: a Rate
Limit policy (3 emails/hour per `remote_ip`) and a Reputation policy
(deny when `ak_reputation_score < -3`).

### Why bind to the authentication flow vs. a standalone URL

Authentik's recovery-flow primitive *is* the authentication flow's
`recovery_flow` field. Setting it surfaces the "Forgot password?" link
on the login page automatically. The alternative — a standalone
`/if/flow/default-recovery-flow/` URL with no link from login — works
but requires users to know the URL exists. The bound approach is the
documented path and is what the audit finding asks for.

### Why exclude akadmin from email recovery

Three reasons, in order of weight:

1. **Blast radius.** akadmin is the break-glass account. If the email
   flow were ever compromised (mail relay credential leak, MX
   hijack, etc.), an attacker with the akadmin email address could
   reset it remotely. Keeping akadmin out of the flow means the
   attack path requires shell access to the cluster, which is
   already protected by Tailscale + SSH key auth.
2. **Email address.** akadmin's recorded email is the operator's
   personal address. The recovery flow assumes the recipient is the
   account owner; for akadmin, that's also the operator, but the
   operator already has a *better* recovery path (cluster shell).
3. **Audit posture.** Recovery flows that include the highest-
   privilege account widen the trust boundary to include the mail
   relay. Excluding akadmin keeps the trust boundary at "shell
   access to gandalf" for the admin path.

The cost is that akadmin password recovery is now a runbook task,
not a UI task. The runbook is short (Path A / Path B in §6) and
mirrored to the future Outline wiki (#92).

### Why a group-membership policy and not a static user filter

Authentik's flow policies evaluate per-request. A Group Membership
policy with `group=homelab-users` is the idiomatic way to say "every
human user except admins." When a new homelab user is added to
`homelab-users`, the recovery flow auto-enables for them. akadmin is
in `akadmins`, not `homelab-users`, so they're naturally excluded.
A static user filter (e.g. "deny if username=akadmin") would have
to be updated each time a new admin is added.

### Why rate-limit + reputation and no CAPTCHA

| Control                                                      | What it does                                                                                                                                              | Cost                                                                                      |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| **Rate Limit policy** (3/hr per remote_ip)                   | Caps the absolute number of recovery emails a single source can trigger; sufficient for a normal user, paid plan ceiling for an attacker                  | One Authentik primitive, no external dep                                                  |
| **Reputation policy** (deny when `ak_reputation_score < -3`) | Authentik tracks failed logins by IP+username; -3 is reached after roughly 3 failed logins in a short window. Denies recovery for IPs already misbehaving | One Authentik primitive, no external dep                                                  |
| **CAPTCHA** (skipped)                                        | Stops bots from automating the recovery form                                                                                                              | Requires Google reCAPTCHA or hCaptcha — external dep, third-party JS, privacy implication |
| **Account-enumeration mitigation** (built-in)                | Authentik returns the same "if an account exists" response for valid and invalid identifiers                                                              | Free — Authentik defaults this on                                                         |

The CAPTCHA cost (external JS on every login page in a tailnet-only
deployment) outweighs the benefit. Rate-limit + reputation handle the
realistic scenarios.

## Layer ownership: Helm vs UI vs Blueprints

Three layers can hold Authentik config. This spec uses two of them
deliberately, and #104 will absorb both into the third.

| Layer                                         | What it owns                                                                                                          | This spec's use                                                            |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| **Helm values** (`k8s/authentik/values.yaml`) | Process-level env vars (`AUTHENTIK_EMAIL__*`, `AUTHENTIK_BOOTSTRAP_*`), chart-shape concerns (Ingress, replicas, PVC) | One line added: `AUTHENTIK_EMAIL__FROM` to override the From: display name |
| **UI (postgres-backed)**                      | Flow bindings, policy bindings, stage `token_expiry`, email subject override, group memberships                       | Everything else in this spec                                               |
| **Blueprints** (config-as-code YAML)          | Same as UI, but declarative and applied by the Authentik worker at startup                                            | Not used yet — #104                                                        |

The Helm change is the only file change in this PR. Everything else
is UI clicks against the live cluster, documented step-by-step in the
updated `k8s/authentik/README.md`.

## Detailed configuration

### `values.yaml` (Helm layer)

One line under the existing `env:` block in `k8s/authentik/values.yaml`:

```yaml
env:
  AUTHENTIK_EMAIL__FROM: "vigihome auth <noreply@vigihome.net>"
  # existing keys stay as-is — AUTHENTIK_EMAIL__HOST, __PORT, __USERNAME,
  # __USE_TLS, __USE_SSL, __TIMEOUT, etc. all already set by PR #56.
```

Rationale: the SMTP envelope sender is already `noreply@vigihome.net`
(set by the chart's existing `__USERNAME` and the relay credential).
This env var controls the **display name** in the From: header so the
recipient sees `vigihome auth <noreply@vigihome.net>` rather than the
default `authentik <noreply@vigihome.net>`.

### Authentik UI changes

All UI work happens against the live cluster at `https://authentik.vigihome.net`,
signed in as akadmin. The README captures the click path; this section
captures the canonical settings.

**1. Group: `homelab-users`** (if not already present)

- Admin interface → Directory → Groups → Create
- Name: `homelab-users`
- Members: every non-admin user (the chart property mapping already
  adds new OIDC-provisioned users to a default group; confirm
  they're in this one)

**2. Policy: `recovery-allowed-group`** (Group Membership)

- Admin interface → Customization → Policies → Create → Group Membership Policy
- Name: `recovery-allowed-group`
- Group: `homelab-users`
- **Negate result:** OFF
- Execution logging: ON (for the first month, then turn off)

**3. Policy: `recovery-rate-limit`** (Rate Limit)

- Admin interface → Customization → Policies → Create → Rate Limit Policy
- Name: `recovery-rate-limit`
- Rate: `3`
- Per: `3600` seconds (1 hour)
- Key: `request.context.remote_ip`
- Authentik 2026.2 (running version) exposes Rate Limit Policy as
  a first-class primitive — no Expression Policy fallback needed.

**4. Policy: `recovery-reputation`** (Reputation)

- Admin interface → Customization → Policies → Create → Reputation Policy
- Name: `recovery-reputation`
- Threshold: -3
- Check IP: ON
- Check Username: ON
- **Important semantic:** Authentik reputation policies pass
  (`passing=true`) when reputation is **bad** (below threshold).
  Bind the policy to a Deny stage with `negate=OFF`. Do not
  misread `passing=true` as "good reputation." (See memory:
  \[[feedback_authentik_reputation_policy_semantics]\].)

**5. Stage: `default-email-recovery`** (built-in, edit)

- Admin interface → Flows & Stages → Stages → `default-email-recovery` → Edit
- Subject: `vigihome.net — password reset requested`
- Token expiry: `hours=1` (i.e. 1 hour)
- Template: default (no customization beyond subject)

**6. Flow binding: bind recovery to authentication flow**

- Admin interface → Flows & Stages → Flows → `default-authentication-flow` → Edit
- **Recovery flow:** `default-recovery-flow`
- Save

**7. Policy bindings on the recovery flow**

- Admin interface → Flows & Stages → Flows → `default-recovery-flow` → Policy Bindings
- Bind `recovery-allowed-group` (order 10)
- Bind `recovery-reputation` (order 20)
- Bind `recovery-rate-limit` (order 30)
- All three: `negate=OFF`, `enabled=ON`

After step 6, the "Forgot password?" link appears on the login page
for anyone visiting the authentication flow. Steps 7's bindings gate
who can proceed past the identification stage.

## Abuse posture

Composed of the three policies bound to the recovery flow:

| Layer            | Trigger                                                  | Effect                                                                            |
| ---------------- | -------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Group membership | User not in `homelab-users`                              | Identification stage refuses to advance ("if an account exists" generic response) |
| Reputation       | `ak_reputation_score < -3` for IP or username            | Identification stage refuses to advance (same generic response)                   |
| Rate limit       | More than 3 emails sent to anyone from this IP in 1 hour | Recovery email stage refuses; user sees a generic "try again later"               |

Account-enumeration mitigation is Authentik's default behavior — a
valid identifier and an invalid identifier produce the same on-screen
response. The differences only show up server-side (no email sent for
the invalid one).

## akadmin recovery — last-resort paths

When akadmin's password is lost (or the operator wants to rotate it
without using the UI), one of these two paths applies. Both are run
from the operator's laptop with kubectl + Bitwarden CLI access. The
runbook lives in `k8s/authentik/README.md` under "akadmin recovery —
last-resort paths" and is mirrored into the future Outline wiki when
#92 lands.

### Path A: postgres-direct via `ak shell`

For the case where the Authentik API is reachable but akadmin's
password is unknown. Uses the management shell to call
`User.set_password()` directly — Django hashes the password before
writing.

```bash
# 1. Confirm the user exists (sanity check)
kubectl -n auth exec -it statefulset/authentik-postgresql -- \
  bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U authentik -d authentik \
    -c "SELECT id, username, email FROM authentik_core_user WHERE username = '"'"'akadmin'"'"';"'

# 2. Reset the password via the ak shell
kubectl -n auth exec -it deployment/authentik-server -- \
  ak shell -c "from authentik.core.models import User; \
    u = User.objects.get(username='akadmin'); \
    u.set_password('REPLACE_WITH_NEW_PASSWORD'); \
    u.save()"

# 3. Update Bitwarden item "Homelab Authentik" with the new password
```

### Path B: bootstrap-token via Authentik API

For the case where shell access to the cluster is harder than API
access, but the Authentik bootstrap token (set via
`AUTHENTIK_BOOTSTRAP_TOKEN` at chart install, stored in Bitwarden) is
known.

```bash
# 1. Unlock Bitwarden and pull the bootstrap token + akadmin user PK
BW_SESSION="$(bw unlock --raw)"
TOKEN="$(bw get item 'Homelab Authentik' \
  | jq -r '.fields[] | select(.name=="bootstrap-token") | .value')"

# 2. Look up akadmin's primary key (need an admin session; bootstrap
#    token is admin-equivalent)
AKADMIN_PK="$(curl -sSf -H "Authorization: Bearer $TOKEN" \
  'https://authentik.vigihome.net/api/v3/core/users/?username=akadmin' \
  | jq -r '.results[0].pk')"

# 3. POST the password reset
curl -sSf -H "Authorization: Bearer $TOKEN" \
  -X POST "https://authentik.vigihome.net/api/v3/core/users/$AKADMIN_PK/set_password/" \
  -H 'Content-Type: application/json' \
  -d '{"password":"REPLACE_WITH_NEW_PASSWORD"}'

# 4. Update Bitwarden item "Homelab Authentik" with the new password
```

**Path A vs Path B choice:** prefer Path A when ssh + kubectl are
available (the common case from gandalf or the operator laptop on
tailnet). Use Path B when the operator is on a machine without
kubectl access but has the bootstrap token cached in Bitwarden.

**Bootstrap token rotation:** the bootstrap token is admin-
equivalent. If used during recovery, rotate it afterward by editing
the chart values and `helm upgrade`-ing (which respins the deployment
and re-reads the env var). Update Bitwarden in lockstep.

## Documentation

Three docs change in this PR:

- **`k8s/authentik/README.md`** — gains two new sections:
  - `## Self-service password recovery (homelab-users only)` —
    describes the bound flow, the rate limit, the reputation
    policy, the token expiry, the email template overrides, the
    group-membership gate, and notes that all of this lives in
    postgres (captured by the nightly restic snapshot) and will
    migrate to Blueprints under #104.
  - `## akadmin recovery — last-resort paths` — the Path A / Path B
    runbook from §6 verbatim. Includes a comment marker
    `<!-- TODO: mirror to Outline when wiki lands -->` so the mirror
    isn't lost when #92 ships.
- **`audits/tier-1-authentik.md`** — flip finding 5-i from open
  follow-up to ✅ Resolved with a reference to this PR. Remove #62
  from the "Open follow-ups" section.
- **PR description** — call out the one-line `values.yaml` change
  explicitly so reviewers know the rest of the cluster state changed
  via UI clicks captured in the README.

## Testing

Five end-to-end scenarios, run against the live cluster (no staging).
Each one is a manual test executed from the operator laptop unless
noted. The README documents them under "Testing the recovery flow"
for future reruns.

| #   | Scenario                           | Steps                                                                                                                 | Pass condition                                                                                                                                                 |
| --- | ---------------------------------- | --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **Negative: akadmin**              | Visit `https://authentik.vigihome.net/`, click "Forgot password?", enter `akadmin`                                    | Identification stage shows generic "if an account exists" message; **no email sent** (verify in Forward Email log)                                             |
| 2   | **Positive: homelab-users member** | Same UI, enter a `homelab-users` member's username                                                                    | Email arrives at the user's address within 30s with the customized subject; link inside opens the password change prompt; new password saves and sign-in works |
| 3   | **Rate limit**                     | From a single browser, complete step 2 three times for the same or different users; attempt a fourth in the same hour | Fourth attempt: identification stage shows generic "try again later" message; **no fourth email sent**                                                         |
| 4   | **Reputation**                     | From a fresh IP, attempt to sign in with a wrong password 3 times against any account; then attempt recovery          | Identification stage refuses to advance (same generic response); **no email sent**. Verify `ak_reputation_score` ≤ -3 in admin UI                              |
| 5   | **Token expiry**                   | Complete step 2, but wait 65 minutes before clicking the email link                                                   | Link shows "token expired" error; new recovery email must be requested                                                                                         |

Tests 1, 2, 3, 5 take ~10 minutes total. Test 4 requires waiting for
reputation to decay back to 0 afterward (or manually clearing it via
the admin UI Reputation section) so it doesn't pollute other tests.

## Related issues and future work

- **#62** — closed when this spec's implementation lands.
- **#92** — Outline wiki deployment (renamed from BookStack 2026-05-20).
  The akadmin runbook gets mirrored there when the wiki ships.
- **#104** — Migrate UI-managed Authentik config to Blueprints. The
  flow binding, policy bindings, and stage overrides from this spec
  are first-class candidates for that migration. Until then, the
  config is captured by the nightly restic snapshot of the
  `authentik` postgres PVC; a from-scratch rebuild without the
  postgres restore means redoing the UI clicks (already documented
  in `k8s/authentik/README.md`'s "What we don't back up" section).

## Things deliberately not done

- **CAPTCHA.** External JS dep, privacy implication, not warranted
  for a tailnet-only deployment with rate-limit + reputation.
- **akadmin self-service recovery.** Reduces blast radius; the
  operator has a documented runbook (Path A / Path B).
- **Blueprints-based config in this PR.** Scope-bounded to recovery
  flow; Blueprints migration covers all UI-managed Authentik state
  and is filed as #104.
- **Custom email body template.** Default Authentik template is
  fine; only the subject and From: display name are customized.
- **MFA recovery codes.** Out of scope — separate Authentik subsystem.
