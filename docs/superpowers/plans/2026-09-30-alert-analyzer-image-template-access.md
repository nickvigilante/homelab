# Alert analyzer image, template and access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the read-only `Analyzer` Coder template, its image, and the cluster access controls that confine it (spec Phase 1).

**Architecture:** A new `images/analyzer/` image layers six tools onto the existing base image.
A new `templates/Analyzer/` template runs one persistent, secret-free workspace pod as the ServiceAccount `analyzer`.
Cluster-side, that ServiceAccount is bound to the built-in `view` ClusterRole, and a NetworkPolicy keyed on a pod label limits the pod's egress.
The work lands as three PRs: two in `homelab-dev-templates` (image, then template) and one in `homelab` (access).

**Tech Stack:** Docker BuildKit, GitHub Actions, OpenTofu with the `coder/coder` and `hashicorp/kubernetes` providers, Coder CLI v2.37, Kubernetes RBAC and NetworkPolicy (k3s's bundled kube-router controller), Flux, POSIX shell.

**Spec:** `docs/superpowers/specs/2026-09-30-alert-analyzer-design.md` in the `homelab` repo (merged, PR #239).
This plan covers its Phase 1 only.
The orchestrator, the Alertmanager route and the Phase 0 spikes belong to other plans.

## Global Constraints

Copied from the spec, and implicit in every task:

- A new `Analyzer` template, separate from `Base`.
- A persistent workspace, pinned to amd64, with 1 core, 2 GB and a small home volume, and no user-facing parameters.
- The image builds on the existing base image and adds `kubectl`, `helm`, `flux`, `promtool`, `yq` and `jq`, published by the same GHCR CI.
- A startup script clones or pulls the public `homelab` repo, with no credentials.
- Autostop after 30 idle minutes is a backstop, and auto-delete is off.
- In-cluster identity is the ServiceAccount `analyzer` in the `coder` namespace, bound to the built-in `view` ClusterRole, so Secrets are not readable.
- A NetworkPolicy limits the workspace pod's network access.
- No secrets in the workspace: nothing baked into the image, no `coder_parameter`, no dotfiles, no Claude Code module.

Repo rules that apply to every commit and PR:

- No `Co-Authored-By` trailer. End each commit message with the trailer `Assisted-by: AI`, and each PR body with `---` and `🤖 Built with AI assistance.`
- Markdown uses semantic line breaks (one clause per line) and "and" or "&", never "+", for "and".
- Never put a secret in a file, a log line or a command line.
- Do not change the live cluster except where a step says to: the first template creation (Task 5) and the acceptance workspace (Task 8).

## Deviations from the spec (decide when reviewing this plan)

The spec was written before I read the live cluster.
These points differ from it, each for a reason found in the repo.
Task 7 amends the spec to match, so approving this plan approves the amendments.

1. **No Loki in the workspace's egress.**
   `k8s/loki/netpol-loki.yaml` keeps Coder workspaces out of Loki on purpose, so a compromised workspace cannot read or purge logs.
   Opening it for the analyzer would undo that.
   The agent reads logs through the Grafana MCP server (datasource `loki`), which `k8s/claude-mcp/README.md` already documents.
2. **"`github.com:443`" becomes "public HTTPS, with every private range cut out".**
   A native NetworkPolicy matches IPs, not names, and k3s's controller has no FQDN support.
   The consequence: any public HTTPS host is reachable from the workspace, so a prompt-injected agent could send data to one.
   The workspace holds no secrets, but it can read cluster state.
   The stricter option is GitHub's published ranges (`gh api meta --jq '.git[]'` returns 60 today), kept current by a refresh job.
   That costs a periodic maintenance task and fails as a visible clone error when GitHub changes ranges.
   Choose it at review if the exposure matters more than the upkeep.
3. **The image is a tag of the base image's package, not a separate package, and it is pinned by full SHA.**
   Tags are `analyzer-<full commit SHA>` and `analyzer-latest` on `ghcr.io/nickvigilante/homelab-dev-templates`.
   A new package would start private and need a visibility change, while the existing one is already public and pullable without a secret.
   The Base template pins a full 40-character SHA today, not a short one.
4. **"Autostop after 30 idle minutes" is a deadline with an activity bump.**
   Coder has no pure idle timer: the template sets `--default-ttl 30m` and `--activity-bump 30m`.
   Whether an agent's tool calls count as activity is unknown, so Task 8 records what happens.
   The orchestrator's own idle stop stays the primary stop.
5. **Not in this plan: restricting the `alert-analyzer` Coder user to the `Analyzer` template.**
   That is a Phase 0 spike and part of the orchestrator plan.
   This plan only sets `--agents-allowed=true` on `Analyzer`.
6. **Decision gate, Task 9:** the `view` role cannot read nodes or PersistentVolumes.
   Node conditions matter for the failures this analyzer exists for (the 2026-09-27 incident was a node-level event).
   Task 9 adds a small extra read-only role for them.
   It is optional and separate, so skipping it changes nothing else.

## Amendment after `homelab-dev-templates#45` (2026-10-02)

This plan was written while a parallel thread built the same groundwork in `homelab-dev-templates`.
That work has merged, so parts of this plan are already done:

- **Multi-template CI exists.**
  [`homelab-dev-templates#45`](https://github.com/nickvigilante/homelab-dev-templates/pull/45) made `lint`, `template-validate` and `template-push` run once per affected template.
  `scripts/changed-templates.sh` picks the templates, and a change under `modules/workspace/` selects all of them.
  Task 4 below is rewritten to build on that instead of repeating it.
- **Every template needs a `template.json`.**
  `template-push` reads the icon from it, and `scripts/template-matrix.sh` fails both the PR check and the push when an affected template has none.
  Task 4 adds `templates/Analyzer/template.json`, which also carries the lifecycle flags that Step 4 used to hardcode.
- **`Base` now calls a shared module, `modules/workspace/`**, which CI copies into every template directory before validating or pushing it.
  The Analyzer stays standalone and does not call it: the module always includes the dotfiles script and the Claude Code module, both of which this plan forbids in the Analyzer.
  The copy CI makes in `templates/Analyzer/modules/workspace/` is unused, so it uploads with the template but is never loaded.
- **The cluster tools stay out of the base image.**
  `homelab-dev-templates#46`, which added `kubectl`, `helm` and `flux` to the base image, was closed in favor of this plan's `images/analyzer/`.
- **Correction to Task 3:** `templates/Base` does commit a `.terraform.lock.hcl`.
  Not committing one for the Analyzer is still fine, because nothing in CI requires it.

## Review Focus

The failure modes the spec implies and no happy-path test would catch, most likely first.
Each line names the task that pins it.

1. **The pod label does not match the NetworkPolicy, so the policy selects nothing and the workspace is unrestricted.**
   Nothing errors; the policy is simply inert.
   Pinned by Task 8: a pod-selection check, blocked-egress checks, and a Base workspace that must still reach what the analyzer cannot.
2. **The policy blocks something the agent needs, and the workspace shows Running but never connects.**
   DNS, the Coder access URL (two addresses) and the API server (a Service address and the node address it lands on) all have to be allowed.
   Pinned by Task 8: agent connected, `kubectl` works, the clone works.
3. **The ServiceAccount does not exist at first start.**
   `wait_for_rollout = false` hides a Deployment that never gets a pod.
   Pinned by Task 6 (check the account exists before any workspace) and Task 8 (the pod runs as `analyzer`).
4. **The clone fails: GitHub unreachable, a half-finished directory from a killed start, or a changed remote.**
   The script must stay bounded, clear the half-finished directory, re-point `origin`, and fail loudly without blocking login.
   Pinned by Task 3's test, which runs against a local repo and was checked by deleting the `set-url` line and watching it fail.
5. **The `view` role has gaps.**
   Nodes, PersistentVolumes and most CRDs are unreadable, so some analyses are partial.
   Pinned by Task 8, which records the answers, and by the optional Task 9.

Also worth a line each:

- `kubectl` must stay within one minor version of the API server (`KUBECTL_MINOR` is 1.35 against k3s v1.35.4).
  Task 2's CI check fails when it drifts, and the Dockerfile says to bump it with the cluster.
- A new template pushed by CI on a pull request would be created live before the PR merges.
  Task 5 therefore creates it by hand first.

## File Structure

`homelab-dev-templates`:

| File                                              | Responsibility                                                      |
| ------------------------------------------------- | ------------------------------------------------------------------- |
| `images/analyzer/Dockerfile`                      | The base image plus the six tools.                                  |
| `images/analyzer/tools.txt`                       | `flux` and `promtool`, in the base image's installer format.        |
| `images/analyzer/README.md`                       | What is in the image, how to build and bump it, the tag scheme.     |
| `.github/workflows/image-build-analyzer.yml`      | Build and smoke-test on PRs, publish on `main`.                     |
| `templates/Analyzer/main.tf`                      | The workspace: agent, PVC, Deployment as ServiceAccount `analyzer`. |
| `templates/Analyzer/clone-homelab.sh`             | Keeps `~/homelab` current.                                          |
| `templates/Analyzer/test-clone-homelab.sh`        | Tests the clone script against a local repo.                        |
| `templates/Analyzer/acceptance.sh`                | Checks run inside a live workspace.                                 |
| `templates/Analyzer/README.md`                    | What the template is, how it is confined, how it is operated.       |
| `templates/Analyzer/template.json`                | Icon and lifecycle flags, which `template-push.yml` applies.        |
| `.github/workflows/lint.yml`, `template-push.yml` | Run the clone test, and apply `template.json`'s `edit_flags`.       |
| `README.md`                                       | Layout list gains the two new directories.                          |

`homelab`:

| File                                                         | Responsibility                                                                                          |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| `k8s/coder/rbac.yaml`                                        | ServiceAccount `analyzer` and its `view` binding. Task 9 appends a node and PersistentVolume read role. |
| `k8s/coder/netpol-analyzer.yaml`                             | The egress allowlist for Analyzer pods.                                                                 |
| `k8s/coder/kustomization.yaml`                               | Lists the two new files.                                                                                |
| `k8s/coder/README.md`                                        | A section on Analyzer workspace access.                                                                 |
| `docs/superpowers/specs/2026-09-30-alert-analyzer-design.md` | Amended for the deviations above.                                                                       |

## Order of work

```text
Task 1 -> Task 2 (PR 1, homelab-dev-templates)  -> merge -> image tag exists
                                                       |
Task 3 -> Task 4 -> Task 5 -> open PR 2 (needs the tag from Task 2)
Task 6 -> Task 7 (PR 3, homelab; independent of PR 1 and 2) -> merge -> Flux applies
Task 8 needs all three merged; Task 9 is optional and follows it.
```

PR 3 does not depend on the other two, so it can run in parallel with them.
The first Analyzer workspace must not be created until PR 3 has reconciled.

______________________________________________________________________

## PR 1 (`homelab-dev-templates`): the analyzer image

Branch `analyzer-image`, from a fresh `origin/main`, in a worktree at `.worktrees/analyzer-image`.

### Task 1: The image directory

**Files:**

- Create: `images/analyzer/Dockerfile`
- Create: `images/analyzer/tools.txt`
- Create: `images/analyzer/README.md`
- Modify: `README.md` (layout list)

**Interfaces:**

- Consumes: the base image tag `ghcr.io/nickvigilante/homelab-dev-templates:c4c96c8988a5300e875db67cf953bb3e62efcb68` (the one `templates/Base/main.tf` runs today) and `images/base/install-tools.sh`.

- Produces: an image with `kubectl` (1.35 line), `helm`, `flux`, `promtool`, `yq` and `jq` on `PATH` for the `coder` user.
  Build argument `KUBECTL_MINOR`, default `1.35`.
  Named build context `basectx` that must point at `images/base`.

- [ ] **Step 1: Make the worktree**

```bash
cd ~/git/nickvigilante/homelab-dev-templates
git fetch -q && git worktree add -q .worktrees/analyzer-image -b analyzer-image origin/main
cd .worktrees/analyzer-image
```

- [ ] **Step 2: Write `images/analyzer/Dockerfile`**

The upstream URLs and asset names below were checked against the live endpoints when this plan was written: `dl.k8s.io/release/stable-1.35.txt` and its `.sha256`, `get-helm-4`, the `yq_linux_<arch>` release assets, and the `flux2` and `prometheus` tarball names.
`get-helm-4` and not `get-helm-3`, because the cluster is managed with Helm 4 and the `3` script installs Helm 3.
The `openssl` package is needed because the helm installer refuses to verify its checksum without it.

```dockerfile
# syntax=docker/dockerfile:1
# Read-only cluster analyzer image: the base image plus the Kubernetes tools the
# agent's shell needs. It adds no secrets and no personal configuration.
#
# Build it with the base image's installer script as a named context:
#   docker buildx build --build-context basectx=../base \
#     --secret id=github_token,env=GITHUB_TOKEN .
# A plain `docker build` fails at the `COPY --from=basectx` below.

# Pinned to the commit the Base template runs today, never :latest, so this
# image changes only when this line does.
ARG BASE_IMAGE=ghcr.io/nickvigilante/homelab-dev-templates:c4c96c8988a5300e875db67cf953bb3e62efcb68
FROM ${BASE_IMAGE}

# kubectl supports one minor version of skew against the API server. The
# cluster runs k3s v1.35.x, so track the 1.35 line. Bump this with the cluster.
ARG KUBECTL_MINOR=1.35

# Without pipefail a failed `curl | bash` would exit 0 and the build would
# carry on with a half-installed tool.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# hadolint ignore=DL3066
USER root

# jq from apt. openssl is for the helm installer, which refuses to run its
# checksum verification without the openssl binary.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends jq openssl \
    && rm -rf /var/lib/apt/lists/*

# kubectl from dl.k8s.io, checksum-verified. dpkg's architecture names (amd64,
# arm64) match the ones dl.k8s.io uses.
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    ver="$(curl -fsSL "https://dl.k8s.io/release/stable-${KUBECTL_MINOR}.txt")"; \
    curl -fsSLo /tmp/kubectl "https://dl.k8s.io/release/${ver}/bin/linux/${arch}/kubectl"; \
    curl -fsSLo /tmp/kubectl.sha256 "https://dl.k8s.io/release/${ver}/bin/linux/${arch}/kubectl.sha256"; \
    echo "$(cat /tmp/kubectl.sha256)  /tmp/kubectl" | sha256sum --check; \
    install -m 0755 /tmp/kubectl /usr/local/bin/kubectl; \
    rm -f /tmp/kubectl /tmp/kubectl.sha256

# helm from its official installer. get-helm-4, not get-helm-3: the cluster is
# managed with Helm 4 and the "3" script installs the latest Helm 3.
RUN curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4 \
    | HELM_INSTALL_DIR=/usr/local/bin USE_SUDO=false bash

# yq (mikefarah) as a raw binary. Its tarball holds yq_linux_<arch>, not `yq`,
# so install-tools.sh cannot place it.
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSLo /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_${arch}"; \
    chmod 0755 /usr/local/bin/yq

# flux and promtool from GitHub releases, with the base image's installer. See
# tools.txt. GITHUB_TOKEN is a BuildKit secret, never baked into a layer.
COPY tools.txt /tmp/tools.txt
# basectx is a named build context, not a FROM stage.
# hadolint ignore=DL3022
COPY --from=basectx install-tools.sh /tmp/install-tools.sh
RUN --mount=type=secret,id=github_token \
    sh -c 'export GITHUB_TOKEN="$(cat /run/secrets/github_token 2>/dev/null || true)"; \
           chmod +x /tmp/install-tools.sh && /tmp/install-tools.sh /tmp/tools.txt' \
    && rm -f /tmp/tools.txt /tmp/install-tools.sh

# hadolint ignore=DL3066
USER coder
WORKDIR /home/coder
ENV USER=coder
```

- [ ] **Step 3: Write `images/analyzer/tools.txt`**

```text
# tools.txt -- GitHub-release tools for the analyzer image, installed by the
# base image's install-tools.sh (the Dockerfile passes it in as a named build
# context). Format: binary_name|owner/repo|asset_template. The placeholders are
# the ones documented at the top of images/base/tools.txt.
#
# kubectl, helm, yq and jq are not here: none of them fits this format (see the
# comments in the Dockerfile). promtool ships inside the Prometheus tarball, so
# the whole tree lands in /opt/promtool and only promtool is put on PATH.
flux|fluxcd/flux2|flux_{VER}_linux_{ARCH_SHORT}.tar.gz
promtool|prometheus/prometheus|prometheus-{VER}.linux-{ARCH_SHORT}.tar.gz
```

- [ ] **Step 4: Lint the Dockerfile**

Run: `hadolint images/analyzer/Dockerfile && echo ok`
Expected: `ok`, with no findings.
The two `# hadolint ignore` comments carry over the base image's `DL3066` ignore and add `DL3022` for the named context.

- [ ] **Step 5: Write `images/analyzer/README.md`**

````markdown
# analyzer image

The base image plus the tools the read-only alert analyzer's shell needs.
It is used only by the `Analyzer` template (`templates/Analyzer/`), and it holds no secrets and no personal configuration.

## What is in it

| Tool | Source | Notes |
| --- | --- | --- |
| `kubectl` | `dl.k8s.io`, checksum-verified | The 1.35 line, set by `KUBECTL_MINOR`. |
| `helm` | `get-helm-4` installer | Helm 4, matching how the cluster is managed. |
| `flux` | GitHub release, via `tools.txt` | Latest at build time. |
| `promtool` | Prometheus release, via `tools.txt` | The whole tarball lands in `/opt/promtool`; only `promtool` is on `PATH`. |
| `yq` | mikefarah/yq release binary | Not `tools.txt`: its tarball has no file called `yq`. |
| `jq` | apt | |

## Tags

Published on merge to `main` as tags of the base image's package, `ghcr.io/nickvigilante/homelab-dev-templates`:

- `analyzer-<full commit SHA>`: what the template pins.
- `analyzer-latest`: convenience only, never referenced by the template.

Amd64 only, because the template pins its pod to amd64.

## Building locally

The build needs the base image's installer script as a named context, and a token to raise GitHub's API rate limit:

```bash
cd images/analyzer
docker buildx build --build-context basectx=../base \
  --secret id=github_token,env=GITHUB_TOKEN -t analyzer:local .
```

A plain `docker build` fails at the `COPY --from=basectx` line.

## Bumping

- **kubectl:** when the cluster's minor version changes, change `KUBECTL_MINOR` in the Dockerfile and the expected minor in `.github/workflows/image-build-analyzer.yml`. kubectl supports one minor of skew against the API server.
- **Base image:** change the `BASE_IMAGE` default to a newer base SHA.
- **After a publish:** put the new `analyzer-<sha>` tag in `templates/Analyzer/main.tf`, because a freshly built image is not used until the template's pinned string changes.
````

- [ ] **Step 6: Add the two directories to the layout list in the root `README.md`**

In the `## Layout` list, after the `images/base/` bullet, add:

```markdown
- `templates/Analyzer/` — the read-only alert-analyzer workspace template.
  Holds no secrets; confined by a ServiceAccount and a NetworkPolicy defined in the `homelab` repo.
- `images/analyzer/` — the base image plus `kubectl`, `helm`, `flux`, `promtool`, `yq` and `jq`, used only by the Analyzer template.
```

- [ ] **Step 7: Run the repo's lint**

Run: `pre-commit run --files images/analyzer/Dockerfile images/analyzer/tools.txt images/analyzer/README.md README.md`
Expected: every hook passes (mdformat may rewrite the README table; re-run once).

- [ ] **Step 8: If Docker is available, build locally**

Run: `cd images/analyzer && docker buildx build --build-context basectx=../base --secret id=github_token,env=GITHUB_TOKEN -t analyzer:local . && docker run --rm analyzer:local sh -c 'kubectl version --client && helm version --short && flux --version && promtool --version && yq --version && jq --version'`
Expected: six version lines, with kubectl on the 1.35 line.
If Docker is not available, skip this step; Task 2's CI does the same build.

- [ ] **Step 9: Commit**

```bash
git add images/analyzer README.md
git commit -m "feat(analyzer): add the read-only analyzer image" \
  -m "The Analyzer template needs kubectl, helm, flux, promtool, yq and jq in its shell. Layer them onto the pinned base image rather than adding them to the base, so Base workspaces do not grow tools they do not use. Amd64 only, because the template is pinned to amd64." \
  -m "Assisted-by: AI"
```

### Task 2: Build, smoke-test and publish in CI

**Files:**

- Create: `.github/workflows/image-build-analyzer.yml`

**Interfaces:**

- Consumes: Task 1's `images/analyzer/` and the named context `basectx=./images/base`.

- Produces: on merge to `main`, the tags `ghcr.io/nickvigilante/homelab-dev-templates:analyzer-<github.sha>` and `...:analyzer-latest`.
  Task 3 pins the first of these.

- [ ] **Step 1: Write the workflow**

It follows `image-build.yml`'s shape (an always-running `changes` job and `if`-gated work), minus the arm64 leg and the manifest merge.
The smoke tests prove the six tools exist, that kubectl is on the 1.35 line, and that no kubeconfig or secret-looking variable was baked in.

```yaml
name: image-build-analyzer

# The read-only analyzer image (images/analyzer). Amd64 only: the Analyzer
# template pins its pod to amd64, so there is no arm64 build and no manifest
# merge, unlike image-build.yml.
#
# The image is published as a tag of the same GHCR package as the base image,
# analyzer-<full commit SHA> and analyzer-latest, so it inherits that package's
# public visibility and the cluster needs no pull secret.
#
# Not a required check, but it keeps image-build.yml's shape anyway: the
# workflow always runs and a path filter gates the work, so adding it to the
# required checks later cannot leave a PR waiting on a status that never comes.
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

permissions:
  contents: read
  packages: write

jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      image: ${{ steps.filter.outputs.image }}
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            image:
              - 'images/analyzer/**'
              - 'images/base/install-tools.sh'
              - '.github/workflows/image-build-analyzer.yml'

  validate:
    name: validate (analyzer)
    needs: changes
    if: github.event_name == 'pull_request' && needs.changes.outputs.image == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - name: Build (load, no push)
        uses: docker/build-push-action@v6
        with:
          context: images/analyzer
          build-contexts: |
            basectx=./images/base
          platforms: linux/amd64
          push: false
          load: true
          tags: analyzer:validate
          secrets: |
            github_token=${{ secrets.GITHUB_TOKEN }}
          cache-from: type=gha,scope=analyzer-amd64
      - name: Verify the tools landed
        run: |
          docker run --rm analyzer:validate sh -c \
            'kubectl version --client && helm version --short && flux --version \
             && promtool --version && yq --version && jq --version'
      - name: Verify kubectl is within one minor of the cluster
        run: |
          minor="$(docker run --rm analyzer:validate kubectl version --client -o json | jq -r '.clientVersion.minor')"
          # Keep in step with ARG KUBECTL_MINOR in images/analyzer/Dockerfile.
          [ "${minor}" = "35" ] || { echo "::error::kubectl minor is ${minor}, expected 35"; exit 1; }
      - name: Verify no credentials were baked in
        run: |
          set -euo pipefail
          if docker run --rm analyzer:validate sh -c 'test -e "$HOME/.kube/config" -o -e "$HOME/.kube/homelab.yaml"'; then
            echo "::error::a kubeconfig is present in the image"
            exit 1
          fi
          leaked_env="$(docker run --rm analyzer:validate sh -c 'env' | grep -i -E 'token|secret|password' || true)"
          if [ -n "${leaked_env}" ]; then
            echo "::error::secret-looking environment variables: ${leaked_env}"
            exit 1
          fi
          echo "OK -- no kubeconfig and no secret-looking environment variables."

  build:
    name: build and push (analyzer)
    needs: changes
    if: github.event_name == 'push' && needs.changes.outputs.image == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: images/analyzer
          build-contexts: |
            basectx=./images/base
          platforms: linux/amd64
          push: true
          tags: |
            ghcr.io/nickvigilante/homelab-dev-templates:analyzer-${{ github.sha }}
            ghcr.io/nickvigilante/homelab-dev-templates:analyzer-latest
          secrets: |
            github_token=${{ secrets.GITHUB_TOKEN }}
          cache-from: type=gha,scope=analyzer-amd64
          cache-to: type=gha,mode=max,scope=analyzer-amd64
```

- [ ] **Step 2: Lint it**

Run: `yamllint -c .github/yamllint.yml .github/workflows/image-build-analyzer.yml && actionlint -config-file .github/actionlint.yaml .github/workflows/image-build-analyzer.yml && echo ok`
Expected: `ok`.

- [ ] **Step 3: Commit, push and open PR 1**

```bash
git add .github/workflows/image-build-analyzer.yml
git commit -m "ci(analyzer): build, smoke-test and publish the analyzer image" \
  -m "Mirrors image-build.yml without the arm64 leg. The image is published as analyzer-<sha> and analyzer-latest tags of the base image's package, so it is public from the start and the cluster needs no pull secret. A PR smoke test checks the tools, the kubectl minor and that no credentials were baked in." \
  -m "Assisted-by: AI"
git push -u origin analyzer-image
gh pr create --base main --head analyzer-image --title "feat(analyzer): add the read-only analyzer image" --body-file - <<'EOF'
## Summary

- Adds `images/analyzer/`: the base image plus `kubectl`, `helm`, `flux`, `promtool`, `yq` and `jq`.
- Adds a workflow that builds and smoke-tests it on PRs and publishes `analyzer-<sha>` on merge.
- Part of the alert analyzer (homelab spec 2026-09-30); the template that uses it is the next PR.

## Test plan

- [ ] `validate (analyzer)` passes on this PR, including the kubectl-minor and no-credentials checks.
- [ ] After merge, the `build and push (analyzer)` job publishes both tags.
- [ ] The new tag pulls anonymously (see the plan's Task 2, step 5).

---
🤖 Built with AI assistance.
EOF
```

- [ ] **Step 4: Verify the PR run, then merge**

Run: `gh pr checks --watch`
Expected: `validate (analyzer)` succeeds, along with `lint`.
The first run builds without cache and is slow.
If `validate (analyzer)` fails on a download, read the log: the likely culprits are a changed upstream asset name or the GitHub API rate limit.
Merge the PR.

- [ ] **Step 5: Confirm the publish and that the tag is public**

```bash
SHA="$(gh run list --workflow image-build-analyzer --branch main --status success --limit 1 --json headSha -q '.[0].headSha')"
echo "${SHA}"
gh run list --workflow image-build-analyzer --branch main --limit 1
TOKEN="$(curl -fsS 'https://ghcr.io/token?scope=repository:nickvigilante/homelab-dev-templates:pull' | jq -r .token)"
curl -fsSI -H "Authorization: Bearer ${TOKEN}" -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json' \
  "https://ghcr.io/v2/nickvigilante/homelab-dev-templates/manifests/analyzer-${SHA}" | head -1
```

Expected: the run is `completed success`, and the last command prints `HTTP/2 200` with an anonymous token.
Task 3 derives the same value with the same `gh run list` command, so nothing needs copying by hand.

______________________________________________________________________

## PR 2 (`homelab-dev-templates`): the Analyzer template

Branch `analyzer-template`, from a fresh `origin/main` that includes PR 1, in a worktree at `.worktrees/analyzer-template`.

### Task 3: The template, the clone script and their test

**Files:**

- Create: `templates/Analyzer/main.tf`
- Create: `templates/Analyzer/clone-homelab.sh`
- Create: `templates/Analyzer/test-clone-homelab.sh`
- Create: `templates/Analyzer/acceptance.sh`
- Create: `templates/Analyzer/README.md`

**Interfaces:**

- Consumes: the image tag `analyzer-<SHA>` from Task 2, and the ServiceAccount `analyzer` that Task 6 creates.

- Produces: pods labelled `app.kubernetes.io/name=coder-workspace` and `app.kubernetes.io/component=analyzer`, running as ServiceAccount `analyzer`.
  Task 6's NetworkPolicy selects on those two labels.
  A workspace-side checkout at `~/homelab`.

- [ ] **Step 1: Make the worktree**

```bash
cd ~/git/nickvigilante/homelab-dev-templates
git fetch -q && git worktree add -q .worktrees/analyzer-template -b analyzer-template origin/main
cd .worktrees/analyzer-template && mkdir -p templates/Analyzer
```

- [ ] **Step 2: Write the clone script's test first**

The test stands a local bare repo in for GitHub.
It covers a first clone, an update that discards local edits, an unreachable remote on an existing checkout, recovery from a half-finished directory, and an unreachable remote on a fresh start.

`templates/Analyzer/test-clone-homelab.sh`:

```sh
#!/usr/bin/env sh
# Test clone-homelab.sh against a local repo standing in for GitHub. Needs only
# git and a POSIX shell. Run it from anywhere: sh templates/Analyzer/test-clone-homelab.sh
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

# A bare "remote" with one commit on main.
git init --quiet --bare --initial-branch=main "${TMP}/remote.git"
git clone --quiet "${TMP}/remote.git" "${TMP}/work" 2>/dev/null
git -C "${TMP}/work" -c user.name=t -c user.email=t@example.invalid \
  commit --quiet --allow-empty -m first
echo "one" >"${TMP}/work/CLAUDE.md"
git -C "${TMP}/work" add CLAUDE.md
git -C "${TMP}/work" -c user.name=t -c user.email=t@example.invalid commit --quiet -m two
git -C "${TMP}/work" push --quiet origin HEAD:main

run() {
  HOME="${TMP}/home" HOMELAB_REPO_URL="$1" sh "${HERE}/clone-homelab.sh"
}
mkdir -p "${TMP}/home"

# 1. First start clones.
run "file://${TMP}/remote.git" >/dev/null 2>&1 || fail "first start should clone"
[ -f "${TMP}/home/homelab/CLAUDE.md" ] || fail "clone is missing CLAUDE.md"

# 2. A later start picks up a new commit and discards local edits.
echo "two" >"${TMP}/work/CLAUDE.md"
git -C "${TMP}/work" -c user.name=t -c user.email=t@example.invalid commit --quiet -am three
git -C "${TMP}/work" push --quiet origin HEAD:main
echo "local edit" >"${TMP}/home/homelab/CLAUDE.md"
run "file://${TMP}/remote.git" >/dev/null 2>&1 || fail "second start should update"
[ "$(cat "${TMP}/home/homelab/CLAUDE.md")" = "two" ] || fail "update did not reach origin/main"

# 3. An unreachable remote on an existing checkout fails loudly and keeps the
#    stale checkout.
if run "file://${TMP}/does-not-exist.git" >"${TMP}/out" 2>&1; then
  fail "unreachable remote should exit non-zero"
fi
grep -q 'ERROR' "${TMP}/out" || fail "failure should say ERROR"
[ -d "${TMP}/home/homelab/.git" ] || fail "stale checkout should survive a failed update"

# 4. A half-finished directory without .git is cleared and re-cloned.
rm -rf "${TMP}/home/homelab"
mkdir -p "${TMP}/home/homelab"
echo junk >"${TMP}/home/homelab/partial"
run "file://${TMP}/remote.git" >/dev/null 2>&1 || fail "should recover from a half-finished directory"
[ -d "${TMP}/home/homelab/.git" ] || fail "recovery did not leave a clone"

# 5. Unreachable remote on a fresh start: non-zero, no checkout left behind.
rm -rf "${TMP}/home/homelab"
if run "file://${TMP}/does-not-exist.git" >/dev/null 2>&1; then
  fail "fresh start with unreachable remote should exit non-zero"
fi

printf 'ok: clone-homelab.sh\n'
```

- [ ] **Step 3: Run it to see it fail**

Run: `sh templates/Analyzer/test-clone-homelab.sh`
Expected: it fails, because `clone-homelab.sh` does not exist yet (`sh: ... clone-homelab.sh: No such file`).

- [ ] **Step 4: Write `templates/Analyzer/clone-homelab.sh`**

Every git call is bounded by `timeout`, so a blocked network cannot hang a start.
It re-points `origin` before fetching, which makes `HOMELAB_REPO_URL` effective and heals a drifted remote.

```sh
#!/usr/bin/env sh
# Keep a read-only checkout of the public homelab repo at ~/homelab, so the
# analyzer can compare the manifests in git with what is running. Runs as
# coder_script.homelab_clone on every workspace start.
#
# No credentials: the repo is public and is fetched over anonymous HTTPS.
# HOMELAB_REPO_URL overrides the source; test-clone-homelab.sh uses it to point
# at a local repo.
#
# A failed fetch exits non-zero so Coder flags the run, but the script has
# start_blocks_login = false: the workspace is still usable, only the checkout
# is missing or stale. Every git call is bounded by `timeout`, so a blocked
# network cannot hang the start.
set -eu

REPO="${HOMELAB_REPO_URL:-https://github.com/nickvigilante/homelab.git}"
DEST="${HOME}/homelab"
export GIT_TERMINAL_PROMPT=0

log() { printf '[homelab-clone] %s\n' "$*"; }

# A previous attempt that died halfway leaves a directory without .git, which
# `git clone` refuses to write into. The directory is ours and holds nothing
# worth keeping, so clear it.
if [ -e "${DEST}" ] && [ ! -d "${DEST}/.git" ]; then
  rm -rf "${DEST}"
fi

if [ -d "${DEST}/.git" ]; then
  log "updating ${DEST}"
  # Re-point origin first, so a changed or overridden REPO takes effect.
  git -C "${DEST}" remote set-url origin "${REPO}"
  if timeout 120 git -C "${DEST}" fetch --quiet --depth 1 origin main &&
    git -C "${DEST}" reset --quiet --hard origin/main; then
    log "up to date at $(git -C "${DEST}" rev-parse --short HEAD)"
    exit 0
  fi
else
  log "cloning ${REPO}"
  if timeout 120 git clone --quiet --depth 1 --branch main "${REPO}" "${DEST}"; then
    log "cloned at $(git -C "${DEST}" rev-parse --short HEAD)"
    exit 0
  fi
fi

log "ERROR: could not reach ${REPO}. The workspace is usable, but ${DEST} is missing or stale."
exit 1
```

- [ ] **Step 5: Run the test to see it pass**

Run: `sh templates/Analyzer/test-clone-homelab.sh`
Expected: `ok: clone-homelab.sh`

- [ ] **Step 6: Prove the test bites**

```bash
M="$(mktemp -d)" && cp templates/Analyzer/clone-homelab.sh templates/Analyzer/test-clone-homelab.sh "$M"/
sed -i '/remote set-url origin/d' "$M/clone-homelab.sh"
sh "$M/test-clone-homelab.sh"; echo "exit=$?"; rm -rf "$M"
```

Expected: `FAIL: unreachable remote should exit non-zero` and a non-zero exit.
(Use `cp` on a throwaway copy as above; do not run a mutated script in place.)

- [ ] **Step 7: Write `templates/Analyzer/main.tf`**

It is derived from `templates/Base/main.tf`, minus the parameters, the git identity, the dotfiles script and the Claude Code module (`modules.tf`).
`IMAGE_SHA` is replaced in the next step.

```hcl
terraform {
  required_providers {
    coder = {
      source = "coder/coder"
    }
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
  }
}

provider "coder" {}

# The Coder provisioner runs in-cluster, so the provider picks up its identity.
provider "kubernetes" {}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

# The read-only analyzer. Unlike Base it has no parameters, no dotfiles, no
# Claude Code module and no stored secrets: a workspace driven by an LLM that
# reads logs and alert text must not hold credentials it does not need. Its only
# identity is the in-cluster ServiceAccount `analyzer`, bound to the built-in
# `view` ClusterRole, which cannot read Secrets (k8s/coder/rbac.yaml).

locals {
  # Everything the Base template puts on its resources, so Coder can find them.
  workspace_labels = {
    "app.kubernetes.io/name"     = "coder-workspace"
    "app.kubernetes.io/instance" = "coder-workspace-${data.coder_workspace.me.id}"
    "app.kubernetes.io/part-of"  = "coder"
    "com.coder.resource"         = "true"
    "com.coder.workspace.id"     = data.coder_workspace.me.id
    "com.coder.workspace.name"   = data.coder_workspace.me.name
    "com.coder.user.id"          = data.coder_workspace_owner.me.id
    "com.coder.user.username"    = data.coder_workspace_owner.me.name
  }

  # Added to the pod only, not to the Deployment selector. This is what the
  # NetworkPolicy in k8s/coder/netpol-analyzer.yaml selects on, so Base
  # workspaces, which share the labels above, are never restricted by it.
  pod_labels = merge(local.workspace_labels, {
    "app.kubernetes.io/component" = "analyzer"
  })
}

resource "coder_agent" "main" {
  os   = "linux"
  arch = "amd64"

  metadata {
    display_name = "CPU Usage"
    key          = "0_cpu_usage"
    script       = "coder stat cpu"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "RAM Usage"
    key          = "1_ram_usage"
    script       = "coder stat mem"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "Home Disk"
    key          = "3_home_disk"
    script       = "coder stat disk --path $${HOME}"
    interval     = 60
    timeout      = 1
  }
}

# Keeps a read-only checkout of the public homelab repo at ~/homelab. See
# clone-homelab.sh. start_blocks_login is stated, not inherited: a failed clone
# must never keep anyone out of the workspace.
resource "coder_script" "homelab_clone" {
  agent_id           = coder_agent.main.id
  display_name       = "Clone homelab repo"
  icon               = "/icon/git.svg"
  run_on_start       = true
  start_blocks_login = false
  script             = file("${path.module}/clone-homelab.sh")
}

resource "kubernetes_persistent_volume_claim_v1" "home" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-home"
    namespace = "coder"
    labels = merge(local.workspace_labels, {
      "app.kubernetes.io/name"     = "coder-pvc"
      "app.kubernetes.io/instance" = "coder-pvc-${data.coder_workspace.me.id}"
    })
    annotations = {
      "com.coder.user.email" = data.coder_workspace_owner.me.email
    }
  }
  wait_until_bound = false
  spec {
    access_modes = ["ReadWriteOnce"]
    resources {
      requests = {
        storage = "5Gi"
      }
    }
  }
}

resource "kubernetes_deployment_v1" "main" {
  count = data.coder_workspace.me.start_count
  depends_on = [
    kubernetes_persistent_volume_claim_v1.home
  ]
  wait_for_rollout = false
  metadata {
    name      = "coder-${data.coder_workspace.me.id}"
    namespace = "coder"
    labels    = local.workspace_labels
    annotations = {
      "com.coder.user.email" = data.coder_workspace_owner.me.email
    }
  }

  spec {
    replicas = 1
    selector {
      match_labels = local.workspace_labels
    }
    strategy {
      type = "Recreate"
    }

    template {
      metadata {
        labels = local.pod_labels
      }
      spec {
        # The ServiceAccount must already exist (k8s/coder/rbac.yaml). If it
        # does not, the Deployment is created but never gets a pod, and
        # wait_for_rollout = false hides that: the workspace shows as started
        # and the agent simply never connects.
        service_account_name = "analyzer"

        # Stated rather than inherited: kubectl in the workspace authenticates
        # with this token, so it has to be mounted.
        automount_service_account_token = true

        # The agent binary the init script downloads is amd64 only, and this
        # cluster also has arm64 Pi nodes. See the note in the Base template.
        node_selector = {
          "kubernetes.io/arch" = "amd64"
        }

        security_context {
          run_as_user     = 1000
          fs_group        = 1000
          run_as_non_root = true
        }

        container {
          name = "dev"
          # Pinned by full commit SHA; see images/analyzer/README.md. The tag
          # is immutable, so IfNotPresent is safe, and it keeps a registry
          # outage from stopping an analysis that is already cached on the node.
          image             = "ghcr.io/nickvigilante/homelab-dev-templates:analyzer-IMAGE_SHA"
          image_pull_policy = "IfNotPresent"
          command           = ["sh", "-c", coder_agent.main.init_script]
          security_context {
            run_as_user = "1000"
          }
          env {
            name  = "CODER_AGENT_TOKEN"
            value = coder_agent.main.token
          }
          resources {
            requests = {
              "cpu"    = "250m"
              "memory" = "512Mi"
            }
            limits = {
              "cpu"    = "1"
              "memory" = "2Gi"
            }
          }
          volume_mount {
            mount_path = "/home/coder"
            name       = "home"
            read_only  = false
          }
        }

        volume {
          name = "home"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.home.metadata.0.name
            read_only  = false
          }
        }
      }
    }
  }
}
```

- [ ] **Step 8: Pin the image to the SHA from Task 2**

```bash
SHA="$(gh run list --repo nickvigilante/homelab-dev-templates --workflow image-build-analyzer --branch main --status success --limit 1 --json headSha -q '.[0].headSha')"
echo "${SHA}"
sed -i "s/analyzer-IMAGE_SHA/analyzer-${SHA}/" templates/Analyzer/main.tf
! rg -n 'IMAGE_SHA' templates/Analyzer/main.tf && echo "token replaced"
rg -n 'analyzer-[0-9a-f]{40}"' templates/Analyzer/main.tf
```

Expected: `token replaced`, then the image line with a 40-character SHA.

- [ ] **Step 9: Validate the template**

Run: `cd templates/Analyzer && tofu fmt -check -diff . && tofu init -input=false && tofu validate; rm -rf .terraform .terraform.lock.hcl; cd ../..`
Expected: `Success! The configuration is valid.`
(Do not commit `.terraform` or a lock file; `templates/Base` does not have them either.)

- [ ] **Step 10: Write `templates/Analyzer/acceptance.sh`**

It is run inside a workspace in Task 8, not in CI.

```bash
#!/usr/bin/env bash
# Acceptance checks for an Analyzer workspace. Run it INSIDE the workspace:
#   coder ssh <workspace> -- bash -s < templates/Analyzer/acceptance.sh
# It prints PASS or FAIL per check and exits non-zero if any FAIL. INFO lines
# record facts for the plan and never fail the run.
set -u

failures=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() {
  printf 'FAIL  %s\n' "$*"
  failures=$((failures + 1))
}
info() { printf 'INFO  %s\n' "$*"; }

# check "<name>" <command...>: passes when the command exits 0.
check() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then pass "${name}"; else fail "${name}"; fi
}
# check_not "<name>" <command...>: passes when the command exits non-zero.
check_not() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then fail "${name}"; else pass "${name}"; fi
}

# 1. The tools the image promises.
for tool in kubectl helm flux promtool yq jq; do
  check "tool present: ${tool}" command -v "${tool}"
done

# 2. Read access works and Secrets are denied.
check "kubectl get pods -A" kubectl get pods -A
check_not "kubectl get secrets -A is denied" kubectl get secrets -A
check_not "cannot create pods" kubectl auth can-i create pods --all-namespaces
for resource in nodes persistentvolumes; do
  answer="$(kubectl auth can-i get "${resource}" 2>&1 || true)"
  info "can get ${resource}: ${answer}"
done

# 3. The homelab checkout from the startup script.
check "homelab checkout exists" test -d "${HOME}/homelab/.git"
check "homelab checkout has CLAUDE.md" test -f "${HOME}/homelab/CLAUDE.md"

# 4. Allowed egress.
check "Grafana answers" curl -fsS -m 10 -o /dev/null \
  http://kps-grafana.monitoring.svc/api/health
check "Prometheus answers" curl -fsS -m 10 -o /dev/null \
  http://kps-kube-prometheus-stack-prometheus.monitoring.svc:9090/-/ready
check "public HTTPS answers" curl -fsS -m 10 -o /dev/null -I https://github.com

# 5. Blocked egress. Each of these is refused by the NetworkPolicy, so curl
#    times out rather than being told no.
check_not "Loki is blocked" curl -fsS -m 5 -o /dev/null \
  http://loki.monitoring.svc:3100/ready
check_not "Alertmanager is blocked" curl -fsS -m 5 -o /dev/null \
  http://kps-kube-prometheus-stack-alertmanager.monitoring.svc:9093/-/ready
check_not "public plain HTTP is blocked" curl -fsS -m 5 -o /dev/null http://example.com

printf '\n%s failure(s)\n' "${failures}"
[ "${failures}" -eq 0 ]
```

- [ ] **Step 11: Lint the scripts**

Run: `shellcheck -S info templates/Analyzer/*.sh && shfmt -d -i 2 -ci templates/Analyzer/*.sh && echo ok`
Expected: `ok`.

- [ ] **Step 12: Write `templates/Analyzer/README.md`**

````markdown
# Analyzer

A persistent, read-only Coder workspace for investigating the homelab cluster.
Alert-driven chats use it; see the `homelab` repo's `docs/superpowers/specs/2026-09-30-alert-analyzer-design.md`.

It is deliberately small:

- No parameters: 1 core, 2 GB, a 5 GiB home volume, pinned to amd64.
- No secrets, no dotfiles and no Claude Code module. Nothing in the pod can authenticate to anything but the cluster, as a role that cannot read Secrets.
- A read-only checkout of the public `homelab` repo at `~/homelab`, refreshed on every start by `clone-homelab.sh`.

## How it is confined

Both pieces live in the `homelab` repo, under `k8s/coder/`:

- `rbac.yaml`: the ServiceAccount `analyzer`, bound to the built-in `view` ClusterRole.
  `main.tf` runs the pod as it, so `kubectl` works and `kubectl get secrets` is forbidden.
- `netpol-analyzer.yaml`: an egress allowlist. It selects pods by `app.kubernetes.io/component: analyzer`, which `main.tf` puts on the pod. Change that label in one place and you must change it in the other, or the policy silently selects nothing.

## Lifecycle

The deadline, activity bump, dormancy and agent access are template settings, not Terraform.
`.github/workflows/template-push.yml` restates them on every push: a 30-minute deadline that each detected activity moves 30 minutes on, dormancy and auto-delete off, and Coder Agents allowed.

## First creation

CI updates this template but must not create it, because a pull-request validation would create it live before the PR merged.
Create it once by hand from a checkout:

```bash
coder templates push Analyzer --directory templates/Analyzer --yes
```

then set its settings with the same `coder templates edit Analyzer ...` flags that `template-push.yml` uses.

## Checking a workspace

```bash
coder create --template Analyzer analyzer-acceptance --yes
coder ssh analyzer-acceptance -- bash -s < templates/Analyzer/acceptance.sh
```

`acceptance.sh` prints PASS or FAIL per check.
The INFO lines record what the `view` role cannot read (nodes and PersistentVolumes).

## Updating the image

Put the new `analyzer-<sha>` tag in `main.tf` and push the template.
The tag is immutable and the pull policy is `IfNotPresent`, so a changed string is the only way a new image is used.
````

- [ ] **Step 13: Lint and commit**

```bash
pre-commit run --files templates/Analyzer/main.tf templates/Analyzer/clone-homelab.sh templates/Analyzer/test-clone-homelab.sh templates/Analyzer/acceptance.sh templates/Analyzer/README.md
git add templates/Analyzer
git commit -m "feat(analyzer): add the Analyzer workspace template" \
  -m "A persistent read-only workspace for the alert analyzer: no parameters, no secrets, no dotfiles, running as the analyzer ServiceAccount and labelled so a NetworkPolicy can confine it. A bounded startup script keeps a checkout of the public homelab repo, with a test that runs against a local repo." \
  -m "Assisted-by: AI"
```

### Task 4: Wire the Analyzer into the multi-template CI

**Files:**

- Create: `templates/Analyzer/template.json`
- Modify: `.github/workflows/lint.yml`
- Modify: `.github/workflows/template-push.yml`

**Interfaces:**

- Consumes: `homelab-dev-templates#45`'s CI: `scripts/changed-templates.sh` and `scripts/template-matrix.sh`, which put each `template.json` key on the push job as `matrix.<key>`, and the `Set the template icon` step in `template-push.yml`.
- Produces: `template.json` gains an optional `edit_flags` list, which `template-push.yml` passes to `coder templates edit` on every push. Templates without it, such as `Base`, are unaffected.

`lint.yml` already validates every template, and `template-validate.yml` and `template-push.yml` already validate and push every affected one, so neither of the latter needs a loop.
What is left is the Analyzer's own clone test, its metadata, and a way for `template.json` to carry flags beyond the icon.
Each edit below was applied to a copy of `main` after #45 and passed `actionlint`, `yamllint` and `yamlfmt -lint`.
Every flag was checked against the installed CLI (v2.37.3).

- [ ] **Step 1: Add `templates/Analyzer/template.json`**

The flags set a 30-minute deadline moved on by activity, turn dormancy, auto-delete and failure cleanup off, and allow Coder Agents.
Dormancy flags need the Premium license, which this deployment has.

```json
{
  "icon": "/emojis/1f50d.png",
  "edit_flags": [
    "--description", "Read-only cluster analyzer. Holds no secrets; see templates/Analyzer/README.md.",
    "--default-ttl", "30m",
    "--activity-bump", "30m",
    "--dormancy-threshold", "0s",
    "--dormancy-auto-deletion", "0s",
    "--failure-ttl", "0s",
    "--agents-allowed=true"
  ]
}
```

- [ ] **Step 2: `lint.yml`: run the clone test**

Insert this step immediately before `- name: tofu test (modules/workspace)`:

```yaml
      - name: Test the Analyzer clone script
        run: sh templates/Analyzer/test-clone-homelab.sh
```

- [ ] **Step 3: `template-push.yml`: apply `edit_flags`**

The in-cluster runner has no `jq`, so the `changes` job shell-quotes each template's `edit_flags` with `jq`'s `@sh`, and the push job unquotes them with `eval "set -- ..."`.
`template.json` comes from `main`, which is all this push-only workflow runs.
Apply this diff:

```diff
@@ -52,7 +52,10 @@
           BEFORE_SHA: ${{ github.event.before }}
         run: |
           set -euo pipefail
-          templates="$(scripts/template-matrix.sh "$BEFORE_SHA" HEAD)"
+          # edit_args is template.json's optional edit_flags list, shell-quoted
+          # by jq here because the in-cluster runner has no jq to unpack it.
+          templates="$(scripts/template-matrix.sh "$BEFORE_SHA" HEAD \
+            | jq -c 'map(.edit_args = ((.edit_flags // []) | @sh))')"
           echo "templates=${templates}" >> "$GITHUB_OUTPUT"
           echo "Affected templates: ${templates}"

@@ -108,13 +111,22 @@
       # dashboard. Any /emojis/*.png or /icon/*.svg path ships with Coder;
       # avoid the /icon/ ones named after a product, which read as that
       # product's template.
-      - name: Set the template icon
+      #
+      # Any other `coder templates edit` flags come from template.json's
+      # edit_flags list, such as lifecycle settings, which are template
+      # metadata too. The eval only unquotes what jq's @sh quoted: template.json
+      # comes from main, which is all this push-only workflow ever runs.
+      - name: Apply the template settings
         env:
           CODER_URL: ${{ secrets.CODER_URL }}
           CODER_SESSION_TOKEN: ${{ secrets.CODER_SESSION_TOKEN }}
           TEMPLATE: ${{ matrix.template }}
           ICON: ${{ matrix.icon }}
-        run: coder templates edit "$TEMPLATE" --icon "$ICON"
+          EDIT_ARGS: ${{ matrix.edit_args }}
+        run: |
+          set -euo pipefail
+          eval "set -- ${EDIT_ARGS}"
+          coder templates edit "$TEMPLATE" --icon "$ICON" "$@" --yes

       - name: Report the active version
         env:
```

- [ ] **Step 4: Check the flags survive the round trip**

Run from the repo root:

```bash
scripts/template-matrix.sh "$(git merge-base origin/main HEAD)" HEAD \
  | jq -c 'map(.edit_args = ((.edit_flags // []) | @sh))' \
  | jq -r '.[] | select(.template == "Analyzer") | .edit_args' \
  | EDIT_ARGS="$(cat)" bash -c 'eval "set -- ${EDIT_ARGS}"; printf "[%s]\n" "$@"'
```

Expected: 13 bracketed lines, one per flag or value, with the whole description on one line.

- [ ] **Step 5: Lint**

Run: `yamllint -c .github/yamllint.yml .github/workflows/ && actionlint -config-file .github/actionlint.yaml .github/workflows/*.yml && echo ok`
Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
git add templates/Analyzer/template.json .github/workflows
git commit -m "ci: deliver the Analyzer template with its settings" \
  -m "Adds the Analyzer's template.json and lets template.json carry extra coder templates edit flags, so its lifecycle settings are restated on every push the way icons already are. lint also runs the Analyzer's clone-script test." \
  -m "Assisted-by: AI"
```

### Task 5: Create the template in Coder, then open PR 2

**Files:** none (this task changes the live Coder deployment).

**Interfaces:**

- Consumes: Tasks 3 and 4 on the `analyzer-template` branch, and a Coder CLI logged in as the operator (`coder login https://coder.vigihome.net`).
- Produces: a template named `Analyzer` in Coder, so PR 2's `template-validate` run can push a non-activated version to something that already exists.

CI must not create the template: `coder templates push` creates a missing template, and a PR validation would then create it live before review.

- [ ] **Step 1: Create it from the branch**

Run: `coder templates push Analyzer --directory templates/Analyzer --yes`
Expected: the provisioner imports and plans the template and prints that it was created.
A failure here is the server-side validation that `tofu validate` cannot do (for example a bad image string); fix it on the branch.

- [ ] **Step 2: Apply the lifecycle settings**

```bash
coder templates edit Analyzer \
  --icon /emojis/1f50d.png \
  --description "Read-only cluster analyzer. Holds no secrets; see templates/Analyzer/README.md." \
  --default-ttl 30m \
  --activity-bump 30m \
  --dormancy-threshold 0s \
  --dormancy-auto-deletion 0s \
  --failure-ttl 0s \
  --agents-allowed=true \
  --yes
```

Expected: the command succeeds.
If the icon path is rejected, use `/emojis/1f9f1.png` and note it; the icon is cosmetic.

- [ ] **Step 3: Confirm**

Run: `coder templates list | rg -i analyzer`
Expected: one row for `Analyzer`.

- [ ] **Step 4: Push the branch and open PR 2**

```bash
git push -u origin analyzer-template
gh pr create --base main --head analyzer-template --title "feat(analyzer): add the Analyzer workspace template" --body-file - <<'EOF'
## Summary

- Adds `templates/Analyzer/`: a persistent, read-only workspace with no parameters, no secrets and no dotfiles, running as the `analyzer` ServiceAccount.
- Adds a bounded clone script for the public `homelab` repo, with a test.
- Teaches lint, template-validate and template-push to handle every template, and restates the Analyzer lifecycle settings on each push.
- The ServiceAccount, its `view` binding and the NetworkPolicy live in the homelab repo (a separate PR).

## Before merge

- [x] No secrets: the template has no variables, no parameters and no credentials.
- [ ] The Analyzer template was created by hand first (Coder CLI), so validation does not create it.

## Test plan

- [ ] `lint` passes, including `tofu validate` for both templates and the clone-script test.
- [ ] `template-validate` pushes a non-activated `Analyzer` version without error.
- [ ] After merge, `template-push` activates it and re-applies the settings.
- [ ] The acceptance run (the homelab plan's Task 8) passes.

---
🤖 Built with AI assistance.
EOF
```

- [ ] **Step 5: Verify and merge**

Run: `gh pr checks --watch`
Expected: `lint` and `template-validate` succeed.
Merge, then confirm `gh run list --workflow template-push --limit 1` is `completed success` and `coder templates show Analyzer` lists the `main-<sha>` version as active.

______________________________________________________________________

## PR 3 (`homelab`): access

Branch `analyzer-access`, from a fresh `origin/main`, in a worktree at `.worktrees/analyzer-access`.
It does not depend on PR 1 or PR 2.

### Task 6: ServiceAccount, `view` binding and NetworkPolicy

**Files:**

- Create: `k8s/coder/rbac.yaml`
- Create: `k8s/coder/netpol-analyzer.yaml`
- Modify: `k8s/coder/kustomization.yaml`

**Interfaces:**

- Consumes: the pod labels from Task 3, `app.kubernetes.io/name=coder-workspace` and `app.kubernetes.io/component=analyzer`.
- Produces: ServiceAccount `coder/analyzer`, ClusterRoleBinding `analyzer-view`, and NetworkPolicy `coder/analyzer-workspace-egress`.

Every name in the policy was read from the live cluster when this plan was written:

| Target           | Selector or address                                                                                                  | Port                                       |
| ---------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| CoreDNS          | `kube-system`, `k8s-app=kube-dns`                                                                                    | 53 UDP and TCP                             |
| Coder access URL | `192.168.50.135` and `100.92.2.25` (Pi-hole's two answers for `coder.vigihome.net`; Traefik fronts them)             | 443                                        |
| API server       | `10.43.0.1` (Service) and `192.168.50.135` (the endpoint it lands on)                                                | 443, 6443                                  |
| Prometheus       | `monitoring`, `app.kubernetes.io/name=prometheus`, `app.kubernetes.io/instance=kps-kube-prometheus-stack-prometheus` | 9090                                       |
| Grafana          | `monitoring`, `app.kubernetes.io/name=grafana`, `app.kubernetes.io/instance=kps`                                     | 3000 (the pod port behind Service port 80) |
| Public HTTPS     | `0.0.0.0/0` except `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, `169.254.0.0/16`                | 443                                        |

Policies are evaluated after Service address translation, so ports are pod ports.
k3s enforces NetworkPolicy with its bundled kube-router controller; `k8s/loki/netpol-loki.yaml` relies on the same thing.

- [ ] **Step 1: Make the worktree**

```bash
cd ~/git/nickvigilante/homelab
git fetch -q && git worktree add -q .worktrees/analyzer-access -b analyzer-access origin/main
cd .worktrees/analyzer-access
```

- [ ] **Step 2: Write `k8s/coder/rbac.yaml`**

The filename is one the kubeconform CI filter already covers (`rbac.yaml`).

```yaml
# Identity for the read-only alert-analyzer workspace (the Analyzer template).
# The workspace pod runs as this ServiceAccount, and kubectl inside it
# authenticates with the mounted token. The built-in `view` ClusterRole reads
# most namespaced objects and pod logs but not Secrets, which is the point. It
# also leaves out cluster-scoped objects such as nodes and PersistentVolumes.
#
# The ServiceAccount has to exist before the first Analyzer workspace starts:
# a Deployment that names a missing ServiceAccount never gets a pod.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: analyzer
  namespace: coder
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: analyzer-view
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view
subjects:
  - kind: ServiceAccount
    name: analyzer
    namespace: coder
```

- [ ] **Step 3: Write `k8s/coder/netpol-analyzer.yaml`**

The filename matches the filter's `netpol-*.yaml`.

```yaml
# Egress allowlist for Analyzer workspace pods, and only those.
#
# The selector needs the `app.kubernetes.io/component: analyzer` label that
# templates/Analyzer/main.tf puts on the pod. Base workspaces share every other
# workspace label but not this one, so they stay unrestricted. A selector that
# matches no pod fails open: the policy is silently inert. The acceptance run
# proves both directions (docs/superpowers/plans/2026-09-30-alert-analyzer-image-template-access.md).
#
# What is allowed, and why:
#   - DNS to CoreDNS.
#   - The Coder access URL, https://coder.vigihome.net. Pi-hole resolves it to
#     one of gandalf's two addresses (LAN or tailnet), which Traefik fronts on
#     443. Without this the agent never connects.
#   - The API server, for kubectl. 10.43.0.1:443 is the in-cluster Service and
#     192.168.50.135:6443 is where it lands after DNAT; the policy is evaluated
#     after DNAT, so the second is the one that matters.
#   - Prometheus (pod port 9090) and Grafana (pod port 3000) in `monitoring`.
#   - Public HTTPS (443) for the homelab clone, with every private range cut
#     out. A native NetworkPolicy cannot match github.com by name, so this is
#     wider than "GitHub only": any public HTTPS host is reachable.
#
# Deliberately NOT allowed: Loki. Its own policy (k8s/loki/netpol-loki.yaml)
# keeps Coder workspaces out on purpose; the agent reads logs through the
# Grafana MCP server instead.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: analyzer-workspace-egress
  namespace: coder
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: coder-workspace
      app.kubernetes.io/component: analyzer
  policyTypes:
    - Egress
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    - to:
        - ipBlock:
            cidr: 192.168.50.135/32
      ports:
        - protocol: TCP
          port: 443
        - protocol: TCP
          port: 6443
    - to:
        - ipBlock:
            cidr: 100.92.2.25/32
      ports:
        - protocol: TCP
          port: 443
    - to:
        - ipBlock:
            cidr: 10.43.0.1/32
      ports:
        - protocol: TCP
          port: 443
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: monitoring
          podSelector:
            matchLabels:
              app.kubernetes.io/name: prometheus
              app.kubernetes.io/instance: kps-kube-prometheus-stack-prometheus
      ports:
        - protocol: TCP
          port: 9090
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: monitoring
          podSelector:
            matchLabels:
              app.kubernetes.io/name: grafana
              app.kubernetes.io/instance: kps
      ports:
        - protocol: TCP
          port: 3000
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
              - 100.64.0.0/10
              - 169.254.0.0/16
      ports:
        - protocol: TCP
          port: 443
```

- [ ] **Step 4: Add both files to `k8s/coder/kustomization.yaml`**

The `resources:` list becomes:

```yaml
resources:
  - namespace.yaml
  - pv-pvc.yaml
  - postgres-helmrelease.yaml
  - helmrelease.yaml
  - rbac.yaml
  - netpol-analyzer.yaml
  - ../../sources/bitnami.yaml
  - ../../sources/coder.yaml
```

The `coder` Flux Kustomization has `prune: false`, so removing these files later will not delete the objects; delete them by hand.

- [ ] **Step 5: Validate**

```bash
kubeconform -strict -summary k8s/coder/rbac.yaml k8s/coder/netpol-analyzer.yaml
kubectl apply --dry-run=client -f k8s/coder/rbac.yaml -f k8s/coder/netpol-analyzer.yaml
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s/coder >/dev/null && echo kustomize-ok
pre-commit run --files k8s/coder/rbac.yaml k8s/coder/netpol-analyzer.yaml k8s/coder/kustomization.yaml
```

Expected: `3 resources found in 2 files - Valid: 3`, three `created (dry run)` lines, `kustomize-ok`, and every hook passing.

- [ ] **Step 6: Commit and open the PR after Task 7**

Do this together with Task 7 (one PR).

### Task 7: Docs and the spec amendments

**Files:**

- Modify: `k8s/coder/README.md` (append a section)
- Modify: `docs/superpowers/specs/2026-09-30-alert-analyzer-design.md` (three edits)

**Interfaces:** none.

- [ ] **Step 1: Append to `k8s/coder/README.md`**

```markdown

## Analyzer workspace access

The `Analyzer` template (in `homelab-dev-templates`) runs a read-only workspace for the alert analyzer.
Two files here define what it can touch:

- `rbac.yaml`: ServiceAccount `analyzer` bound to the built-in `view` ClusterRole.
  It reads most namespaced objects and pod logs, but not Secrets, nodes or PersistentVolumes.
- `netpol-analyzer.yaml`: an egress allowlist, selected by the pod labels `app.kubernetes.io/name=coder-workspace` and `app.kubernetes.io/component=analyzer`.
  Base workspaces do not carry the second label, so they are unrestricted.
  If the label in `templates/Analyzer/main.tf` and the selector here ever disagree, the policy selects nothing and the workspace is unrestricted, with no error.

The ServiceAccount must exist before an Analyzer workspace starts: a Deployment naming a missing account never gets a pod.

Loki is deliberately not in the allowlist, because `k8s/loki/netpol-loki.yaml` keeps workspaces out on purpose; logs go through the Grafana MCP server.
Public HTTPS is allowed with every private range excluded, because a native NetworkPolicy cannot match `github.com` by name.
```

- [ ] **Step 2: Amend the spec: the image sentence**

In `### Analyzer template and image`, replace:

```markdown
  It is published by the same GHCR CI and pinned by short SHA in the template.
```

with:

```markdown
  It is published by its own workflow as the tags `analyzer-<full commit SHA>` and `analyzer-latest` of the base image's package, and the template pins the SHA tag.
```

- [ ] **Step 3: Amend the spec: autostop**

Replace:

```markdown
- Autostop after 30 idle minutes is a backstop.
  Auto-delete is off.
```

with:

```markdown
- Autostop is a template deadline of 30 minutes that each detected activity extends by 30 minutes.
  It is a backstop for the orchestrator's own idle stop, not an idle timer.
  Auto-delete is off.
```

- [ ] **Step 4: Amend the spec: network**

Replace:

```markdown
- **Network:** a NetworkPolicy limits the workspace pod to DNS, the API server, Loki, Prometheus, Grafana and `github.com:443`.
```

with:

```markdown
- **Network:** a NetworkPolicy limits the workspace pod to DNS, the Coder access URL, the API server, Prometheus, Grafana, and public HTTPS with every private range excluded.
  Loki is deliberately absent: its own policy keeps Coder workspaces out, and the agent reads logs through the Grafana MCP server.
  A native NetworkPolicy cannot match `github.com` by name, so any public HTTPS host is reachable.
```

- [ ] **Step 5: Lint**

Run: `pre-commit run --files k8s/coder/README.md docs/superpowers/specs/2026-09-30-alert-analyzer-design.md`
Expected: every hook passes.

- [ ] **Step 6: Commit and open PR 3**

```bash
git add k8s/coder docs/superpowers/specs/2026-09-30-alert-analyzer-design.md
git commit -m "feat(coder): confine the read-only Analyzer workspace" \
  -m "Adds ServiceAccount analyzer bound to the built-in view role, and an egress NetworkPolicy selected by a pod label only Analyzer workspaces carry, so Base workspaces stay unrestricted. Loki is left out on purpose because its own policy keeps workspaces away, and public HTTPS is allowed minus private ranges because a native policy cannot match github.com by name. Amends the spec to match." \
  -m "Assisted-by: AI"
git push -u origin analyzer-access
gh pr create --base main --head analyzer-access --title "feat(coder): confine the read-only Analyzer workspace" --body-file - <<'EOF'
## Summary

- Adds ServiceAccount `coder/analyzer` bound to the built-in `view` ClusterRole, and an egress NetworkPolicy for Analyzer workspace pods only.
- Selected by the pod label `app.kubernetes.io/component=analyzer`, so Base workspaces are unaffected.
- Deliberate departures from the spec, now written into it: no Loki in the allowlist (its own policy keeps workspaces out), and public HTTPS minus private ranges instead of `github.com:443` (a native policy has no name matching).

## Before merge

- [x] No secrets in the diff.
- [ ] Backup wiring, SPOF, DNS: not applicable.
  The workspace's only dependency on this PR is that the ServiceAccount exists before it starts.

## Test plan

- [x] `kubeconform -strict`, `kubectl apply --dry-run=client` and `kubectl kustomize` pass locally.
- [ ] After merge and `flux reconcile kustomization coder`: `kubectl -n coder get sa analyzer` and `get netpol analyzer-workspace-egress` exist.
- [ ] `kubectl auth can-i get secrets -A --as=system:serviceaccount:coder:analyzer` is `no`, and `get pods -A` is `yes`.
- [ ] The acceptance run passes (the plan's Task 8).

---
🤖 Built with AI assistance.
EOF
```

- [ ] **Step 7: After merge, verify the objects exist and the role is right**

```bash
flux reconcile kustomization coder --with-source
kubectl -n coder get sa analyzer
kubectl -n coder get netpol analyzer-workspace-egress
kubectl auth can-i get pods -A --as=system:serviceaccount:coder:analyzer
kubectl auth can-i get secrets -A --as=system:serviceaccount:coder:analyzer
kubectl auth can-i get nodes --as=system:serviceaccount:coder:analyzer
```

Expected: the account and policy exist, then `yes`, `no`, `no`.
`can-i --as` is a read-only access review, so it checks the role before any workspace exists.
The `no` for nodes is the known `view` gap that Task 9 may close.

______________________________________________________________________

## Acceptance and the optional role

### Task 8: Prove it in a real workspace

**Files:** none (this task creates and deletes a workspace).

**Interfaces:**

- Consumes: all three PRs merged, and the policy and account from Task 6 reconciled.

- Produces: a recorded PASS or FAIL for each Review Focus item.

- [ ] **Step 1: Preconditions**

```bash
kubectl -n coder get sa analyzer && kubectl -n coder get netpol analyzer-workspace-egress
coder templates show Analyzer | head -5
```

Expected: both objects exist, and the template shows an active version.
Do not create the workspace if either object is missing (Review Focus 3).

- [ ] **Step 2: Create the workspace**

```bash
coder create --template Analyzer analyzer-acceptance --yes
coder list | rg analyzer-acceptance
```

Expected: the workspace reaches `Running` and the agent shows `Connected`.
If it stays `Running` with the agent disconnected, the policy is blocking something the agent needs: check DNS, `192.168.50.135:443` and `100.92.2.25:443` first (Review Focus 2).

- [ ] **Step 3: The policy selects exactly the Analyzer pod (Review Focus 1)**

```bash
kubectl -n coder get pods -l app.kubernetes.io/name=coder-workspace,app.kubernetes.io/component=analyzer --show-labels
kubectl -n coder get pod -l app.kubernetes.io/component=analyzer -o jsonpath='{.items[0].spec.serviceAccountName}{"\n"}'
```

Expected: exactly one pod, and `analyzer`.
Zero pods means the label in `main.tf` and the selector have diverged, and the policy is inert.

- [ ] **Step 4: Run the in-workspace checks**

Run: `coder ssh analyzer-acceptance -- bash -s < templates/Analyzer/acceptance.sh`
Expected: every `PASS`, no `FAIL`, and `0 failure(s)`.
Record the three `INFO` lines (`can get nodes`, `can get persistentvolumes`).

- [ ] **Step 5: The policy is scoped to Analyzer pods only (the negative control)**

From an existing running Base workspace (create one first if none is running), the traffic the analyzer cannot send must still work:

```bash
BASE_WS="$(coder list -o json | jq -r '[.[] | select(.template_name=="Base")][0].name')"
echo "${BASE_WS}"
coder ssh "${BASE_WS}" -- curl -fsS -m 10 -o /dev/null -w '%{http_code}\n' \
  http://kps-kube-prometheus-stack-alertmanager.monitoring.svc:9093/-/ready
```

Expected: `200`.
If this fails or times out, the policy is selecting Base pods and must be fixed before merge.

- [ ] **Step 6: Stop, start, and the deadline**

```bash
coder stop analyzer-acceptance --yes && coder start analyzer-acceptance --yes
coder ssh analyzer-acceptance -- 'git -C ~/homelab log -1 --format=%h'
coder list -o json | jq -r '.[] | select(.name=="analyzer-acceptance") | .latest_build.deadline'
```

Expected: the checkout survives the restart, and the deadline is about 30 minutes after the start.
Record whether running commands moves the deadline (the activity bump); the plan's deviation 4 depends on the answer.

- [ ] **Step 7: Clean up**

```bash
coder delete analyzer-acceptance --yes
kubectl -n coder get pvc | rg analyzer-acceptance || echo "no leftover PVC"
```

Expected: the workspace is gone and no PVC is left behind.

- [ ] **Step 8: Record the results**

Add the outcome, including the INFO lines and the deadline observation, to PR 3's description or a comment, so the next plan starts from facts.

### Task 9 (decision gate): read nodes and PersistentVolumes

Do this task only if the INFO lines from Task 8 show the gap matters, and if you accept widening the analyzer's reach to cluster-scoped, read-only objects.
Skipping it changes nothing above.

**Files:**

- Modify: `k8s/coder/rbac.yaml` (append two objects)

**Interfaces:**

- Consumes: ServiceAccount `coder/analyzer` from Task 6.
- Produces: ClusterRole `analyzer-extra-read` and its binding.

The objects go in the existing `rbac.yaml` because the kubeconform filter matches that exact filename, and a new file name would silently bypass validation.

- [ ] **Step 1: Append to `k8s/coder/rbac.yaml`**

```yaml
---
# Read-only access to the cluster-scoped objects the `view` role leaves out.
# Node conditions are the first thing to check for an infrastructure alert.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: analyzer-extra-read
rules:
  - apiGroups: [""]
    resources: ["nodes", "persistentvolumes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["storage.k8s.io"]
    resources: ["storageclasses"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: analyzer-extra-read
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: analyzer-extra-read
subjects:
  - kind: ServiceAccount
    name: analyzer
    namespace: coder
```

`kustomization.yaml` needs no change, because the file is already listed.

- [ ] **Step 2: Validate**

Run: `kubeconform -strict -summary k8s/coder/rbac.yaml && pre-commit run --files k8s/coder/rbac.yaml`
Expected: `Valid: 4` (the account, the first binding, the new role and its binding), and every hook passing.

- [ ] **Step 3: Commit, open a PR, and verify after merge**

```bash
git add k8s/coder/rbac.yaml
git commit -m "feat(coder): let the Analyzer read nodes and PersistentVolumes" \
  -m "The built-in view role omits cluster-scoped objects. Node conditions are the first thing to check for an infrastructure alert, so grant read-only access to nodes, PersistentVolumes and StorageClasses. Still no Secrets." \
  -m "Assisted-by: AI"
```

After merge and `flux reconcile kustomization coder`, run `kubectl auth can-i get nodes --as=system:serviceaccount:coder:analyzer` (expect `yes`) and `kubectl auth can-i get secrets -A --as=system:serviceaccount:coder:analyzer` (expect `no`).

______________________________________________________________________

## Self-review

**Spec coverage.**
Image with the six tools on the base image, published by CI: Tasks 1 and 2.
Persistent amd64 workspace, 1 core, 2 GB, small volume, no parameters: Task 3 (`main.tf`).
Startup clone of the public repo: Task 3 (`clone-homelab.sh` and its test).
Autostop backstop and auto-delete off: Tasks 4 and 5 (`coder templates edit`).
ServiceAccount `analyzer` on `view`: Task 6.
NetworkPolicy: Task 6, with the deviations stated and amended into the spec in Task 7.
No secrets in the workspace: Task 1 (image checks in CI), Task 3 (no parameters, no dotfiles, no modules), Task 8 (`acceptance.sh`).
`--agents-allowed`: Tasks 4 and 5.
Not covered here by design: the `alert-analyzer` Coder user and its template restriction, the orchestrator, and the Alertmanager route.

**Placeholder scan.**
The only substitution token is `analyzer-IMAGE_SHA` in `main.tf`, which Task 3 step 8 replaces with an exact command and then checks is gone.
No "TBD" or "similar to Task N".

**Consistency.**
The label `app.kubernetes.io/component=analyzer` is defined in Task 3 and selected in Task 6, and Task 8 step 3 checks they agree.
The ServiceAccount name `analyzer` is set in Task 3 and created in Task 6.
The tag `analyzer-<full SHA>` is produced in Task 2, pinned in Task 3, and described in the Task 1 README and the Task 7 spec edit.
`KUBECTL_MINOR` 1.35 appears in the Dockerfile and in the Task 2 minor check, and both say to change together.
The settings flags are identical in Tasks 4 and 5.

**Verified when this plan was written:** the Dockerfile lints with `hadolint`; the clone script and its test pass `shellcheck` and `shfmt`, the test passes, and it fails when the `set-url` line is removed; `main.tf` passes `tofu fmt` and `tofu validate` against the real providers; the manifests pass `kubeconform -strict`, `yamllint` and a client-side dry run; the workflows pass `yamllint` and `actionlint`; every Coder flag exists in the installed CLI; and all upstream URLs return 200.
**Not verified, and why:** no Docker here, so the image was not built; the NetworkPolicy has not been enforced on a pod; and no workspace has been created.
Tasks 2 and 8 exist to close exactly those gaps.
