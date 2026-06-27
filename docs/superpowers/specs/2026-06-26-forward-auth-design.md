# Authentik Forward-Auth Primitive — Design

**Issue:** #137.
Cross-refs #117, #134 (the dead `externalUrl` symptom), #155 (Grafana stays a link tile, unaffected).

## Goal

Stand up a reusable edge-authentication primitive so services that lack
their own login can be exposed at `*.vigihome.net` gated by Authentik SSO,
with authentication happening before traffic reaches the app.
Prove it end-to-end by putting the Prometheus and Alertmanager admin UIs
behind it and giving Alertmanager a real, reachable `externalUrl`.

## Why now

There is no forward-auth infrastructure today.
That is the direct reason the Prometheus and Alertmanager admin UIs have
**no Ingress** — their APIs are unauthenticated, so exposing them would
expose an open admin surface.
The knock-on effect is that Alertmanager's `externalUrl` is pinned at
`http://localhost:9093` (a port-forward placeholder),
so alert-notification emails carry links that resolve nowhere.
Forward-auth removes the blocker for both UIs and becomes a reusable
primitive for any future no-auth service.

## Decisions

These were settled during brainstorming; they shape everything below.

- **Domain-level, not per-application.**
  One proxy provider for the whole `*.vigihome.net` domain, one embedded
  outpost, one shared middleware pattern.
  Authorization is coarse — "is a member of `homelab-users`" — applied
  domain-wide.
  Chosen for maximum reusability: gating a new service becomes a single
  Ingress annotation, not a new provider and route each time.
- **Embedded outpost, not a standalone outpost Deployment.**
  `authentik-server` already runs an embedded outpost; the proxy provider
  is assigned to it, so there is no new workload to deploy or monitor.
- **Gate on the existing `homelab-users` group.**
  The single homelab SSO group, already used by the Coder and Outline
  application blueprints.
- **`allowCrossNamespace` stays OFF.**
  The forward-auth Middleware is copied per consuming namespace rather than
  shared from one namespace via Traefik's `allowCrossNamespace` flag.
  Keeping the flag off avoids globally widening Traefik's cross-namespace
  trust model to serve a security primitive, and matches the existing
  same-namespace Middleware precedent (`k8s/pihole/middleware-root-redirect.yaml`).
  The cost — a ~15-line Middleware copy per namespace — is bounded
  (per-namespace, not per-service) and the copies are identical.
- **First consumers only: Prometheus and Alertmanager.**
  Build the primitive and wire exactly these two UIs.
  Retrofitting other admin UIs is deferred (YAGNI) until each is touched
  for its own reasons.

## Architecture

### Authentik side (config-as-code, defined once)

A **proxy provider** in domain-forward mode
(`authentik_providers_proxy.proxyprovider`, mode `forward_domain`),
with the SSO cookie scoped to `vigihome.net` so one login covers every
`*.vigihome.net` host.
The provider is assigned to the **embedded outpost**
(`authentik_outposts.outpost`, the pre-existing "authentik Embedded Outpost").
An **Application** (`authentik_core.application`) binds the provider and a
**policy binding** gates it on the `homelab-users` group.

All of this lands as a new blueprint file alongside the existing ones,
reconciled by the Authentik worker from the `authentik-blueprints`
ConfigMap — the same mechanism as `blueprints/applications/coder.yaml`.
The exact provider/outpost attribute names are pinned during the
implementation-plan phase against the running Authentik version
(2026.2.x); this design fixes the topology, not the field spelling.

### Edge side (Traefik)

A Traefik **`forwardAuth` Middleware** whose `address` is the embedded
outpost's Traefik endpoint:

```
http://authentik-server.auth.svc.cluster.local:9000/outpost.goauthentik.io/auth/traefik
```

with `trustForwardHeader: true` and an `authResponseHeaders` list covering
the full `X-authentik-*` identity-header set, so Traefik copies the
identity Authentik asserts onto the upstream request.

The middleware is **opt-in per route** via the
`traefik.ingress.kubernetes.io/router.middlewares` annotation — it is
deliberately *not* an entrypoint default, so only annotated Ingresses are
gated.

### The two-route handshake topology

Domain-level forward-auth still requires **two routes per protected host**:

1. **App route** — `Host(<svc>.vigihome.net) && PathPrefix(/)` → the app
   Service, carrying the `forwardAuth` middleware annotation.
   Lives in the app's own namespace.
2. **Outpost route** — `Host(<svc>.vigihome.net) && PathPrefix(/outpost.goauthentik.io/)`
   → `authentik-server:9000`, **un-gated** (no middleware — the login
   handshake must not gate itself).
   Lives in the `auth` namespace, co-located with `authentik-server`.

Traefik watches Ingresses across all namespaces and aggregates them into
routers by host and path, so these two routes compose into one host's
routing table even though they live in different namespaces.
Traefik's path-length priority makes `/outpost.goauthentik.io/` win over
`/` with no manual priority needed.
Co-locating the outpost route in `auth` is what lets `allowCrossNamespace`
stay off: the outpost Ingress points at a Service in its own namespace, and
the middleware reference is same-namespace within each app namespace.

