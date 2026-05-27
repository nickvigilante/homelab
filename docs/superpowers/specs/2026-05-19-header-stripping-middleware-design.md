# Tier-1 Authentik audit 6-ii: header-stripping middleware

**Date:** 2026-05-19
**Audit reference:** `audits/tier-1-authentik.md`, finding 6-ii
**Status:** Design approved; awaiting implementation

## Problem

Tier-1 Authentik audit finding 6-ii (open):

> Traefik passes through arbitrary upstream headers (e.g. `X-Forwarded-User`) that a misbehaving downstream could trust.

A client on the LAN or tailnet today can include `X-Forwarded-User: anyone` (or `Remote-User`, or `X-Authentik-Username`) in an HTTP request, and Traefik will forward those headers verbatim to the backend service. Every service behind Traefik must independently know to ignore them. The audit-grade defense is to strip those headers at the edge so backends can't trust them even by mistake.

Compounding factor: vigihome.net is internal-only (no public DNS or port-forward), but the trust boundary still includes any device on the LAN or tailnet — including potentially-compromised laptops. The middleware is defense-in-depth that hardens against header-injection from any direction, not just public traffic.

## Goal

Add a Traefik `Middleware` attached as a default to the `websecure` entryPoint, so every HTTPS request entering the cluster has client-supplied auth-context headers cleared before reaching any backend. No per-service annotation; new Ingresses inherit the protection automatically.

## Non-goals

