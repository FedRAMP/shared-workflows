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

`build-fr-zensical.yml` contains the common build environment and optional Pages
deployment. `import-zensical-sites.yml` calls it for each changed site with
`deploy: false`, `preview-urls: false`, and `artifact-mode: archive`. Python, Bun,
and locked Python dependencies are configured in one place. The `$/` same-repository
reusable-workflow reference and shared source checkout keep builds and
dependencies on the same release commit.

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

A caller in fedramp.gov can use:

```yaml
name: Import Zensical Sites
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
  overlap or traverse symlinks. Optional `use-bun`, `submodules`, `venv-path`, and
  `build-script` use the common builder's defaults.
- `create-pr` defaults to `false`: builds and collected artifacts are retained
  for seven days without writing a branch or PR. Setting it to `true` is allowed
  only for the caller repository `FedRAMP/fedramp.gov`.
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

`build-fr-zensical.yml` accepts the existing builder options plus
`source-repository` and `source-ref` (empty means the normal caller checkout),
`preview-urls` (default `true`), `artifact-mode` (`pages` or `archive`, default
`pages`), and `artifact-name` (default `site-archive`). Archive mode preserves
hidden files and permissions inside a tar archive and records build versions
alongside it. Set `deploy: false` for build-only use; the deployment job is skipped.

The workflow retains `deploy: true` by default for existing Pages callers.
Archive mode cannot be combined with Pages deployment. Import matrix
jobs use distinct artifact names and concurrency groups.

### Validation and rollout

GitHub runtime validation should exercise Pages callers,
the 2026 build script, failed matrix jobs, and draft PR creation/update.

Release all shared files together before enabling the fedramp.gov caller. Test
an immutable release with `create-pr: false` while sources differ from the base,
then follow the release process below to update `v1`. If rolling `v1` back to a
release without the importer, restore the previous fedramp.gov importer too.

## Creating a release

Callers installed from `zensical-to-preview.yml` use `@v1`. Merging changes into
`main` does not update those callers: move the `v1` tag when a release is ready.
The shared workflow checks out its own commit for `requirements.txt`, so the
workflow and Python dependencies are released together.

Use two kinds of tags:

- **Version tags**, such as `v1.0.2`, identify a specific release. Never move or
  overwrite a published version tag; publish a new version for fixes.
- **Major tags**, such as `v1`, point to the recommended compatible release.
  Update `v1` for compatible fixes and features. For breaking changes, release
  `v2.0.0` and create `v2`, leaving `v1` available for existing callers.

### Prepare and tag a version

Merge the intended changes into `main` first. If Python dependencies changed,
update and commit both `requirements.in` and the generated `requirements.txt`.
Run these commands from this repository with a clean working tree and permission
to push tags. The release command also requires the GitHub CLI (`gh`) to be
installed and authenticated with `gh auth login`.

```bash
git switch main
git pull --ff-only origin main
git fetch origin --tags
git status --short
git tag --list 'v*' --sort=-version:refname
```

If fetching reports that a local major tag would be overwritten, refresh that
specific moving tag with `git fetch origin +refs/tags/v1:refs/tags/v1` and retry.
Stop if the working tree is not clean. Review the commit to release, then choose
an unused version below (`v1.0.2` is an example, not an automatically selected
next version):

```bash
release_version=v1.0.2
git log -1 --oneline origin/main
git tag -a "$release_version" origin/main -m "Release $release_version"
git push origin "refs/tags/$release_version"
```

Before updating `v1`, test the new version in a consuming site's workflow by
temporarily replacing `@v1` with the version tag. Check the build and deployment,
including any custom build script used by that site.

### Publish the release and update the major tag

Create a GitHub release for the version tag. `--verify-tag` requires the tag to
already exist on GitHub; `--generate-notes` supplies initial release notes.

```bash
gh release create "$release_version" \
  --repo FedRAMP/shared-workflows \
  --verify-tag \
  --title "$release_version" \
  --generate-notes
```

Alternatively, use the repository's **Releases → Draft a new release** page and
select the version tag you just pushed. Review the release notes and describe
any changes callers need to make. See the
[GitHub CLI release documentation](https://cli.github.com/manual/gh_release_create)
for more options.

Move the major tag to the tested release commit:

```bash
git tag -fa v1 "${release_version}^{commit}" -m "Latest compatible v1 release: $release_version"
git push origin +refs/tags/v1:refs/tags/v1
git ls-remote origin 'refs/tags/v1*'
```

The `+` deliberately replaces only the remote `v1` tag. Repository tag rules
must allow that update. Creating a GitHub release alone does not move `v1`.
Callers using `@v1` pick up the update on subsequent runs; moving the tag does
not itself trigger their workflows. Callers pinned to a version or commit must
update their reference explicitly. Changes to the copied caller template also
need to be applied in each consuming repository.

### Roll back the major tag

If a release causes problems, point `v1` back to a known working version and
start a new workflow run in affected repositories:

```bash
rollback_version=v1.0.1
git tag -fa v1 "${rollback_version}^{commit}" -m "Roll back v1 to $rollback_version"
git push origin +refs/tags/v1:refs/tags/v1
```

Keep the published version tags intact, document the problem in the affected
release's notes, and publish the fix under a new version.
