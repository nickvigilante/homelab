# Coder dev templates repo — design

**Goal:** Move Coder workspace templates and their base image out of ad hoc `coder templates push` runs into a source-controlled, CI-validated repo, with a base image that boots correctly on both of this cluster's architectures and bakes in enough of the user's dotfiles that a workspace needs almost no runtime personalization.

**Status:** Approved design, pre-implementation.

**Date:** 2026-09-16.

**Related issues:** None yet — new repo has no tracker history. Two upstream Coder product-gap reports (runtime arch-detection support for `coder_agent`; surfacing agent-timeout vs. pod-Running divergence) were drafted in-chat for the user to file against `coder/coder` separately; they're informational context for this design, not work items in this repo.

## Context and current state

`gandalf` (amd64, control-plane) runs Coder plus `frodo` and `samwise` (both arm64 Pi agent nodes). Coder currently has three templates on the live server — `kubernetes`, `docker`, `scratch` — none source-controlled, all managed by whoever last ran `coder templates push` from wherever they happened to be logged in.

The `kubernetes` template's Terraform hardcoded `coder_agent.main.arch = "amd64"` with no matching `node_selector` on the generated pod, so the k8s scheduler was free to place workspace pods on the arm64 Pis, where the amd64 agent binary fails with `Exec format error`. The pod still reports `Running` (the startup script traps the failure and sleeps 24h to preserve logs for debugging), making the failure easy to miss. A `node_selector: kubernetes.io/arch: amd64` fix was already pushed live as template version `panicked_lin22` via `coder templates push`, but that fix — and the template itself — exists only inside Coder's database, not in git.

The base image is upstream `codercom/example-base:ubuntu`, single-arch-agnostic content only insofar as it happens to run on either architecture; nothing about the workspace environment reflects the user's actual dev setup (shell, CLI tools, editor config), which live in the separately source-controlled `dotfiles` repo (chezmoi-managed, `~/git/nickvigilante/dotfiles`, public, no license).

This effort formalizes both the template and the image, and closes the loop on the arch-scheduling bug at the source-control level rather than leaving the fix only in the live server's template history.

## Decisions (locked during brainstorming)

