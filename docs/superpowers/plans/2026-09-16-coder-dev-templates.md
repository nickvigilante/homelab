# homelab-dev-templates bootstrap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a new public repo, `homelab-dev-templates`, that source-controls the Coder `Base` workspace template (carrying forward the arch-scheduling fix already live as template version `panicked_lin22`) and a custom multi-arch base image with the user's dotfiles baked in at build time, wired to CI that validates both and auto-deploys on merge to `main`.

**Architecture:** One repo, two build artifacts (`templates/Base/` — Terraform pushed to Coder via CLI; `images/base/` — a Dockerfile built multi-arch and pushed to GHCR), four GitHub Actions workflows (lint, image build/publish, template push), tied together only by the template referencing the image by pinned SHA tag.

**Tech Stack:** OpenTofu (Terraform), Docker Buildx (multi-arch), GitHub Actions, GHCR, `coder` CLI, chezmoi, Homebrew (linuxbrew), pre-commit.

**Spec:** `docs/superpowers/specs/2026-09-16-coder-dev-templates-design.md`

## Global Constraints

- Repo: `nickvigilante/homelab-dev-templates`, public, MIT license.
- Only the `Base` template for now (renamed from `kubernetes`) — no `docker`/`scratch` migration in this plan.
- Repo layout: `templates/<name>/` and `images/base/`, per the spec's tree.
- Base image published to GHCR only, never Docker Hub.
- Template pins the base image by short-SHA tag — never `:latest`.
- **Nothing secret-dependent may be baked into the image** — it's public on GHCR. Any dotfile touching live secrets during `chezmoi apply` must be excluded for this build context.
- CI: plain pre-commit-based lint (no new tooling beyond what `homelab`/`infrastructure` already use), path-filtered image-publish workflow must **not** be a required branch-protection check.
- Commits: plain imperative-subject messages (no `release-please`/`commitlint`), matching `homelab`/`infrastructure` convention. No `Co-Authored-By: Claude …` trailer — end commit messages with `Assisted-by: AI` per the user's global git-attribution convention; PR bodies end with `---\n🤖 Built with AI assistance.`.
- `.terraform.lock.hcl` is committed, not ignored. `.terraform/` (provider cache) and any `*.tfplan` output are ignored.

______________________________________________________________________

### Task 1: Bootstrap the repo

**Files:**

- Create: `LICENSE`
- Create: `README.md`
- Create: `.gitignore`

**Interfaces:**

- Produces: the repo `nickvigilante/homelab-dev-templates` on GitHub, public, with `main` as the default branch and branch protection requiring PRs.

- [ ] **Step 1: Create the empty GitHub repo**

```bash
gh repo create nickvigilante/homelab-dev-templates --public \
  --description "Source-controlled Coder workspace templates + multi-arch base image for the homelab Coder deployment"
```

- [ ] **Step 2: Clone it and add the MIT license**

```bash
cd ~/git/nickvigilante
gh repo clone nickvigilante/homelab-dev-templates
cd homelab-dev-templates
```

Write `LICENSE`:

```
MIT License

Copyright (c) 2026 Nick Vigilante

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: Write `.gitignore`**

```
# Terraform
**/.terraform/
*.tfplan
crash.log
crash.*.log

# macOS
.DS_Store

# Editor
.vscode/
.idea/
*.swp
```

- [ ] **Step 4: Write a stub `README.md`**

```markdown
# homelab-dev-templates

Source-controlled Coder workspace templates and their base image for my
homelab Coder deployment at `coder.vigihome.net`.

## Layout

- `templates/Base/` — the base Kubernetes workspace template, pushed to
  Coder via CI on merge to `main`.
- `images/base/` — the custom multi-arch base image workspaces run on,
  published to `ghcr.io/nickvigilante/homelab-dev-templates` on merge to
  `main`.

See each directory's own README for details.
```

- [ ] **Step 5: Commit and push**

```bash
git add LICENSE README.md .gitignore
git commit -m "$(cat <<'EOF'
Initial repo scaffold

Assisted-by: AI
EOF
)"
git push -u origin main
```

- [ ] **Step 6: Add branch protection on `main`**

```bash
gh api -X PUT repos/nickvigilante/homelab-dev-templates/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  -f required_status_checks.strict=true \
  -f 'required_status_checks.contexts[]=lint' \
  -F required_pull_request_reviews.required_approving_review_count=0 \
  -F enforce_admins=false \
  -F required_linear_history=true \
  -F allow_force_pushes=false \
  -F allow_deletions=false \
  -F restrictions=null
