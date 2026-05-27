# Authentik Blueprints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture Authentik's UI-/postgres-only config (email scope mapping, Coder + Outline OIDC apps, the password-recovery flow stack) as native Blueprint YAML in the repo, so a from-scratch Authentik rebuilds its full config without a postgres restore.

**Architecture:** A single `authentik-blueprints` ConfigMap (source files under `k8s/authentik/blueprints/`, flattened by basename) is mounted by the chart via `blueprints.configMaps`; the worker discovers + reconciles each `.yaml` on startup. Objects use `state: present` + natural-key `identifiers` so they adopt existing prod objects in place. OIDC `client_secret` values are injected via the `!Env` tag from a `authentik-oidc-secrets` k8s Secret (Bitwarden-backed) — never in the repo. Blueprints are authored by **exporting the live objects**, then parameterizing; validated on a throwaway scratch instance from empty postgres before prod.

**Tech Stack:** Authentik 2026.2.2 (Helm chart `authentik/authentik`), Authentik Blueprints v1, `ak export_blueprint` / `ak apply_blueprint`, k3s, kubectl, Helm, Bitwarden CLI.

**Spec:** `docs/superpowers/specs/2026-05-25-authentik-blueprints-design.md`

**Conventions for this plan:**

- Branch `authentik-blueprints` already exists (the spec is committed there). Do all work on it.
- "Verify reconcile" = the object exists after applying. Inspect via `ak shell` or the API; the per-task commands show how.
- Raw exports may contain real client secrets — **never commit a raw export**. They are local scratch only; the committed files carry `!Env` references instead.
- Tag every live command with the host it runs on (laptop with kubectl context = homelab, or gandalf). Live prod-mutating commands (Task 10) are handed to the operator to run, with `git checkout`/`pull` included.
- Scratch-instance curls below use `$TOK` for the scratch bootstrap token — run `export TOK=scratch-token-0000` (the value set in `scratch-values.yaml`) once before them.

______________________________________________________________________

## File structure

| File                                                 | Responsibility                                                                                         |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `k8s/authentik/blueprints/groups.yaml`               | `homelab-users` group                                                                                  |
| `k8s/authentik/blueprints/email-scope-mapping.yaml`  | Override built-in `email` scope mapping → `email_verified: True`                                       |
| `k8s/authentik/blueprints/recovery-flow.yaml`        | Recovery policy + stages + flow + stage/policy bindings + brand assignment                             |
| `k8s/authentik/blueprints/applications/coder.yaml`   | Coder OIDC provider + application + group binding                                                      |
| `k8s/authentik/blueprints/applications/outline.yaml` | Outline OIDC provider + application + group binding                                                    |
| `k8s/authentik/blueprints/README.md`                 | How blueprints are rendered into the ConfigMap + applied                                               |
| `k8s/authentik/scratch-values.yaml`                  | Helm values overlay for the throwaway validation instance                                              |
| `k8s/authentik/values.yaml` (modify)                 | Add `blueprints.configMaps` + two `global.env` OIDC-secret entries                                     |
| `k8s/authentik/secret.example.yaml` (modify)         | Document `authentik-oidc-secrets` keys                                                                 |
| `k8s/authentik/README.md` (modify)                   | Replace "click in the UI" sections with blueprint references + add the oidc-secrets create-secret step |
| `.github/workflows/lint.yml` (modify, if needed)     | Cover the new file types                                                                               |

______________________________________________________________________

## Task 1: Stand up the throwaway scratch instance

**Files:**

- Create: `k8s/authentik/scratch-values.yaml`

- [ ] **Step 1: Write the scratch values overlay**

`k8s/authentik/scratch-values.yaml`:

```yaml
# Throwaway Authentik for validating blueprints from an EMPTY postgres.
# Namespace: auth-scratch. ClusterIP only (no ingress, no host ports) —
# reach it via `kubectl -n auth-scratch port-forward`. Postgres uses the
# k3s default local-path storageClass (dynamic, disposable) — NOT the
# hostPath PV that prod uses. Delete the whole namespace when done.
global:
  env:
    - name: AUTHENTIK_SECRET_KEY
      value: "scratch-only-not-secret-0000000000000000000000000000"
    - name: AUTHENTIK_BOOTSTRAP_PASSWORD
      value: "scratch-admin"
    - name: AUTHENTIK_BOOTSTRAP_TOKEN
      value: "scratch-token-0000"
    # Dummy OIDC secrets so the !Env references resolve on scratch.
    - name: AUTHENTIK_OIDC_CODER_SECRET
      value: "scratch-coder-secret"
    - name: AUTHENTIK_OIDC_OUTLINE_SECRET
      value: "scratch-outline-secret"
server:
  ingress:
    enabled: false
postgresql:
  enabled: true
  auth:
    username: authentik
    database: authentik
    password: scratch-pg
    postgresPassword: scratch-pg-super
  primary:
    persistence:
      enabled: true
      # default storageClass (local-path) — dynamic + disposable
      storageClass: ""
```

