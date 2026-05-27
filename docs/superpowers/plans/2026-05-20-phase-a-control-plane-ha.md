# Phase A Implementation Plan — k3s control-plane HA

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the homelab cluster from single-server k3s (gandalf + SQLite backend) to a 3-server embedded etcd quorum (gandalf + frodo + samwise as servers; merry + pippin as agents) so the cluster API survives any single node loss.

**Architecture:** Cluster rebuild — there's no in-place migration path from k3s SQLite mode to embedded etcd. Uninstall k3s on gandalf, fresh-install with `--cluster-init`, join the other two servers, then add merry+pippin as agents. PV hostPath data on `/opt/<service>/` survives the rebuild (k3s uninstall doesn't touch user data dirs); Helm releases and k8s Secrets must be re-created.

**Tech Stack:** k3s (server + agent), Ansible (existing playbook for host config and manifest delivery), Helm (per-service install), Bitwarden CLI (Secret material), kubectl (verification).

**Spec reference:** `docs/superpowers/specs/2026-05-20-ha-architecture-design.md` Phase A.

**Estimated downtime:** ~1–2 hours total. Cluster API and ingress unavailable from cluster-teardown through "all services verified working."

---

## File Structure

This plan changes **no repo files**. It runs operations against the live cluster. The repo's existing structure (Helm `values.yaml` per service, `system/*.yaml` manifests applied by Ansible, READMEs documenting per-service Secret creation) is what gets reapplied during the rebuild.

Optional post-rebuild repo changes (out of scope for this plan, separate small PRs):

- Add k3s install arguments to `ansible/provision-gandalf.yml` (currently the k3s install is shell-history only — Phase A surfaces this gap).
- Add an `ansible/provision-pi.yml` server-mode variant if Phase A reveals that the current playbook only handles agent joins.

---

## Pre-flight checklist (run BEFORE teardown)

These steps gather material needed for recovery. Do not start teardown without all of them completed.

### Task 1: Verify a fresh restic snapshot exists

**Files:** none — verification only.

- [ ] **Step 1: Check the latest restic snapshot timestamp**

  ```bash
  ssh gandalf 'sudo restic -r s3:gateway.storjshare.io/<bucket-name> snapshots --last 5 \
    --password-file /root/.restic-password'
  ```

  Expected: a snapshot timestamp within the last 24 hours that includes the relevant `--tag` set (authentik, coder, jellyfin, pihole, syncthing — whatever your CronJob covers).

  If the latest snapshot is stale (>24h old) or missing tags, trigger a manual restic run before continuing:

  ```bash
  kubectl -n backup create job --from=cronjob/backup-snapshot manual-pre-phase-a
  kubectl -n backup logs -f job/manual-pre-phase-a
  ```

  Wait for it to complete cleanly. Confirm via Uptime Kuma's push monitor that the success ping fired.

- [ ] **Step 2: Note the snapshot ID**

  Capture the most-recent snapshot ID — you'll reference it in a rollback worst-case scenario:

  ```bash
  ssh gandalf 'sudo restic -r s3:... snapshots --last 1 --json --password-file /root/.restic-password | jq -r .[].short_id'
  ```

  Write this down somewhere outside the repo (Bitwarden secure note, etc.).

### Task 2: Inventory all k8s Secrets that need re-creation

**Files:** none — produces a checklist you maintain alongside the rebuild.

The cluster's authoritative secret list is whatever currently exists in the cluster, since Secrets aren't in the repo (gitleaks enforced).

