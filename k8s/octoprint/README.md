# octoprint

Wires an **external OctoPrint** instance (the 3D printer's controller, at
`192.168.50.118:5000`, NOT a k3s workload) into the vigihome TLS/DNS stack
so it's reachable at `https://octoprint.vigihome.net` on LAN + tailnet.
Same pattern as `k8s/home-assistant/`.

## Layout

- `namespace.yaml` — the `octoprint` namespace.
- `ingress-vigihome.yaml` — `Service` (no selector) + manual `EndpointSlice`
  (→ `192.168.50.118:5000`) + `Ingress` at `octoprint.vigihome.net`
  (websecure, reflected `vigihome-tls`).

## Auth

OctoPrint has no OIDC, so it is **not** behind Authentik — its native login
(Access Control) stays on, and access is gated by being LAN/tailnet-only per
the vigihome private-only rule. Keep an OctoPrint admin credential in
Bitwarden as the fallback, like the other native-auth services.

## Backup

OctoPrint is self-managed and off-cluster; its config + SD card are **not**
in the restic backup. Back it up via OctoPrint's own backup feature if you
care about its settings/printer profiles.

## One-time setup

1. **Generate an OctoPrint API key** (Settings → Application Keys, or a user
   API key) and store it in Bitwarden item `Homelab OctoPrint`, field
   `api-key`. The Homepage widget needs it (see `../homepage/README.md`).

2. **Reflect the TLS cert in.** Add `octoprint` to
   `reflection-auto-namespaces` on `../cert-manager/certificate.yaml` (done
   in this change), then apply so reflector mirrors `vigihome-tls`:
   ```sh
   kubectl apply -f ../cert-manager/certificate.yaml
   ```

3. **Apply the namespace + proxy resources:**
   ```sh
   kubectl apply -f namespace.yaml -f ingress-vigihome.yaml
   ```

4. **Verify the cert reflected + the site serves:**
   ```sh
   kubectl -n octoprint get secret vigihome-tls          # appears within ~5s
   curl -sv https://octoprint.vigihome.net 2>&1 | grep -E "issuer|HTTP/"
   ```
   Expect a Let's Encrypt cert and the OctoPrint login.

5. **Wire the Homepage widget** — see `../homepage/README.md`
   ("Service widgets & secrets").

## OctoPrint-side reverse-proxy config

If OctoPrint shows a "reverse proxy configured incorrectly" banner, or builds
`http://` URLs / mis-redirects behind the Ingress, set `server.reverseProxy`
in its `config.yaml` to trust the forwarded headers (Traefik sends
`X-Forwarded-Proto: https` and `X-Forwarded-Host`). This is the OctoPrint
analogue of Home Assistant's `trusted_proxies`.

## If the printer host moves

Update the `EndpointSlice` address in `ingress-vigihome.yaml` and re-apply.
