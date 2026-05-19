# Header-Stripping Middleware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Traefik `Middleware` attached as a default to the `websecure` entryPoint that strips inbound auth-context headers (X-Forwarded-User / Remote-User / X-Authentik-*) so backends behind Traefik can't accidentally trust client-supplied identity claims. Closes Tier-1 Authentik audit finding 6-ii.

**Architecture:** One Middleware CRD in `kube-system`, referenced as a default middleware on the `websecure` entryPoint via the existing Traefik HelmChartConfig. Both files deployed by Ansible to k3s's auto-apply manifests directory (`/var/lib/rancher/k3s/server/manifests/`), so a single Ansible run lands the change atomically.

**Tech Stack:** Traefik (k3s-bundled, via HelmChartConfig override), Kubernetes Middleware CRD (`traefik.io/v1alpha1`), Ansible (`provision-gandalf.yml`), yamllint for local validation.

**Spec reference:** `docs/superpowers/specs/2026-05-19-header-stripping-middleware-design.md`

**Working branch:** `traefik-strip-auth-headers-middleware` (already exists, spec already committed there)

**Dependency:** PR #61 (audit doc issue-ref fix) is in flight. Task 4 must rebase the branch onto main *after* #61 merges, otherwise the audit doc update conflicts. Tasks 1-3 are independent of #61 and can proceed immediately.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `system/traefik-strip-auth-headers-middleware.yaml` | New | The Traefik Middleware CRD definition (just the spec) |
| `system/traefik-helmchartconfig.yaml` | Modify | Wires the Middleware as a default on the `websecure` entryPoint |
| `ansible/provision-gandalf.yml` | Modify | Copies the Middleware manifest to k3s's auto-apply directory |
| `audits/tier-1-authentik.md` | Modify | Flips finding 6-ii to Resolved + trims the open-follow-ups list |

---

## Task 1: Create the Middleware CRD manifest

**Files:**
- Create: `system/traefik-strip-auth-headers-middleware.yaml`

- [ ] **Step 1: Create the file with the complete Middleware spec**

Write `system/traefik-strip-auth-headers-middleware.yaml`:

```yaml
# Traefik Middleware that strips inbound auth-context headers (X-Forwarded-User,
# Remote-User, X-Authentik-* etc.) so backends behind Traefik can't accidentally
# trust client-supplied identity claims.
#
# Attached as a default middleware on the websecure entryPoint via
# system/traefik-helmchartconfig.yaml, so every HTTPS route inherits it
# without per-Ingress annotation.
#
# Closes Tier-1 Authentik audit finding 6-ii (see audits/tier-1-authentik.md).
#
# Mechanism: headers.customRequestHeaders with empty-string values is the
# standard Traefik pattern for removing a header from the request before
# it reaches the backend.
#
# NOT stripped: transport headers (X-Forwarded-For/-Proto/-Host/-Port,
# X-Real-IP) and legitimate client auth material (Authorization, cookies).
# Stripping XFF would regress PR #53 (real client IPs).
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: strip-auth-headers
  namespace: kube-system
spec:
  headers:
    customRequestHeaders:
      # oauth2-proxy / traefik-forward-auth family
      X-Forwarded-User: ""
      X-Forwarded-Email: ""
      X-Forwarded-Preferred-Username: ""
      X-Forwarded-Groups: ""
      X-Forwarded-Name: ""
      X-Forwarded-Roles: ""
      # Authelia / nginx auth_request family
      Remote-User: ""
      Remote-Email: ""
      Remote-Groups: ""
      Remote-Name: ""
      # Authentik outpost (proxy provider) family
      X-Authentik-Username: ""
      X-Authentik-Groups: ""
      X-Authentik-Email: ""
      X-Authentik-Name: ""
      X-Authentik-Uid: ""
      X-Authentik-Jwt: ""
      X-Authentik-Meta-Jwks: ""
      X-Authentik-Meta-Outpost: ""
      X-Authentik-Meta-Provider: ""
      X-Authentik-Meta-App: ""
      X-Authentik-Meta-Version: ""
```

- [ ] **Step 2: Lint the new file locally (matches CI)**

Run: `yamllint --config-file .github/yamllint.yml system/traefik-strip-auth-headers-middleware.yaml`
Expected: no output (clean). If it errors, fix and re-run.

- [ ] **Step 3: Commit**

```bash
git add system/traefik-strip-auth-headers-middleware.yaml
git commit -m "Traefik: add strip-auth-headers Middleware CRD"
```

---

## Task 2: Wire the entryPoint default middleware in the HelmChartConfig

**Files:**
- Modify: `system/traefik-helmchartconfig.yaml`

- [ ] **Step 1: Extend `valuesContent` with the entryPoint default-middleware reference**

Find the current block at the bottom of `system/traefik-helmchartconfig.yaml`:

