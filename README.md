# shared-workflows

This repo is not of any particular interest to the FedRAMP community - but it must be public in order to be called by public repositories.

It contains shared workflows for GitHub actions so that multiple repositories can easily reuse the same underlying workflow.

## Installing the Generic Workflow

This workflow is for sites with a standard FR Zensical setup, run this from the repository root:

```
mkdir -p .github/workflows
cd .github/workflows
wget https://raw.githubusercontent.com/FedRAMP/shared-workflows/refs/heads/main/zensical-to-preview.yml
```

Clean up any old actions if appropriate, as well as any leftover `requirements.in` or `requirements.txt` files.

> Note: If it's 2026 or another site with enrichment, uncomment and configure the
> relevant parts of the action!

## Importing multiple sites into fedramp.gov

`build-zensical-artifact.yml` contains the common build environment and requests
only `contents: read`. `build-fr-zensical.yml` calls it and optionally deploys to
Pages; only its deployment job requests Pages and OIDC write permissions.
`import-zensical-sites.yml` calls the build-only workflow for each changed site
with `preview-urls: false` and `artifact-mode: archive`, then prepares a PR.

Shared workflows use same-commit `./.github/workflows/...` references internally.
They do not follow a second moving `@v1` reference: the selected entry point and
its builder resolve from the same commit. The release process uses lightweight
version and `v1` tags pointing directly at the tested commit, rather than
annotated tag objects. Copyable caller templates keep their normal `@v1` references.

The import workflow uses
[`import-zensical-sites/zensical-import.py`](import-zensical-sites/zensical-import.py)
for change discovery, artifact collection, and applying built sites. It checks
out this helper from the same shared-workflows release; callers do not need to
copy the script into their repositories.

The importer resolves each source repository's `main` commit and compares it
with the destination's `.zensical-source-sha`. Changed sites build independently;
all builds and artifacts must succeed before one draft PR can be prepared.
Imports preserve source URLs. Existing Pages callers retain their preview-domain
rewrite and deployment defaults.

Copy the root-level [import-zensical-sites.yml](import-zensical-sites.yml) to
`.github/workflows/import-zensical-sites.yml` in fedramp.gov. It contains:

```yaml
name: (Local) Import: Prepare Site Updates PR
on:
  workflow_dispatch:
permissions:
  contents: write
  pull-requests: write
concurrency:
  group: import-zensical-sites
  cancel-in-progress: false
jobs:
  import:
    uses: FedRAMP/shared-workflows/.github/workflows/import-zensical-sites.yml@v1
    with:
      create-pr: true
      base-branch: develop
      import-branch: automation/zensical-sites
      sites: |
        [
          {"id":"join","repository":"FedRAMP/join","destination":"static/join"},
          {"id":"phase4","repository":"FedRAMP/phase4","destination":"static/20x/phase4"},
          {"id":"2026","repository":"FedRAMP/2026","destination":"static/2026",
           "use-bun":true,"submodules":true,"venv-path":"tools/.venv",
           "build-script":"tools/scripts/build-ci.sh"},
          {"id":"legacy","repository":"FedRAMP/docs-legacy","destination":"static/legacy"}
        ]
    secrets:
      ZENSICAL_IMPORT_TOKEN: ${{ secrets.ZENSICAL_IMPORT_TOKEN }}
```

### Import inputs and behavior

- `sites` is a required JSON array. Each entry requires a unique `id`, a source
  `repository`, and a `destination` strictly below `static/`. Destinations cannot
  overlap or traverse symlinks. An optional 40-character `sha` pins a source
  commit; otherwise discovery resolves `main`. Optional `use-bun`, `submodules`, `venv-path`, and
  `build-script` use the common builder's defaults.
- `create-pr` defaults to `false`: builds and collected artifacts are retained
  for seven days without writing a branch or PR. Setting it to `true` is allowed
  for the caller repository `FedRAMP/fedramp.gov`. The release harness can also
  create PRs within `FedRAMP/shared-workflows` using `release-test: true`, but only
  with matching `release-tests/<run-id>-<attempt>/{harness,base,import}` branches
  and a `workflow_dispatch` event. It cannot target `main` or `develop`.
