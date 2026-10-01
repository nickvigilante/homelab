# Pi-hole

Network-wide DNS and ad-blocking for the LAN and tailnet,
installed with the `mojo2600/pihole` Helm chart (not yet under Flux).
Binds host port 53 on gandalf; admin UI at `https://pi-hole.vigihome.net`.

| File                            | Purpose                                                                     |
| ------------------------------- | --------------------------------------------------------------------------- |
| `values.yaml`                   | Helm values: image pin, hostPort 53, Ingress, upstream, the Unbound sidecar |
| `pv-pvc.yaml`                   | hostPath PV and PVC for `/opt/pihole/etc-pihole` (`pihole.toml`, gravity)   |
| `middleware-root-redirect.yaml` | Traefik Middleware bouncing `/` to `/admin/`                                |
| `unbound-configmap.yaml`        | Config for the recursive Unbound sidecar                                    |

Local records (the `*.vigihome.net` wildcard, `dns.hosts`) live in `pihole.toml` on the PV;
see the DNS comment at the bottom of `values.yaml`.

## Upstream resolution: recursive Unbound

Pi-hole's only upstream is an Unbound sidecar in the same pod, on `127.0.0.1#5335`.
Unbound resolves recursively: it walks the root, TLD, and authoritative servers itself,
so no single third-party resolver sees the household's full query stream.
It also validates DNSSEC locally
and uses QNAME minimisation, so each server only sees the part of the name it's responsible for.

Before this, upstreams were `1.1.1.1` and `9.9.9.9` over plain port 53:
Cloudflare and Quad9 saw every query and the ISP could read them in transit.

**Trade-off to know about:** recursion removes the central resolver,
but queries to authoritative servers are still plaintext port 53,
so the ISP can still observe them.
Unbound's DNS-over-TLS and DNS-over-HTTPS support is for *clients talking to Unbound*,
and for forwarding to a resolver that speaks DoT;
authoritative servers don't generally offer encrypted transport.
If hiding queries from the ISP ever matters more than avoiding a central resolver,
switch Unbound to forwarding mode with DoT
(`forward-zone` with `forward-tls-upstream: yes` to e.g. `9.9.9.9@853#dns.quad9.net`),
accepting that the chosen resolver then sees everything.

**Source of truth for upstreams is `values.yaml`, not the admin UI.**
The chart renders `DNS1`/`DNS2` into `FTLCONF_dns_upstreams`,
and Pi-hole v6 env vars override `pihole.toml` and lock the field read-only in the UI.
Don't add a public resolver alongside Unbound:
FTL spreads load across upstreams and favours the fastest,
so most queries would bypass Unbound.

**DNSSEC:** Unbound validates.
Pi-hole's own `dns.dnssec` is turned off via `FTLCONF_dns_dnssec: "false"` in `values.yaml`
(locked in the UI), since validating twice is redundant and only adds query-log noise.
If the upstream ever moves back to a public resolver, turn it back on.

**No backup needed.** Unbound keeps no persistent state:
the cache is disposable,
and the root hints and trust anchor ship in the image and are re-seeded on restart.

## Applying changes

```sh
cd ~/git/nickvigilante/homelab
kubectl apply -f k8s/pihole/unbound-configmap.yaml
helm upgrade pihole mojo2600/pihole --namespace networking \
  --version 2.38.0 -f k8s/pihole/values.yaml
```

Unbound reads its config only at start, and the ConfigMap is mounted via `subPath`,
so a ConfigMap-only change needs
`kubectl -n networking rollout restart deployment/pihole`.

### First cutover to Unbound: pre-flight

Some ISPs transparently intercept outbound port 53, which breaks recursion.
From gandalf, before the upgrade:

```sh
dig +short @198.41.0.4 com. NS     # a.root-servers.net; expect the gtld servers
dig +short whoami.akamai.net       # compare with the next line
dig +short whoami.akamai.net @ns1-1.akamaitech.net
```

The first must answer.
The second pair (through the normal resolver vs. direct to Akamai)
should both return gandalf's public IP; a mismatch on the direct query suggests interception.

Pi-hole is a single replica and the whole house resolves through it,
so do the cutover when a brief outage is acceptable,
and keep a device with a hard-coded fallback resolver handy.

### Verify

```sh
POD=$(kubectl -n networking get pod -l app=pihole -o name)
kubectl -n networking get $POD -o jsonpath='{.status.containerStatuses[*].ready}'; echo
kubectl -n networking logs $POD -c unbound --tail=20

dig @192.168.50.135 example.com                   # resolves
dig @192.168.50.135 dnssec-failed.org             # SERVFAIL: DNSSEC validation working
dig @192.168.50.135 +short jellyfin.vigihome.net  # still the local wildcard answer
dig @192.168.50.135 +short txt o-o.myaddr.l.google.com  # your public IP, not a Cloudflare/Quad9 egress IP
```

The admin UI's Settings → DNS page should show the upstream as `127.0.0.1#5335` and locked.

### Rollback

Set `DNS1: "1.1.1.1"` and `DNS2: "9.9.9.9"` in `values.yaml`
(the sidecar can stay; nothing will query it) and re-run the `helm upgrade` above.