```yaml
spec:
  valuesContent: |-
    service:
      spec:
        externalTrafficPolicy: Local
```

Replace it with:

```yaml
spec:
  valuesContent: |-
    service:
      spec:
        externalTrafficPolicy: Local
    # Default middleware on the websecure entryPoint: every HTTPS request
    # passes through strip-auth-headers before reaching any backend. Closes
    # Tier-1 Authentik audit finding 6-ii. The middleware itself lives at
    # system/traefik-strip-auth-headers-middleware.yaml (kube-system ns).
    # Traefik refers to k8s-CRD middlewares as <namespace>-<name>@kubernetescrd.
    ports:
      websecure:
        middlewares:
          - kube-system-strip-auth-headers@kubernetescrd
```

- [ ] **Step 2: Lint**

Run: `yamllint --config-file .github/yamllint.yml system/traefik-helmchartconfig.yaml`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add system/traefik-helmchartconfig.yaml
git commit -m "Traefik: default strip-auth-headers on websecure entryPoint"
```

---

## Task 3: Add Ansible copy task for the new manifest

**Files:**
- Modify: `ansible/provision-gandalf.yml` (around line 113, the existing HelmChartConfig copy task)

- [ ] **Step 1: Find the existing HelmChartConfig copy task**

The existing task is at approximately line 110-114 in `ansible/provision-gandalf.yml`:

```yaml
    # with the new values. See system/traefik-helmchartconfig.yaml.
    - name: Copy Traefik HelmChartConfig to k3s manifests dir
      copy:
        src: ../system/traefik-helmchartconfig.yaml
        dest: /var/lib/rancher/k3s/server/manifests/traefik-config.yaml
```

(The exact task name and `mode`/`owner` lines may differ — match what's there.)

- [ ] **Step 2: Add a sibling task immediately after for the Middleware**

Insert this block right after the existing HelmChartConfig `copy` task ends (preserving the same indentation as the other tasks in the file):

```yaml
    # The Middleware CRD strips inbound auth-context headers on the
    # websecure entryPoint; see system/traefik-strip-auth-headers-middleware.yaml.
    # k3s's manifests-dir controller auto-applies non-HelmChart YAML dropped
    # here, so no kubectl apply is needed.
    - name: Copy strip-auth-headers Middleware to k3s manifests dir
      copy:
        src: ../system/traefik-strip-auth-headers-middleware.yaml
        dest: /var/lib/rancher/k3s/server/manifests/strip-auth-headers-middleware.yaml
        owner: root
        group: root
        mode: '0600'
```

If the existing HelmChartConfig task includes `become: true` or `tags:` at the task level (not just play-level), match those too.

- [ ] **Step 3: Lint**

Run: `yamllint --config-file .github/yamllint.yml ansible/provision-gandalf.yml`
Expected: clean.

- [ ] **Step 4: Sanity-check Ansible syntax**

Run: `ansible-playbook --syntax-check ansible/provision-gandalf.yml`
Expected: `playbook: ansible/provision-gandalf.yml` (no errors).

- [ ] **Step 5: Commit**

```bash
git add ansible/provision-gandalf.yml
git commit -m "Ansible: ship strip-auth-headers Middleware to k3s manifests dir"
```

---

## Task 4: Update audit doc — finding 6-ii Resolved

**Files:**
- Modify: `audits/tier-1-authentik.md`

**Dependency check:** This task may conflict with the changes in PR #61 (audit doc issue-ref fix). Before starting, verify #61's state.

- [ ] **Step 1: Check PR #61 status**

Run: `gh pr view 61 --json state,mergedAt --jq '.state, .mergedAt'`

- If state is `MERGED`: rebase the branch onto the updated main before editing the audit doc.
  ```bash
  git fetch origin
  git rebase origin/main
  ```
  Resolve any conflicts by keeping main's `#59` / `#60` issue references and re-applying any spec/plan additions on top. The audit doc itself shouldn't have local edits yet on this branch (the spec/plan are in `docs/`).

- If state is `OPEN`: pause here and resume once #61 merges. Tasks 1-3 can remain landed on this branch; the audit doc update is the only piece that conflicts with #61.

- [ ] **Step 2: Update finding 6-ii in the "Check 6 — network exposure" table**

