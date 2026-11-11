# Renovate

Self-hosted [Renovate](https://docs.renovatebot.com/), run as a daily CronJob.
It opens pull requests for outdated Helm chart versions and container image tags.
It never merges anything: merging a PR is what makes Flux apply the change.

## How a run works

1. The `token` init container signs a short-lived JWT with the GitHub App's private key.
2. It exchanges the JWT for an installation token, which GitHub expires after one hour.
3. The `renovate` container reads that token, runs against the repos listed in `config.js`, and pings Uptime Kuma with the result.

Renovate cannot sign in as a GitHub App on its own, which is why the token step exists.

## Which repos it covers

`config.js` lists the repos, and each repo opts in by committing a `renovate.json`.
A repo without one is skipped, so Renovate never opens an onboarding PR.
Today that is `nickvigilante/homelab` only.
To add a repo, commit a `renovate.json` there, add the repo to `config.js`, and install the GitHub App on it.

Renovate's Flux and Kubernetes managers scan nothing by default, so `renovate.json` names the directories explicitly.
A `HelmRelease` is only linked to its `HelmRepository` when both carry a `metadata.namespace`, so check that for new charts.

## Before this can merge

The manifests reference secrets that do not exist yet.

1. **Create a GitHub App** at github.com/settings/apps with these repository permissions, and no webhook.
   - Administration: read
   - Checks: read and write
   - Commit statuses: read and write
   - Contents: read and write
   - Dependabot alerts: read
   - Issues: read and write
   - Merge queues: read and write
   - Metadata: read
   - Pull requests: read and write
   - Workflows: read and write
2. **Install the app** on `nickvigilante/homelab` only, then note the App ID and the installation id from the installation URL.
3. **Generate a private key** on the app's settings page.
4. **Create four BWS secrets**: the App ID, the installation id, the PEM private key, and the Uptime Kuma push URL.
5. **Create a push monitor** named `renovate` in Uptime Kuma, with a heartbeat interval of 93600 seconds (26 hours).
6. **Replace the four `REPLACE-WITH-BWS-SECRET-ID` values** in `external-secret.yaml` with the BWS secret ids.

## Monitoring

Three independent channels, so a silent failure of one is caught by another:

- **Uptime Kuma** push monitor `renovate`, pinged on success and on failure.
  A run that dies early sends no ping, and the monitor goes DOWN when its heartbeat expires.
- **Prometheus rules** in `prometheusrule.yaml`: `RenovateRunFailed` for a failed Job, `RenovateNotSucceeding` for no success in 30 hours, and `RenovateNeverSucceeded` for a CronJob that has never worked.
  Alertmanager mails them through the existing route.
- **Flux**: the existing `FluxReconciliationFailure` alert covers the `renovate` Kustomization.

Renovate can exit 0 when it fails on one repo, so a green ping means the run finished, not that every repo was processed.
The dependency dashboard issue in each repo shows what Renovate saw.

## Manual run

```bash
kubectl -n renovate create job --from=cronjob/renovate test-renovate-$(date +%s)
kubectl -n renovate logs -f job/test-renovate-<id> --all-containers
```