- `base-branch` defaults to `develop`; `import-branch` defaults to
  `automation/zensical-sites`. A PR-mode run skips builds if the existing open
  import PR already has all desired source markers. Build-only runs do not skip
  based on pending PRs.
- `ZENSICAL_IMPORT_TOKEN` is optional. A fine-grained PAT with contents and pull
  requests read/write can trigger downstream CI. Without it, the workflow uses
  `github.token`; enable Actions PR creation in the caller repository settings.
- Only the PR job requests write permissions. Site builds receive no import PAT
  or Pages deployment permissions. The caller must grant the permissions needed
  by the selected workflow, as shown above.
- A base branch that advances during builds causes PR preparation to fail;
  rerun against the latest base. Imported files are staged only within changed
  destinations. PRs remain drafts and require manual review and merge.
- Changes to build tooling alone do not trigger imports when source SHAs are
  unchanged. The 2026 build script also syncs upstream rules, so its source
  marker identifies the site commit, not every dependency input.

### Build-only interface

`build-zensical-artifact.yml` accepts the existing builder options plus
`source-repository` and `source-ref` (empty means the normal caller checkout),
`preview-urls` (default `true`), `artifact-mode` (`pages` or `archive`, default
`pages`), and `artifact-name` (default `site-archive`). Archive mode preserves
hidden files and permissions inside a tar archive and records build versions
alongside it. It has no deployment job and requests no Pages or OIDC permissions.

`build-fr-zensical.yml` retains `deploy: true` by default for existing Pages callers.
Its `deploy: false` option skips deployment but still requires the caller to allow
the declared Pages and OIDC permissions during workflow validation. Import callers
avoid that requirement by using the build-only workflow directly.
Archive mode cannot be combined with Pages deployment. Import matrix
jobs use distinct artifact names and concurrency groups.

## Continuous integration and releases

Release tooling, tests, and the quick-check composite action all live under
`release-management/`. Workflow entry points stay in `.github/workflows/` for
GitHub discovery.

Workflow filenames starting with `local-` are maintenance workflows for this
repository only. Actions display names start with `(Local)` for repository-specific
entry points and `(Shared)` for reusable workflows. Root caller templates use
`(Local)` because they run in the consuming repository. The three reusable workflows
(`build-fr-zensical.yml`, `build-zensical-artifact.yml`, and
`import-zensical-sites.yml`) retain their existing filenames. Root-level YAML files
are copyable caller templates, not active workflows in this repository.

| Workflow file | Actions display name |
|---|---|
| `.github/workflows/local-ci.yml` | (Local) Test: Quick CI Checks |
| `.github/workflows/local-test.yml` | (Local) Test: All Builds & Pull Requests |
| `.github/workflows/local-release.yml` | (Local) Publish: Full Release |
| `.github/workflows/local-release-e2e.yml` | (Local) Test: Internal Pipeline Phase |
| `.github/workflows/build-fr-zensical.yml` | (Shared) Publish: Build Site And Deploy Pages |
| `.github/workflows/build-zensical-artifact.yml` | (Shared) Build Only: Upload Site Artifact |
| `.github/workflows/import-zensical-sites.yml` | (Shared) Import: Build Sites And Prepare Draft PR |
| `import-zensical-sites.yml` | (Local) Import: Prepare Site Updates PR |
| `zensical-to-preview.yml` | (Local) Publish: Deploy Preview Site |

The label after `(Local)` or `(Shared)` describes the purpose: **Test** validates,
**Publish** can deploy a site or publish a workflow release, **Build Only** uploads
artifacts without deployment, and **Import** prepares site changes in a draft PR.
The shared Pages workflow supports disabling deployment, as the test harness does.