In `audits/tier-1-authentik.md`, find the row beginning with `| 6-ii —`. The current row reads (post-#61):

```
| 6-ii — Traefik passes through arbitrary upstream headers (e.g. `X-Forwarded-User`) that a misbehaving downstream could trust | **Open** | Header-stripping middleware listed below |
```

Replace with:

```
| 6-ii — Traefik passes through arbitrary upstream headers (e.g. `X-Forwarded-User`) that a misbehaving downstream could trust | **Resolved** | PR #NN — default Middleware on the `websecure` entryPoint strips `X-Forwarded-User` / `Remote-User` / `X-Authentik-*` families on every HTTPS route |
```

Leave `#NN` literal for now; backfill the actual PR number once Task 5 opens the PR.

- [ ] **Step 3: Drop entry #1 from "Open follow-ups"**

Find the section:

```
## Open follow-ups

Two items deliberately left open at end of Tier-1. Both have a clear
shape but need work outside the audit cadence.

1. **Header-stripping middleware** (finding 6-ii). Add a Traefik
   `Middleware` that strips `X-Forwarded-User` and similar
   client-supplied auth-context headers at the edge, then attach it
   to the Authentik Ingress (and ideally as a default for every
   `*.vigihome.net` route). No PR yet.
2. **Email-based recovery flow** (finding 5-i). Now that SMTP is
   wired, the recovery flow can be enabled and bound to the
   authentication flow. Needs user sign-off on UX details (which
   users / which lockout duration / what the email template looks
   like) before it lands.
```

Replace with:

```
## Open follow-ups

One item deliberately left open at end of Tier-1. Has a clear shape
but needs work outside the audit cadence.

1. **Email-based recovery flow** (finding 5-i). Now that SMTP is
   wired, the recovery flow can be enabled and bound to the
   authentication flow. Needs user sign-off on UX details (which
   users / which lockout duration / what the email template looks
   like) before it lands.
```

(Header-stripping was item #1; the renumber drops the old #1 and re-leads with the recovery flow.)

- [ ] **Step 4: Lint the markdown file**

Run: `yamllint --config-file .github/yamllint.yml audits/tier-1-authentik.md 2>/dev/null || true`
(yamllint will likely skip `.md` files — that's expected. There's no markdown linter in CI; manual proofread is the bar.)

- [ ] **Step 5: Commit**

```bash
git add audits/tier-1-authentik.md
git commit -m "Audit: flip finding 6-ii to Resolved"
```

---

## Task 5: Push branch and open PR

- [ ] **Step 1: Push**

```bash
git push -u origin traefik-strip-auth-headers-middleware
```

If the branch already exists on origin (the spec was pushed earlier), the push fast-forwards.

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "Traefik: strip inbound auth-context headers (audit 6-ii)" --body "$(cat <<'EOF'
## Summary

- Add a Traefik `Middleware` (`kube-system/strip-auth-headers`) that clears inbound `X-Forwarded-User`, `Remote-User`, and `X-Authentik-*` header families so backends can't accidentally trust client-supplied identity claims.
- Attach it as a default middleware on the `websecure` entryPoint via the existing HelmChartConfig — every HTTPS route inherits the protection automatically.
- Add a sibling Ansible `copy` task so the new Middleware manifest ships to k3s's auto-apply directory alongside the HelmChartConfig.
- Closes Tier-1 Authentik audit finding 6-ii (see `audits/tier-1-authentik.md`).

Design spec: `docs/superpowers/specs/2026-05-19-header-stripping-middleware-design.md`.

## Test plan

Live verification post-merge, from laptop:

- [ ] `ansible-playbook -i ansible/inventory.yml ansible/provision-gandalf.yml`
- [ ] `kubectl -n kube-system get middleware strip-auth-headers -o yaml` — spec matches
- [ ] Confirm Traefik pod args include `--entryPoints.websecure.http.middlewares=kube-system-strip-auth-headers@kubernetescrd` (fall back to `additionalArguments` if the chart values key didn't render)
- [ ] Inject `X-Forwarded-User`, `X-Authentik-Username`, `Remote-User` headers via curl against `authentik.vigihome.net` — verify they don't reach the backend (Authentik request log, or temporary httpbin Ingress)
- [ ] Sign in to authentik.vigihome.net, jellyfin.vigihome.net, coder.vigihome.net end-to-end via OIDC
- [ ] Authentik admin → Events shows real client IPs (LAN 192.168.50.x or tailnet 100.x.x.x), confirming PR #53 not regressed
EOF
)"
```

Capture the PR number from the URL the command prints.

- [ ] **Step 3: Backfill `#NN` in the audit doc**

In `audits/tier-1-authentik.md`, replace `PR #NN` in finding 6-ii's row with the actual PR number from Step 2. Commit and push:

```bash
git add audits/tier-1-authentik.md
git commit -m "Audit: backfill PR number in finding 6-ii"
git push
```

---

## Task 6: Apply on cluster and verify (post-merge)

Run only after the PR is merged to main.

- [ ] **Step 1: Pull latest main on the laptop**

```bash
cd ~/git/nickvigilante/homelab
git checkout main
git pull origin main --ff-only
```

- [ ] **Step 2: Run the Ansible playbook against gandalf**

```bash
ansible-playbook -i ansible/inventory.yml ansible/provision-gandalf.yml
```

Expected: green run. Specifically the two Traefik copy tasks (HelmChartConfig + Middleware) should report `changed` on the first run after merge, then `ok` on subsequent runs.

- [ ] **Step 3: Wait for k3s to apply the new manifests** (run on gandalf)

```bash
ssh gandalf 'kubectl -n kube-system rollout status deploy/traefik --timeout=120s'
```

Expected: `deployment "traefik" successfully rolled out`.

- [ ] **Step 4: Verify the Middleware CRD is loaded**

```bash
ssh gandalf 'kubectl -n kube-system get middleware strip-auth-headers -o yaml'
```

Expected: spec.headers.customRequestHeaders contains all 21 keys from Task 1.

- [ ] **Step 5: Verify the entryPoint default attachment**

```bash
ssh gandalf "kubectl -n kube-system get pod -l app.kubernetes.io/name=traefik -o jsonpath='{.items[0].spec.containers[0].args}' | tr ',' '\n' | grep -iE 'middleware|websecure'"
```

Expected: one of these args is present:
- `--entryPoints.websecure.http.middlewares=kube-system-strip-auth-headers@kubernetescrd`

If absent, the `ports.websecure.middlewares` chart values key didn't render in the k3s-bundled chart. Fall back to the explicit `additionalArguments` form by editing `system/traefik-helmchartconfig.yaml`:

```yaml
spec:
  valuesContent: |-
    service:
      spec:
        externalTrafficPolicy: Local
    additionalArguments:
      - --entryPoints.websecure.http.middlewares=kube-system-strip-auth-headers@kubernetescrd
```

Open a follow-up PR with that change (don't sneak it into main without review).

- [ ] **Step 6: Header-injection test from a tailnet host**

```bash
curl -sk -H "X-Forwarded-User: attacker" \
         -H "X-Authentik-Username: pwn" \
         -H "Remote-User: bypass" \
         https://authentik.vigihome.net/-/health/ready/ -o /dev/null -w "HTTP %{http_code}\n"
```

Expected: `HTTP 200` (the health endpoint).

Then in Authentik admin → Events → recent requests, verify the injected headers do not appear in the recorded request metadata.

If Authentik doesn't surface raw inbound headers, deploy a temporary echo backend:

```bash
ssh gandalf 'kubectl run httpbin --image=kennethreitz/httpbin:latest --port=80 --namespace=default'
ssh gandalf 'kubectl expose pod httpbin --port=80 --namespace=default'
# Add a temporary Ingress for httpbin.vigihome.net via your favorite quick path,
# then:
curl -sk -H "X-Forwarded-User: attacker" \
         -H "X-Authentik-Username: pwn" \
         -H "Remote-User: bypass" \
         https://httpbin.vigihome.net/headers
# Verify the response JSON does NOT echo back those three headers.
ssh gandalf 'kubectl delete pod httpbin -n default; kubectl delete svc httpbin -n default'
# Remove the temporary Ingress.
```

- [ ] **Step 7: Legitimate flow regression test**

Sign in to each end-to-end via a browser:
- https://authentik.vigihome.net — admin login
- https://jellyfin.vigihome.net — OIDC sign-in
- https://coder.vigihome.net — OIDC sign-in

Expected: all three work normally.

- [ ] **Step 8: PR #53 (real-client-IP) non-regression**

In Authentik admin → Events, inspect a recent login event from your laptop. The client IP field should show:
- Your LAN address (e.g. `192.168.50.42`) when on LAN
- Your tailnet address (e.g. `100.92.x.y`) when on tailnet
- NOT `10.42.0.1` (CNI bridge)
- NOT `192.168.50.135` (gandalf node IP)

If the IP is wrong, the new middleware may have inadvertently shadowed XFF handling — investigate before declaring done.

- [ ] **Step 9: Mark verification complete in TaskUpdate**

Mark Task #3 ("Plan + implement header-stripping middleware") as completed once all steps 1-8 pass.

---

## Rollback procedure

If any verification step in Task 6 fails and a quick fix isn't obvious, rollback:

```bash
git revert <merge-commit-sha>          # creates revert commit on main
git push origin main                    # branch protection: needs PR
# (Open + merge the revert PR through the normal flow)
cd ~/git/nickvigilante/homelab && git pull
ansible-playbook -i ansible/inventory.yml ansible/provision-gandalf.yml
```

If rollback is urgent (the middleware is actively breaking traffic), emergency stop-gap from the laptop:

```bash
ssh gandalf 'sudo rm /var/lib/rancher/k3s/server/manifests/strip-auth-headers-middleware.yaml'
ssh gandalf 'kubectl -n kube-system delete middleware strip-auth-headers'
```

(Traefik will log a warning about the entryPoint referencing a missing middleware but will pass requests through unchanged — missing middleware is no-op, not request-drop.)
