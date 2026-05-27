# Homelab HA architecture

**Date:** 2026-05-20
**Status:** Design approved — Phases A, B, C in detail; D and E deferred to future specs

## Problem

The homelab today is gandalf-bound by every measurable axis:

- **Control plane:** gandalf is the only k3s server. If gandalf dies, the API server, scheduler, and controller-manager all die with it. Even pods running on frodo/samwise become unscheduleable and unable to recover.
- **Storage:** every stateful service uses a hostPath PV anchored to gandalf (Authentik postgres, Coder, Jellyfin media, Pi-hole config + gravity DB, Syncthing). Losing gandalf's disk loses that data outright; losing gandalf the node makes the services unreachable until a new node mounts the same paths.
- **Ingress:** Traefik runs `hostNetwork: true` pinned to gandalf (per PR #70). DNS for `*.vigihome.net` points at gandalf's IP. Loss of gandalf = no ingress.
- **DNS:** Pi-hole runs as a single pod on gandalf. Loss of gandalf = no LAN DNS resolution for `*.vigihome.net` and no upstream filtering.

The user goal: **any single node can die without taking the homelab down.** That includes gandalf.

## Goal

Reach a state where gandalf's loss (or any single node's loss) is tolerable — workloads survive, ingress survives, DNS survives, stateful data survives — by adding redundancy in deliberate phases that each leave the system in a working, reviewable state.

## Non-goals

- **Multi-node simultaneous failure tolerance.** The HA design targets _single_ node loss. Surviving two-of-five-down requires more replicas, larger etcd quorums, and is not part of this design.
- **Zero-downtime migration paths.** Each phase has a defined downtime window. Brief cluster restarts during k3s reinstallation, ingress IP cutover, etc. are acceptable on a homelab.
- **External-facing exposure.** vigihome.net stays internal-only (LAN + tailnet). HA does not change the public-exposure posture; that's a separate decision.
- **Migrating storage to enterprise-grade systems** (Ceph). Longhorn is the chosen storage layer for Phase E; Ceph is out of scope (see Phase E rationale).
- **Backup architecture changes.** Restic → Storj remains the primary backup chain. Longhorn's built-in S3 backup may or may not be wired up later, separate from this design.

## Hardware reality

The hardware on hand caps several Phase E decisions and shapes the overall sequencing:

| Node    | Role today                | Storage                  | Notes                                                          |
| ------- | ------------------------- | ------------------------ | -------------------------------------------------------------- |
| gandalf | k3s server                | SSD/NVMe, ample capacity | Hosts every stateful PV via hostPath. ThinkCentre form factor. |
| frodo   | k3s agent (Pi 5)          | microSD                  | Supports NVMe HAT, **not yet purchased** (waiting for a sale). |
| samwise | k3s agent (Pi 4B)         | microSD (likely 512GB)   | No SSD/NVMe option on the PoE+ HAT models in use.              |
| merry   | not yet in cluster (Pi 4) | 32GB microSD             | Smallest storage; microSD wear concerns under Longhorn.        |
| pippin  | not yet in cluster (Pi 4) | 32GB microSD             | Same as merry.                                                 |

**Implications driving the roadmap:**

1. **Longhorn-on-microSD is a footgun.** Constant metadata writes burn out cards within months — especially the 32GB ones. Longhorn replicas need at least SSD-backed storage to be operationally sound.
2. **Pi 4 PoE+ HATs don't expose an NVMe slot.** Adding fast storage to a Pi 4 means USB 3.0 SSD — a future hardware purchase.
3. **gandalf is the only node with adequate storage for Longhorn replicas today.** A single-replica Longhorn defeats the HA purpose (gandalf still SPOF).

→ **Defer Longhorn to Phase E**, after the NVMe lands on frodo (and ideally USB SSDs on samwise/merry/pippin). Everything else in the roadmap can proceed without it.

## Phased roadmap