- [ ] **Step 2: Create the namespace and install**

Run (laptop, kubectl context = homelab):

```bash
kubectl create namespace auth-scratch
helm install authentik authentik/authentik -n auth-scratch \
  --version 2026.2.2 -f k8s/authentik/scratch-values.yaml
kubectl -n auth-scratch rollout status deploy/authentik-server --timeout=300s
```

Expected: server + worker + postgres pods reach Ready (first start runs migrations; allow a few minutes).

- [ ] **Step 3: Verify API reachability**

```bash
kubectl -n auth-scratch port-forward deploy/authentik-server 9000:9000 &
curl -sf -H "Authorization: Bearer $TOK" \
  http://localhost:9000/api/v3/core/users/?username=akadmin | jq '.results[0].username'
```

Expected: `"akadmin"`. (Leave the port-forward running for later tasks, or re-open as needed.)

- [ ] **Step 4: Commit the scratch overlay**

```bash
git add k8s/authentik/scratch-values.yaml
git commit -m "Authentik: scratch instance values for blueprint validation"
```

______________________________________________________________________

## Task 2: Export the live prod objects as reference blueprints

**Files:** none committed (local scratch artifacts only).

- [ ] **Step 1: Export the full prod object set**

Run (laptop, context = homelab) — writes a local file, **do not commit it** (contains real client secrets):

```bash
kubectl -n auth exec deploy/authentik-worker -- \
  ak export_blueprint > /tmp/ak-prod-export.yaml
wc -l /tmp/ak-prod-export.yaml
```

Expected: a `version: 1` blueprint with an `entries:` list containing the group, scope mappings, OAuth2 providers (Coder, Outline), applications, policies, stages, flow, flow-stage-bindings, policy-bindings, and brand.

- [ ] **Step 2: Export the recovery flow as a self-contained blueprint**

The Flow export endpoint bundles a flow with its stages, bindings, and policies:

```bash
curl -sf -H "Authorization: Bearer $(kubectl -n auth get secret authentik-secrets -o jsonpath='{.data.bootstrap-token}' | base64 -d)" \
  https://authentik.vigihome.net/api/v3/flows/instances/recovery/export/ \
  > /tmp/ak-recovery-flow-export.yaml
head -30 /tmp/ak-recovery-flow-export.yaml
```

Expected: a blueprint containing the `recovery` flow plus `recovery-identification`, `recovery-email`, the password-change prompt/write stages, the flow-stage-bindings, and the `recovery-allowed-group` policy + its bindings.

- [ ] **Step 3: Record the authoritative field shapes**

Skim both exports and note, for use in later tasks, the exact:

- model dotted-paths (e.g. `authentik_providers_oauth2.models.OAuth2Provider`, `authentik_core.models.Application`, `authentik_providers_oauth2.models.ScopeMapping`, `authentik_policies.models.PolicyBinding`, `authentik_stages_identification.models.IdentificationStage`, `authentik_stages_email.models.EmailStage`, `authentik_flows.models.Flow`, `authentik_flows.models.FlowStageBinding`, `authentik_brands.models.Brand`);
- the `redirect_uris` shape for the providers (current Authentik uses a list of `{matching_mode, url}` objects);
- the providers' `authorization_flow` / `invalidation_flow` slugs, `signing_key` name, `property_mappings` (the openid/email/profile scope mappings), `client_id`, `sub_mode`;
- the recovery flow-stage-binding orders and the `re_evaluate_policies` / `evaluate_on_plan` booleans on bindings 20/30/40.

These exports are the source of truth for field names in Tasks 3–7. No commit.

______________________________________________________________________

## Task 3: Author `groups.yaml`

**Files:**

- Create: `k8s/authentik/blueprints/groups.yaml`

- [ ] **Step 1: Write the blueprint**

`k8s/authentik/blueprints/groups.yaml`:

```yaml
version: 1
metadata:
  name: "homelab - groups"
entries:
  - model: authentik_core.models.Group
    state: present
    identifiers:
      name: homelab-users
    attrs:
      is_superuser: false
```

- [ ] **Step 2: Apply to scratch and verify it reconciles**

```bash
kubectl -n auth-scratch cp k8s/authentik/blueprints/groups.yaml \
  "$(kubectl -n auth-scratch get pod -l app.kubernetes.io/component=worker -o name | head -1 | cut -d/ -f2)":/tmp/groups.yaml
kubectl -n auth-scratch exec deploy/authentik-worker -- ak apply_blueprint /tmp/groups.yaml
```