- **New repo**, not folded into `homelab` or `infrastructure`. Those two have their own apply models (Flux reconcile; `tofu plan`/`apply` against real cloud state) that don't fit a Terraform-template-plus-Docker-image artifact with a different change cadence. Matches the user's own pattern of one-purpose-per-repo for real tools (`passgen`, `cliclack`, `headroom`).
- **Name:** `homelab-dev-templates`.
- **Visibility:** public.
- **License:** MIT (matches the user's own precedent on `passgen`; `headroom`'s Apache-2.0 came from the upstream fork it's based on, not the user's own choice, so it's not a real second data point).
- **Scope:** `kubernetes` template only for now, renamed to **`Base`** (the intent is to build other templates on top of it later; `docker`/`scratch` migration is explicitly deferred, not decided to be dropped).
- **Repo layout:** monorepo-of-templates shape from day one (`templates/<name>/`) even with only one template populated, so adding `docker`/`scratch` later needs no restructuring.
- **Base image registry:** GHCR, multi-arch (`linux/amd64` + `linux/arm64`) via `docker buildx`. No separate Docker Hub account needed; free and unlimited for a public repo.
- **Image pinning:** template references the base image by short-SHA tag, never `:latest` — same digest-pinning discipline already used for the Bitnami postgres image in `homelab` (`k8s/coder/postgres-helmrelease.yaml`).
- **Dotfiles strategy:** bake at image-build time by running the user's actual chezmoi/Brewfile tooling against the `dotfiles` repo (pinned to a commit) inside the Dockerfile — not a hand-duplicated package list, not a bespoke Dockerfile-generator, not runtime `chezmoi apply`/Coder's built-in dotfiles-URL personalization feature (which would reintroduce the per-workspace runtime cost this whole effort is trying to remove). Reuses the `profile: personal, os: linux, display: false` shape already exercised on `gandalf` — no new chezmoi profile dimension needed as a starting hypothesis, pending the audit below.
- **Versioning:** plain imperative-subject commits, matching `homelab`/`infrastructure` — no `release-please`/`commitlint` (that apparatus in `headroom` exists for a project with external users; this repo has none).

## Repo structure

```
homelab-dev-templates/
├── templates/
│   └── Base/
│       ├── main.tf
│       ├── modules.tf
│       ├── startup.sh          # extracted from the coder_agent init heredoc (see below)
│       ├── README.md
│       └── .terraform.lock.hcl
├── images/
│   └── base/
│       ├── Dockerfile
│       └── README.md
├── .github/workflows/
│   ├── lint.yml
│   ├── image-build.yml
│   └── template-push.yml
├── .pre-commit-config.yaml
├── .yamlfmt
├── LICENSE                     # MIT
└── README.md
```

`templates/<name>/` per template keeps room for `docker`/`scratch` (or anything new) without changing shape. `images/base/` is separate from `templates/Base/` because the image is a shared build artifact, not template-specific Terraform.

## Template changes (`templates/Base/`)

- Carry forward the `node_selector: kubernetes.io/arch: amd64` fix already live as `panicked_lin22`, so the pushed-from-CI version matches (or improves on) what's already running.
- Extract the `coder_agent.main` startup script out of the inline HCL heredoc into `startup.sh`, loaded via `templatefile()`. This makes it shellcheck/shfmt-able like every other shell script in the user's repos, and is where the arch-detection logic (`uname -m` branch picking `coder-linux-amd64` vs. `coder-linux-arm64`) belongs if/when the template is made to schedule across both architectures instead of pinning to amd64 only. Whether to make that change now or keep the simpler `node_selector`-only fix is an implementation-time call, not decided here.
- `coder_agent.os`/`arch` attributes stay for dashboard display metadata even if `startup.sh` becomes runtime-arch-aware; note in the template's own comments that they can drift from the pod's real node if so.

## Base image (`images/base/`)

- Multi-stage or single-stage Dockerfile (implementation detail) that ends with a non-interactive chezmoi bootstrap against the `dotfiles` repo, pinned to a specific commit/tag — not a floating branch — so image rebuilds are reproducible and don't silently pick up unrelated dotfiles changes mid-flight.
- **Hard rule, non-negotiable given GHCR visibility is public:** nothing secret-dependent gets baked in. No SSH private keys, no git credential material, no `bw`/`1password` session state. The Brewfile's own secrets-CLI installs are already no-ops on Linux (confirmed: bitwarden-cli is darwin-only in the Brewfile template; Linux gets `bw` from snap outside the manifest), but the rest of the dotfiles tree has not been audited for anything else that touches live secrets during `chezmoi apply`. **That audit is a required task before the first image build lands anything from a secrets-touching dotfile — not an optional nice-to-have.**
- Known integration wrinkles to solve during implementation, not now:
  - `chezmoi init` needs a fully non-interactive path (`--data-file` or equivalent) since a Docker build has no TTY for prompts.
  - The `run_once_01-install-bootstrap-prereqs.sh.tmpl` script that requires interactive `sudo` (hit live on `gandalf` earlier this session) will also fire during the image build; Docker builds typically run as root already, so the script likely needs a "skip sudo if already root" branch.
- Genuinely personal, non-bakeable remainder (git `user.name`/`email`, maybe an SSH key) is handled via Coder's native `coder_parameter`/environment-variable mechanism on the template's `coder_agent` block — set per-workspace at create time — not via dotfiles machinery at all, since by the time this matters almost everything else is already baked and free.

## CI (`.github/workflows/`)

- **`lint.yml`** — `tofu fmt -check -recursive`, `tofu init && tofu validate` for `templates/Base/`, `hadolint` on the Dockerfile, plus pre-commit (yamllint, mdformat, betterleaks) for everything else, mirroring `infrastructure`'s `lint.yml` pattern.
- **`image-build.yml`** — on PRs, `docker buildx build --platform linux/amd64,linux/arm64` without pushing, so a broken arm64 build (e.g. an apt package missing an arm64 build) fails the PR. Follow with a smoke test under QEMU emulation (`docker run --platform linux/arm64 <image> uname -m` or similar) to catch an image that builds but doesn't actually boot on the non-native arch.
- **Image publish** — on merge to `main`, path-filtered to `images/base/**` via `dorny/paths-filter` (same pattern as `infrastructure`'s `homelab-plan.yml`). Builds + pushes both platforms to GHCR, tagged by short SHA. **Must not be a required branch-protection check**, since it's intentionally path-filtered — a required check that never fires on non-matching PRs gets stuck "Expected — Waiting for status to be reported" forever (the exact trap already documented in the user's own memory from prior homelab CI work).
- **`template-push.yml`** — on merge to `main`, `coder templates push Base --directory templates/Base -y` against the live deployment using a `CODER_SESSION_TOKEN` repo secret. Makes merging to `main` the actual deploy step, matching the git-is-source-of-truth model already in place for cluster manifests via Flux.

## Suggested implementation order

Each phase should be independently mergeable/provable before the next starts:

- **Phase 0 — Repo bootstrap.** Create `homelab-dev-templates` (public, MIT), base README, `.pre-commit-config.yaml`/`.yamlfmt` copied from `homelab`'s conventions, branch protection matching `homelab`'s (PR-only, no direct push to `main`).
- **Phase 1 — Port the template.** Move the live `kubernetes`/`Base` template (already includes the `node_selector` fix) into `templates/Base/`, extract `startup.sh`, add `lint.yml`. No image or dotfiles work yet — the template still points at `codercom/example-base:ubuntu`. Prove `tofu validate` and a manual `coder templates push --directory templates/Base` work end to end before automating the push.
- **Phase 2 — Base image, no dotfiles yet.** Write `images/base/Dockerfile` as a minimal multi-arch image (could even start as `FROM codercom/example-base:ubuntu` unchanged, just republished multi-arch through GHCR) to prove the build/publish CI pipeline (`image-build.yml`, path-filtering, GHCR auth) before adding dotfiles complexity on top.
- **Phase 3 — Dotfiles audit + bake.** Do the secrets audit of the `dotfiles` tree first (blocking); then wire the non-interactive `chezmoi apply` into the Dockerfile, solve the sudo/run_once wrinkle, confirm the image boots and has the expected tools on both architectures.
- **Phase 4 — Wire `template-push.yml`.** Once Phases 1–3 are each individually proven, make merges to `main` the actual deploy step for both the image and the template.

## Deferred / explicitly out of scope

- `docker` and `scratch` templates — migrate later using the same `templates/<name>/` pattern once `Base` is proven.
- `tflint`, `cosign` image signing, SBOM generation, Dependabot/Renovate for base-image and Action version bumps — real options, not defaults. Consistent with the "no bundled wrappers, don't build for hypothetical needs" posture already documented in `homelab`'s own "Things deliberately not done."
- Whether to make `startup.sh` runtime-arch-detecting (letting workspace pods schedule onto the Pi nodes too) vs. keeping the simpler `node_selector`-pinned-to-amd64 fix — a real design fork, deliberately left to implementation time rather than decided here.
