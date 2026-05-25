# Tier-1 pentest — Authentik

A focused audit of the Authentik deployment (`auth` namespace) and the
parts of the cluster that touch it. Tier-1 was deliberately scoped to
Authentik only; the full-stack pass is tracked separately as
**Tier-2** (#59).

| | |
|---|---|
| **Scope** | Authentik server + worker + bundled postgres; Authentik's external exposure via Traefik; Authentik's role as OIDC issuer for Coder + Home Assistant |
| **Out of scope** | Pi-hole admin, Jellyfin, Uptime Kuma, Coder workspaces, Syncthing, Storj/restic crypto, host-level (gandalf) OS hardening |
| **Started** | 2026-05-18 |
| **Concluded** | 2026-05-19 |
| **Method** | Checklist-driven walkthrough of Authentik's surface area (admin/bootstrap, sessions/MFA, container hardening, OIDC bindings, self-service flows, network exposure) with each finding tracked to either a PR or a deliberate accept-the-risk decision |

The actionable findings shipped as PRs #52, #53, #54, #55, #56, #57.
Remaining follow-ups are tracked as GitHub issues; see "Open follow-ups" at the bottom.

## Findings

### Check 1 — admin & bootstrap

- **akadmin password recoverability** — at audit time the only path to
  rotate or recover akadmin was via the postgres DB or the bootstrap
  env vars; email-based recovery silently no-op'd because Authentik
  had no SMTP relay wired. Resolved by **PR #56** (Forward Email
  SMTP) — the recovery flow itself is now tracked as #62 (see
  "Open follow-ups").
- **Bootstrap token disposition** — `AUTHENTIK_BOOTSTRAP_TOKEN` lives
  in Bitwarden and remains a static admin token. Tracked separately
  as **#60** (move static tokens out of Bitwarden onto a KeePass
  database on a USB stick) — Bitwarden-hosted static admin tokens are
  a low-grade SPOF that doesn't belong on the same blast radius as
  the daily-use credential store.

### Check 2 — sessions, MFA, password policies

- No findings worth a PR. The default flows were already configured
  with reasonable session limits and the chart's defaults for cookie
  hardening (Secure, HttpOnly, SameSite) are in effect because of the
  HTTPS cutover (PR #43).
- Reputation policy semantics double-checked here: `passing=true`
  signals **bad** reputation in Authentik's policy framework (not
  good), so a reputation gate must bind to a Deny stage with
  `negate=OFF`. Captured in memory; not a code change.

### Check 3 — container & secret hardening

| Finding | Status | Reference |
|---|---|---|
| 3a — no Pod Security Standards label on `auth` namespace | **Resolved** | PR #54 sets `enforce=baseline, audit=restricted, warn=restricted`; deliberately not `enforce=restricted` because the bitnami postgres chart's `volumePermissions` init container needs uid 0 |
| 3b — no NetworkPolicy on `authentik-server` | **Resolved** | PR #54 adds `netpol-authentik-server.yaml`: ingress to port 9000 allowed only from Traefik + same-namespace pods; closes lateral path from a compromised Jellyfin / Coder workspace |
| `AUTHENTIK_SECRET_KEY` exposure | **Accepted** | Already documented — single Bitwarden item, restic-backed postgres, namespace-scoped Secret. No reasonable hardening short of an external KMS, out of scope for a single-node homelab |

### Check 4 — OIDC providers, applications, property mappings

| Sub-check | Finding | Status | Reference |
|---|---|---|---|
| 4a — Home Assistant OIDC user UX | Blank Username + "no HA credentials" warning for OIDC-only users | **Not a bug** | HA design quirk — verified by confirming OIDC sign-in actually worked. User subsequently abandoned the HA-Authentik integration on unrelated grounds; Authentik-side artifacts cleaned up via API. Memory: `feedback_ha_oidc_user_ux` |
| 4b — OIDC signing-cert rotation | Annual rotation was a manual click-through per provider; no tooling | **Resolved** | PR #55 ships `scripts/rotate-authentik-signing-cert.sh` (auto-discovers providers, generates ECDSA P-256, PATCHes signing_key, restarts server, verifies JWKS) plus `scripts/cleanup-old-authentik-signing-certs.sh`. CI gains `shellcheck` |
| 4c — property mappings | Default `email` scope mapping returns `email_verified: False` because no SMTP/verification flow exists; breaks strict OIDC clients | **Resolved (UI)** | Mapping edited in Authentik UI to return `email_verified: True` unconditionally. Documented in `k8s/authentik/README.md` since the mapping lives in postgres, not the repo. Re-apply on from-scratch rebuild |
| 4d — group bindings on Coder + HA applications | Applications were exposed to *all* authenticated users; no group-scoped allow rule | **Resolved (UI)** | Per-application policy bindings added against the `homelab-users` group. Applied directly to the cluster; README still pending the explicit "how to add a new group binding" note (low priority; pattern is the same as 4c) |

### Check 5 — self-service flows

