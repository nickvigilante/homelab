# homelab

Kubernetes manifests, Helm values, and host-level system configs for my home lab — a single-node k3s cluster running on `gandalf` (a ThinkCentre on Ubuntu Server 26.04 LTS).

This repo is the source of truth. The working copy lives at `~/git/nickvigilante/homelab/` on `gandalf` and tracks `main`.

## Layout

| Path                 | What's in it                                                                                                                                                                                        |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `system/`            | Host-level systemd units, apt drop-ins, and config templates (rclone Storj mount; unattended-upgrades auto-reboot override)                                                                         |
| `k8s/jellyfin/`      | Jellyfin (`jellyfin/jellyfin` Helm chart) — values.yaml + PV/PVC for config persistence                                                                                                             |
| `k8s/pihole/`        | Pi-hole (`mojo2600/pihole` Helm chart) — values.yaml + PV/PVC for `/etc/pihole` persistence                                                                                                         |
| `k8s/backup/`        | Restic backup CronJob → Storj. See [`k8s/backup/README.md`](./k8s/backup/README.md) for setup + restore.                                                                                            |
| `k8s/uptime-kuma/`   | Uptime Kuma (`dirsigler/uptime-kuma-helm` Helm chart) — status board + push-monitor sink for restic. See [`k8s/uptime-kuma/README.md`](./k8s/uptime-kuma/README.md).                                |
| `k8s/authentik/`     | Authentik (`authentik/authentik` Helm chart) — identity provider for future SSO across services. Bundled PostgreSQL. See [`k8s/authentik/README.md`](./k8s/authentik/README.md).                    |
| `k8s/homeassistant/` | (planned) Home Assistant                                                                                                                                                                            |
| `ansible/`           | Host-level config for future Pi workers (Stage 2). One playbook today — `provision-pi.yml` — that joins a fresh Pi as a k3s agent + Tailscale node. See [`ansible/README.md`](./ansible/README.md). |
| `audits/`            | Pentest / security audits. One entry today — [`audits/tier-1-authentik.md`](./audits/tier-1-authentik.md), the Tier-1 Authentik pass.                                                               |

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

# Host-level apt / unattended-upgrades drop-ins
sudo install -m 644 -o root -g root system/<file>.conf /etc/apt/apt.conf.d/
```

## Secrets

No secrets live in this repo. Where they live instead:

- `pihole-admin` k8s Secret — created by `kubectl create secret generic` sourced from Bitwarden ("Pi-Hole" item).
- `rclone.conf` — `/etc/rclone/rclone.conf` on gandalf (root, 0600), populated by hand from Storj S3 access grant. Template in `system/rclone.conf.template`.

## Pre-commit hooks (format + lint + secret scan)

Managed by the [pre-commit](https://pre-commit.com) framework
([`.pre-commit-config.yaml`](./.pre-commit-config.yaml)): formats files
(mdformat for Markdown, yamlfmt for YAML, shfmt for shell), lints
(yamllint, shellcheck), and scans for secrets with
[betterleaks](https://github.com/betterleaks/betterleaks) (the maintained
successor to gitleaks). The same hooks run in CI via
`pre-commit run --all-files`, so local and CI can't drift. After cloning:

```bash
brew install pre-commit yamlfmt shfmt shellcheck yamllint betterleaks
git config --unset core.hooksPath   # drop the old .githooks gitleaks hook
pre-commit install
```

Commits are then auto-formatted and blocked if betterleaks detects a secret.
Bypass only if certain: `git commit --no-verify`.

## Related

- [`nickvigilante/infrastructure`](https://github.com/nickvigilante/infrastructure) — OpenTofu for cluster-external resources (Tailscale tailnet DNS, GitHub repo settings, future Storj buckets).