| Phase | Title                                            | Depends on                                    | Hardware          | Effort          | Downtime                           |
| ----- | ------------------------------------------------ | --------------------------------------------- | ----------------- | --------------- | ---------------------------------- |
| **A** | k3s control-plane HA (3 servers)                 | Nothing                                       | None              | ~3 hrs          | ~1–2 hrs cluster down              |
| **B** | Unpin stateless services from gandalf            | A                                             | None              | ~30 min         | Per-service helm-upgrade restarts  |
| **C** | MetalLB activation; ingress fails over           | A                                             | None              | ~1 hr           | ~5 min ingress flap during cutover |
| **D** | DNS HA                                           | A (probably C too)                            | None              | TBD             | TBD                                |
| **E** | Longhorn deployment + stateful service migration | A, B, C; NVMe on frodo; ideally SSDs on Pi 4s | Hardware purchase | TBD per service | Variable                           |

Phases A–C are scoped in detail below. Phases D and E get their own future specs.

## Phase A — k3s control-plane HA

### Design

Convert from single-server (k3s + SQLite backend, on gandalf only) to three-server embedded etcd quorum across gandalf, frodo, samwise. Etcd survives 1 failure (quorum 2/3); cluster API stays available regardless of which one dies. merry and pippin remain k3s **agents** (workers), not servers — small-storage Pi 4Bs shouldn't host etcd.

| Node    | Role after Phase A | Reason                                                                       |
| ------- | ------------------ | ---------------------------------------------------------------------------- |
| gandalf | server             | Workhorse; control plane stays where most workloads live (for now)           |
| frodo   | server             | Pi 5 — most capable Pi; expected long-term cluster lifespan                  |
| samwise | server             | Pi 4B — already in cluster, more proven than the freshly-imaged merry/pippin |
| merry   | agent              | Fresh Pi 4; agent role keeps the etcd quorum on more-tested nodes            |
| pippin  | agent              | Same as merry                                                                |

### Migration approach

