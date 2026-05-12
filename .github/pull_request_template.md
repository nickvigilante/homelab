## Summary

<!-- 1-3 bullets. What changes and why. -->

## Before merge

- [ ] No secrets in the diff — `secret.example.yaml` placeholders only.
- [ ] New persistent dirs (`/opt/<service>/...`) added to `k8s/backup/backup-cronjob.yaml` under a new tag.
- [ ] Host-level changes captured in `ansible/provision-gandalf.yml` (or noted as deliberate manual).
- [ ] SPOF impact: any new service behind Authentik documents its local-fallback credential.
- [ ] DNS: any new `*.home` hostname added to Pi-hole's `dns.hosts` and the service's README.
- [ ] README walks through the one-time setup end-to-end.

## Test plan

<!-- Markdown checklist of the steps to verify the change works. -->