### Interaction with the existing `strip-auth-headers` middleware

The websecure entrypoint already carries a default `strip-auth-headers`
middleware (`system/traefik-strip-auth-headers-middleware.yaml`) that blanks
all inbound `X-authentik-*` / `X-Forwarded-User` headers, closing
Tier-1 audit finding 6-ii (clients cannot spoof identity headers).

This **complements** forward-auth rather than conflicting with it.
Traefik runs entrypoint-default middlewares *before* router middlewares, so
the chain is: `strip-auth-headers` (blanks any client-supplied identity
headers) → then `forwardAuth` (re-injects them authoritatively from the
outpost response via `authResponseHeaders`).
The upstream therefore sees only Authentik-asserted identity.
For the first two consumers this is moot — Prometheus and Alertmanager do
not read identity headers; for them forward-auth is purely a pass-or-302
gate — but the ordering is correct for any future header-trusting consumer.

## First consumers: Prometheus and Alertmanager

Both Ingresses are enabled in the kube-prometheus-stack `values.yaml`
(`prometheus.ingress` and `alertmanager.ingress`), applied via a pinned
`helm upgrade` (kps is direct-helm-managed, not a HelmRelease):

- Hosts `prometheus.vigihome.net` and `alertmanager.vigihome.net`,
  `websecure` entrypoint, `secretName: vigihome-tls`.
  The `monitoring` namespace already carries the reflected `vigihome-tls`
  (Grafana's Ingress lives there), and Pi-hole's wildcard
  `address=/vigihome.net/...` already resolves any new `*.vigihome.net`
  host — so no per-host DNS or TLS plumbing is required.
- Each Ingress annotation references the `monitoring`-namespace copy of the
  `forwardAuth` middleware (`monitoring-forward-auth@kubernetescrd`).
- The matching outpost routes for both hosts are added to the
  `auth`-namespace outpost Ingress.
- `externalUrl` set to the real `https://prometheus.vigihome.net` and
  `https://alertmanager.vigihome.net`, replacing the `localhost:9093`
  placeholder — so alert-notification email links finally resolve.

## Files

New:

- `k8s/authentik/blueprints/applications/forward-auth.yaml` — proxy
  provider, embedded-outpost assignment, application, `homelab-users`
  policy binding.
- `k8s/authentik/ingress-forward-auth-outpost.yaml` — un-gated outpost-path
  Ingress; new protected hosts get a rule added here as they onboard.
- `k8s/kube-prometheus-stack/middleware-forward-auth.yaml` — the
  `monitoring`-namespace `forwardAuth` Middleware copy.

Modified:

- `k8s/kube-prometheus-stack/values.yaml` — enable the two Ingresses, set
  both `externalUrl`s.
- `k8s/authentik/blueprints/README.md` — document the new blueprint.
- `k8s/kube-prometheus-stack/README.md` — document the forward-auth
  exposure, the SPOF posture, and the port-forward fallback.
- `.github/workflows/lint.yml` — the kubeconform filter already covers
  `middleware-*.yaml` and `ingress-*.yaml`; confirm the new filenames match
  (`middleware-forward-auth.yaml` does; `ingress-forward-auth-outpost.yaml`
  matches `ingress-*.yaml`). No filter change expected.

## SPOF and fallback

Forward-auth makes Authentik a hard dependency for reaching these two UIs
through the browser.
This is consistent with the lab's SPOF discipline.
Because Prometheus and Alertmanager have **no native login of their own**,
the fallback is not a Bitwarden credential but **`kubectl port-forward`**,
which bypasses Traefik (and therefore Authentik) entirely:

```
kubectl -n monitoring port-forward svc/<prometheus|alertmanager> <port>
```

This is documented in the kps README so the recovery path is discoverable
when Authentik is down.
No new local-fallback credential is created because there is no native auth
surface to attach one to.

## Testing / acceptance

1. **Unauthenticated** request to `https://prometheus.vigihome.net` →
   302 redirect to the Authentik login.
2. **Member** of `homelab-users` completes login → Prometheus UI loads;
   same for Alertmanager.
3. **Non-member** authenticated user → 403 from Authentik (denied by the
   policy binding).
4. **Fallback** — `kubectl port-forward` to each Service still reaches the
   UI with Traefik/Authentik out of the path.
5. **`externalUrl` fix** — trigger or inspect an alert notification; the
   email's source link points at `https://alertmanager.vigihome.net` and
   resolves to the gated UI.
6. **Anti-spoof intact** — a request carrying a forged `X-authentik-username`
   header does not bypass the gate or reach the upstream with the forged
   value (strip-then-forwardAuth ordering).

## Out of scope

- Retrofitting any admin UI other than Prometheus and Alertmanager.
- Per-application authorization (finer than `homelab-users`).
- Enabling `allowCrossNamespace` or any change to the Ansible-owned Traefik
  chart configuration.
- Grafana — it keeps its native OIDC login and its Homepage **link tile**
  (#155); it is not a forward-auth consumer.