CLAUDE.md already notes that "k3s itself is not under IaC" and "a from-scratch rebuild means re-running curl … | sh - and re-importing all PVs. Accepted trade-off for now." Phase A leans into that — rebuild the cluster cleanly rather than attempting an in-place SQLite → embedded-etcd conversion (which k3s doesn't formally support).

Outline (full procedure goes into the Phase A implementation plan, not this spec):

1. **Pre-flight:** verify restic backup is fresh (Storj snapshot of last ~24h). Export all Secrets from Bitwarden into laptop's session shell so they're ready to re-create via `kubectl create secret`. Confirm all `values.yaml` files committed and pulled to gandalf.
2. **Tear down current cluster** on gandalf: `/usr/local/bin/k3s-uninstall.sh`. This stops k3s and removes its state, but leaves `/opt/<service>/` hostPath PV data intact on gandalf's disk (k3s uninstall doesn't touch user data dirs).
3. **Re-install k3s on gandalf with `--cluster-init`** plus `--tls-san <gandalf-tailnet-IP>` so the cert covers cross-tailnet API access.
4. **Capture node-join token:** `cat /var/lib/rancher/k3s/server/node-token`.
5. **Install k3s on frodo + samwise as servers** with `--server https://gandalf:6443 --token <node-token>`. Both join the etcd quorum.
6. **Verify etcd health:** `kubectl get nodes` shows all 3 servers Ready; `k3s etcd-snapshot ls` lists snapshots.
7. **Install k3s on merry + pippin as agents** (`K3S_URL=https://gandalf:6443 K3S_TOKEN=…` — agent role, not server).
8. **Re-apply manifests:** Ansible drops the `system/*.yaml` files into `/var/lib/rancher/k3s/server/manifests/` (HelmChartConfig override, strip-auth-headers Middleware, sysctl drop-ins). k3s's HelmChart controller picks them up and renders.
9. **Re-create k8s Secrets** for each service via `kubectl create secret generic` sourced from Bitwarden (per each service's README — Authentik, Coder, restic, smtp-relay, etc.).
10. **`helm install`** each chart-managed service from its `k8s/<service>/values.yaml`. Apply raw manifests for Syncthing and any non-chart services.
11. **Verify each service end-to-end:** OIDC sign-ins, ingress paths, Authentik audit log shows real client IPs (regression-check for the post-Phase-C ingress changes).

### Risks and mitigations

- **A Secret is forgotten during re-creation.** Mitigated by maintaining a checklist of every k8s Secret (sourced from each service README) and running through it explicitly. Detection: services fail to start with `Error: secret not found` events.
- **PV hostPath ownership shifts.** k3s reinstall might re-run any init containers that chown the hostPath. If ownership differs, bitnami postgres might CrashLoopBackOff (see Authentik's volumePermissions init container pattern in k8s/authentik/values.yaml). Mitigation: stat the relevant dirs before/after; chown back if needed.
- **TLS cert (`vigihome-tls`) loss.** cert-manager will re-issue from the existing ClusterIssuer once it's running again. Brief gap of ~5 min during which TLS handshakes fail.
- **OIDC tokens become stale.** Existing tokens reference the old API issuer. Each downstream service may need a brief re-login. Mitigation: clear browser sessions, re-authenticate as needed.

### Acceptance criteria

- `kubectl get nodes` shows 3 server nodes and 2 agent nodes, all `Ready`.
- Killing gandalf (powering off) leaves frodo+samwise quorum intact; `kubectl` keeps working via either of those two as the API target (tailnet allows it).
- All services from Tier-1 audit return to a working state post-rebuild.

## Phase B — unpin stateless services from gandalf

### Design

Audit each Deployment / StatefulSet for `nodeSelector: kubernetes.io/hostname: gandalf` pins. For services whose state is either:

- **Truly absent** (config baked into `values.yaml`, no PV), or
- **Cache-only** (rebuildable on restart), or
- **Stored elsewhere** (in a Service-backed external store, e.g., a remote DB)

…remove the pin. The scheduler is then free to place the pod on any node with capacity.

Expected scope (to be finalized during execution, but a starting list):

| Service        | Pin today?                      | Phase B action                                                           | Why                                                     |
| -------------- | ------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------- |
| Authentik      | Yes (postgres hostPath)         | Keep pinned                                                              | Stateful; needs Longhorn (Phase E)                      |
| Coder          | Yes (state hostPath)            | Keep pinned                                                              | Stateful; workspaces are ephemeral but auth state isn't |
| Jellyfin       | Yes (media hostPath)            | Keep pinned                                                              | Stateful media library                                  |
| Pi-hole        | Yes (gravity + config hostPath) | Keep pinned                                                              | Stateful until Phase D                                  |
| Syncthing      | Yes (data hostPath)             | Keep pinned                                                              | Stateful                                                |
| homepage       | Likely yes                      | **Unpin** if no PV — config is in `values.yaml` and is rebuildable       |
| restic CronJob | Yes                             | Keep pinned                                                              | Needs hostPath access to backup-source dirs             |
| cert-manager   | No                              | n/a                                                                      | Already unpinned                                        |
| reflector      | No                              | n/a                                                                      | Already unpinned                                        |
| Traefik        | Yes via hostNetwork (PR #70)    | Defer to Phase C — Traefik gets unpinned when MetalLB takes over ingress |

### Acceptance criteria

- Every service whose unpinning is in-scope has its `nodeSelector` removed in its `values.yaml`, helm-upgraded, and verified scheduling somewhere other than gandalf at least once (via `kubectl delete pod` and watching where it lands).
- No regressions in any service's user-facing behavior.

## Phase C — MetalLB activation

### Design

Replace k3s's bundled klipper-lb (svcLB) with MetalLB. Goal: a LAN-routable virtual IP that floats between nodes via L2 ARP advertisement, so any node up in Phase A's quorum can serve as ingress.

| Component                        | Today                                              | After Phase C                                                                                                     |
| -------------------------------- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| LoadBalancer implementation      | klipper-lb (DaemonSet of svclb pods on every node) | MetalLB controller + speaker DaemonSet                                                                            |
| Traefik Service type             | ClusterIP (per PR #70)                             | LoadBalancer with annotation `metallb.io/loadBalancerIPs: <virtual-IP>`                                           |
| Traefik scheduling               | hostNetwork: true, pinned to gandalf               | Pod-network, no nodeSelector — schedules anywhere                                                                 |
| Traefik replicas                 | 1                                                  | 2+ with pod anti-affinity (so different nodes); MetalLB IP fails over to whichever node has a running Traefik pod |
| DNS records for `*.vigihome.net` | `192.168.50.135` (gandalf)                         | `<MetalLB-virtual-IP>`                                                                                            |

### Migration steps (full plan separate)

1. **Disable klipper-lb** in k3s install args (`--disable=servicelb`). Brief k3s server restart on each of the 3 server nodes (rolling). Existing svclb pods get cleaned up.
2. **Install MetalLB** via helm chart in `metallb-system` namespace.
3. **Configure `IPAddressPool`** with one reserved LAN address. Recommended: pick an IP in the LAN's static range outside DHCP (e.g., `192.168.50.250/32`). Confirmed unused via `arping`.
4. **Configure `L2Advertisement`** to advertise the pool via ARP. No BGP needed for a homelab LAN.
5. **Update Traefik HelmChartConfig:**
   - Remove `hostNetwork: true`
   - Remove `nodeSelector` (gandalf pin)
   - Change `service.type` back from `ClusterIP` to `LoadBalancer`
   - Remove the `ports.web/websecure` override (chart defaults of 8000/8443 are fine again — klipper-lb is gone but MetalLB handles the external port mapping)
   - Add the MetalLB IP annotation
   - Bump `deployment.replicas` to 2 with pod anti-affinity across nodes
6. **Update Pi-hole DNS** to point `*.vigihome.net` at the new MetalLB IP. Use a short TTL (60s) just before the cutover to limit cache pollution.
7. **Test:** external client hits the new IP → MetalLB speaker on whichever node is the elected leader → routes to a Traefik pod → backend.
8. **Failover test:** `kubectl drain <node-with-active-MetalLB-leader>` — MetalLB should re-elect a new leader on a different node within ARP cache TTL (~30s).

### Risks and mitigations

- **The MetalLB IP collides with an existing LAN device.** Pre-check via `arping`. Reserve it on the router/DHCP if there's any chance of conflict.
- **Pi-hole DNS doesn't honor the TTL change because clients cache aggressively.** Mitigation: bump TTL down 24h before cutover. Worst case: a few minutes of "old DNS resolves to gandalf, new DNS resolves to MetalLB" — both work because gandalf is still in the cluster and Traefik will serve the request via cluster-internal routing.
- **The new replicas need PV access.** Traefik is stateless (configs from k8s API; no PV needed), so adding replicas just works. No PV concerns.

### Acceptance criteria

- `kubectl get svc traefik -n kube-system` shows `LoadBalancer` with the MetalLB-allocated IP.
- External LAN client curl to `*.vigihome.net` lands on Traefik via the MetalLB IP.
- Killing the node currently advertising the MetalLB IP (`kubectl drain` or hard power-off) results in another node taking over within ~30s, with no manual intervention.
- Source-IP preservation (audit finding 6-i) is non-regressed: external clients' `X-Forwarded-For` still shows real LAN IPs.

## Phase D — DNS HA (deferred)

Spec'd separately when chosen. The two candidate architectures and their trade-offs are noted under the earlier brainstorm; no commitment yet. Pi-hole remains a SPOF on gandalf in the interim. Acceptable because (a) LAN clients fall back to upstream DNS for non-`*.vigihome.net` lookups, and (b) the gandalf-down failure window is the same as it was pre-HA work for DNS, so this phase is _additive_ survival, not a regression risk.

## Phase E — Longhorn deployment + stateful service migration (deferred, hardware-gated)

Future spec; sketched here for trajectory. The gating triggers:

1. **NVMe HAT + NVMe drive on frodo** — Pi 5 with PCIe Gen 3 NVMe gives Longhorn a fast, durable replica node alongside gandalf.
2. **(Optional but recommended)** USB 3.0 SSDs on samwise / merry / pippin — extends replication factor to 3 with adequate write performance and removes microSD-wear concerns.

When those exist, Phase E covers:

- Deploy Longhorn (helm chart, `longhorn-system` namespace) with replica nodes labeled appropriately so only SSD-backed nodes host replicas.
- Migrate each stateful service from hostPath PV to a Longhorn PVC, one at a time, with downtime windows. Order proposed: lowest-stakes first (e.g., homepage state, Uptime Kuma) → Syncthing → Pi-hole → Coder → Jellyfin → Authentik (highest stakes; postgres-backed; needs the most care).
- Each migration: snapshot the hostPath, create the Longhorn PVC, restore the data, point the service at the new PVC, helm-upgrade, verify, delete the hostPath PV.

Longhorn's S3 backup integration may complement (or partially replace) the restic chain — to be decided in the Phase E spec.

## Failure tolerance matrix

| Snapshot in time | gandalf dies                                                                 | frodo dies                                       | samwise dies       | merry dies               | pippin dies |
| ---------------- | ---------------------------------------------------------------------------- | ------------------------------------------------ | ------------------ | ------------------------ | ----------- |
| Today            | 🔴 Cluster down                                                              | 🟡 Workers reschedule (none to spill onto today) | 🟡 Same as frodo   | n/a (not in cluster yet) | n/a         |
| After A          | 🟡 Quorum survives (2/3); stateful services unreachable until gandalf back   | 🟢 Quorum survives                               | 🟢 Quorum survives | 🟢 Worker                | 🟢 Worker   |
| After B          | 🟡 Same as A, plus stateless services reschedule onto Pis                    | 🟢                                               | 🟢                 | 🟢                       | 🟢          |
| After C          | 🟡 Same, plus ingress fails over to surviving Traefik replica                | 🟢                                               | 🟢                 | 🟢                       | 🟢          |
| After D          | 🟡 Same, plus DNS survives                                                   | 🟢                                               | 🟢                 | 🟢                       | 🟢          |
| After E          | 🟢 **Stateful survives** — Longhorn replica on frodo takes over reads/writes | 🟢                                               | 🟢                 | 🟢                       | 🟢          |

🔴 = cluster-wide outage. 🟡 = degraded (some services unavailable). 🟢 = transparent failover.

## Sequencing constraints

- **A must precede B, C, D, E.** No HA without multi-server quorum first.
- **B can run in parallel with C.** Independent concerns.
- **C requires the cluster to be in a stable post-A state**, but doesn't strictly need B done.
- **D is independent of B/C/E** in design, but DNS cutover is easier if Phase C (MetalLB) is done first because the MetalLB-allocated IP simplifies "where does DNS point."
- **E depends on hardware purchase** and ideally A+B+C+D done so the storage migration isn't bundled with other moving parts.

## Backup strategy (unchanged)

- Restic CronJob → Storj (per `k8s/backup/backup-cronjob.yaml`) continues unchanged through Phases A–D.
- Phase A's cluster rebuild relies on a fresh restic snapshot as the recovery anchor.
- Phase E may integrate Longhorn's native S3 backup (to a different Storj bucket) as a complementary chain. Decided in the Phase E spec.

## Open implementation questions

For each phase's implementation plan to settle, not the architecture spec:

1. **Phase A — k3s install args specifics.** Whether to pin a specific k3s channel/version (`INSTALL_K3S_VERSION=...`) for reproducibility, vs. tracking the stable channel. Affects upgrade cadence.
2. **Phase A — etcd snapshot retention.** k3s defaults; whether to point snapshots at Storj for off-cluster persistence.
3. **Phase B — exact list of services to unpin.** Inventory pass during execution; this spec lists likely candidates but doesn't commit.
4. **Phase C — MetalLB IP selection.** Specific LAN address; confirmed unused via `arping`.
5. **Phase C — Traefik replica count and anti-affinity rule.** 2 is the floor; whether to go higher.
6. **Phase E — Longhorn replica placement strategy.** Affinity rules for "only on SSD-backed nodes." Specific Longhorn settings (replica count, soft/hard anti-affinity).

## Out of scope (deliberately not done)

- **Multi-region / off-site replication.** Backup chain handles disaster recovery; HA is about node-level not site-level redundancy.
- **External etcd cluster.** k3s embedded etcd is sufficient at this scale.
- **GitOps controller (Argo CD, Flux).** Per CLAUDE.md, manual apply is intentional. HA doesn't change that.
- **Cloud bursting / hybrid cluster.** Strictly on-prem.
- **Replacing k3s with full Kubernetes.** k3s is the right choice for this scale.

## Next steps after spec approval

This spec is the architecture document. Implementation work happens via per-phase plans:

- `docs/superpowers/plans/<date>-phase-a-control-plane-ha.md` — written separately when Phase A is queued for execution.
- Similarly for Phases B, C. Phases D and E get their own full specs when their preconditions land.
