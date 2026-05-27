# reflector

`emberstack/reflector` — Kubernetes controller that mirrors annotated
Secrets / ConfigMaps across namespaces. Installed so the wildcard TLS
cert issued by cert-manager in the `cert-manager` namespace
(`vigihome-tls`) can be consumed by Ingresses in every other namespace
without per-namespace Certificate resources or hand-copying.

## Prereqs

- cert-manager installed (PR #23) + `vigihome-tls` Certificate issued
  (PR #24). Without those, this controller has nothing useful to do.
- `helm` on gandalf.
- emberstack repo registered:
  ```sh
  helm repo add emberstack https://emberstack.github.io/helm-charts
  helm repo update
  ```

## One-time install

1. **Apply the namespace:**

   ```sh
   kubectl apply -f namespace.yaml
   ```

2. **Confirm the chart version**, then install. (Check
   `helm search repo emberstack/reflector` — version pin below was
   current at install time.)

   ```sh
   helm install reflector emberstack/reflector \
     --namespace reflector \
     --version 10.0.42 \
     -f values.yaml
   ```

3. **Wait for the controller pod to be Ready:**

   ```sh
   kubectl -n reflector get pod -w
   # reflector-xxxx   1/1   Running
   ```

## Annotating a Secret to be mirrored

Reflector reads annotations on the *source* Secret. Two annotations
do the heavy lifting:

```yaml
metadata:
  annotations:
    # Mark this Secret as eligible for mirroring.
    reflector.v1.k8s.emberstack.com/reflection-allowed: "true"
    # Comma-separated list (or regex) of namespaces that should
    # receive a mirror, and a toggle to actually do the mirror.
    reflector.v1.k8s.emberstack.com/reflection-auto-enabled: "true"
    reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: "auth,coder,syncthing"
```

When applied, reflector copies the Secret into each listed namespace
under the same name. On every rotation of the source Secret, the
mirrors update automatically — no Ingress restart needed (most
ingress controllers, including Traefik, watch the referenced TLS
Secret).

### Driving this from cert-manager

For TLS Secrets specifically, set the annotations on the *Certificate
resource* via `secretTemplate` — cert-manager will copy them to the
issued Secret on every reconcile. Per-service PRs in this repo add a
namespace to the auto-namespaces list via this mechanism:

```yaml
# k8s/cert-manager/certificate.yaml
spec:
  secretTemplate:
    annotations:
      reflector.v1.k8s.emberstack.com/reflection-allowed: "true"
      reflector.v1.k8s.emberstack.com/reflection-auto-enabled: "true"
      reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: "homepage,auth"
```

## Day-to-day ops

- **Add a namespace to the mirror list:** edit
  `k8s/cert-manager/certificate.yaml`'s
  `reflection-auto-namespaces` annotation, `kubectl apply`. Reflector
  picks up the change within seconds; the new mirror appears in the
  target namespace.

- **Upgrade reflector:** bump the version pin above, then
  `helm upgrade reflector emberstack/reflector -n reflector \ --version XX.YY.ZZ -f values.yaml`. Controller pod restarts; mirrors
  are not affected (state lives on the source Secret's annotations).

- **Uninstall:** `helm uninstall reflector -n reflector` then
  `kubectl delete -f namespace.yaml`. **Mirrors are not auto-cleaned**
  — they become orphaned Secrets in their target namespaces. Either
  delete them manually, or first remove the
  `reflection-auto-namespaces` from the source Secret, wait for the
  mirrors to be removed by reflector, then uninstall.

## Pitfalls

- **Don't mirror to `kube-system` or `kube-public`.** Reflector
  technically can, but it's a footgun for upgrades and not needed.
  Keep the auto-namespaces list explicit and minimal.

- **Wildcard / regex namespace patterns.** Reflector supports regex
  in `reflection-auto-namespaces` (e.g. `"app-.*"`). We use explicit
  comma-separated lists in this repo because the namespace set is
  small and explicit is easier to grep. Don't switch to regex without
  a concrete reason.

- **Secret type must be `kubernetes.io/tls` for Ingress consumption.**
  Reflector preserves the source Secret's `type`, so as long as
  cert-manager issues a TLS Secret (it does), the mirrors are
  Ingress-consumable.

- **Stale mirrors after a source Secret's annotations change.** If
  you remove a namespace from `reflection-auto-namespaces`, reflector
  removes its mirror in that namespace. If you remove the
  `reflection-allowed: "true"` annotation entirely, reflector removes
  *all* mirrors. This is correct behavior but can surprise on
  refactors — be deliberate.