- Authentik ships no recovery flow at all, and even if one existed it
  assumes outbound email, which at audit time didn't exist. Tracked as
  finding 5-i (email-based recovery). **Resolved** by PR #56 (SMTP
  wiring) + PR #106 (recovery flow built from scratch, group gate,
  akadmin runbook). Email-based self-recovery is enabled for
  `homelab-users` (akadmin out of scope per blast-radius decision). The
  gate is a single Expression policy checking `pending_user`'s group
  membership, bound to every post-identification stage (a flow-level
  binding denies everyone since recovery users are anonymous until
  identification; gating only the email stage lets the skip cascade
  into the password prompt — both verified). Account enumeration is
  blocked via "Pretend user exists". **No rate-limit or reputation
  throttle** — deliberately deferred (see "Things deliberately not
  done"). See `k8s/authentik/README.md` for the full wiring and
  `docs/superpowers/specs/2026-05-20-authentik-recovery-flow-design.md`
  for the design rationale.

### Check 6 — network exposure

| Sub-finding | Status | Reference |
|---|---|---|
| 6-i — Authentik recorded `10.42.0.1` (CNI bridge) as the source IP of every request, breaking IP reputation and audit log forensics | **Resolved** | Originally attempted via PR #52 (`externalTrafficPolicy: Local`) + PR #53 (`AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS`), but k3s's bundled klipper-lb (v0.4.x) always MASQUERADEs at the svclb pod's POSTROUTING, so real client IPs never reached Traefik. **PR #70** switches Traefik to `hostNetwork: true` on gandalf with `service.type: ClusterIP` and a host sysctl (`net.ipv4.ip_unprivileged_port_start = 0`) so Traefik binds 80/443 directly as the chart-default uid 65532, bypassing klipper-lb entirely. Verified end-to-end via httpbin: an external LAN client's IP reaches Traefik unchanged. Same scope benefits Pi-hole, Jellyfin, Coder, Uptime Kuma audit logs |
| 6-ii — Traefik passes through arbitrary upstream headers (e.g. `X-Forwarded-User`) that a misbehaving downstream could trust | **Resolved** | Two-PR fix: **PR #64** adds the strip-auth-headers Middleware (clears `X-Forwarded-User`, `Remote-User`, and `X-Authentik-*` families) and wires it as a default on the `websecure` entryPoint; **PR #65** switches that wire from the chart's `ports.<name>.middlewares` values key (silently ignored by the k3s-bundled Traefik 3.6.13) to `additionalArguments`, which is what actually renders into the Traefik pod's CLI args |
| 6-iii — Traefik DaemonSet placement on multi-node cluster could cause `externalTrafficPolicy: Local` to drop traffic on nodes without a Traefik pod | **Resolved** | **PR #70** switches Traefik to `hostNetwork: true` with `nodeSelector: gandalf` and `service.type: ClusterIP` — the LoadBalancer + `externalTrafficPolicy: Local` configuration this concern was about no longer exists; placement is now explicit. Future multi-node ingress is tracked separately as #69 (MetalLB) |

## Cross-cutting work that came out of the audit

These items aren't audit checks per se but landed because the audit
surfaced the underlying need:

- **PR #55 — cert rotation tooling.** Made it cheap enough to rotate
  the OIDC signing cert annually that there's no excuse not to.
- **PR #56 — Forward Email SMTP relay.** Audit-driven (Check 1 +
  Check 5) and also unblocked unrelated alerting work.
- **PR #57 — restic backup-failure email alerts.** Not an Authentik
  finding, but the SMTP wiring from #56 made it trivial to add a
  second out-of-band failure channel for the nightly backup job.

## Open follow-ups

Tracked as GitHub issues in this repo, not described inline. Items
currently open:

_None — all Tier-1 audit findings resolved as of 2026-05-22._

## Things deliberately not done

- **No `enforce: restricted` PSA on `auth`.** Would require either
  disabling bitnami's `volumePermissions` init container or
  switching postgres charts. Accepted as a known gap; `audit/warn`
  surfaces drift toward the tighter standard without enforcing it.
- **No external KMS for `AUTHENTIK_SECRET_KEY`.** Out of scope for
  a single-node homelab; postgres + the key + the restic password
  + Bitwarden export discipline together form the recovery path.
- **No automated penetration tooling** (ZAP, etc.). The Tier-1 pass
  was checklist-driven, not fuzzing-driven. If Tier-2 (#59) wants
  fuzzing, it'll add that explicitly.
- **No rate-limit / reputation throttle on the recovery flow.**
  Authentik 2026.2 has no rate-limit policy primitive, and a reputation
  deny was judged not worth the wiring for a single-member group: the
  group gate already blocks non-members, "Pretend user exists" blocks
  enumeration, and the only residual abuse vector is spamming the one
  member's inbox with reset emails — which cannot compromise the
  account (the reset still requires mailbox access). Revisit before
  `homelab-users` grows or recovery is opened more broadly.

## Pointer to Tier-2

Tier-2 is the full-stack version of this audit — same approach
applied to every cluster service plus host-level gandalf. Tracked in
issue **#59**. When it kicks off, write `audits/tier-2-full-stack.md`
alongside this file using the same shape.
