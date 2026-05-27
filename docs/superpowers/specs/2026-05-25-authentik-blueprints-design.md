# Authentik Blueprints — Design

**Issue:** #104
**Status:** Design approved 2026-05-25; pending spec review → implementation plan.

## Goal

Migrate Authentik's UI-/postgres-only configuration to native **Blueprints** (declarative YAML applied by the worker on startup), checked into the repo. After this, a from-scratch Authentik rebuild reconstructs its full configuration **without a postgres restore** — closing the "redo the UI clicks" gap called out in `k8s/authentik/README.md`, the Tier-1 audit, and CLAUDE.md's "What we don't back up."

## Background

Authentik config today lives only in the `authentik` postgres DB, created by clicking through the admin UI. The surface (per #104, the README, and the Tier-1 audit):

- The `email` scope property mapping override (returns `email_verified: True`).
- OIDC provider + application records for **Coder** and **Outline**, each bound to the `homelab-users` group.
- The self-service password-recovery stack (group, expression policy, identification + email stages, flow, stage bindings, policy bindings, brand assignment) — built in the UI under #62.

Blueprints are the native, idempotent config-as-code primitive. The Authentik Helm chart mounts them from a ConfigMap via `blueprints.configMaps: []` (confirmed against chart 2026.2.2: "Only keys ending in `.yaml` are discovered and applied"). The worker discovers and reconciles them on startup.

## Decisions

1. **Mount via a single ConfigMap** `authentik-blueprints`, referenced from `values.yaml` as `blueprints.configMaps: [authentik-blueprints]`, applied with `kubectl apply` + a worker rollout — consistent with the repo's manual, no-GitOps model. Source files live under `k8s/authentik/blueprints/` and are **organized into subfolders for readability** (see Inventory). ConfigMap keys cannot contain `/`, so the source tree is **flattened by basename** into the ConfigMap (all basenames are unique); the in-cluster `/blueprints` view is flat, which is fine — the worker discovers blueprints by `.yaml` content, not path.

2. **Idempotent adoption via natural-key identifiers.** Every object uses `state: present` with `identifiers` on its natural key (slug / name / managed-marker). Applied against the live prod DB, the blueprints **adopt the existing UI-created objects in place** rather than creating duplicates. Reconciliation on every worker start also makes the config **self-healing** — UI drift or an upgrade that resets a default is reasserted.

3. **OIDC client secrets via env injection, backed by Bitwarden.** Blueprints never contain secret values. Each provider's `client_secret` is set from an environment variable using Authentik's env blueprint tag (exact tag/syntax — `!Env` — to be pinned in the plan). The env vars come from a dedicated k8s Secret `authentik-oidc-secrets`, materialized from the existing `oidc-client-secret` fields in Bitwarden items `Homelab Coder` / `Homelab Outline`. One Bitwarden field feeds **both** the Authentik provider (this env) and the downstream service's own Secret.

4. **Validate on a scratch instance first.** Stand up a throwaway Authentik on a fresh, empty postgres and confirm the full object set reconstructs from zero before touching prod. Authentik is a hard SPOF; this is #104's stated acceptance test.

5. **Override the built-in `email` mapping in place** (target its managed identifier `goauthentik.io/providers/oauth2/scope-email`, set `email_verified: True`) rather than creating a parallel custom mapping. This mirrors the current live behavior, keeps providers' `property_mappings` lists unchanged, and — because the blueprint reasserts on every restart — is self-healing against an upgrade resetting the default.

## Architecture

```
Bitwarden (Homelab Coder / Homelab Outline: oidc-client-secret)
   │  bw get → kubectl create secret
   ▼
k8s Secret  authentik-oidc-secrets   ──secretKeyRef──▶  worker env
                                                          AUTHENTIK_OIDC_CODER_SECRET
                                                          AUTHENTIK_OIDC_OUTLINE_SECRET
ConfigMap  authentik-blueprints  ──chart blueprints.configMaps──▶  /blueprints/*.yaml
                                                          │  worker discovers + reconciles
                                                          ▼  (client_secret: !Env ...)
                                              Authentik objects (adopted in place by identifier)
```

## Blueprint inventory

Source tree under `k8s/authentik/blueprints/` (foundational/global blueprints at root; per-application integrations — the set that grows as apps are added — in `applications/`):

```
k8s/authentik/blueprints/
  groups.yaml
  email-scope-mapping.yaml
  recovery-flow.yaml
  applications/
    coder.yaml
    outline.yaml
```

Each `.yaml` is flattened by basename into the `authentik-blueprints` ConfigMap:

| File | Objects |
|------|---------|
| `groups.yaml` | `homelab-users` group (foundational; referenced by app bindings + the recovery gate) |
| `email-scope-mapping.yaml` | Reassert built-in `email` scope mapping (managed id `goauthentik.io/providers/oauth2/scope-email`) → `email_verified: True` |
| `recovery-flow.yaml` | `recovery-allowed-group` expression policy; `recovery-identification` + `recovery-email` stages; the `recovery` flow; stage bindings 10/20/30/40 (identification/email/prompt/write — prompt+write reuse the default stages via `!Find`); policy bindings on 20/30/40 with *evaluate-when-stage-run* ON / *evaluate-when-flow-planned* OFF; default Brand `flow_recovery` = `recovery` |
| `applications/coder.yaml` | Coder OAuth2/OIDC provider (`client_secret: !Env`) + application + app→`homelab-users` PolicyBinding |
| `applications/outline.yaml` | Outline OAuth2/OIDC provider (`client_secret: !Env`) + application + app→`homelab-users` PolicyBinding |

A new application later = drop a `applications/<slug>.yaml` in and re-render the ConfigMap; no other file changes. Cross-file references use `!Find`/`!KeyOf` (e.g. both app blueprints and the recovery policy reference the single `homelab-users` group). Concern-per-file keeps intra-file ordering explicit; Authentik resolves cross-file dependencies regardless of discovery order.

The `recovery-allowed-group` expression (unchanged from the as-built README):

```python
pending_user = request.context.get("pending_user")
if not pending_user:
    return False
return ak_is_group_member(pending_user, name="homelab-users")
```

The binding semantics (evaluate-on-stage-run, bound to all three post-identification stages to prevent the skip-cascade bypass) are reproduced exactly as documented in the recovery-flow README — the blueprint encodes the same wiring that was verified the hard way.

## Secret injection

- New k8s Secret **`authentik-oidc-secrets`** (`auth` namespace), separate from `authentik-secrets` (different lifecycle, narrower blast radius — mirrors the `smtp-relay` separation). Keys: `oidc-coder-client-secret`, `oidc-outline-client-secret`. Created via the standard `bw get` → `kubectl create secret generic` pattern; documented in the README.
- `values.yaml`: add `AUTHENTIK_OIDC_CODER_SECRET` and `AUTHENTIK_OIDC_OUTLINE_SECRET` under the chart's shared `global.env`, each via `secretKeyRef` to `authentik-oidc-secrets` (shared env reaches the worker, which applies blueprints).
- `secret.example.yaml` (or a new template) documents the two keys with `REPLACE_WITH_*` placeholders. No secret values in the repo (gitleaks pre-commit stays green).

## Validation & rollout

1. **Scratch instance.** Deploy a throwaway Authentik in namespace `auth-scratch` on a fresh, empty postgres (ephemeral volume), same chart version, the blueprints ConfigMap mounted, with *dummy* OIDC secret env values. Confirm the worker applies every blueprint and the full object set reconstructs from zero (group, email mapping, both providers+apps+bindings, recovery policy/stages/flow/bindings, brand). Tear down.
2. **Prod prep.** Fresh postgres/restic backup; confirm `AUTHENTIK_SECRET_KEY` is in Bitwarden (rollback escape hatch).
3. **Prod apply.** Create `authentik-oidc-secrets`; render + apply the `authentik-blueprints` ConfigMap from the blueprints tree (flattened by basename, e.g. `kubectl create configmap authentik-blueprints --from-file=<each .yaml> -o yaml --dry-run=client | kubectl apply -f -`); `helm upgrade` (pinned `--version`) to add `blueprints.configMaps` + the env vars; roll the worker. Identifier-matching adopts the existing UI objects in place.
4. **Verify on prod.** Every blueprint instance shows *successful* (admin → System → Blueprints); the recovery-flow regression checklist passes (member resets / non-member dead-ends / no enumeration); Coder + Outline OIDC logins work; `email_verified` mapping intact; **no duplicate objects**.
5. **Rollback.** If a blueprint mismatches and breaks a flow, restore postgres from the pre-apply backup.
6. **Docs.** Replace the README "click here in the UI" sections (email mapping, adding an OIDC client, the recovery-flow reapply table) with "captured in `blueprints/<file>.yaml`; apply via …". Add the `authentik-oidc-secrets` create-secret step to the setup runbook. Note the ConfigMap + new lint coverage in CLAUDE.md if needed.

## Acceptance criteria

- A fresh-postgres scratch instance reconstructs **100% of the UI-managed surface** from blueprints alone.
- Applying to prod adopts existing objects with **zero duplicates**.
- The recovery-flow regression checklist passes; Coder + Outline OIDC logins still work.
- `gitleaks` clean — no secret values in the repo.
- READMEs updated to point at the blueprint files; the lint filter (`.github/workflows/lint.yml`) covers the new file types if they fall outside the current globs.

## Out of scope

- Migrating to the Terraform `goauthentik/authentik` provider (Blueprints is the simpler native primitive for a single-operator lab; revisit only on a cross-cluster need — per #104).
- Secrets beyond the OIDC client secrets — `AUTHENTIK_SECRET_KEY`, SMTP, postgres passwords stay as `kubectl create secret` from Bitwarden.
- Reproducible config for Uptime Kuma monitors (separate follow-up).

## To confirm during planning

- Exact env blueprint tag + argument syntax (`!Env VAR` vs `!Env [VAR, default]`).
- Whether the chart mounts each ConfigMap key directly under `/blueprints/` or under a subdirectory, and whether the worker needs a restart vs picks up the ConfigMap on its discovery interval.
- Exact model paths / identifier fields for each object (e.g. `authentik_providers_oauth2.models.OAuth2Provider`, `authentik_core.models.Application`, `authentik_policies.models.PolicyBinding`) and that the recovery objects' natural keys match the live UI-created records so they adopt rather than duplicate.
