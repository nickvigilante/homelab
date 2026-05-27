# home-assistant

Home Assistant runs on **HAOS** on a separate Raspberry Pi at
`192.168.50.42` — it's not a k3s workload. This dir wires HAOS
into the cluster's vigihome.net TLS/DNS ecosystem so it's reachable
at `https://home-assistant.vigihome.net` from LAN + tailnet.

## Two URLs, by audience

- **`https://<account>.ui.nabu.casa` — public, default Nabu Casa Remote URL.** Stays in place; partner + partner's mom use this from devices that aren't on the tailnet. Installing Tailscale on every family device isn't realistic; Nabu Casa Remote is the right path for non-operator household users. (Subscription stays — also covers Alexa/Google/IFTTT integrations.)
- **`https://home-assistant.vigihome.net` — private, this PR's work.** Resolves only via Pi-hole (LAN + tailnet). For the operator, who has Tailscale on their devices. Optional Authentik SSO can be layered on top later (see "Planned follow-ups" below).

The two URLs front the **same HA instance** at .42. No data fork; sessions are independent.

## Why HAOS, not containerized

Decided 2026-05-17. HA has:

1. A **Zigbee USB dongle** plugged into the Pi. USB passthrough into
   a k8s Pod is painful (USB device plugins, careful node affinity)
   and would hard-pin HA to whichever node holds the dongle — turning
   HA into a SPOF on that node.
2. Three HAOS **add-ons** in use: VS Code Web, Terminal, Matter
   Server. None of these are available in HA Container; replacing
   them is a noticeable workflow cost.

Containerizing buys nothing operationally and loses both of the
above. HAOS stays.

## Architecture

```
home-assistant.vigihome.net
        │
        │  (DNS: Pi-hole dnsmasq_lines wildcard
        │         *.vigihome.net → gandalf)
        ▼
┌─────────────────────────────┐
│  gandalf (k3s control       │
│  plane + Traefik)           │
│                             │
│  Traefik :443               │
│   └── terminates TLS        │
│       (vigihome-tls cert)   │
│   └── routes by Host header │
│       to ClusterIP Service  │
│       'home-assistant'      │
└─────────────────────────────┘
        │
        │  k8s Service (no selector) +
        │  manual EndpointSlice
        ▼
┌─────────────────────────────┐
│  HAOS on Pi at .42:8123     │
│   (plain HTTP, no TLS)      │
└─────────────────────────────┘
```

Traefik terminates the browser-trusted TLS; the in-cluster hop to
HAOS is plain HTTP. HA never sees TLS — and doesn't need to.

## Setup

### 1. Apply the namespace + Ingress

The `home-assistant` namespace must be in the reflector's
`reflection-auto-namespaces` list on
`k8s/cert-manager/certificate.yaml` before the Ingress works — that's
what mirrors the `vigihome-tls` Secret into this namespace. That edit
ships in the same PR as this dir.

```bash
kubectl apply -f namespace.yaml
kubectl apply -f ingress-vigihome.yaml
```

Verify the cert mirrored in:

```bash
kubectl -n home-assistant get secret vigihome-tls
```

Should show up within ~5s of the namespace being created.

### 2. Tell HA about the proxy

In HAOS, open the **File Editor** add-on (or SSH in) and edit
`/config/configuration.yaml`. Add (or extend) the `http:` block:

```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 192.168.50.0/24   # LAN — covers all k3s node IPs
```

Then **Developer Tools → YAML → Check Configuration**, then
**Developer Tools → Restart → Restart Home Assistant**.

### Why a /24 instead of pod CIDR + a specific node

Traefik proxies the request out of a pod on whichever k3s node
the scheduler placed it on, and Linux NATs pod-outbound through
the node's IP. HA at .42 sees the source IP as the *node IP*
(192.168.50.11 / .12 / .135 / any future worker), not the pod
IP. A `/24` on the LAN covers all current and future cluster
nodes without enumerating them. The pod CIDR (10.42.0.0/16)
wouldn't help — pod IPs never appear on this hop.

If you skip this step entirely, recent HA versions reject the
proxied request with `400 Bad Request: Bad Host header` because
the `X-Forwarded-Host` header arrived from an untrusted source.
The browser shows `400` from `Python/3.14 aiohttp/...` — that's
HA itself, the request has reached it correctly, it's just refusing.

### 3. Verify

Browser → `https://home-assistant.vigihome.net` — should load HA's
login page with a trusted cert from `O=Let's Encrypt, CN=E7`.

Sign in. The HA UI's live-updating Lovelace cards / energy graphs
use WebSocket; Traefik handles the upgrade transparently.

### 4. (Optional) Install the Tailscale HAOS add-on

If you want HA reachable on the tailnet (e.g., from cellular without
nabu.casa Remote), HAOS has an official **Tailscale** add-on. Install
it from **Settings → Add-ons → Add-on Store → Search "Tailscale"**.
Auth with `tailscale up --hostname=home-assistant --advertise-tags=tag:homelab`
when prompted (mint a one-shot key in the Tailscale admin UI first).

Once HA is on the tailnet, `home-assistant.vigihome.net` works
identically over Tailscale because Pi-hole's wildcard already
returns gandalf's tailnet IP as the second answer.

## Day-to-day

The Pi 5 / Pi 4 hosting HAOS isn't managed by anything in this repo
— HAOS auto-updates itself and the add-ons. The cluster-side proxy
config (this dir) only changes if HA's IP changes (DHCP reservation
keeps it pinned to `.42`) or if HA's listen port changes from `8123`.

If you move HA to different hardware (HA Green/Yellow purchase,
e.g.), update the `addresses[].ip` in `ingress-vigihome.yaml`'s
Endpoints, `kubectl apply`, and the cutover is complete.

## Planned follow-ups (not in this PR)

- **Authentik SSO for the operator URL.** Optional layer on the
  `home-assistant.vigihome.net` path only — nabu.casa stays native
  for the household. Approach: install HACS (Home Assistant Community
  Store), install the Authentik integration from there, configure
  HA's `auth_providers` to add an OIDC provider pointing at
  Authentik. **Critical SPOF caveat:** HA controls real-world things
  (lights, climate, alarms). When Authentik is down, you can't sign
  in via OIDC, which is fine *only if HA's native auth is preserved
  as a fallback*. The operator's native HA account stays in
  Bitwarden; OIDC is additive, not a replacement. Standard SPOF
  discipline per the repo's CLAUDE.md.

## What we deliberately don't do here

- **No backup wiring in this repo.** HAOS handles its own snapshot/
  backup workflow (Settings → Backups). The HAOS Google Drive /
  OneDrive / Storj backup add-ons are the right path; the restic
  CronJob in this repo is for cluster-side data only.
- **No nabu.casa removal.** Subscription stays — covers Alexa /
  Google Assistant / IFTTT integrations *and* serves the household
  members who don't have Tailscale. Dropping nabu.casa is a separate
  decision that would force every family member to install Tailscale.
- **No HA-side `configuration.yaml` enforcement.** The
  `use_x_forwarded_for` + `trusted_proxies` block has to be edited
  manually in HAOS (File Editor add-on or SSH). Not part of this
  repo's apply pattern.