- **Forward-auth wiring.** Not adding Authentik forward-auth in this PR. The middleware is designed to coexist with future forward-auth (see "Forward-auth future-proofing" below).
- **Cluster-wide header policy beyond auth-context.** Not touching transport headers (`X-Forwarded-For`, etc.) or general security headers (CSP, HSTS, etc.). Scoped narrowly to the audit finding.
- **Public-internet exposure decisions.** "vigihome.net should stay tailnet-only" is a separate Tier-2 audit item (#59).

## Approach

Single Traefik `Middleware` CRD in `kube-system`, referenced by name from the `websecure` entryPoint's default-middleware list in the Traefik HelmChartConfig.

### Why entryPoint default and not per-Ingress

| Approach                                                       | Pros                                                                                         | Cons                                                                                                            |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| **EntryPoint default middleware** (chosen)                     | One config change, applies to every HTTPS route automatically, new Ingresses get it for free | Requires HelmChartConfig edit; less visible to a reader skimming a service's `values.yaml`                      |
| Per-Ingress annotation                                         | Visible at each Ingress                                                                      | Requires touching every `values.yaml` / raw Ingress; future Ingresses can silently skip; harder to keep in sync |
| Cross-namespace single Middleware + each Ingress references it | Same as above, plus one source-of-truth                                                      | Requires `--providers.kubernetescrd.allowCrossNamespace=true` _and_ still per-Ingress annotation                |

The entryPoint approach wins because it's right-by-default. The trade-off (one extra config file to read alongside `values.yaml` to understand what runs on a request) is acceptable given the doc trail this spec leaves.

### Why this specific header list

Three forward-auth conventions exist in the wild:

| Family                      | Origin                             | Headers                                                                                                                                             |
| --------------------------- | ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `X-Forwarded-*` auth subset | oauth2-proxy, traefik-forward-auth | `X-Forwarded-User`, `-Email`, `-Preferred-Username`, `-Groups`, `-Name`, `-Roles`                                                                   |
| `Remote-*`                  | Authelia, nginx `auth_request`     | `Remote-User`, `-Email`, `-Groups`, `-Name`                                                                                                         |
| `X-Authentik-*`             | Authentik outpost (proxy provider) | `X-Authentik-Username`, `-Groups`, `-Email`, `-Name`, `-Uid`, `-Jwt`, `-Meta-Jwks`, `-Meta-Outpost`, `-Meta-Provider`, `-Meta-App`, `-Meta-Version` |

A downstream service that trusts _any_ of these would be vulnerable. Stripping all three families covers the whole convention space at zero cost — legitimate browsers don't send these inbound.

Transport headers (`X-Forwarded-For`, `-Proto`, `-Host`, `-Port`, `X-Real-IP`) and standard auth material (`Authorization`, cookies) are explicitly NOT stripped — they are either Traefik's own additions or legitimate client material.

### Forward-auth future-proofing

If Authentik forward-auth is added later (Authentik outpost in `proxy` mode behind Traefik), its outpost middleware injects `X-Authentik-*` headers on the **outbound** leg (after auth). The strip-middleware runs on the **inbound** leg (before backend hand-off).

Middleware order on the route: `strip-auth-headers → forward-auth → backend`. Strip clears whatever the client sent; forward-auth then writes the authenticated values. No collision because they run on different legs.

When forward-auth lands, the route for that service will list both middlewares in order. The entryPoint default still applies first; route-level middlewares chain after entryPoint defaults in Traefik.

## File changes

| File                                                | Action | Purpose                                                                               |
| --------------------------------------------------- | ------ | ------------------------------------------------------------------------------------- |
| `system/traefik-strip-auth-headers-middleware.yaml` | New    | The Middleware CRD                                                                    |
| `system/traefik-helmchartconfig.yaml`               | Modify | Add `ports.websecure.middlewares` reference                                           |
| `ansible/provision-gandalf.yml`                     | Modify | One new `copy` task to ship the Middleware to k3s's auto-apply manifest dir           |
| `audits/tier-1-authentik.md`                        | Modify | Flip finding 6-ii row from "Open" to "Resolved"; drop entry #1 from "Open follow-ups" |

### `system/traefik-strip-auth-headers-middleware.yaml` (new)

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

### `system/traefik-helmchartconfig.yaml` (modify)

Extend `valuesContent` with the entryPoint default-middleware reference. The exact key in the Traefik chart values is `ports.<entrypoint>.middlewares` — list of middleware references in the form `<namespace>-<name>@kubernetescrd`.

Before:

```yaml
spec:
  valuesContent: |-
    service:
      spec:
        externalTrafficPolicy: Local
```

After:

```yaml
spec:
  valuesContent: |-
    service:
      spec:
        externalTrafficPolicy: Local
    ports:
      websecure:
        middlewares:
          - kube-system-strip-auth-headers@kubernetescrd
```

**Implementation note (chart-values fallback):** if the k3s-bundled Traefik chart doesn't render `ports.websecure.middlewares` into the static config (older chart versions may not), fall back to `additionalArguments`:

```yaml
additionalArguments:
  - --entryPoints.websecure.http.middlewares=kube-system-strip-auth-headers@kubernetescrd
```

Verify on first apply via:

```
kubectl -n kube-system get pod -l app.kubernetes.io/name=traefik -o jsonpath='{.items[0].spec.containers[0].args}'
```

and adjust if the `entryPoints.websecure.http.middlewares` arg is absent.

### `ansible/provision-gandalf.yml` (modify)

Add one task after the existing HelmChartConfig copy task (currently at line ~113):

```yaml
- name: Copy strip-auth-headers Middleware to k3s manifests dir
  copy:
    src: ../system/traefik-strip-auth-headers-middleware.yaml
    dest: /var/lib/rancher/k3s/server/manifests/strip-auth-headers-middleware.yaml
    owner: root
    group: root
    mode: "0600"
```

k3s's manifests-dir controller picks up any YAML dropped there, not just HelmCharts, and applies them via its embedded kubectl. No separate `kubectl apply` step.

### `audits/tier-1-authentik.md` (modify)

Flip finding 6-ii row in the table under "Check 6 — network exposure" from `**Open**` to `**Resolved**` with the new PR ref. Also strike entry #1 (header-stripping middleware) from the "Open follow-ups" section, leaving the email-based recovery flow as the sole remaining open item.

(PR number filled in at merge time; the audit doc is updated in the same commit.)

## Verification

Local pre-merge:

1. `yamllint .` clean (matches CI).
2. Spec doc reviewed and approved.

Live post-apply (from laptop, against gandalf):

1. **Middleware loaded:**

   ```
   kubectl -n kube-system get middleware strip-auth-headers -o yaml
   ```

   Expect: spec matches the manifest.

2. **EntryPoint default attachment:**

   ```
   kubectl -n kube-system get pod -l app.kubernetes.io/name=traefik \
     -o jsonpath='{.items[0].spec.containers[0].args}' | tr ',' '\n' | grep -i middleware
   ```

   Expect: an `--entryPoints.websecure.http.middlewares=...` argument referencing `kube-system-strip-auth-headers@kubernetescrd`. If absent, the chart values didn't render — fall back to `additionalArguments`.

3. **Header-injection block**, from a tailnet host (gandalf or laptop):

   ```
   curl -sk -H "X-Forwarded-User: attacker" \
            -H "X-Authentik-Username: pwn" \
            -H "Remote-User: bypass" \
            https://authentik.vigihome.net/api/v3/core/users/me/ \
            -b "session=<valid>" -o /tmp/resp.json
   ```

   Then in Authentik admin → Events, inspect the request log entry. The injected headers should not appear. (Authentik logs the request after Traefik strip, so they should be absent.)

   Alternative if Authentik doesn't surface raw inbound headers: temporarily deploy `kennethreitz/httpbin` behind `httpbin.vigihome.net`, hit `/headers`, verify the strip-list headers are absent in the response.

4. **Legitimate flow regression test:** sign in to authentik.vigihome.net, jellyfin.vigihome.net, coder.vigihome.net via OIDC. All should work normally.

5. **PR #53 (real-client-IP) non-regression:** in Authentik admin → Events, recent login events should show real client IPs (LAN `192.168.50.x` or tailnet `100.x.x.x`), not `10.42.0.1` and not the gandalf node IP.

## Rollout

Single PR containing all four file changes. Atomic — the Middleware CRD and the entryPoint reference land together; no window where the entryPoint references a non-existent middleware.

Apply procedure (post-merge, from laptop):

```
cd ~/git/nickvigilante/homelab
git pull
ansible-playbook -i ansible/inventory.yml ansible/provision-gandalf.yml
```

## Rollback

`git revert` the PR + re-run Ansible. k3s's manifests-dir controller removes the Middleware CRD (the YAML disappears from the dir) and re-renders Traefik without the default middleware reference. ~30 seconds end-to-end.

If rollback is needed mid-incident (the middleware is breaking something), the emergency stop-gap is `kubectl -n kube-system delete middleware strip-auth-headers`. Traefik logs a warning about the entryPoint referencing a missing middleware but passes requests through unchanged (missing-middleware is no-op, not request-drop). The full rollback then is `git revert` + `ansible-playbook`.

## Open implementation questions

1. **Chart values key name** — `ports.websecure.middlewares` vs `additionalArguments` fallback. Resolve at first apply by inspecting rendered Traefik pod args; iterate if needed. Not a design-blocker.
2. **Audit doc update bundling** — the audit doc fix PR (#61) is in flight. This branch was cut from main _before_ #61 merged, so the audit doc change here will conflict-or-rebase against the issue-ref fix. Plan: rebase this branch onto main after #61 merges, then update finding 6-ii on top. Mechanical, not a design issue.
