# cert-manager

Issues wildcard TLS for `*.vigihome.net` (+ apex) via Let's Encrypt
DNS-01 against Cloudflare. Foundation for moving every internal
service from plain HTTP at `*.home` to HTTPS at `*.vigihome.net`,
which in turn unlocks WebAuthn / passkey enrollment in Authentik.

This PR (A) installs cert-manager + two ClusterIssuers (staging + prod).
The actual wildcard Certificate resource lands in a follow-up PR (B)
once the DNS-01 flow is validated end-to-end against staging.

## Why staging *and* prod

Let's Encrypt prod has a 5-duplicate-certs-per-week rate limit for an
exact name set; misconfigured DNS-01 (wrong token scope, wrong zone,
missing nameserver flag) burns through it fast. Staging has effectively
no rate limit and is byte-for-byte identical except for the signing
CA. Iterate on staging, then flip `issuerRef.name` on the Certificate
to `letsencrypt-prod`.

## Prereqs

- Cloudflare API token for `vigihome.net` (Zone.DNS Edit + Zone.Zone
  Read) stored in Bitwarden item
  `Cloudflare DNS API Token — vigihome.net`.
- `helm` and `bw` CLIs on gandalf.
- jetstack repo registered with helm:
  ```sh
  helm repo add jetstack https://charts.jetstack.io
  helm repo update
  ```

## One-time install

1. **Apply the namespace:**

   ```sh
   kubectl apply -f namespace.yaml
   ```

2. **Confirm the chart version**, then install. (Check
   `helm search repo jetstack/cert-manager` for the latest; the pinned
   version below was current at install time.)

   ```sh
   helm install cert-manager jetstack/cert-manager \
     --namespace cert-manager \
     --version v1.20.2 \
     -f values.yaml
   ```

3. **Wait for all three Deployments to be Ready:**

   ```sh
   kubectl -n cert-manager get deploy -w
   # cert-manager, cert-manager-webhook, cert-manager-cainjector
   ```

4. **Create the Cloudflare API token Secret from Bitwarden.** The
   value lives only in Bitwarden; the cluster gets a copy via
   `kubectl create secret`. Piping `bw get` into `--from-file=/dev/stdin`
   keeps the token out of shell history and process args (which
   `--from-literal` would expose).

   ```sh
   export BW_SESSION=$(bw unlock --raw)
   bw get password 'Cloudflare DNS API Token — vigihome.net' \
     | kubectl create secret generic cloudflare-api-token \
         -n cert-manager --from-file=api-token=/dev/stdin
   bw lock
   unset BW_SESSION
   ```

   If the Bitwarden item is a Secure Note rather than a Login (token
   in the notes body), substitute `bw get notes` for `bw get password`.

5. **Apply both ClusterIssuers:**

   ```sh
   kubectl apply -f clusterissuer-staging.yaml
   kubectl apply -f clusterissuer-prod.yaml
   ```

6. **Verify both register an ACME account with Let's Encrypt** (takes
   a few seconds each):

   ```sh
   kubectl get clusterissuer
   # NAME                  READY   AGE
   # letsencrypt-staging   True    1m
   # letsencrypt-prod      True    1m
   ```

   `kubectl describe clusterissuer letsencrypt-staging` should show
   `The ACME account was registered with the ACME server`. Same for
   prod.

## What success looks like (PR A)

```
$ kubectl get clusterissuer
NAME                  READY   AGE
letsencrypt-staging   True    1m
letsencrypt-prod      True    1m
```

## Issuing the wildcard certificate (`certificate.yaml`)

