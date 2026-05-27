# coredns

Cluster-wide CoreDNS customization for k3s. The k3s helm-controller
owns the main CoreDNS install; this directory only contributes the
`coredns-custom` ConfigMap, which k3s's Corefile imports at load time
via `import /etc/coredns/custom/*.server`.

## What's in here

- `coredns-custom.yaml` — adds a `home:53` stub zone that forwards
  `*.home` queries to Pi-hole's in-cluster Service. Lets pods
  resolve LAN names like `authentik.home`, `coder.home`,
  `jellyfin.home` etc. without per-pod `hostAliases` workarounds.

## Apply

```bash
kubectl apply -f coredns-custom.yaml
kubectl -n kube-system rollout restart deployment coredns
```

The rollout is belt-and-suspenders — CoreDNS has the `reload` plugin
in the main Corefile so new imported files _should_ pick up within
~30s — but forcing a fresh pod removes ambiguity.

## Verify

```bash
kubectl run dns-test --rm -i --restart=Never --image=busybox -- \
  nslookup authentik.home
```

Expect `192.168.50.135` (gandalf's LAN IP) as the answer. If you get
`server can't find authentik.home: NXDOMAIN`, the import didn't
take — check the CoreDNS logs:

```bash
kubectl -n kube-system logs deployment/coredns | tail -50
```

## Things that depend on this

When this stub zone is in place, downstream services don't need their
own `hostAliases` entries to reach `*.home` hostnames from inside the
cluster. The Coder Helm values still keep `hostAliases` for
`authentik.home` — that's redundant now but left in place to avoid
coupling DNS to a single point of failure. Future integrations
(Jellyfin OIDC, Pi-hole forward-auth) can skip the `hostAliases`
pattern entirely.

## Failure mode

If Pi-hole goes down, this stub zone forwards into a black hole and
pod-originated `*.home` lookups will time out — but Pi-hole being
down already breaks the LAN's DNS in general, so this isn't a new
SPOF, just a deeper visibility of the existing one. Services with
fallback configs (e.g. Coder's lingering `hostAliases`) still
resolve.