```

This will fail right now because the `lint` status check doesn't exist yet — that's expected. Re-run this exact command again at the end of Task 4 once `lint.yml` exists and has run at least once (GitHub only accepts a context name into `required_status_checks` once it has appeared at least once on the repo, or you can add it via the web UI now and it'll simply show "Expected" until the first run). If the command fails here, skip it and re-run at the end of Task 4 instead — note that in your task tracking.

**Verify:** `gh repo view nickvigilante/homelab-dev-templates --json isPrivate,licenseInfo` should show `"isPrivate": false` and `"licenseInfo": {"key": "mit", ...}`.

______________________________________________________________________

### Task 2: Add pre-commit tooling

**Files:**

- Create: `.pre-commit-config.yaml`
- Create: `.yamlfmt`
- Create: `.github/yamllint.yml`

**Interfaces:**

- Produces: `pre-commit run --all-files` passing cleanly on the repo as it exists after this task — later tasks must keep it passing.

- [ ] **Step 1: Write `.pre-commit-config.yaml`**

Adapted from `homelab`'s config — same hooks, minus nothing (this repo has no k8s manifests, but every other hook type applies: Terraform, Dockerfile, shell, YAML, Markdown):

```yaml
# Pre-commit hooks for homelab-dev-templates. Mirrors the homelab repo's
# setup so local and CI can't drift (.github/workflows/lint.yml runs
# `pre-commit run --all-files`).
#
# One-time local setup:
#   brew install pre-commit yamlfmt shfmt shellcheck yamllint betterleaks hadolint
#   pre-commit install

repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: end-of-file-fixer
      - id: trailing-whitespace
      - id: mixed-line-ending
        args: [--fix=lf]
      - id: check-merge-conflict
      - id: check-added-large-files

  - repo: https://github.com/hukkin/mdformat
    rev: 0.7.22
    hooks:
      - id: mdformat
        args: [--wrap, keep, --number]
        additional_dependencies:
          - mdformat-tables

  - repo: local
    hooks:
      - id: yamlfmt
        name: yamlfmt (format YAML)
        entry: yamlfmt -conf .yamlfmt
        language: system
        files: \.ya?ml$
      - id: yamllint
        name: yamllint
        entry: yamllint -c .github/yamllint.yml
        language: system
        files: \.ya?ml$
      - id: shfmt
        name: shfmt (format shell)
        entry: shfmt -w -i 2 -ci -bn
        language: system
        types: [shell]
      - id: shellcheck
        name: shellcheck (-S info)
        entry: shellcheck -S info
        language: system
        types: [shell]
      - id: hadolint
        name: hadolint (Dockerfile lint)
        entry: hadolint
        language: system
        files: Dockerfile$
      - id: tofu-fmt
        name: tofu fmt -check
        entry: tofu fmt -check -diff
        language: system
        files: \.tf$
        pass_filenames: false

  - repo: https://github.com/betterleaks/betterleaks
    rev: v1.3.1
    hooks:
      - id: betterleaks
```

- [ ] **Step 2: Copy `.yamlfmt` and `.github/yamllint.yml` verbatim from `homelab`**

```bash
mkdir -p .github
cp ~/git/nickvigilante/homelab/.yamlfmt .yamlfmt
cp ~/git/nickvigilante/homelab/.github/yamllint.yml .github/yamllint.yml
```

- [ ] **Step 3: Install pre-commit and run it**

```bash
pre-commit install
pre-commit run --all-files --show-diff-on-failure
```

Expected: PASS (only files present are `LICENSE`, `README.md`, `.gitignore`, and the config files just added — nothing for `tofu-fmt`/`hadolint` to check yet since `pass_filenames: false`/`files: Dockerfile$` mean they no-op without matching files).

- [ ] **Step 4: Commit**

```bash
git add .pre-commit-config.yaml .yamlfmt .github/yamllint.yml
git commit -m "$(cat <<'EOF'
Add pre-commit tooling

Assisted-by: AI

