# Authentik Blueprints

Config-as-code for Authentik's otherwise UI-/postgres-only objects (#104),
so a from-scratch rebuild reconstructs them without a postgres restore.

## What's here

| File                             | Captures                                                                                                                 |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `groups.yaml`                    | `homelab-users` group                                                                                                    |
| `email-scope-mapping.yaml`       | Override the default `email` scope mapping to emit `email_verified: true`                                                |
| `recovery-flow.yaml`             | Password-recovery flow + stages + the `homelab-users` group gate + brand wiring                                          |
| `applications/coder.yaml`        | Coder OIDC provider + application + `homelab-users` gate                                                                 |
| `applications/forward-auth.yaml` | Domain-level forward-auth proxy provider & application gated on `homelab-users`, assigned to the embedded outpost (#137) |
| `applications/outline.yaml`      | Outline OIDC provider + application + `homelab-users` gate                                                               |

The files are flattened **by basename** into the `authentik-blueprints`
ConfigMap (ConfigMap keys can't contain `/`, so the `applications/`
subfolder is authoring-only — basenames must stay unique across the tree).
The chart mounts the ConfigMap at `/blueprints/mounted/cm-authentik-blueprints/`
and the worker discovers + reconciles every `.yaml` on startup, tracking an
id→pk mapping per file so re-applies adopt rather than duplicate.

## Conventions (Authentik 2026.2.x — verified, not guessed)

- **Model form is two-part lowercase**: `authentik_core.group`,
  `authentik_providers_oauth2.oauth2provider`, etc. The `app.models.ClassName`
  form fails with "too many values to unpack".
- **`!Env [VARNAME, default]`** is a *list* (not bare `!Env VAR`). OIDC
  `client_secret`s use this, sourced from the `authentik-oidc-secrets` Secret
  (Bitwarden-backed) — never written in these files. See `../README.md`.
- `!Find [<two-part-model>, [<field>, <value>]]` resolves shipped objects
  (flows, the signing cert, scope mappings). `!KeyOf <id>` references another
  entry in the same blueprint.
- Every object uses `state: present` + natural-key `identifiers` so it
  **adopts** the existing prod object in place (no duplicate on first prod apply).

## Render + apply the ConfigMap

```bash
# laptop, kubectl context = homelab
kubectl -n auth create configmap authentik-blueprints \
  $(find k8s/authentik/blueprints -name '*.yaml' -printf '--from-file=%f=%p ') \
  --dry-run=client -o yaml | kubectl apply -f -
# roll the worker so it picks up the changed ConfigMap:
kubectl -n auth rollout restart deploy/authentik-worker
kubectl -n auth rollout status deploy/authentik-worker
# THEN trigger discovery explicitly — the boot-time discovery can race the
# ConfigMap volume mount and silently no-op, leaving the apply to wait for
# the hourly discovery cron. This makes it deterministic:
kubectl -n auth exec deploy/authentik-worker -- \
  ak shell -c "from authentik.blueprints.v1.tasks import blueprints_discovery; blueprints_discovery.send()"
```

`blueprints.configMaps: [authentik-blueprints]` is already set in
`../values.yaml`, so the worker mounts and discovers this ConfigMap.
Verify with `GET /api/v3/managed/blueprints/` — each
`mounted/cm-authentik-blueprints/*.yaml` should show `status: successful`.

## Validating changes

Validate on a throwaway scratch instance before prod — see
`../scratch-values.yaml` and the #104 plan. Two gotchas learned the hard way:

- **Validate via discovery (mounted CM + worker restart), not
  `ak apply_blueprint <file>`.** The manual command has no state tracking, so
  for objects whose name isn't DB-unique (ExpressionPolicy) it creates a NEW
  copy on every run. The discovery path is idempotent.
- A fresh instance discovers all mounted files on first boot; files *added*
  to a long-running instance wait for the periodic discovery tick.

## Known gap: `email-scope-mapping.yaml` can silently regress on restart (#193)

`email-scope-mapping.yaml` overrides a *shipped* Authentik object (the
`goauthentik.io/providers/oauth2/scope-email` scope mapping), not a
fresh custom one. Shipped system blueprints (e.g.
`providers-oauth2.yaml`, which ships the stock `email_verified: False`
expression this file overrides) re-apply on every `authentik-server` /
`authentik-worker` boot -- including Authentik version upgrades.

Discovery for our own ConfigMap-mounted blueprints is gated on a
content hash of the file: if `email-scope-mapping.yaml` hasn't
changed since it was last applied, discovery treats it as
"already applied" and skips re-writing the database, even though a
*later* system-blueprint reapply may have already overwritten that
same row. This happened for real on 2026-09-04's Authentik 2026.8.1
upgrade -- the override silently reverted to the stock
`email_verified: False`, broke Coder + Outline login, and neither the
boot-time discovery nor the hourly discovery cron caught it over the
following 12 days, because both share the same hash-gated blind spot.
Full writeup in #193.

**After every `authentik-server` / `authentik-worker` restart or
Authentik version bump**, explicitly re-apply this one file --
discovery alone is not sufficient:

```bash
kubectl -n auth exec deploy/authentik-worker -- ak apply_blueprint \
  "$(kubectl -n auth exec deploy/authentik-worker -- \
     find /blueprints/mounted -iname 'email-scope-mapping.yaml' | head -1)"
```

This is safe specifically for this file because `authentik_providers_oauth2.scopemapping` objects are
matched by the stable `managed:` key, so re-running it adopts the
existing row instead of duplicating it -- unlike the
`ak apply_blueprint` ExpressionPolicy gotcha above. **Do not** loop
this command over every blueprint file for the same reason that
gotcha exists; `recovery-flow.yaml`'s ExpressionPolicy has no such
stable key and will duplicate on repeated `ak apply_blueprint` runs.

Verify it took with a direct DB read (also useful any time Coder or
Outline OIDC login unexpectedly fails with an email-verification
error):

```bash
kubectl -n auth exec deploy/authentik-worker -- ak shell -c "
from authentik.providers.oauth2.models import ScopeMapping
print(ScopeMapping.objects.get(managed='goauthentik.io/providers/oauth2/scope-email').expression)
"
```
