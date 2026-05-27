# Authentik Blueprints

Config-as-code for Authentik's otherwise UI-/postgres-only objects (#104),
so a from-scratch rebuild reconstructs them without a postgres restore.

## What's here

| File                        | Captures                                                                        |
| --------------------------- | ------------------------------------------------------------------------------- |
| `groups.yaml`               | `homelab-users` group                                                           |
| `email-scope-mapping.yaml`  | Override the default `email` scope mapping to emit `email_verified: true`       |
| `recovery-flow.yaml`        | Password-recovery flow + stages + the `homelab-users` group gate + brand wiring |
| `applications/coder.yaml`   | Coder OIDC provider + application + `homelab-users` gate                        |
| `applications/outline.yaml` | Outline OIDC provider + application + `homelab-users` gate                      |

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