There are two levels of validation. `local-ci.yml` runs on pull requests targeting
`main` and pushes to `main`. It performs actionlint, workflow-interface and
permission checks, Python/shell syntax checks, and offline importer/release tests.
It does not build sites, create PRs, deploy Pages, or change tags. The tests in
`release-management/tests/` are part of CI, not the production import path.

To run the full tests **without releasing**, use **Actions → (Local) Test: All Builds & Pull Requests → Run workflow → main** after merging these workflows. This runs the same twelve site
builds and draft-PR create/refresh/skip checks as a release, then closes the test
PR and removes its temporary branches. It creates no version tags or GitHub
release and does not move `v1`. Results are attached as a `test-evidence-*`
artifact, including `test-manifest.json`. Run `local-test`, not the internal
`local-release-e2e` harness, which requires controller-supplied inputs.

Every manual full test deliberately builds all four sites even if no downstream
SHA has changed. It removes source markers only from the isolated test base, then
runs four import builds and four preview builds. The refresh test invalidates a
pending marker to trigger four more import builds and update the same PR. Normal
imports skip unchanged sources; the final test verifies that behavior by expecting
zero builds and no PR changes. The run summary explains this deliberate test behavior.
Production import behavior is unchanged.

Each full suite dispatches three child runs: `create` tests the initial PR and
preview builds; `refresh` tests updating that PR; `skip` verifies unchanged sources
are not rebuilt. These remain separate runs so the controller can test the exact
candidate commit and inspect or modify the test PR between phases. Child run titles
include the parent run ID. The parent's summary links to each phase as it starts,
and each child's summary links back to its parent. No child publishes a release or
deploys Pages. Cleanup is safe to repeat after the test controller has already
removed its branches.

`local-test` requires successful CI for the selected main commit and the same
Actions PR/branch permissions described below. It uses the automatic token and
can be rerun even when there are no changes since the last release. Test and
release runs share a concurrency group to avoid overlapping full suites.

`local-release.yml` is manually dispatched from `main`. It requires successful push CI
for the exact source SHA, reruns the quick checks, and then runs full integration
checks before publishing. Use **Actions → (Local) Publish: Full Release → Run workflow → main**. No
version or release-note input is required; releases increment the highest existing
`v1.x.y` patch version. Breaking changes require a separate major-version process;
this workflow never promotes a v2 release to v1.
The release workflow reruns the full suite before publishing; a previous manual
test run does not substitute for its release gate.

### One-time repository setup

1. Merge these workflows into the default branch (`main`). GitHub must know
   `local-release-e2e.yml` there before the controller can dispatch the committed
   harness at the candidate commit on a temporary branch.
2. Under **Settings → Actions → General**, enable **Allow GitHub Actions to create
   and approve pull requests**. The isolated import test uses its own
   `GITHUB_TOKEN` to create draft PRs in this repository.
3. Ensure repository rules permit GitHub Actions to create version tags,
   move `v1`, and create/delete `release-tests/**` branches. Make **(Local) Test: Quick CI Checks / Quick checks** required for merging into `main` (the displayed check is **Quick
   checks**). No environment approval is required by these workflows.

**No PAT or custom release secret is required.** GitHub supplies the job-scoped
`GITHUB_TOKEN` automatically. The release controller requests `contents: write`
for branches/tags/releases, `actions: write` for dispatching/cancelling test runs,
and `pull-requests: write` for inspecting/closing test PRs. Builds receive their
own restricted tokens, not the controller's token. The existing optional
`ZENSICAL_IMPORT_TOKEN` for production imports is unrelated and remains optional.

These are repository settings; committing the files alone does not configure
rules or Actions permissions. The release workflow does not require access to the
private fedramp.gov repository. It reads the public source-site repositories for
builds; all release branches, PRs, tags, and releases stay in shared-workflows.
GitHub permits `workflow_dispatch` with the automatic token, so no separate token
is needed to start the child test runs.

### What a release does

1. Captures the dispatch's exact `main` SHA. A later push to main does not change
   that candidate. It waits up to ten minutes for that SHA's successful push CI.