Expected: exits 0, logs a successful apply (no `Failed to apply blueprint`).

- [ ] **Step 3: Verify the object exists**

```bash
curl -sf -H "Authorization: Bearer $TOK" \
  http://localhost:9000/api/v3/core/groups/?name=homelab-users | jq '.results[0].name'
```

Expected: `"homelab-users"`.

- [ ] **Step 4: Commit**

```bash
git add k8s/authentik/blueprints/groups.yaml
git commit -m "Authentik blueprint: homelab-users group"
```

______________________________________________________________________

## Task 4: Author `email-scope-mapping.yaml`

**Files:**

- Create: `k8s/authentik/blueprints/email-scope-mapping.yaml`

- [ ] **Step 1: Write the blueprint** (override the built-in mapping by its managed id)

`k8s/authentik/blueprints/email-scope-mapping.yaml`:

```yaml
version: 1
metadata:
  name: "homelab - email scope mapping (email_verified=true)"
entries:
  - model: authentik_providers_oauth2.models.ScopeMapping
    state: present
    # Target the shipped default mapping by its managed marker so this
    # override is reasserted on every worker start (self-healing against
    # an upgrade resetting the default). Confirm the managed id against
    # /tmp/ak-prod-export.yaml.
    identifiers:
      managed: goauthentik.io/providers/oauth2/scope-email
    attrs:
      expression: |
        return {
            "email": request.user.email,
            "email_verified": True,
        }
```

- [ ] **Step 2: Apply to scratch and verify reconcile**

```bash
kubectl -n auth-scratch cp k8s/authentik/blueprints/email-scope-mapping.yaml \
  "$(kubectl -n auth-scratch get pod -l app.kubernetes.io/component=worker -o name | head -1 | cut -d/ -f2)":/tmp/email.yaml
kubectl -n auth-scratch exec deploy/authentik-worker -- ak apply_blueprint /tmp/email.yaml
```

Expected: exits 0.

- [ ] **Step 3: Verify the expression took**

```bash
curl -sf -H "Authorization: Bearer $TOK" \
  "http://localhost:9000/api/v3/propertymappings/all/?managed=goauthentik.io/providers/oauth2/scope-email" \
  | jq -r '.results[0].expression' | grep email_verified
```

Expected: a line containing `"email_verified": True`.

- [ ] **Step 4: Commit**

```bash
git add k8s/authentik/blueprints/email-scope-mapping.yaml
git commit -m "Authentik blueprint: email scope mapping returns email_verified=true"
```

______________________________________________________________________

## Task 5: Author `recovery-flow.yaml`

**Files:**

- Create: `k8s/authentik/blueprints/recovery-flow.yaml`

- [ ] **Step 1: Adapt the flow export into the committed blueprint**

Start from `/tmp/ak-recovery-flow-export.yaml` and transform it:

1. Set `metadata.name: "homelab - recovery flow"`.
2. Add `state: present` to every entry; set `identifiers` to natural keys (flow `slug: recovery`; stages by `name`; policy by `name: recovery-allowed-group`; flow-stage-bindings by `{target: !KeyOf flow, order: N}`; policy-bindings by `{target: !KeyOf binding, policy: !KeyOf policy}`).
3. Replace any embedded pk/UUID cross-references with `!KeyOf <id>` (give each entry an `id:`), and reference the shipped prompt/write stages via `!Find [authentik_flows.models.Stage, [name, default-password-change-prompt]]` and `default-password-change-write`.
4. Confirm the expression policy body matches the spec exactly:

```python
pending_user = request.context.get("pending_user")
if not pending_user:
    return False
return ak_is_group_member(pending_user, name="homelab-users")
```