EOF
)"
git push
```

______________________________________________________________________

### Task 3: Port the Base template

**Files:**

- Create: `templates/Base/main.tf`
- Create: `templates/Base/modules.tf`
- Create: `templates/Base/startup.sh`
- Create: `templates/Base/README.md`
- Create: `templates/Base/.terraform.lock.hcl` (generated by `tofu init`)

**Interfaces:**

- Consumes: nothing from earlier tasks.

- Produces: `templates/Base/` — a `tofu validate`-clean Terraform template, functionally identical to the live `kubernetes` template (version `panicked_lin22`, which already has the `node_selector: kubernetes.io/arch: amd64` fix), with the trivial post-connect `startup_script` extracted to its own file.

- [ ] **Step 1: Create the directory and copy the live template's fixed files**

The live template was already pulled and fixed earlier at
`/tmp/claude-1000/-home-nickv/65424f2b-89e2-49dc-b865-f3600edad57b/scratchpad/coder-templates/kubernetes/` in this session — if that scratchpad no longer exists when you run this, re-pull it fresh instead:

```bash
mkdir -p templates/Base
coder templates pull kubernetes templates/Base --version active -y
```

(Either source gives the same content — the live active version already has the `node_selector` fix.)

- [ ] **Step 2: Extract the post-connect `startup_script` into its own file**

In `templates/Base/main.tf`, the `coder_agent.main` resource currently has:

```hcl
resource "coder_agent" "main" {
  os             = "linux"
  arch           = "amd64"
  startup_script = <<-EOT
    set -e

    # Add any commands that should be executed at workspace startup (e.g install requirements, start a program, etc) here
  EOT
  ...
```

Create `templates/Base/startup.sh`:

```sh
#!/usr/bin/env sh
set -e

# Add any commands that should be executed at workspace startup (e.g
# install requirements, start a program, etc.) here.
```

Replace the `startup_script` attribute in `main.tf` to load that file:

```hcl
resource "coder_agent" "main" {
  os             = "linux"
  arch           = "amd64"
  startup_script = file("${path.module}/startup.sh")
  ...
```

Note: this extracts the *post-connect* setup script only — it does **not** touch `container.command = ["sh", "-c", coder_agent.main.init_script]`, which is the provider-generated *bootstrap* script (the one with the hardcoded `coder-linux-amd64` download URL). Making the bootstrap step itself runtime-arch-detecting, so workspaces could schedule onto the arm64 Pis too, is explicitly deferred — see the spec's "Deferred / explicitly out of scope" section. The `node_selector` already in `main.tf`'s `kubernetes_deployment_v1.main.spec` block is what keeps this template correct today; leave it in place.

- [ ] **Step 3: Rename the template's display references**

`main.tf`'s resources don't reference the template's own name (Coder tracks that separately via what you name it on push in Task 10), so no further renaming is needed inside the `.tf` files themselves.

- [ ] **Step 4: `tofu fmt` and `tofu init`**

```bash
cd templates/Base
tofu fmt -recursive
tofu init -input=false
```

Expected: `OpenTofu has been successfully initialized!` — this generates `.terraform.lock.hcl`.

- [ ] **Step 5: `tofu validate`**

```bash
tofu validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Write `templates/Base/README.md`**

````markdown
# Base

The foundational Coder Kubernetes workspace template. Runs workspace pods
in the `coder` namespace on gandalf's k3s cluster, pinned to the amd64
node via `node_selector` (this cluster also has arm64 Pi worker nodes;
see the repo-level design doc for why that pin exists).

## Parameters

- `cpu` — 2/4/6/8 cores (default 2)
- `memory` — 2/4/6/8 GB (default 2)
- `home_disk_size` — GB, immutable after creation (default 10)

## Updating

```bash
tofu fmt -recursive && tofu validate
coder templates push Base --directory . -y
````

Merging to `main` on the repo does this automatically via
`.github/workflows/template-push.yml` — manual push is only for local
iteration before opening a PR.

````

- [ ] **Step 7: Commit**

```bash
cd ~/git/nickvigilante/homelab-dev-templates
git add templates/Base
git commit -m "$(cat <<'EOF'
Port the Base workspace template from Coder

Carries forward the node_selector fix already live on the server as
template version panicked_lin22 (pins workspace pods to the amd64
node — this cluster also has arm64 Pi workers). Extracts the
post-connect startup_script into its own file for shellcheck/shfmt.

Assisted-by: AI

EOF
)"
git push
````

______________________________________________________________________

### Task 4: `lint.yml` CI workflow

**Files:**

- Create: `.github/workflows/lint.yml`

**Interfaces:**

- Produces: a `lint` status check on every PR and push to `main`.

- [ ] **Step 1: Write the workflow**

```yaml
name: lint

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

jobs:
  lint:
    name: lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: opentofu/setup-opentofu@v1
        with:
          tofu_version: 1.12.0
      - name: Install pre-commit hook tools
        run: |
          set -euo pipefail
          pip install pre-commit yamllint==1.35.1
          go install github.com/google/yamlfmt/cmd/yamlfmt@v0.21.0
          echo "$(go env GOPATH)/bin" >> "$GITHUB_PATH"
      - uses: actions/setup-go@v5
      - name: Install hadolint
        run: |
          set -euo pipefail
          curl -sL -o /usr/local/bin/hadolint \
            https://github.com/hadolint/hadolint/releases/download/v2.12.0/hadolint-Linux-x86_64
          chmod +x /usr/local/bin/hadolint
      - name: pre-commit run --all-files
        run: pre-commit run --all-files --show-diff-on-failure
      - name: tofu validate (templates/Base)
        working-directory: templates/Base
        run: |
          tofu init -input=false
          tofu validate
```

Note the job name is `lint` — this is the exact string the branch-protection `required_status_checks.contexts` from Task 1 Step 6 must match. GitHub Actions reports the check using the job's `name:` key.

- [ ] **Step 2: Push on a branch and open a PR to prove it runs**

```bash
git checkout -b add-lint-workflow
git add .github/workflows/lint.yml
git commit -m "$(cat <<'EOF'
Add lint CI workflow

Assisted-by: AI

EOF
)"
git push -u origin add-lint-workflow
gh pr create --title "Add lint CI workflow" --body "$(cat <<'EOF'
## Summary

Adds the `lint` workflow: pre-commit (format/lint/secrets) plus `tofu
validate` on `templates/Base`.

## Testing

- Verify this PR's own `lint` check goes green.

---
🤖 Built with AI assistance.
EOF
)"
```

- [ ] **Step 3: Watch the check, confirm green, merge**

```bash
gh pr checks --watch
gh pr merge --squash --delete-branch
```

Expected: `lint` check passes.

- [ ] **Step 4: Finish branch protection from Task 1 Step 6**

If Task 1 Step 6 was skipped because the `lint` context didn't exist yet, run it now:

```bash
gh api -X PUT repos/nickvigilante/homelab-dev-templates/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  -f required_status_checks.strict=true \
  -f 'required_status_checks.contexts[]=lint' \
  -F required_pull_request_reviews.required_approving_review_count=0 \
  -F enforce_admins=false \
  -F required_linear_history=true \
  -F allow_force_pushes=false \
  -F allow_deletions=false \
  -F restrictions=null
```

**Verify:** `gh api repos/nickvigilante/homelab-dev-templates/branches/main/protection --jq '.required_status_checks.contexts'` returns `["lint"]`.

______________________________________________________________________

### Task 5: Minimal base image + publish pipeline

**Files:**

- Create: `images/base/Dockerfile`
- Create: `images/base/README.md`
- Create: `.github/workflows/image-build.yml`

**Interfaces:**

- Produces: `ghcr.io/nickvigilante/homelab-dev-templates:<short-sha>` — a multi-arch (amd64+arm64) republish of `codercom/example-base:ubuntu`, unchanged in content. This task proves the CI pipeline before Task 9 adds real dotfiles content on top.

- [ ] **Step 1: Write the minimal Dockerfile**

```dockerfile
# syntax=docker/dockerfile:1
FROM codercom/example-base:ubuntu
```

- [ ] **Step 2: Write `images/base/README.md`**

````markdown
# base image

Multi-arch (`linux/amd64`, `linux/arm64`) base image for Coder
workspaces on this cluster. Published to
`ghcr.io/nickvigilante/homelab-dev-templates` on merge to `main`,
tagged by short commit SHA — never `:latest`, so the template's pinned
reference is always reproducible.

## Building locally

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t base:local .
````

## What's baked in

Starts as an unmodified republish of `codercom/example-base:ubuntu`.
Dotfiles get baked in a follow-up change — see the repo-level design
doc for the plan and the required secrets audit that gates it.

````

- [ ] **Step 3: Add `hadolint` to CI — write `.github/workflows/image-build.yml`**

```yaml
name: image-build

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
              - 'images/base/**'
              - '.github/workflows/image-build.yml'

  validate:
    name: validate (build only, both platforms)
    needs: changes
    if: needs.changes.outputs.image == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - name: Build (no push)
        uses: docker/build-push-action@v6
        with:
          context: images/base
          platforms: linux/amd64,linux/arm64
          push: false
          tags: base:pr-validate
          outputs: type=oci,dest=/tmp/base-image.tar
      - name: Smoke test — confirm the arm64 image actually boots
        run: |
          set -euo pipefail
          docker buildx build --platform linux/arm64 \
            --load -t base:arm64-smoke images/base
          docker run --rm --platform linux/arm64 base:arm64-smoke uname -m

  publish:
    name: publish (main only)
    needs: changes
    if: github.event_name == 'push' && needs.changes.outputs.image == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: images/base
          platforms: linux/amd64,linux/arm64
          push: true
          tags: |
            ghcr.io/nickvigilante/homelab-dev-templates:${{ github.sha }}
            ghcr.io/nickvigilante/homelab-dev-templates:latest
````

This workflow is intentionally path-filtered (the `changes` job) and therefore **must never be added to branch-protection required checks** — a required check that's skipped on non-matching PRs gets stuck "Expected — Waiting for status to be reported" forever.

- [ ] **Step 4: Open a PR, confirm both jobs (`validate`, and after merge, `publish`) go green**

```bash
git checkout -b add-image-build-workflow
git add images/base .github/workflows/image-build.yml
git commit -m "$(cat <<'EOF'
Add minimal base image and multi-arch build/publish pipeline

Republishes codercom/example-base:ubuntu unchanged, multi-arch, to
prove the CI pipeline before layering dotfiles on top.

Assisted-by: AI

EOF
)"
git push -u origin add-image-build-workflow
gh pr create --title "Add minimal base image + multi-arch CI" --body "$(cat <<'EOF'
## Summary

