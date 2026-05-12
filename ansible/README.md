# ansible

Host-level config management for the home lab. Covers maintaining
gandalf and adding Pi workers / the Pi Zero exit node.

## Where to run from

**Run from the MacBook** (or any workstation with Ansible installed,
SSH access to the targets, and either LAN reach or a Tailscale
connection). This is the standard admin-workstation pattern, matches
how `provision-pi.yml` is meant to run, and avoids the self-SSH
chicken-and-egg on gandalf.

```bash
cd ~/git/nickvigilante/homelab/ansible   # commands assume this CWD —
                                          # ansible.cfg lives here
ansible-playbook provision-gandalf.yml --ask-become-pass
```

`ansible.cfg` is in `ansible/` and uses a *relative* inventory path,
so running from anywhere else silently falls back to implicit
localhost (no hosts matched). Always `cd` in first.

**Fallback: running from gandalf itself.** If you're already SSH'd
into gandalf, add `--connection=local --limit gandalf` so Ansible
doesn't try to SSH back to itself (gandalf doesn't have a self-SSH
key by design):

```bash
ansible-playbook provision-gandalf.yml \
  --connection=local --limit gandalf --ask-become-pass
```

## What's in here

| File | What it does |
|------|--------------|
| `ansible.cfg` | Defaults: inventory path, SSH multiplexing, YAML output, fact caching |
| `inventory.yml` | Hosts grouped by role (`cluster_servers`, `cluster_agents`, `tailscale_only`). Pi entries are commented out until you image one. |
| `provision-pi.yml` | Image-a-Pi-as-k3s-agent flow. Reads the k3s join token from gandalf, brings the Pi up on Tailscale with `tag:homelab`, installs the agent, joins the cluster. |
| `provision-gandalf.yml` | Idempotent host-config maintenance for gandalf (the control plane). Covers baseline packages, the unattended-upgrades drop-in, and the k3s `secrets-encryption` flag. Not a from-scratch bootstrap — gandalf was set up by hand and this playbook intentionally avoids the risky bits (k3s install, rclone mount, LVM, fstab). |
| `bin/mint-tailscale-authkey.sh` | Mints a single-use Tailscale auth key via the `opentofu-homelab` OAuth client and writes it to `~/.tailscale-authkey`. Run before each `provision-pi.yml` invocation. |

## When you image a new Pi

1. **Mint a fresh Tailscale auth key.** Run the bundled script — it asks
   Tailscale to mint a single-use, 1-hour, `tag:homelab`-scoped key via
   the OAuth client, writes it to `~/.tailscale-authkey` with mode 600,
   and prints the next command for you.

   ```bash
   ./bin/mint-tailscale-authkey.sh
   # Defaults: expiry 1h, output ~/.tailscale-authkey, tag tag:homelab.
   # Override with --expiry 24h, --out /path, --tag tag:other.
   ```

   The script uses the same `opentofu-homelab` OAuth client credentials
   that Tofu reads from `~/.homelab-opentofu.env`. The client needs the
   `auth_keys` scope (Write) and `tag:homelab` in its allowed-tags list
   — verify in the [Tailscale admin OAuth settings](https://login.tailscale.com/admin/settings/oauth).

   Why a script instead of the admin UI? Hand-clicked keys default to a
   90-day expiry and age out unused. Minting on-demand via OAuth gives
   a fresh, short-lived key per provisioning session — the OAuth
   credentials themselves never expire, so this is the long-term
   rotation story.

2. **Image the SD card.** Raspberry Pi OS Lite (64-bit). Set SSH on,
   add your public key, set `pi` user with a known password (for the
   one-time `--ask-pass` run).

3. **First boot, find the IP.** Either fixed it in DHCP (using the
   plan's IP scheme — Pi 5 `.11`, Pi 4Bs `.12`-`.15`, Pi Zero `.16`),
   or grab it from `arp` / your router.

4. **Add to inventory.** Edit `inventory.yml` and uncomment / add the
   entry under `cluster_agents`. For the Pi 4B that becomes the
   dedicated Pi-hole host, add `k3s_node_labels: { role: pihole }`
   so Pi-hole pods can target it via nodeSelector.

5. **Run the playbook**:

   ```bash
   # First run — Pi still has the `pi` user; we use it to bootstrap.
   ansible-playbook provision-pi.yml \
     --limit <hostname> \
     --extra-vars 'ansible_user=pi' \
     --extra-vars "tailscale_authkey=$(cat ~/.tailscale-authkey)" \
     --ask-pass --ask-become-pass

   # Re-runs after a `nickv` user has been added (idempotent):
   ansible-playbook provision-pi.yml --limit <hostname> \
     --extra-vars "tailscale_authkey=$(cat ~/.tailscale-authkey)"
   ```

6. **Verify on gandalf**:

   ```bash
   kubectl get nodes -o wide
   # New node should show Ready in <60s.
   ```

## What the playbook actually does

In order:

1. **On gandalf:** reads the k3s join token from `/var/lib/rancher/k3s/server/node-token`.
2. **On the Pi:** sets hostname, sets timezone to America/New_York, installs baseline packages (curl, htop, jq, vim, unattended-upgrades).
3. **Drops in the auto-reboot config** from `../system/52unattended-upgrades-local.conf` — same security-patch policy as gandalf.
4. **Installs Tailscale**, brings it up with `--advertise-tags=tag:homelab` and the supplied auth key. Tag matches the homelab ACL (managed in the infrastructure repo).
5. **Installs the k3s agent**, points it at gandalf's API server, joins the cluster.
6. **Applies node labels** specified in inventory (e.g., `role: pihole`).

The playbook is idempotent — running it twice doesn't break anything. Each major step has a `creates:` guard or relies on the underlying installer's idempotency.

## Maintaining gandalf

The `provision-gandalf.yml` playbook is the idempotent home for
host-level changes on gandalf. Run it after pulling repo changes that
touch `system/` or the playbook itself. From your MacBook:

```bash
cd ~/git/nickvigilante/homelab/ansible
ansible-playbook provision-gandalf.yml --ask-become-pass
```

(Or from gandalf itself, use the `--connection=local --limit gandalf`
form documented in the "Where to run from" section.)

What changes it picks up:

- Updates to `../system/52unattended-upgrades-local.conf` (auto-reboot policy)
- Toggling `k3s_secrets_encryption` (enables k3s `secrets-encryption` and
  re-encrypts existing Secrets after a single restart). First enablement
  causes one brief k3s downtime; subsequent runs are no-ops.

What it intentionally does NOT do (left as manual, with notes in
`../docs` or the project memory): k3s install, rclone systemd unit,
LVM/fstab edits, the host's Tailscale install/up. Adding any of these
should be a deliberate decision — `provision-gandalf.yml` is meant to
be safely re-runnable without surprises.

## Why no Semaphore (yet)

A web UI on top of Ansible is over-engineered for a one-user, six-host
lab. Run `ansible-playbook` from gandalf or your laptop. Reconsider
Semaphore only if multiple humans start triggering playbooks, or you
want an audit trail. See the project memory for the full rationale.

## Why no roles

Roles pay off when you're reusing chunks across multiple playbooks.
We have one playbook today. When `provision-pi-zero.yml` arrives (Pi
Zero W as a Tailscale-only exit node — different config, no k3s),
extract a shared `roles/common/` and `roles/tailscale/` then.