- [ ] **Step 1: Enumerate every namespace's Secrets**

  ```bash
  kubectl get secrets --all-namespaces \
    -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,TYPE:.type,AGE:.metadata.creationTimestamp \
    --field-selector type!=kubernetes.io/service-account-token,type!=helm.sh/release.v1 \
    > /tmp/pre-phase-a-secrets.txt
  ```

  Open the file and remove auto-managed Secrets that the cluster recreates on its own:
  - `*-token-*` from default service accounts
  - cert-manager-issued `*-tls` Secrets (cert-manager will re-issue)
  - `sh.helm.release.v1.*` (helm tracking, recreated by `helm install`)
  - reflector-mirrored Secrets in consumer namespaces (reflector recreates from the source)

  What remains is the list of Secrets to manually re-create. Expected entries (cross-check against each service's README):

  | Namespace      | Secret                    | Source                                           |
  | -------------- | ------------------------- | ------------------------------------------------ |
  | auth           | authentik-secrets         | Bitwarden item "Homelab Authentik"               |
  | auth           | smtp-relay                | Bitwarden item "Homelab Forward Email SMTP"      |
  | backup         | restic-secrets            | Bitwarden item "Homelab Restic Repository"       |
  | backup         | uptime-kuma-push-urls     | Bitwarden item "Homelab Uptime Kuma Push URLs"   |
  | cert-manager   | cloudflare-api-token      | Bitwarden item "Homelab Cloudflare DNS-01 Token" |
  | coder          | coder-secrets             | Bitwarden item "Homelab Coder"                   |
  | home-assistant | (if any custom secrets)   | Bitwarden item "Homelab Home Assistant"          |
  | media          | jellyfin-secrets (if any) | Bitwarden item "Homelab Jellyfin"                |
  | networking     | pihole-secrets            | Bitwarden item "Pi-Hole"                         |
  | syncthing      | (if any custom secrets)   | Bitwarden item "Homelab Syncthing"               |

  Adjust the list to match your actual cluster. If a service shows a Secret not in the table, find the matching Bitwarden item and add it.

- [ ] **Step 2: Test Bitwarden CLI access**

  ```bash
  bw status
  bw list items --search "Homelab Authentik" | jq -r '.[].name'
  ```

  If `bw status` says `unlocked`, you're good. If `locked`, run `bw unlock` and export the session token:

  ```bash
  export BW_SESSION=$(bw unlock --raw)
  ```

  The session must stay unlocked through the full rebuild — keep this shell open.

### Task 3: Inventory all Helm releases

**Files:** none.

- [ ] **Step 1: List active Helm releases**

  ```bash
  helm list --all-namespaces > /tmp/pre-phase-a-helm.txt
  cat /tmp/pre-phase-a-helm.txt
  ```

  Expected entries (one per chart-managed service): authentik, coder, jellyfin, pihole, homepage, uptime-kuma, cert-manager, reflector, homeassistant.

  Capture chart version + namespace for each. You'll reinstall using the same versions to avoid version-upgrade surprises mid-rebuild.

- [ ] **Step 2: Identify raw-manifest services**

  Per the chart-vs-raw convention in CLAUDE.md, Syncthing is raw. Anything else?

  ```bash
  ls k8s/*/deployment.yaml
  ls k8s/*/ingress-vigihome.yaml
  ```

  Files in either list → that service is raw-managed and gets `kubectl apply` post-rebuild instead of `helm install`.

### Task 4: Confirm a maintenance window and notify stakeholders

**Files:** none.

- [ ] **Step 1: Pick a time when ingress can be down for ~2 hours.** No active OIDC sessions in use; no automated jobs scheduled to fire mid-window.
- [ ] **Step 2: Tell anyone who depends on the homelab.** Single-operator case: just self-acknowledgment that the window is reserved.

### Task 5: Verify SSH access to all 5 nodes

**Files:** none.

- [ ] **Step 1: SSH-check each node**

  ```bash
  for h in gandalf frodo samwise merry pippin; do
    echo "=== $h ==="
    ssh -o ConnectTimeout=5 $h 'hostname && uname -r' || echo "FAILED: $h"
  done
  ```

  Every node must respond. If merry or pippin still fail (per the open Pi triage), resolve that before starting Phase A — adding them as agents requires functional SSH.

### Task 6: Snapshot the current cluster state for reference

**Files:** none — write to `/tmp/`.

- [ ] **Step 1: Capture current node + pod state**

  ```bash
  kubectl get nodes -o wide > /tmp/pre-phase-a-nodes.txt
  kubectl get pods --all-namespaces -o wide > /tmp/pre-phase-a-pods.txt
  kubectl get pv > /tmp/pre-phase-a-pvs.txt
  kubectl get pvc --all-namespaces > /tmp/pre-phase-a-pvcs.txt
  kubectl get ingress --all-namespaces > /tmp/pre-phase-a-ingress.txt
  ```

  These are pre/post comparison anchors. Keep them around for the verification phase.

---

## Cluster teardown and rebuild

### Task 7: Uninstall k3s on gandalf

**Files:** none.

- [ ] **Step 1: Confirm what `k3s-uninstall.sh` does NOT touch**

  Per the k3s docs, `k3s-uninstall.sh` removes:
  - k3s binary, systemd service, state DB
  - `/etc/rancher/k3s`, `/var/lib/rancher/k3s`
  - Network interfaces (cni0, flannel.1)

  It does NOT remove:
  - `/opt/<service>/` hostPath PV data (Authentik postgres, Pi-hole config, Jellyfin media, etc.)
  - User-installed packages
  - Anything outside k3s-managed directories

  Verify the `/opt/` data exists before teardown so you can confirm it survives after:

  ```bash
  ssh gandalf 'sudo du -sh /opt/* 2>/dev/null | tee /tmp/pre-phase-a-opt-sizes.txt'
  ```

- [ ] **Step 2: Run the uninstall script**

  ```bash
  ssh gandalf 'sudo /usr/local/bin/k3s-uninstall.sh'
  ```

  Expected: script completes in <30s. `kubectl` from your laptop will start failing as soon as the API server stops.

- [ ] **Step 3: Verify /opt/ data is intact**

  ```bash
  ssh gandalf 'sudo du -sh /opt/* 2>/dev/null'
  ```

  Output should match `/tmp/pre-phase-a-opt-sizes.txt` (or be a near-identical match — the postgres pid file etc. might shift slightly).

### Task 8: Fresh-install k3s on gandalf with --cluster-init

**Files:** none on the repo; this changes only the live host.

- [ ] **Step 1: Install k3s in server mode with embedded etcd**

  ```bash
  ssh gandalf 'curl -sfL https://get.k3s.io | sh -s - server \
    --cluster-init \
    --disable=servicelb \
    --tls-san=192.168.50.135 \
    --tls-san=100.92.2.25 \
    --write-kubeconfig-mode=644'
  ```

  Notes:
  - `--cluster-init` initializes a new embedded etcd cluster (vs. joining an existing one).
  - `--disable=servicelb` — preempts Phase C work; klipper-lb is not needed because Traefik is `hostNetwork: true` (per PR #70). Leaving it disabled also simplifies the eventual MetalLB cutover.
  - `--tls-san` flags pre-register both the LAN IP and the tailnet IP on the cluster cert, so kubectl from either network works without cert errors.
  - `--write-kubeconfig-mode=644` lets non-root users read the kubeconfig on gandalf, matching the current setup.

  Wait for the install to complete (~1-3 minutes).

- [ ] **Step 2: Verify k3s server is up and healthy**

  ```bash
  ssh gandalf 'sudo systemctl status k3s'
  ssh gandalf 'sudo k3s kubectl get nodes'
  ```

  Expected: `Active: active (running)`. `kubectl get nodes` shows gandalf as `Ready`, role `control-plane,etcd,master`.

- [ ] **Step 3: Capture the node-join token**

  ```bash
  ssh gandalf 'sudo cat /var/lib/rancher/k3s/server/node-token' > /tmp/k3s-node-token
  chmod 600 /tmp/k3s-node-token
  ```

  This token is what frodo, samwise, merry, and pippin use to join. Do not commit it.

- [ ] **Step 4: Pull the new kubeconfig to the laptop**

  ```bash
  scp gandalf:/etc/rancher/k3s/k3s.yaml ~/.kube/config-homelab
  sed -i 's/127.0.0.1/192.168.50.135/g' ~/.kube/config-homelab
  export KUBECONFIG=~/.kube/config-homelab
  kubectl get nodes
  ```

  Expected: `kubectl get nodes` succeeds against gandalf from the laptop.

### Task 9: Join frodo as a server

**Files:** none on the repo.

- [ ] **Step 1: Install k3s on frodo in server mode**

  ```bash
  TOKEN=$(cat /tmp/k3s-node-token)
  ssh frodo "curl -sfL https://get.k3s.io | K3S_TOKEN=$TOKEN sh -s - server \
    --server https://192.168.50.135:6443 \
    --disable=servicelb \
    --tls-san=192.168.50.11 \
    --tls-san=100.92.2.26"
  ```

  (Use frodo's tailnet IP from your Tailscale admin console — adjust the `--tls-san=100.92.*` arg accordingly.)

  Wait ~1-3 minutes.

- [ ] **Step 2: Verify frodo joined as a server**

  ```bash
  kubectl get nodes
  ```

  Expected: gandalf and frodo both `Ready`, both with role `control-plane,etcd,master`.

### Task 10: Join samwise as a server

**Files:** none on the repo.

- [ ] **Step 1: Install k3s on samwise in server mode**

  ```bash
  TOKEN=$(cat /tmp/k3s-node-token)
  ssh samwise "curl -sfL https://get.k3s.io | K3S_TOKEN=$TOKEN sh -s - server \
    --server https://192.168.50.135:6443 \
    --disable=servicelb \
    --tls-san=192.168.50.12 \
    --tls-san=100.92.2.27"
  ```

  Wait ~1-3 minutes.

- [ ] **Step 2: Verify the 3-server quorum**

  ```bash
  kubectl get nodes
  ssh gandalf 'sudo k3s etcd-snapshot ls'
  ```

  Expected: gandalf, frodo, samwise all `Ready`, all `control-plane,etcd,master`. Etcd snapshots listing succeeds (proves etcd is running).

- [ ] **Step 3: Functional etcd quorum test**

  ```bash
  ssh gandalf 'sudo ETCDCTL_API=3 etcdctl \
    --endpoints=https://127.0.0.1:2379 \
    --cacert=/var/lib/rancher/k3s/server/tls/etcd/server-ca.crt \
    --cert=/var/lib/rancher/k3s/server/tls/etcd/server-client.crt \
    --key=/var/lib/rancher/k3s/server/tls/etcd/server-client.key \
    endpoint status --write-out=table 2>/dev/null'
  ```

  Expected: 3 endpoints listed, one elected leader, no errors.

### Task 11: Join merry and pippin as agents

**Files:** none on the repo.

- [ ] **Step 1: Install k3s on merry in agent mode**

  ```bash
  TOKEN=$(cat /tmp/k3s-node-token)
  ssh merry "curl -sfL https://get.k3s.io | K3S_URL=https://192.168.50.135:6443 K3S_TOKEN=$TOKEN sh -"
  ```

- [ ] **Step 2: Install k3s on pippin in agent mode**

  ```bash
  TOKEN=$(cat /tmp/k3s-node-token)
  ssh pippin "curl -sfL https://get.k3s.io | K3S_URL=https://192.168.50.135:6443 K3S_TOKEN=$TOKEN sh -"
  ```

- [ ] **Step 3: Verify all 5 nodes are Ready**

  ```bash
  kubectl get nodes -o wide
  ```

  Expected: 5 nodes, 3 with role `control-plane,etcd,master` (gandalf/frodo/samwise) and 2 with role `<none>` (merry/pippin). All `Ready`.

---

## Re-deploy cluster manifests and services

The order matters: cluster-scoped infrastructure first, then services that depend on it.

### Task 12: Run Ansible to drop the `system/*.yaml` manifests

**Files:** none on the repo (uses existing playbook).

- [ ] **Step 1: Run provision-gandalf.yml**

  ```bash
  cd ~/git/nickvigilante/homelab
  ansible-playbook -i ansible/inventory.yml ansible/provision-gandalf.yml --ask-become-pass
  ```

  Expected: `changed=N` where N covers all the system file installations. The k3s manifest-dir controller picks up `system/traefik-helmchartconfig.yaml`, `system/traefik-strip-auth-headers-middleware.yaml`, etc., and applies them.

- [ ] **Step 2: Run provision-pi.yml against all four Pis**

  ```bash
  ansible-playbook -i ansible/inventory.yml ansible/provision-pi.yml \
    --limit frodo,samwise,merry,pippin --ask-become-pass
  ```

  Expected: each Pi gets the unattended-upgrades and sshd hardening drop-ins. Confirm `changed` counts make sense.

- [ ] **Step 3: Verify Traefik is running with the post-PR-70 strategy**

  ```bash
  kubectl -n kube-system get pod -l app.kubernetes.io/name=traefik -o wide
  kubectl -n kube-system get svc traefik -o jsonpath='{.spec.type}'
  ```

  Expected: Traefik pod on gandalf, host IP = 192.168.50.135, service type `ClusterIP`.

### Task 13: Re-create cluster-foundation Secrets

**Files:** none on the repo.

- [ ] **Step 1: cert-manager Cloudflare token**

  ```bash
  CF_TOKEN=$(bw get item "Homelab Cloudflare DNS-01 Token" | jq -r '.notes')
  kubectl create namespace cert-manager
  kubectl -n cert-manager create secret generic cloudflare-api-token \
    --from-literal=api-token="$CF_TOKEN"
  ```

  Replace `notes` with the actual field name where you store it (per the item's Bitwarden structure).

### Task 14: Re-deploy cert-manager + the vigihome wildcard cert

**Files:** none on the repo (consumes `k8s/cert-manager/*` manifests).

- [ ] **Step 1: Install cert-manager via helm**

  ```bash
  helm repo add jetstack https://charts.jetstack.io
  helm repo update
  helm install cert-manager jetstack/cert-manager \
    --namespace cert-manager \
    --version <pinned-version-from-/tmp/pre-phase-a-helm.txt> \
    --set installCRDs=true
  ```

- [ ] **Step 2: Apply the ClusterIssuer and Certificate manifests**

  ```bash
  kubectl apply -f k8s/cert-manager/clusterissuer-staging.yaml
  kubectl apply -f k8s/cert-manager/clusterissuer-prod.yaml
  kubectl apply -f k8s/cert-manager/certificate.yaml
  ```

- [ ] **Step 3: Wait for the certificate to be issued**

  ```bash
  kubectl -n cert-manager wait certificate vigihome-tls --for=condition=Ready --timeout=600s
  ```

  Expected: cert issued (`Ready=True`) within ~3 minutes.

### Task 15: Re-deploy reflector

**Files:** none on the repo (consumes `k8s/reflector/*`).

- [ ] **Step 1: Install reflector helm chart**

  ```bash
  helm repo add emberstack https://emberstack.github.io/helm-charts
  helm repo update
  kubectl create namespace reflector
  helm install reflector emberstack/reflector \
    --namespace reflector \
    --version <pinned-version>
  ```

- [ ] **Step 2: Wait for vigihome-tls to be mirrored**

  ```bash
  kubectl get secret vigihome-tls -A
  ```

  Expected: vigihome-tls appears in every namespace listed in `k8s/cert-manager/certificate.yaml`'s `reflection-auto-namespaces` annotation. Reflector mirrors within ~5s.

### Task 16: Re-deploy each application service

**Files:** none new on the repo. Per-service deployment follows the existing service READMEs.

For each service (Authentik, Pi-hole, Coder, Jellyfin, Syncthing, Home Assistant, homepage, Uptime Kuma):

- [ ] **Step 1: Create namespace + PV/PVC**

  ```bash
  kubectl apply -f k8s/<service>/namespace.yaml
  kubectl apply -f k8s/<service>/pv-pvc.yaml
  ```

- [ ] **Step 2: Create the service's k8s Secret from Bitwarden**

  See `k8s/<service>/README.md` for the exact `kubectl create secret generic` invocation. Each follows the pattern:

  ```bash
  kubectl -n <namespace> create secret generic <secret-name> \
    --from-literal=<key1>="$(bw get item '<bitwarden-item>' | jq -r '.fields[] | select(.name=="<key1>") | .value')" \
    --from-literal=<key2>=...
  ```

- [ ] **Step 3: Install the chart (or apply raw manifests)**

  Chart-managed (default):

  ```bash
  helm install <release-name> <chart-source> \
    --namespace <namespace> \
    --version <pinned-version> \
    -f k8s/<service>/values.yaml
  ```

  Raw-managed (Syncthing):

  ```bash
  kubectl apply -f k8s/syncthing/deployment.yaml
  kubectl apply -f k8s/syncthing/ingress-vigihome.yaml
  ```

- [ ] **Step 4: Verify service is up**

  ```bash
  kubectl -n <namespace> rollout status deploy/<deployment-name> --timeout=180s
  ```

- [ ] **Step 5: Smoke test from the laptop**

  Visit `https://<service>.vigihome.net` in a browser. Sign in. Confirm the data PV is in the expected state (e.g., Authentik shows existing users; Pi-hole shows existing custom DNS records; Jellyfin shows existing media library).

  If a service's PV data doesn't appear: stop and investigate before continuing. The hostPath probably needs ownership fix (chown to the container's uid).

Recommended order (lowest blast-radius first):

1. homepage (stateless or near-stateless)
2. Uptime Kuma
3. Syncthing (raw-managed; tests the raw-manifest pipeline)
4. Jellyfin
5. Pi-hole (critical for LAN DNS resolution — once it's back, vigihome.net works)
6. Home Assistant
7. Coder
8. Authentik (most-stateful; OIDC dependencies)
9. Backup CronJob (verify it can read all hostPaths)

### Task 17: Re-deploy the backup CronJob

**Files:** none on the repo (consumes `k8s/backup/backup-cronjob.yaml`).

- [ ] **Step 1: Create the backup namespace + Secrets**

  ```bash
  kubectl create namespace backup
  RESTIC_PW=$(bw get password "Homelab Restic Repository")
  STORJ_AK=$(bw get item "Homelab Storj S3" | jq -r '.fields[] | select(.name=="access_key") | .value')
  STORJ_SK=$(bw get item "Homelab Storj S3" | jq -r '.fields[] | select(.name=="secret_key") | .value')
  kubectl -n backup create secret generic restic-secrets \
    --from-literal=password="$RESTIC_PW" \
    --from-literal=aws-access-key-id="$STORJ_AK" \
    --from-literal=aws-secret-access-key="$STORJ_SK"
  ```

- [ ] **Step 2: Apply the CronJob**

  ```bash
  kubectl apply -f k8s/backup/backup-cronjob.yaml
  ```

- [ ] **Step 3: Trigger a manual run and verify success**

  ```bash
  kubectl -n backup create job --from=cronjob/backup-snapshot manual-post-phase-a
  kubectl -n backup logs -f job/manual-post-phase-a
  ```

  Expected: clean exit, success ping fires to Uptime Kuma.

---

## Verification

### Task 18: Full service inventory matches pre-rebuild state

**Files:** none on the repo.

- [ ] **Step 1: Diff current state against pre-rebuild snapshots**

  ```bash
  kubectl get pods --all-namespaces -o wide > /tmp/post-phase-a-pods.txt
  diff /tmp/pre-phase-a-pods.txt /tmp/post-phase-a-pods.txt | head -50
  ```

  Acceptable diffs: pod names changed (new deployment, new generated suffix), nodes assigned might differ. Unacceptable diffs: missing namespaces, missing deployments, services in CrashLoopBackOff or Pending.

- [ ] **Step 2: Verify ingress paths**

  Curl each `*.vigihome.net` host. Each should return a non-error response (200, 302, 401 if OIDC-gated — all acceptable). 5xx or connection-refused is failure.

  ```bash
  for h in authentik coder jellyfin pi-hole syncthing home-assistant homepage uptime-kuma; do
    code=$(curl -sk -o /dev/null -w "%{http_code}" https://${h}.vigihome.net)
    echo "${h}.vigihome.net: HTTP ${code}"
  done
  ```

### Task 19: Failure tolerance test — kill gandalf

**Files:** none on the repo.

This is the actual HA validation. Skip if you'd rather not; the etcd quorum should survive even if untested.

- [ ] **Step 1: Mark gandalf cordoned and drained**

  ```bash
  kubectl drain gandalf --ignore-daemonsets --delete-emptydir-data --force
  ```

  Stateful pods pinned to gandalf will fail to evict (`PV is hostPath`). That's expected — Phase A doesn't move stateful workloads off gandalf; Phase E does.

- [ ] **Step 2: Power off gandalf (or stop the k3s service)**

  Cleanest: just stop the k3s service to simulate node death without taking the host down:

  ```bash
  ssh gandalf 'sudo systemctl stop k3s'
  ```

- [ ] **Step 3: Verify the cluster is still reachable**

  From your laptop, but pointing kubectl at frodo (a surviving server):

  ```bash
  KUBECONFIG=~/.kube/config-homelab-frodo kubectl get nodes
  ```

  (You'll need to fetch frodo's kubeconfig: `scp frodo:/etc/rancher/k3s/k3s.yaml ~/.kube/config-homelab-frodo` and `sed -i 's/127.0.0.1/192.168.50.11/g'`.)

  Expected: kubectl works. gandalf shows as `NotReady`. Etcd quorum holds with 2/3.

- [ ] **Step 4: Restart gandalf**

  ```bash
  ssh gandalf 'sudo systemctl start k3s'
  kubectl uncordon gandalf
  ```

  Expected: gandalf rejoins the cluster. Etcd resyncs.

---

## Commit pre-rebuild artifacts (optional)

### Task 20: Commit the rebuild-procedure notes

**Files:** the rebuild snapshots in `/tmp/` are throwaway. Whether to capture the lessons-learned in the repo is a judgment call.

- [ ] **Step 1: Decide whether to add a "Phase A rebuild lessons" appendix to the HA architecture spec.**

  If anything surprised you during the rebuild (a Secret you missed, a service that needed extra care), capture it. Future-you re-running this procedure will appreciate it.

  Otherwise, no commit needed — the plan's existence in git plus the spec is the durable artifact.

---

## Rollback

Phase A is a destructive rebuild — there's no clean "undo" once teardown happens. The recovery paths in order of pain:

1. **Mid-rebuild abort, gandalf is in a partial state.** Re-run the install command (Task 8 Step 1) to overwrite the partial install. Continue from Task 9.
2. **A service won't come back up after rebuild.** This is just a per-service recovery — Helm rollback (if the chart accepts it), or kubectl delete + recreate the deployment with values.yaml.
3. **PV hostPath data corrupted.** Use the restic snapshot from Task 1 Step 2 to restore:
   ```bash
   restic -r s3:... restore <snapshot-id> --target /tmp/restore --include="/opt/<service>"
   rsync -a /tmp/restore/opt/<service>/ /opt/<service>/
   ```
4. **Cluster won't come up at all.** Last-resort: complete teardown of all nodes via each node's `k3s-uninstall.sh` (or `k3s-agent-uninstall.sh` for agents), then re-run this plan from Task 8.
