# Syncthing

Peer-to-peer file sync across the user's devices (Framework16,
Framework12, Main PC, MacBook Pro, Pixel phone), with this in-cluster
instance acting as the always-on rendezvous that the previous (now
retired) standalone Pi used to play. Architecture follows **Approach
C** from the [[homelab service candidates]] memory:

- Pod runs in k3s with a local hostPath PV on gandalf — fast, simple,
  Syncthing's index DB doesn't fight object storage.
- That PV is included in the nightly restic CronJob → encrypted
  off-site snapshots on Storj. Critical for the receiveonly folders
  (DCIM, Pixel Downloads) where the phone is the only other replica.
- Peer-to-peer connections go through gandalf's host network
  (hostNetwork: true on the pod). Peers reach `gandalf:22000` on the
  LAN or `gandalf.tail395fc0.ts.net:22000` from anywhere on the
  tailnet — no NodePort translation.

## Why /mnt/storage/syncthing instead of /opt/syncthing

The repo convention is `/opt/<service>/` for service hostPaths, but
Syncthing's data is bulk (~180 GB out of the gate, will grow). Putting
it on gandalf's root LV would eat most of the remaining headroom.
`/mnt/storage` is a 1.8 TB ext4 disk reserved exactly for this case
(per the host-state notes in `../../CLAUDE.md`). Syncthing is the
first service to use it; future bulk-volume services (Immich,
Paperless-ngx, etc.) should follow the same pattern.

The trade-off is HDD-speed reads vs. SSD; Syncthing isn't latency-
sensitive, so it doesn't matter.

## Pre-flight (do these before `kubectl apply`)

### 1. Create the Bitwarden item

Item name: **`Homelab Syncthing`**.

Required fields:

| Field | Value |
|-------|-------|
| `gui-admin-password` | Strong password, used for the web UI at `https://syncthing.vigihome.net`. Set this in step 5 — no Secret/Secret-from-env wiring on the cluster side, Syncthing stores its own GUI auth in `config/config.xml`. |

(Optional: stash the pod's eventual device ID here too after step 5
for reference — peers will need it.)

### 2. Pre-create the directory tree on gandalf

```bash
sudo mkdir -p /mnt/storage/syncthing/{config,Sync}
sudo chown -R 1000:1000 /mnt/storage/syncthing
```

The PV is `DirectoryOrCreate`, so technically this isn't required for
the pod to start — but the chown is, otherwise the container's
PUID=1000 user can't write into a root-owned hostPath. Doing this
explicitly also makes the next step possible.

### 3. Move the recovery archive into place