Minimal `images/base/Dockerfile` (unmodified `codercom/example-base:ubuntu`)
plus `image-build.yml`: validates both `linux/amd64` and `linux/arm64`
build on PRs (with an arm64 boot smoke test), publishes to GHCR on
merge to `main`, tagged by SHA.

## Testing

- Confirm the `validate` job passes on this PR (both platforms build,
  smoke test runs).
- After merge, confirm `publish` pushes `ghcr.io/nickvigilante/homelab-dev-templates:<sha>`.

---
🤖 Built with AI assistance.
EOF
)"
gh pr checks --watch
gh pr merge --squash --delete-branch
```

- [ ] **Step 5: Confirm the published image exists**

```bash
gh api /user/packages/container/homelab-dev-templates/versions --jq '.[0].metadata.container.tags'
```

Expected: includes a tag matching the merge commit's short SHA, and `latest`.

______________________________________________________________________

### Task 6: Pin the template to the published image

**Files:**

- Modify: `templates/Base/main.tf`

**Interfaces:**

- Consumes: the image tag published in Task 5.

- Produces: `templates/Base` referencing the repo's own image instead of upstream `codercom/example-base:ubuntu`.

- [ ] **Step 1: Get the published SHA tag**

```bash
gh api /user/packages/container/homelab-dev-templates/versions --jq '.[0].metadata.container.tags[] | select(length==7 or length==40)'
```

- [ ] **Step 2: Update `main.tf`**

In `kubernetes_deployment_v1.main`'s container block, change:

```hcl
          image             = "codercom/example-base:ubuntu"
