# Grafana MCP — read-only homelab access for Claude — Design

**Issue:** #184 (Claude MCP access — Grafana).
The Kubernetes MCP is a separate sub-project, tracked separately.

**Goal:** Give Claude read-only, conversational access to the homelab's
observability data — Grafana dashboards plus the Prometheus metrics Grafana
fronts — through the official, **self-hosted** `grafana/mcp-grafana` server,
reached over Tailscale and authenticated with a Viewer service-account token
sourced from Bitwarden.

**Non-goals (this sub-project):**

- Any write or admin access to Grafana (creating/editing dashboards, alerts,
  users) — Viewer only.
- Scraping Uptime Kuma's `/metrics` into Prometheus so its up/down data is
  queryable through this MCP — worthwhile, but a separate Prometheus/Service
  Monitor change tracked as its own issue.
- Loki/log querying (no Loki deployed yet; sub-project B, #116).
- Running the MCP as a remote/hosted server — it stays a local stdio process.

## Architecture

`grafana/mcp-grafana` runs **locally on the laptop** as a stdio MCP server,
registered with Claude Code. It is a self-contained Go binary that makes HTTPS
calls **directly to the homelab Grafana** — nothing transits Grafana Labs.

```
Claude Code  ──stdio──▶  mcp-grafana (laptop)  ──HTTPS over Tailscale──▶  grafana.vigihome.net
```

`GRAFANA_URL = https://grafana.vigihome.net`, which Pi-hole resolves to the
tailnet address (`100.92.2.25`) when the laptop is on the tailnet, so the
request never leaves the private network and the existing `vigihome-tls`
Let's Encrypt cert validates normally.

## Components

| Component              | Role                                                   | Placement      |
| ---------------------- | ------------------------------------------------------ | -------------- |
| `mcp-grafana`          | Exposes Grafana/Prometheus query tools to Claude       | laptop (stdio) |
| Grafana                | Already deployed via `kube-prometheus-stack`           | gandalf        |
| Prometheus             | Queried *through* Grafana's datasource proxy           | gandalf        |
| Viewer service account | Identity the MCP authenticates as (token in Bitwarden) | Grafana        |

## Identity and least privilege

- A dedicated **Viewer-role Grafana service account** (e.g. `claude-mcp`),
  created in Grafana → Administration → Service accounts — mirrors the existing
  Viewer-login pattern used for the Homepage widget, not the admin account.
- The server runs with **only read toolsets enabled**; the admin toolset is off
  by default and stays off. Write-capable toolsets are not enabled.
- Viewer role means the token cannot create or modify dashboards, alerts, or
  users even if a tool were mis-invoked — defense in depth (role + toolset).

## Networking

- Reached over **Tailscale** via `grafana.vigihome.net` (Pi-hole wildcard →
  tailnet `100.92.2.25`). No new ingress, no public exposure, no Cloudflare.
- Requires the laptop to be on the tailnet and using Pi-hole for `*.vigihome.net`
  resolution — already the case per the DNS pattern in `CLAUDE.md`.

## Secrets

- The Viewer service-account token lives **only in Bitwarden**, vault item
  `Homelab Grafana`, new field `mcp-sa-token` — consistent with the
  secrets-discipline rule that Bitwarden is the source of truth and nothing
  lands in the repo.
- It is laptop-side (the MCP runs locally), so it does **not** need ESO/BWS
  cluster sync. It is injected into the chezmoi-managed Claude MCP config the
  same way `~/.kube/homelab.yaml` is — a chezmoi `bitwarden` template renders
  the token into a 0600 file at `chezmoi apply` time, so the secret reproduces
  across machines without ever being committed.

## Privacy boundary

The MCP server and credentials stay on the private network, but **tool results
become context sent to Claude** (the model). For this MCP that means metric
values, dashboard metadata, and query results — no secrets — which is
acceptable. Log toolsets (which could carry sensitive payloads) are out of
scope here; revisit when Loki lands (#116).

## Implementation surface

This spec's plan will touch two repos:

- **This repo (homelab):** this spec; optionally a short `README` note under
  `k8s/kube-prometheus-stack/` documenting the `claude-mcp` Viewer service
  account.
- **dotfiles:** install `mcp-grafana` (declared in the chezmoi Brewfile per the
  packages workflow); register the MCP server in the chezmoi-managed Claude
  config with the Bitwarden-templated token.

## Staged rollout

1. **Token** — create the `claude-mcp` Viewer service account in Grafana, store
   its token in Bitwarden `Homelab Grafana` / `mcp-sa-token`.
2. **Install + register** — add `mcp-grafana` to the Brewfile; register the
   stdio MCP server in the Claude config with `GRAFANA_URL` and the
   Bitwarden-templated `GRAFANA_SERVICE_ACCOUNT_TOKEN`; `chezmoi apply`.
3. **Verify** — from Claude, list dashboards and run a sample PromQL query
   returning live data over Tailscale; confirm a write attempt (e.g. create
   dashboard) is refused.

## Acceptance criteria

- Claude can list Grafana dashboards and run a PromQL query that returns live
  homelab data.
- A write/admin action (create dashboard, edit alert) is refused (Viewer +
  read-only toolsets).
- All traffic resolves to the tailnet address; Grafana gains no public
  exposure.
- The token is sourced from Bitwarden and present in no committed file;
  betterleaks pre-commit and CI are clean.

## Things deliberately not done

- **Uptime Kuma metrics scrape.** Surfacing Kuma through Prometheus needs a
  `/metrics` scrape + ServiceMonitor; tracked as its own issue, not bundled
  here.
- **Write/admin access.** Excluded by design; revisit only with a concrete need
  and a separate, scoped service account.
- **Remote-hosted MCP.** Local stdio only, to keep the credential and surface on
  the laptop.