The retired Pi's data lives in `gandalf:~/syncthing-pi42-recovery/`
(177 GB rsync'd 2026-05-14). Move it — don't copy — to save a duplicate
of 177 GB on the root LV:

```bash
for d in Downloads Documents Pictures Videos Backups DCIM "Pixel Downloads"; do
  sudo mv "$HOME/syncthing-pi42-recovery/$d" "/mnt/storage/syncthing/Sync/$d"
done
sudo chown -R 1000:1000 /mnt/storage/syncthing/Sync
```

After this, `du -sh /mnt/storage/syncthing/Sync/*` should match the
sizes you saw on the recovery archive. The leftover
`~/syncthing-pi42-recovery/` (now empty except for the metadata files
that didn't transfer due to perms) can be removed when you're sure
the move was clean:

```bash
ls -la ~/syncthing-pi42-recovery/   # only Syncthing internals left
rm -rf ~/syncthing-pi42-recovery/
```

### 4. DNS (no action needed)

`syncthing.vigihome.net` is resolved by Pi-hole's
`address=/vigihome.net/...` wildcard in `misc.dnsmasq_lines` (see
`k8s/pihole/values.yaml`) — both LAN and tailnet. No per-service
record to add.

## Apply

```bash
cd ~/git/nickvigilante/homelab/k8s/syncthing
kubectl apply -f namespace.yaml
kubectl apply -f pv-pvc.yaml
kubectl apply -f deployment.yaml
kubectl apply -f ingress-vigihome.yaml

# Watch the pod come up — first start with 180 GB to scan takes a
# few minutes (Syncthing builds its index by walking the files).
kubectl -n syncthing get pods -w
```

Also re-apply the backup CronJob so the new `syncthing-data` volume
mount picks up:

```bash
kubectl apply -f ../backup/backup-cronjob.yaml
```

(The CronJob itself isn't re-run on apply; the change just takes
effect on the next 03:00 schedule. To test sooner, see step 9.)

## First-launch GUI config

### 5. Open the web UI and capture the device ID

Browse to `https://syncthing.vigihome.net`. On first launch, Syncthing
prompts you to set a GUI admin user/password — use `Homelab Syncthing`
from Bitwarden. (Decline the anonymous-usage-report dialog; we're
self-hosting on purpose.)

Note the **Device ID** shown at the top of the UI — it's a long
hyphen-separated string like
`BC7WTLX-TKCGIXN-PRGBMUA-WYMBQV5-R7TJAK6-IAZOYCV-25TBQYJ-24BN6AP`.
Every peer needs this to authorize the pod. Save it in the
`Homelab Syncthing` Bitwarden item as a `device-id` field.

### 6. Add the shared folders

For each subdirectory you moved into `/mnt/storage/syncthing/Sync/`,
add a folder in the UI: **Add Folder** → set the **Folder Path** to
the in-container path (`/var/syncthing/Sync/<name>`).

Folder types — match the original setup so the receiveonly folders
don't accidentally propagate deletions back to the phone:

| Folder | Type |
|--------|------|
| `Downloads` | Send & Receive |
| `Documents` | Send & Receive |
| `Pictures` | Send & Receive |
| `Videos` | Send & Receive |
| `Backups` | Send & Receive |
| `DCIM` | Receive Only |
| `Pixel Downloads` | Receive Only |

Syncthing will scan each folder; the first scan of `Pictures` /
`Videos` can take many minutes depending on file count.

## Peer migration — one at a time

**Important:** don't add the pod to a peer with the matching folders
linked all-at-once. Each peer reconciliation has the potential to
generate `.sync-conflict-*` files; adding peers sequentially bounds
the noise. Recommended order (least-likely-to-conflict first):

1. **Framework16** — Syncthing is currently inactive (we verified
   2026-05-13 via `systemctl status`). After re-enabling it, its
   index reflects whatever its disk had at last sync (possibly
   stale). Add the pod's device ID; link folders as Send & Receive
   (DCIM/Pixel Downloads as Receive Only).
2. **Main PC** — same pattern.
3. **Framework12** — same pattern.
4. **MacBook Pro** — same pattern.
5. **Pixel phone** — install Syncthing-Fork (the maintained Android
   fork), pair to the pod, configure DCIM and Pixel Downloads as
   **Send Only** on the phone side (Receive Only on the pod side, which
   is already set in step 6).

Between each peer addition, wait for the pod's UI to show "Up to
Date" on every folder before moving to the next. Resolve any
`.sync-conflict-*` files manually before adding the next peer.

### 7. Verify backup wiring (after the first 03:00 cycle)

After the next nightly run (or trigger one manually — see step 9):

```bash
kubectl -n backup logs -l job-name --tail=200 | grep -i 'syncthing\|snapshot'
```

You should see a `=== Syncthing data ===` section, followed by the
snapshot summary listing `syncthing-data` as one of the tags. If the
section is empty, the dir doesn't exist or is empty — go back to step 3.

### 8. Lock in SPOF discipline

Syncthing's web UI is fronted by the GUI password you set in step 5,
**not** by Authentik (yet). That's intentional for the initial deploy:

- Authentik forward-auth via Traefik can be layered on later in a
  follow-up PR — same pattern as Pi-hole's planned integration.
- When that happens, **keep the native admin password** in Bitwarden
  as the local-fallback per [[k3s home lab plan]] SPOF discipline.

For now: native auth is sufficient.

## Day-to-day operations

### Triggering a backup manually

```bash
kubectl -n backup create job --from=cronjob/restic-backup \
  test-syncthing-$(date +%s)
kubectl -n backup logs -l job-name=test-syncthing-... -f
```

### Bumping the image

Pin to a real tag in `deployment.yaml` (the manifest currently uses
`latest` so the very first apply pulls the newest; change to e.g.
`syncthing/syncthing:1.30.0` after the first deploy succeeds). Bump
by editing the image line and `kubectl apply`.

### Adding a peer later

In the pod's web UI: **Add Remote Device**, paste the peer's device
ID, give it a name, and select which folders to share with it.
The peer authorizes the pod the same way from its UI.

### Restoring a single file from restic

```bash
# Find the snapshot
kubectl -n backup run restic-shell --rm -it --restart=Never \
  --image=restic/restic:latest \
  --env-from=secretRef:name=restic-credentials \
  -- snapshots --tag syncthing-data

# Restore a specific path to /tmp inside that pod (or to a hostPath)
restic restore <snapshot-id> --target /restore \
  --include /backup/syncthing/Sync/Documents/foo.pdf
```

## Pitfalls (discovered in deploy / migration)

- **GUI defaults to HTTPS with a self-signed cert.** Some images
  ignore `STGUIADDRESS=0.0.0.0:8384` and still flip on TLS internally,
  which then mismatches with Traefik's plain HTTP upstream route. If
  `https://syncthing.vigihome.net` returns 502 or "protocol error,"
  shell into the pod and edit `config/config.xml`: under `<gui ...>`
  change `tls="true"` to `tls="false"`, then `kubectl -n syncthing
  rollout restart deploy/syncthing`. Or add the
  `traefik.ingress.kubernetes.io/service.serverstransport:
  insecureskipverify@file` annotation to the Ingress and let Traefik
  skip cert verification on the upstream (Traefik still terminates
  the public TLS with the wildcard vigihome cert; this only affects
  the in-cluster hop).

- **`hostNetwork: true` means port collisions are fatal.** Anything
  else on gandalf binding 22000, 21027, or 8384 will block the pod
  from starting. Check with `sudo ss -tunlp '( sport = :22000 or
  sport = :21027 or sport = :8384 )'` before applying.

- **Empty-folder propagation risk.** When adding the pod to an
  existing peer, **make sure the pod's folder has the recovery data
  already** (step 3) — otherwise the peer treats the pod's "empty"
  folder as authoritative for deletion and you lose everything.
  Re-read the order in step 7 if you're not sure.

- **Phone-side configuration.** If the Pixel had a Syncthing
  configuration pointing at the dead Pi, that pairing is stale.
  Remove the old device entry from the phone before adding the new
  pod's device ID, or you'll get duplicate folder entries.