`certificate.yaml` asks cert-manager for a single wildcard cert
covering `vigihome.net` + `*.vigihome.net`, signed by
`letsencrypt-prod`. The file ships pointed at prod because the
DNS-01 flow has already been validated against staging during the
original PR (#24). For DR re-issuance, applying as-is goes straight
to prod and is the expected path.

1. **Apply:**

   ```sh
   kubectl apply -f certificate.yaml
   kubectl -n cert-manager get certificate vigihome-tls -w
   # Wait for READY=True (~30–90s for DNS-01 propagation + LE issuance).
   ```

2. **Inspect the cert chain** to confirm prod issuance:

   ```sh
   kubectl -n cert-manager get secret vigihome-tls \
     -o jsonpath='{.data.tls\.crt}' | base64 -d \
     | openssl x509 -noout -subject -issuer -dates -ext subjectAltName
   ```

   Expect:

   - `subject = CN = vigihome.net`
   - `issuer = ... O = Let's Encrypt, CN = E7` (or whatever LE's
     current ECDSA intermediate is — `E7` and `E5` for ECDSA, `R10`
     and `R11` for RSA)
   - `DNS:vigihome.net, DNS:*.vigihome.net` in the SAN extension.

### Validating a risky change against staging first

If you change something risky in `certificate.yaml` (new `dnsNames`,
algorithm swap, key size change) and want a dry-run against staging
to avoid burning LE prod's rate budget, edit `issuerRef.name` to
`letsencrypt-staging` locally, apply, verify, then flip back to
`letsencrypt-prod`. **Do not commit the staging value to `main`.**
cert-manager re-requests in-place when the issuer changes; the
Secret rotates without an Ingress restart.

## Day-to-day ops

- **Rotate the Cloudflare token:** rotate in the Cloudflare UI →
  update the Bitwarden item → overwrite the Secret:

  ```sh
  export BW_SESSION=$(bw unlock --raw)
  bw get password 'Cloudflare DNS API Token — vigihome.net' \
    | kubectl create secret generic cloudflare-api-token \
        -n cert-manager --from-file=api-token=/dev/stdin \
        --dry-run=client -o yaml | kubectl apply -f -
  bw lock
  unset BW_SESSION
  ```

  cert-manager re-reads the Secret on next reconcile; no restart needed.

- **Upgrade cert-manager:** bump the version pin above, then
  `helm upgrade cert-manager jetstack/cert-manager -n cert-manager --version vX.Y.Z -f values.yaml`. Cert renewals happen on their own
  schedule; force one with `cmctl renew <cert-name>` if needed.

- **Force a renewal manually** (e.g. after rotating the token to
  verify the new token works):

  ```sh
  kubectl annotate certificate <name> \
    cert-manager.io/issue-temporary-certificate=true --overwrite
  ```

- **Uninstall:** `helm uninstall cert-manager -n cert-manager`, then
  `kubectl delete -f namespace.yaml`. CRDs go with the chart because
  `crds.enabled: true`.

## Pitfalls

- **Don't apply `secret.example.yaml`.** It's a placeholder with
  `REPLACE_WITH_*` and applying it overwrites the real token with
  nonsense — every challenge breaks immediately. The file exists for
  documentation only.

- **DNS-01 resolver pinning is in `values.yaml`, not the issuer.** The
  `--dns01-recursive-nameservers` args pin cert-manager's propagation
  self-check to 1.1.1.1 + 8.8.8.8. Without them, the controller asks
  the in-cluster CoreDNS, which works but adds avoidable indirection.

- **Token scope mistakes are silent until the first challenge.** If
  the token lacks `Zone.Zone Read` on vigihome.net, cert-manager
  fails to find the zone and the ClusterIssuer goes Ready=False with
  a 4xx from Cloudflare. Fix by re-minting the token with the "Edit
  zone DNS" template, scoped to vigihome.net only, then overwriting
  the Secret per the rotation recipe above.

- **Account key Secrets are auto-created.** Don't pre-create
  `letsencrypt-staging-account-key` or `letsencrypt-prod-account-key`
  — cert-manager makes them on first reconcile. If they're missing
  unexpectedly, that's the sign the issuer hasn't reconciled yet, not
  a setup miss.

- **The user's email** in both ClusterIssuers is currently the LE
  registration contact. LE uses it for expiry warnings if renewals
  fail. Update both files if it changes.