2. Creates a release commit adding only a `release-management/release.json` record.
   Workflow files are unchanged, and main is not modified. Same-commit references
   keep the builder at this candidate commit without rewriting workflow files.
3. Resolves the four source-site commits from the root importer template and
   dispatches the **fixed, committed harness** at the candidate commit. It passes
   site definitions and SHAs as inputs, validates them against the importer
   template, and uses the same entry points as both root caller templates. Static
   checks keep those entry points aligned. No test call follows the current `v1`.
4. Builds all four sites through the actual importer, creates a draft PR in this
   repository, and verifies its source markers, HTML, notes, changed-path scope,
   and deletion of deliberately stale output. It separately builds all four
   sites through the Pages wrapper with preview URLs and `deploy: false`, then
   downloads and verifies the real Pages artifacts and canonical URLs.
5. Corrupts a pending marker on the disposable PR branch, runs the importer again,
   and verifies that it refreshes the **same PR**. A third run verifies that an
   unchanged pending import produces no artifacts and does not modify that PR.
6. Closes the draft PR without merging and deletes this attempt's test branches.
   Cleanup must succeed before publication. An `always()` step also attempts
   cleanup after failures or cancellations, cancelling child runs first.
7. Creates a numbered tag and GitHub release, attaches `release-manifest.json`,
   and moves `v1` **last**, using a compare-and-swap against its previous target.
   It verifies the resulting remote ref and writes a workflow summary.

The integration test makes twelve full site builds: four import builds, four
preview builds, and four import-refresh builds. The final pending-PR check skips
building. The 2026 builds continue to use the repository's built-in script and
latest rules synchronization. Resolved site commits are fixed across these tests;
upstream rules can advance during the run, so both import passes record their
actual submodule commits in the release evidence.

These tests exercise artifact production and real PR creation/update/skip in this
repository. They intentionally do not deploy a public Pages site, merge test PRs,
or access fedramp.gov. Private-caller access settings and actual Pages deployment
remain outside this release gate.

### Release notes and evidence

Notes are generated from **all non-merge commit subjects** since the previous
release's source commit, including direct commits. Use descriptive commit messages
(or squash-merge titles); no labeling or manual note writing is needed. Commit and
PR references link to this repository.

Each release records its source SHA, prepared release SHA, builder SHA (the release
SHA), prior `v1` tag object and peeled commit, dependency-lock checksum, source-site
SHAs, build versions/submodule SHAs, source CI link, all E2E run links, and the
closed test PR. The manifest is attached to the release. Workflow evidence is also
retained for 30 days, including on failures when available. Numbered version tags
are never overwritten; `v1` is a moving lightweight tag pointing directly to the
release commit.

### Failures, retries, and rollback

Only one release runs at a time. A failed gate leaves `v1` unchanged. If publication
was interrupted after the numbered tag was pushed, rerunning from the same source
reuses that exact candidate and reruns the integration checks; it does not overwrite
the version tag. Existing published notes/assets are preserved. Running an already
promoted source is a no-op. If a run is forcibly terminated before cleanup can
complete, inspect its `release-tests/<run-id>-<attempt>/` branches and draft PR;
the controller never deletes another attempt's branches.

Every release includes a rollback command with the previous commit and an expected
current `v1` target. A rollback moves only `v1`; keep numbered tags and releases
intact. Consumers pinned to a version or SHA keep their original implementation.
Consumers using `@v1` pick up a promotion or rollback on their next fresh workflow
run. Moving a tag does not itself run workflows in consuming repositories.

### Local quick checks

Python 3.12+ and Ruby are required. With actionlint 1.7.12 on PATH:

```bash
python3 release-management/check.py --actionlint
python3 -m unittest discover -s release-management/tests -v
git diff --check
```

Without actionlint, omit that flag to run the other static checks. The linter
suppression is limited to its missing `job.workflow_repository` and
`job.workflow_sha` context properties; all other lint diagnostics remain errors.
Full release checks require GitHub Actions and configured repository credentials.
