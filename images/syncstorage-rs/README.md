# syncstorage-rs image

[mozilla-services/syncstorage-rs](https://github.com/mozilla-services/syncstorage-rs) is the
self-hosted Firefox Sync storage server (used with Mozilla's Firefox Accounts for auth — see
`k8s/syncstorage-rs/README.md` for the deployment). Upstream does not publish a Docker image for
any backend: every `docker/build-push-action` step in their CI has `push: false` and uploads the
built image only as a short-lived e2e-test artifact. This folder is what makes a real, pullable
image exist.

## Why this folder, and not a vendored copy of their source

`.github/workflows/build-syncstorage-rs.yml` builds straight from upstream's own `Dockerfile`
using a **remote git context** (`docker/build-push-action` with `context:` set to a GitHub URL),
so nothing here needs to track their Rust source. The only local state is:

- `VERSION` — the upstream git tag to build (e.g. `0.23.3`, no `v` prefix — check their
  [releases](https://github.com/mozilla-services/syncstorage-rs/releases) for the tag format).
- This README.

## Backend

Built with:

```
--build-arg SYNCSTORAGE_DATABASE_BACKEND=postgres
--build-arg TOKENSERVER_DATABASE_BACKEND=postgres
```

Postgres is a fully CI-tested backend upstream (`syncstorage-postgres` / `tokenserver-postgres`
crates, its own e2e job in their `main-workflow.yml`) but isn't one of the backends they publish
prebuilt artifacts for — hence building our own.

## Image

Published to `ghcr.io/nickvigilante/syncstorage-rs:<version>-postgres`, e.g. `0.23.3-postgres`.
No `latest` tag, matching this repo's "pin every image to a specific version" convention.

**One-time step after the first push**: GHCR packages default to private regardless of the
publishing repo's visibility. Go to the package's settings on GitHub
(`github.com/nickvigilante?tab=packages` → `syncstorage-rs` → Package settings) and set visibility
to Public, or the cluster won't be able to pull it without an `imagePullSecret`.

## Bumping the version

Edit `VERSION`, commit to `main`. The workflow triggers on changes to this path and rebuilds
automatically. To rebuild the current version (e.g. after a base-image security patch) without
bumping it, run the workflow manually via `workflow_dispatch`.