5. Confirm bindings 20/30/40 carry the recovery policy with **re-evaluate on stage run ON / evaluate on plan OFF** (field names per export, typically `re_evaluate_policies: true`, `evaluate_on_plan: false` on the FlowStageBinding, plus a PolicyBinding linking `recovery-allowed-group` to each binding).
6. Add the brand entry setting `flow_recovery` → the recovery flow (identifiers: the default brand's `domain`, per export).

Write the result to `k8s/authentik/blueprints/recovery-flow.yaml`. (The export gives the exact field set for this version; do not invent fields.)

- [ ] **Step 2: Apply to scratch and verify reconcile**

```bash
kubectl -n auth-scratch cp k8s/authentik/blueprints/recovery-flow.yaml \
  "$(kubectl -n auth-scratch get pod -l app.kubernetes.io/component=worker -o name | head -1 | cut -d/ -f2)":/tmp/recovery.yaml
kubectl -n auth-scratch exec deploy/authentik-worker -- ak apply_blueprint /tmp/recovery.yaml
```

Expected: exits 0.

- [ ] **Step 3: Verify the flow + bindings reconstructed**

```bash
curl -sf -H "Authorization: Bearer $TOK" \
  http://localhost:9000/api/v3/flows/instances/recovery/ | jq '{slug,designation}'
curl -sf -H "Authorization: Bearer $TOK" \
  "http://localhost:9000/api/v3/flows/bindings/?target__slug=recovery" \
  | jq '[.results[] | {order, stage_obj: .stage_obj.name, re_evaluate_policies, evaluate_on_plan}]'
```

Expected: flow `designation: "recovery"`; four bindings at orders 10/20/30/40; bindings 20/30/40 show `re_evaluate_policies: true`, `evaluate_on_plan: false`.

- [ ] **Step 4: Commit**

```bash
git add k8s/authentik/blueprints/recovery-flow.yaml
git commit -m "Authentik blueprint: password-recovery flow + group gate"
```

______________________________________________________________________

## Task 6: Author `applications/coder.yaml`

**Files:**

- Create: `k8s/authentik/blueprints/applications/coder.yaml`

- [ ] **Step 1: Write the blueprint** (fill bracketed values from `/tmp/ak-prod-export.yaml`)

`k8s/authentik/blueprints/applications/coder.yaml`:

```yaml
version: 1
metadata:
  name: "homelab - application - coder"
entries:
  - id: coder-provider
    model: authentik_providers_oauth2.models.OAuth2Provider
    state: present
    identifiers:
      name: Coder
    attrs:
      client_type: confidential
      client_id: "[COPY client_id FROM EXPORT]"
      client_secret: !Env AUTHENTIK_OIDC_CODER_SECRET   # confirm !Env arg syntax
      authorization_flow: !Find [authentik_flows.models.Flow, [slug, default-provider-authorization-implicit-consent]]
      invalidation_flow: !Find [authentik_flows.models.Flow, [slug, default-provider-invalidation-flow]]
      signing_key: !Find [authentik_crypto.models.CertificateKeyPair, [name, "authentik Self-signed Certificate"]]
      property_mappings:
        - !Find [authentik_providers_oauth2.models.ScopeMapping, [managed, goauthentik.io/providers/oauth2/scope-openid]]
        - !Find [authentik_providers_oauth2.models.ScopeMapping, [managed, goauthentik.io/providers/oauth2/scope-email]]
        - !Find [authentik_providers_oauth2.models.ScopeMapping, [managed, goauthentik.io/providers/oauth2/scope-profile]]
      redirect_uris: "[COPY redirect_uris STRUCTURE FROM EXPORT]"
      sub_mode: "[COPY FROM EXPORT, e.g. hashed_user_id]"
  - id: coder-app
    model: authentik_core.models.Application
    state: present
    identifiers:
      slug: coder
    attrs:
      name: Coder
      provider: !KeyOf coder-provider
      meta_launch_url: https://coder.vigihome.net
      policy_engine_mode: any
  - model: authentik_policies.models.PolicyBinding
    state: present
    identifiers:
      target: !Find [authentik_core.models.Application, [slug, coder]]
      group: !Find [authentik_core.models.Group, [name, homelab-users]]
      order: 0
    attrs:
      enabled: true
```

- [ ] **Step 2: Apply to scratch and verify reconcile + secret-from-env**

```bash
kubectl -n auth-scratch cp k8s/authentik/blueprints/applications/coder.yaml \
  "$(kubectl -n auth-scratch get pod -l app.kubernetes.io/component=worker -o name | head -1 | cut -d/ -f2)":/tmp/coder.yaml
kubectl -n auth-scratch exec deploy/authentik-worker -- ak apply_blueprint /tmp/coder.yaml
curl -sf -H "Authorization: Bearer $TOK" \
  http://localhost:9000/api/v3/providers/oauth2/?ordering=name | jq '.results[] | select(.name=="Coder") | {client_id, client_secret}'
```

Expected: exits 0; `client_secret` equals `scratch-coder-secret` (proves `!Env` injection works).

- [ ] **Step 3: Verify the group binding**

```bash
curl -sf -H "Authorization: Bearer $TOK" \
  "http://localhost:9000/api/v3/policies/bindings/?target__in=$(curl -sf -H 'Authorization: Bearer $TOK' http://localhost:9000/api/v3/core/applications/?slug=coder | jq -r '.results[0].pk')" \
  | jq '[.results[] | .group_obj.name]'
```

Expected: includes `"homelab-users"`.

- [ ] **Step 4: Commit**

```bash
git add k8s/authentik/blueprints/applications/coder.yaml
git commit -m "Authentik blueprint: Coder OIDC application"
```

______________________________________________________________________

## Task 7: Author `applications/outline.yaml`

**Files:**

- Create: `k8s/authentik/blueprints/applications/outline.yaml`

- [ ] **Step 1: Write the blueprint**

Same structure as Task 6's `coder.yaml`, with these substitutions (values from `/tmp/ak-prod-export.yaml`): `metadata.name: "homelab - application - outline"`; provider `id: outline-provider`, `identifiers.name: Outline`, `client_secret: !Env AUTHENTIK_OIDC_OUTLINE_SECRET`, Outline's `client_id` / `redirect_uris` / `sub_mode`; app `id: outline-app`, `identifiers.slug: outline`, `name: Outline`, `provider: !KeyOf outline-provider`, `meta_launch_url: https://docs.vigihome.net`; PolicyBinding `target: !Find [authentik_core.models.Application, [slug, outline]]`, `group: !Find [authentik_core.models.Group, [name, homelab-users]]`, `order: 0`. The `authorization_flow`, `invalidation_flow`, `signing_key`, and the three `property_mappings` are identical to Coder's.

`k8s/authentik/blueprints/applications/outline.yaml`:

```yaml
version: 1
metadata:
  name: "homelab - application - outline"
entries:
  - id: outline-provider
    model: authentik_providers_oauth2.models.OAuth2Provider
    state: present
    identifiers:
      name: Outline
    attrs:
      client_type: confidential
      client_id: "[COPY client_id FROM EXPORT]"
      client_secret: !Env AUTHENTIK_OIDC_OUTLINE_SECRET
      authorization_flow: !Find [authentik_flows.models.Flow, [slug, default-provider-authorization-implicit-consent]]
      invalidation_flow: !Find [authentik_flows.models.Flow, [slug, default-provider-invalidation-flow]]
      signing_key: !Find [authentik_crypto.models.CertificateKeyPair, [name, "authentik Self-signed Certificate"]]
      property_mappings:
        - !Find [authentik_providers_oauth2.models.ScopeMapping, [managed, goauthentik.io/providers/oauth2/scope-openid]]
        - !Find [authentik_providers_oauth2.models.ScopeMapping, [managed, goauthentik.io/providers/oauth2/scope-email]]
        - !Find [authentik_providers_oauth2.models.ScopeMapping, [managed, goauthentik.io/providers/oauth2/scope-profile]]
      redirect_uris: "[COPY redirect_uris STRUCTURE FROM EXPORT]"
      sub_mode: "[COPY FROM EXPORT]"
  - id: outline-app
    model: authentik_core.models.Application
    state: present
    identifiers:
      slug: outline
    attrs:
      name: Outline
      provider: !KeyOf outline-provider
      meta_launch_url: https://docs.vigihome.net
      policy_engine_mode: any
  - model: authentik_policies.models.PolicyBinding
    state: present
    identifiers:
      target: !Find [authentik_core.models.Application, [slug, outline]]
      group: !Find [authentik_core.models.Group, [name, homelab-users]]
      order: 0
    attrs:
      enabled: true
```

- [ ] **Step 2: Apply to scratch and verify (mirror Task 6 Steps 2–3 with `outline`)**

```bash
kubectl -n auth-scratch cp k8s/authentik/blueprints/applications/outline.yaml \
  "$(kubectl -n auth-scratch get pod -l app.kubernetes.io/component=worker -o name | head -1 | cut -d/ -f2)":/tmp/outline.yaml
kubectl -n auth-scratch exec deploy/authentik-worker -- ak apply_blueprint /tmp/outline.yaml
curl -sf -H "Authorization: Bearer $TOK" \
  http://localhost:9000/api/v3/providers/oauth2/?ordering=name | jq '.results[] | select(.name=="Outline") | {client_id, client_secret}'
```

Expected: exits 0; `client_secret` equals `scratch-outline-secret`.

- [ ] **Step 3: Commit**

```bash
git add k8s/authentik/blueprints/applications/outline.yaml
git commit -m "Authentik blueprint: Outline OIDC application"
```

______________________________________________________________________

## Task 8: Full reconstruction test on a wiped scratch instance

**Files:**

- Create: `k8s/authentik/blueprints/README.md`

- [ ] **Step 1: Write the blueprints README (render + apply instructions)**

`k8s/authentik/blueprints/README.md`:

````markdown
# Authentik Blueprints

Config-as-code for Authentik's otherwise UI-/postgres-only objects (#104).
Files here are flattened by basename into the `authentik-blueprints`
ConfigMap (ConfigMap keys can't contain `/`, so the `applications/`
subfolder is authoring-only). The worker discovers and reconciles every
`.yaml` on startup.

## Render + apply the ConfigMap

```bash
# laptop, kubectl context = homelab
kubectl -n auth create configmap authentik-blueprints \
  $(find k8s/authentik/blueprints -name '*.yaml' -printf '--from-file=%f=%p ') \
  --dry-run=client -o yaml | kubectl apply -f -
# then roll the worker so it re-discovers:
kubectl -n auth rollout restart deploy/authentik-worker
```

Secrets are never in these files: OIDC `client_secret`s use `!Env`,
sourced from the `authentik-oidc-secrets` Secret (see ../README.md).
````

- [ ] **Step 2: Wipe the scratch DB for a true from-zero test**

```bash
helm uninstall authentik -n auth-scratch
kubectl -n auth-scratch delete pvc --all
helm install authentik authentik/authentik -n auth-scratch \
  --version 2026.2.2 -f k8s/authentik/scratch-values.yaml
kubectl -n auth-scratch rollout status deploy/authentik-server --timeout=300s
```

- [ ] **Step 3: Render the ConfigMap into auth-scratch and restart the worker**

```bash
kubectl -n auth-scratch create configmap authentik-blueprints \
  $(find k8s/authentik/blueprints -name '*.yaml' -printf '--from-file=%f=%p ') \
  --dry-run=client -o yaml | kubectl apply -f -
# scratch was installed without blueprints.configMaps; apply each file directly
# to prove discovery-from-zero instead:
for f in $(find k8s/authentik/blueprints -name '*.yaml'); do
  b=$(basename "$f")
  kubectl -n auth-scratch cp "$f" "$(kubectl -n auth-scratch get pod -l app.kubernetes.io/component=worker -o name | head -1 | cut -d/ -f2)":/tmp/"$b"
  kubectl -n auth-scratch exec deploy/authentik-worker -- ak apply_blueprint /tmp/"$b"
done
```

Expected: every `ak apply_blueprint` exits 0.

- [ ] **Step 4: Assert the full surface reconstructed from empty postgres**

```bash
kubectl -n auth-scratch port-forward deploy/authentik-server 9001:9000 &
A="http://localhost:9001/api/v3"; H=(-H "Authorization: Bearer $TOK")
curl -sf "${H[@]}" "$A/core/groups/?name=homelab-users" | jq -e '.results|length==1'
curl -sf "${H[@]}" "$A/flows/instances/recovery/" | jq -e '.designation=="recovery"'
curl -sf "${H[@]}" "$A/core/applications/?slug=coder" | jq -e '.results|length==1'
curl -sf "${H[@]}" "$A/core/applications/?slug=outline" | jq -e '.results|length==1'
curl -sf "${H[@]}" "$A/propertymappings/all/?managed=goauthentik.io/providers/oauth2/scope-email" | jq -e '.results[0].expression|test("email_verified.*True")'
```

Expected: every `jq -e` exits 0 (all assertions true). **This is the #104 acceptance gate.**

- [ ] **Step 5: Tear down scratch and commit the README**

```bash
kubectl delete namespace auth-scratch
git add k8s/authentik/blueprints/README.md
git commit -m "Authentik blueprints: render/apply README + validated from-zero reconstruction"
```

______________________________________________________________________

## Task 9: Wire prod values + secret template

**Files:**

- Modify: `k8s/authentik/values.yaml`

- Modify: `k8s/authentik/secret.example.yaml`

- Modify: `.github/workflows/lint.yml` (only if the new files fall outside the current globs)

- [ ] **Step 1: Add the OIDC-secret env + blueprints mount to `values.yaml`**

In `k8s/authentik/values.yaml`, append two entries to the existing `global.env` list (after `AUTHENTIK_EMAIL__FROM`):

```yaml
    # OIDC provider client secrets for the blueprinted apps (#104). The
    # blueprint references these via !Env; values come from the
    # authentik-oidc-secrets Secret (Bitwarden-backed). One Bitwarden
    # field per app feeds both this env and the downstream service Secret.
    - name: AUTHENTIK_OIDC_CODER_SECRET
      valueFrom:
        secretKeyRef:
          name: authentik-oidc-secrets
          key: oidc-coder-client-secret
    - name: AUTHENTIK_OIDC_OUTLINE_SECRET
      valueFrom:
        secretKeyRef:
          name: authentik-oidc-secrets
          key: oidc-outline-client-secret
```

And add the top-level blueprints mount (sibling of `server:` / `worker:`):

```yaml
# Config-as-code (#104). Mounts the authentik-blueprints ConfigMap; the
# worker discovers + reconciles every .yaml key on startup. Rendered from
# k8s/authentik/blueprints/ — see that dir's README.
blueprints:
  configMaps:
    - authentik-blueprints
```

- [ ] **Step 2: Document the new Secret in `secret.example.yaml`**

Append to `k8s/authentik/secret.example.yaml` a second documented Secret:

```yaml
---
# TEMPLATE ONLY. The authentik-oidc-secrets Secret holds OIDC client
# secrets referenced by blueprints via !Env. Created from Bitwarden:
#   oidc-coder-client-secret   ← item 'Homelab Coder',   field oidc-client-secret
#   oidc-outline-client-secret ← item 'Homelab Outline', field oidc-client-secret
apiVersion: v1
kind: Secret
metadata:
  name: authentik-oidc-secrets
  namespace: auth
type: Opaque
stringData:
  oidc-coder-client-secret: REPLACE_WITH_CODER_OIDC_CLIENT_SECRET
  oidc-outline-client-secret: REPLACE_WITH_OUTLINE_OIDC_CLIENT_SECRET
```

- [ ] **Step 3: Verify lint coverage and render**

```bash
# Confirm the lint filter already covers values.yaml + secret.example.yaml (it does).
# Blueprint files live under blueprints/ and are not k8s manifests, so they're
# intentionally outside kubeconform; confirm yamllint still parses them:
yamllint k8s/authentik/blueprints/ k8s/authentik/values.yaml k8s/authentik/secret.example.yaml
helm template authentik authentik/authentik --version 2026.2.2 \
  -f k8s/authentik/values.yaml --show-only charts/... 2>/dev/null | head -1 || \
  helm lint --strict authentik/authentik --version 2026.2.2 -f k8s/authentik/values.yaml
```

Expected: yamllint clean; helm renders without error. If yamllint flags the blueprint files (the `!Env`/`!Find` custom tags can trip generic YAML linters), add `k8s/authentik/blueprints/` to yamllint's ignore list in the repo's lint config and note why.

- [ ] **Step 4: Commit**

```bash
git add k8s/authentik/values.yaml k8s/authentik/secret.example.yaml .github/workflows/lint.yml
git commit -m "Authentik: wire blueprints ConfigMap + oidc-secrets env into values"
```

______________________________________________________________________

## Task 10: Prod cutover (operator-run, live)

**Files:** none (live ops).

> Hand these to the operator to run. They mutate the live SPOF. Each block includes `git checkout`/`pull` so a stale checkout can't no-op, and is tagged with its host.

- [ ] **Step 1: Fresh backup + branch checkout**

```bash
# gandalf — force a restic snapshot of the authentik postgres before mutating
kubectl -n backup create job --from=cronjob/restic-backup pre-blueprints-$(date +%s)
# laptop — get the branch
cd ~/git/nickvigilante/homelab && git checkout authentik-blueprints && git pull
```

Confirm `AUTHENTIK_SECRET_KEY` is in Bitwarden item `Homelab Authentik` (rollback needs it).

- [ ] **Step 2: Create the authentik-oidc-secrets Secret from Bitwarden**

```bash
# laptop, context = homelab
export BW_SESSION="$(bw unlock --raw)"; bw sync
kubectl -n auth create secret generic authentik-oidc-secrets \
  --from-literal=oidc-coder-client-secret="$(bw get item 'Homelab Coder' | jq -r '.fields[]|select(.name=="oidc-client-secret")|.value')" \
  --from-literal=oidc-outline-client-secret="$(bw get item 'Homelab Outline' | jq -r '.fields[]|select(.name=="oidc-client-secret")|.value')"
unset BW_SESSION
kubectl -n auth get secret authentik-oidc-secrets -o jsonpath='{.data.oidc-coder-client-secret}' | base64 -d | wc -c
```

Expected: a non-zero length (sanity-check no empty value, per the Outline-deploy lesson).

- [ ] **Step 3: Render the ConfigMap + upgrade (pinned) + roll worker**

```bash
# laptop, context = homelab
kubectl -n auth create configmap authentik-blueprints \
  $(find k8s/authentik/blueprints -name '*.yaml' -printf '--from-file=%f=%p ') \
  --dry-run=client -o yaml | kubectl apply -f -
CHART_VER="$(helm list -n auth -f '^authentik$' -o json | jq -r '.[0].chart' | sed 's/^authentik-//')"
helm -n auth upgrade authentik authentik/authentik --version "$CHART_VER" -f k8s/authentik/values.yaml
kubectl -n auth rollout status deploy/authentik-worker
```

Expected: chart version unchanged (pinned); worker rolls cleanly.

- [ ] **Step 4: Verify adoption — successful instances, zero duplicates, flows intact**

```bash
# laptop, context = homelab
kubectl -n auth exec deploy/authentik-server -- \
  curl -sf -H "Authorization: Bearer $(kubectl -n auth get secret authentik-secrets -o jsonpath='{.data.bootstrap-token}'|base64 -d)" \
  http://localhost:9000/api/v3/managed/blueprints/ | jq '[.results[]|{name,status}]'
# zero duplicates: each app/provider/group appears exactly once
for q in "core/applications/?slug=coder" "core/applications/?slug=outline" "core/groups/?name=homelab-users" "providers/oauth2/?name=Coder" "providers/oauth2/?name=Outline"; do
  echo -n "$q -> "; curl -sf -H "Authorization: Bearer <bootstrap-token>" "https://authentik.vigihome.net/api/v3/$q" | jq '.pagination.count'
done
```

Expected: every blueprint instance `status` successful; every count `1` (no duplicates).

- [ ] **Step 5: Run the recovery-flow regression checklist + OIDC logins**

Per `k8s/authentik/README.md` "Testing the recovery flow": (1) a `homelab-users` member resets via "Forgot password?" and the new password works; (2) akadmin/non-member dead-ends (no email, no prompt); (3) a bogus identifier returns the same response (no enumeration). Then confirm **Coder** and **Outline** OIDC logins still succeed.

Expected: all pass. **If anything breaks:** restore `/opt/authentik/postgres` from the pre-cutover snapshot (README "Postgres backup / restore"), then investigate on scratch.

______________________________________________________________________

## Task 11: Documentation

**Files:**

- Modify: `k8s/authentik/README.md`

- Modify: `CLAUDE.md` (the "What we don't back up" Authentik note)

- [ ] **Step 1: Replace the "click in the UI" sections in `k8s/authentik/README.md`**

For each of: "Customize the `email` scope mapping", "Adding an OIDC client", and the recovery-flow "Reapply on rebuild (in order)" table — replace the manual click-through with a pointer to the blueprint, e.g.:

> **This is now captured as code.** See `blueprints/email-scope-mapping.yaml`. Applied automatically by the worker from the `authentik-blueprints` ConfigMap; to change it, edit the file and re-render (see `blueprints/README.md`).

Keep the akadmin recovery / break-glass sections as-is (those are runbooks, not config). Add to "One-time setup" a step to create `authentik-oidc-secrets` (the Task 10 Step 2 command) and to render + reference the `authentik-blueprints` ConfigMap (`blueprints.configMaps` is already in `values.yaml`).

- [ ] **Step 2: Update the CLAUDE.md Authentik backup note**

In CLAUDE.md "What we don't back up" → the "Authentik UI-only configuration state" bullet, append:

```
The recovery flow, OIDC providers/apps, group bindings, and the email
scope mapping are now captured as Blueprints in k8s/authentik/blueprints/
(#104) — a from-scratch rebuild reconstructs them without the postgres
restore. Remaining DB-only state: users, sessions, and event history.
```

- [ ] **Step 3: Commit**

```bash
git add k8s/authentik/README.md CLAUDE.md
git commit -m "Authentik docs: point UI-config sections at blueprints (#104)"
```

______________________________________________________________________

## Task 12: Finish the development branch

- [ ] **Step 1: Use the finishing-a-development-branch skill**

Announce and use **superpowers:finishing-a-development-branch** to verify all tasks are committed, open the PR (title referencing #104), and confirm the recovery regression + scratch reconstruction evidence is in the PR body. Follow the repo's PR conventions (squash-merge, no Co-Authored-By trailer).

______________________________________________________________________

## Self-review notes

- **Spec coverage:** mount ConfigMap (T9) · identifier adoption (T3–7 identifiers, verified T10.4) · `!Env`/Bitwarden secret injection (T6/T7/T9/T10.2) · scratch-first validation (T1/T8) · email mapping in-place override (T4) · file layout incl. `applications/` (T3–8) · scratch→prod rollout + rollback (T8/T10) · docs (T11) · acceptance criteria (T8.4 + T10.4–5) · lint coverage (T9.3). All spec sections map to a task.
- **Open items the spec flagged** (`!Env` arg syntax; chart mount path/discovery; exact model/field names) are resolved empirically: model/field names come from the Task 2 export; `!Env` and discovery are proven on the Task 1/8 scratch instance before prod.
- **Bracketed `[COPY … FROM EXPORT]` markers** in T6/T7 are deliberate — the authoritative values live in the per-version export from Task 2, not in this plan; copying invented client_ids/redirect_uris would be wrong. Every *structural* field is shown.
