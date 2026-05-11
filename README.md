# homelab

Kubernetes manifests, Helm values, and host-level system configs for my home lab — a single-node k3s cluster running on `gandalf` (a ThinkCentre on Ubuntu Server 26.04 LTS).

This repo is the source of truth. The working copy lives at `~/git/nickvigilante/homelab/` on `gandalf` and tracks `main`.

## Layout

| Path | What's in it |
|------|--------------|
| `system/` | Host-level systemd units and config templates (currently: rclone Storj mount for the media bucket) |
| `k8s/jellyfin/` | Jellyfin (`jellyfin/jellyfin` Helm chart) — values.yaml + PV/PVC for config persistence |
| `k8s/pihole/` | Pi-hole (`mojo2600/pihole` Helm chart) — values.yaml + PV/PVC for `/etc/pihole` persistence |
| `k8s/homeassistant/` | (planned) Home Assistant |

## How changes land in the cluster

Manual, single-node, no GitOps controller (yet). Edit in this repo, then on gandalf:

```bash
# k8s manifests / PVs
kubectl apply -f k8s/<service>/pv-pvc.yaml

# Helm releases — upgrade an existing release with new values
helm -n <namespace> upgrade <release> <chart> -f k8s/<service>/values.yaml
kubectl -n <namespace> rollout status deployment/<release>

# Host-level systemd units
sudo install -m 644 -o root -g root system/<unit>.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart <unit>.service
```

## Secrets

No secrets live in this repo. Where they live instead:
- `pihole-admin` k8s Secret — created by `kubectl create secret generic` sourced from Bitwarden ("Pi-Hole" item).
- `rclone.conf` — `/etc/rclone/rclone.conf` on gandalf (root, 0600), populated by hand from Storj S3 access grant. Template in `system/rclone.conf.template`.

## Pre-commit secret scan

Same pattern as [`nickvigilante/infrastructure`](https://github.com/nickvigilante/infrastructure). After cloning:

```bash
brew install gitleaks
git config core.hooksPath .githooks
```

`gitleaks` then scans every staged change before each commit, blocking commits that contain detected secrets. See [`.gitleaks.toml`](./.gitleaks.toml).

## Related

- [`nickvigilante/infrastructure`](https://github.com/nickvigilante/infrastructure) — OpenTofu for cluster-external resources (Tailscale tailnet DNS, GitHub repo settings, future Storj buckets).