```

to (using the actual SHA from Step 1, not `<sha>` literally):

```hcl
          image             = "ghcr.io/nickvigilante/homelab-dev-templates:<sha>"
```

- [ ] **Step 3: Validate**

```bash
cd templates/Base
tofu fmt -check
tofu validate
```

Expected: `Success!`

- [ ] **Step 4: Commit via PR**

```bash
git checkout -b pin-template-to-base-image
git add templates/Base/main.tf
git commit -m "$(cat <<'EOF'
Point Base template at the repo's own base image

Assisted-by: AI

EOF
)"
git push -u origin pin-template-to-base-image
gh pr create --title "Point Base template at homelab-dev-templates base image" --body "$(cat <<'EOF'
## Summary

Repoints the container image from upstream `codercom/example-base:ubuntu`
to this repo's own multi-arch republish, pinned by SHA.

## Testing

- `tofu validate` passes (CI `lint` check).

---
🤖 Built with AI assistance.
EOF
)"
gh pr checks --watch
gh pr merge --squash --delete-branch
```

**Verify:** the merged `templates/Base/main.tf` no longer references `codercom/example-base:ubuntu` anywhere.

______________________________________________________________________

### Task 7: `template-push.yml` CI workflow

**Files:**

- Create: `.github/workflows/template-push.yml`

**Interfaces:**

- Consumes: a `CODER_SESSION_TOKEN` repo secret (created in this task).

- Produces: merges to `main` that touch `templates/Base/**` automatically run `coder templates push`.

- [ ] **Step 1: Create a Coder API token for CI**

On gandalf (or anywhere with `coder` CLI logged in as an owner-role user):

```bash
coder tokens create --name homelab-dev-templates-ci --lifetime 8760h
```

Copy the printed token — it's shown once.

- [ ] **Step 2: Store it as a repo secret**

```bash
gh secret set CODER_SESSION_TOKEN --repo nickvigilante/homelab-dev-templates
```

(Paste the token when prompted, or pipe it in — don't leave it in shell history.)

- [ ] **Step 3: Write the workflow**

```yaml
name: template-push

on:
  push:
    branches: [main]

jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      template: ${{ steps.filter.outputs.template }}
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            template:
              - 'templates/Base/**'
              - '.github/workflows/template-push.yml'

  push:
    name: push
    needs: changes
    if: needs.changes.outputs.template == 'true'
    runs-on: ubuntu-latest
    env:
      CODER_URL: https://coder.vigihome.net
      CODER_SESSION_TOKEN: ${{ secrets.CODER_SESSION_TOKEN }}
    steps:
      - uses: actions/checkout@v4
      - name: Install coder CLI
        run: curl -fsSL https://coder.vigihome.net/install.sh | sh
      - name: Push template
        working-directory: templates/Base
        run: coder templates push Base --directory . -y
```

Like `image-build.yml`, this is path-filtered and must not be a required branch-protection check.

- [ ] **Step 4: Open a PR, merge, confirm the push job runs**

```bash
git checkout -b add-template-push-workflow
git add .github/workflows/template-push.yml
git commit -m "$(cat <<'EOF'
Add template-push CI workflow

Merging to main now deploys templates/Base to the live Coder
deployment automatically, matching the git-is-source-of-truth model
already used for cluster manifests via Flux.

Assisted-by: AI

EOF
)"
git push -u origin add-template-push-workflow
gh pr create --title "Add template-push CI workflow" --body "$(cat <<'EOF'
## Summary

On merge to `main`, path-filtered to `templates/Base/**`, runs `coder
templates push` against the live deployment using a stored session
token. Makes merging the actual deploy step.

## Testing

- Confirm `lint` passes on this PR.
- After merge, confirm the `push` job runs and `coder templates
  versions list Base` shows a new active version.

---
🤖 Built with AI assistance.
EOF
)"
gh pr checks --watch
gh pr merge --squash --delete-branch
```

- [ ] **Step 5: Verify on the live deployment**

```bash
export KUBECONFIG=~/.kube/config
coder templates versions list Base
```

Expected: a new version, `Active`, `Succeeded`, created at the time of the merge.

______________________________________________________________________

### Task 8: Dotfiles secrets audit (blocking — must complete before Task 9)

**Files:**

- Create: `images/base/DOTFILES-AUDIT.md`

**Interfaces:**

- Produces: a written record of which dotfiles are safe to bake into a **public** image under `profile: personal, os: linux, display: false`, and which are excluded and why. Task 9 must not bake anything this audit flags.

- [ ] **Step 1: Confirm the Brewfile-level secrets installs are no-ops on Linux (already verified once this session — re-verify against current source)**

```bash
cd ~/git/nickvigilante/dotfiles  # or ~/.local/share/chezmoi, same repo
grep -n -A3 'secrets.*bitwarden\|bitwarden-cli\|1password' home/dot_config/dotfiles/Brewfile.tmpl
```

Confirm the `bitwarden-cli`/`1password-cli` lines are still gated to `{{ if eq .chezmoi.os "darwin" }}` — if that gating has changed since this plan was written, stop and re-scope this task.

- [ ] **Step 2: Search the full dotfiles source tree for anything that fetches a live secret during apply**

```bash
grep -rn 'bw get\|bw unlock\|op read\|op item\|BW_SESSION\|op://\|1password' home/ --include='*.tmpl' --include='*.sh' -l
```

For every file this returns, read it and determine: does it run during a `chezmoi apply` under `profile: personal, os: linux, display: false`? Record each finding in `images/base/DOTFILES-AUDIT.md` (Step 4) — either "excluded, doesn't apply under this profile" or "excluded via `.chezmoiignore`, added in this task" if it does.

- [ ] **Step 3: Check for any `run_once_`/`run_onchange_` script that touches secrets or SSH/git credential material**

```bash
find home/ -name 'run_*' -o -name '*private_*' -o -name '*.age' | sort
```

Read each result. Anything under the personal/linux/headless profile that touches SSH keys, `.netrc`, git credential helpers, or API tokens must be excluded for this build context — add it to `.chezmoiignore` gated to a way of detecting "building a container image" (e.g. an env var the Dockerfile sets, matching the pattern already used for OS/profile gating in this repo).

- [ ] **Step 4: Write `images/base/DOTFILES-AUDIT.md`**

```markdown
# Dotfiles bake audit

Before Task 9 bakes dotfiles into a **public** GHCR image, this
records what was checked and what's excluded.

## Checked

- Brewfile.tmpl secrets-CLI installs: darwin-only, no-op under
  `profile: personal, os: linux, display: false`. (Confirmed
  <date>.)
- [Fill in with the actual findings from Steps 2–3 — list every file
  found, what it does, and whether/how it's excluded.]

## Excluded from the image build

- [List every `.chezmoiignore` entry added because of this audit, or
  state "none needed" if the search turned up nothing applicable to
  this profile.]

## Conclusion

[State plainly: is it safe to proceed to Task 9 with the current
`.chezmoiignore` state, or does `dotfiles` need changes first?]
```

- [ ] **Step 5: If `.chezmoiignore` changes were needed, land them in `dotfiles` first**

Follow the `chezmoi` skill's worktree + PR workflow in `~/git/nickvigilante/dotfiles`. Do not proceed to Task 9 until any such PR is merged.

- [ ] **Step 6: Commit the audit doc**

```bash
cd ~/git/nickvigilante/homelab-dev-templates
git checkout -b dotfiles-secrets-audit
git add images/base/DOTFILES-AUDIT.md
git commit -m "$(cat <<'EOF'
Audit dotfiles tree for secrets before baking into the base image

Assisted-by: AI

EOF
)"
git push -u origin dotfiles-secrets-audit
gh pr create --title "Audit dotfiles for secrets before baking into base image" --body "$(cat <<'EOF'
## Summary

Required gate before Task 9 (baking dotfiles into the public base
image): documents what was checked in the dotfiles tree and what, if
anything, needed excluding.

---
🤖 Built with AI assistance.
EOF
)"
gh pr checks --watch
gh pr merge --squash --delete-branch
```

______________________________________________________________________

### Task 9: Bake dotfiles into the base image

**Files:**

- Modify: `images/base/Dockerfile`
- Modify: `images/base/README.md`

**Interfaces:**

- Consumes: `images/base/DOTFILES-AUDIT.md` from Task 8 — do not bake anything it flagged as excluded.

- Produces: a base image with the user's CLI tooling and shell config baked in, verified present via `docker run`.

- [ ] **Step 1: Determine the exact non-interactive chezmoi invocation**

Read `home/.chezmoi.toml.tmpl` in the `dotfiles` repo to see how `profile`/`secrets`/`display` are normally resolved (interactive prompts, or something scriptable). chezmoi supports overriding template data non-interactively via a pre-seeded `~/.config/chezmoi/chezmoi.toml` or `CHEZMOI_<KEY>` environment variables — confirm which mechanism this repo's `.chezmoi.toml.tmpl` actually needs by reading it, rather than assuming; do not guess at a flag that hasn't been verified against the real template.

- [ ] **Step 2: Write the updated Dockerfile**

```dockerfile
# syntax=docker/dockerfile:1
FROM codercom/example-base:ubuntu

ARG DOTFILES_REF=main

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git build-essential procps file \
    && rm -rf /var/lib/apt/lists/*

USER coder
WORKDIR /home/coder

# Install Homebrew (linuxbrew) non-interactively, matching how gandalf
# itself is provisioned.
ENV NONINTERACTIVE=1
RUN /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
ENV PATH="/home/linuxbrew/.linuxbrew/bin:${PATH}"

RUN brew install chezmoi

# Non-interactive chezmoi init+apply, pinned to a commit (not a floating
# branch) for reproducible builds. The exact data-seeding mechanism here
# must match what Task 9 Step 1 found in .chezmoi.toml.tmpl.
RUN chezmoi init --source-url https://github.com/nickvigilante/dotfiles.git \
      --branch "${DOTFILES_REF}" \
    && chezmoi apply
```

The `DOTFILES_REF` build arg is deliberately a branch name with a sensible default rather than a hardcoded commit SHA in the file itself — pin it per-build via `--build-arg DOTFILES_REF=<commit-sha>` when you want a specific reproducible pin (e.g. from CI, once you decide how the workflow should supply it), so the Dockerfile doesn't need editing on every dotfiles change.

- [ ] **Step 3: Build locally and verify tools landed**

```bash
cd images/base
docker buildx build --platform linux/amd64 --load -t base:dotfiles-test .
docker run --rm base:dotfiles-test sh -c 'rg --version && fd --version && starship --version && which chezmoi'
```

Expected: all four commands succeed (or whichever subset of the Brewfile's cross-platform CLI tools you expect — adjust the check list to match `home/dot_config/dotfiles/Brewfile.tmpl`'s actual `# Cross-platform CLI` section at the time you run this).

- [ ] **Step 4: Verify nothing from the audit's excluded list leaked in**

```bash
docker run --rm base:dotfiles-test sh -c 'ls -la ~/.ssh 2>/dev/null; cat ~/.netrc 2>/dev/null; env | grep -i -E "token|secret|password"'
```

Expected: no SSH keys, no `.netrc` contents, no secret-looking environment variables. If anything shows up here, stop — go back to Task 8, the audit missed something.

- [ ] **Step 5: Build both platforms to confirm the arm64 path still works**

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t base:dotfiles-test-multi .
```

Expected: succeeds for both platforms (the brew/chezmoi install steps must work under QEMU emulation for arm64 — if this fails, it's likely a package with no arm64 build; note the failure and resolve before continuing, rather than skip-and-commit).

- [ ] **Step 6: Update `images/base/README.md`'s "What's baked in" section**

Replace the placeholder paragraph from Task 5 Step 2 with an accurate description of what's actually installed (the cross-platform CLI section of the Brewfile, minus anything darwin-only/secrets-gated) and a pointer to `DOTFILES-AUDIT.md`.

- [ ] **Step 7: Commit via PR**

```bash
git checkout -b bake-dotfiles-into-base-image
git add images/base/Dockerfile images/base/README.md
git commit -m "$(cat <<'EOF'
Bake dotfiles into the base image at build time

Runs the real chezmoi/Brewfile tooling against the dotfiles repo
during the image build instead of duplicating the package list or
applying dotfiles at workspace runtime. Pinned to a commit/branch via
DOTFILES_REF, not a floating default. Verified against the secrets
audit in DOTFILES-AUDIT.md before landing.

Assisted-by: AI

EOF
)"
git push -u origin bake-dotfiles-into-base-image
gh pr create --title "Bake dotfiles into the base image" --body "$(cat <<'EOF'
## Summary

Extends the base image to run the real chezmoi/Brewfile tooling
against the dotfiles repo at build time, so workspaces need almost no
runtime personalization.

## Changes

- `images/base/Dockerfile` — installs linuxbrew + chezmoi, runs a
  non-interactive `chezmoi init && chezmoi apply` pinned to a
  commit/branch.

## Testing

- Built both platforms locally; verified expected CLI tools present.
- Verified no SSH keys / `.netrc` / secret-looking env vars leaked
  into the image (checked against `DOTFILES-AUDIT.md`'s exclusion
  list).
- CI `validate` job (multi-arch build + arm64 boot smoke test) passes
  on this PR.

---
🤖 Built with AI assistance.
EOF
)"
gh pr checks --watch
gh pr merge --squash --delete-branch
```

- [ ] **Step 8: Repeat Task 6 with the new image**

Once `publish` runs on the merge above, get the new SHA tag and update `templates/Base/main.tf` again (same steps as Task 6), via its own PR.

______________________________________________________________________

## Self-review notes

- **Spec coverage:** repo/visibility/license (Task 1), layout (Tasks 3/5), template port + node_selector carry-forward (Task 3), base image + GHCR + SHA pinning (Tasks 5/6), CI — lint/image-build/template-push (Tasks 4/5/7), dotfiles baking with the mandatory secrets audit (Tasks 8/9). `docker`/`scratch` migration and `startup.sh` runtime-arch-detection are explicitly out of scope per the spec and not tasked here.
- **Required-check path-filter trap:** called out explicitly in Tasks 5 and 7 — neither `image-build.yml` nor `template-push.yml` may be added to branch protection.
- **Public-image secrets rule:** enforced structurally by making Task 8 a hard blocker before Task 9, with a verification step (Task 9 Step 4) that actively checks the built image rather than trusting the audit alone.
