#!/usr/bin/env python3
"""Release controller. All remote writes are confined to REPOSITORY.

Run only from local-release.yml on main. The committed harness is dispatched at
the candidate commit; only data is passed as inputs. No workflow files are written.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import time
import tomllib
from html.parser import HTMLParser

from common import ROOT, REPOSITORY, output_json, run, yaml_read

VERSION = re.compile(r'v1\.(\d+)\.(\d+)')
SHA = re.compile(r'[0-9a-f]{40}')
BUILDER = './.github/workflows/build-zensical-artifact.yml'
PHASE_LABELS = {
    'create': 'Build all sites and previews; create a draft import PR',
    'refresh': 'Rebuild all imports and update the existing draft PR',
    'skip': 'Verify unchanged sources skip builds and leave the PR unchanged',
}
BUILD_POLICY = (
    'Every full test run deliberately rebuilds all configured sites, even when their '
    'source SHAs have not changed. The isolated test base has no source markers, '
    'so the initial phase builds every import and preview. The refresh phase '
    'deliberately invalidates a pending marker to exercise rebuilding and PR updates. '
    'Normal imports skip unchanged sources; the final phase verifies that behavior '
    'by expecting no builds and no PR changes.'
)


def api(endpoint, method='GET', data=None, missing_ok=False):
    args = ['gh', 'api', '--method', method, endpoint]
    if data is not None:
        args += ['--input', '-']
    result = run(*args, input=json.dumps(data) if data is not None else None, check=False)
    if result.returncode:
        if missing_ok and 'HTTP 404' in result.stderr:
            return None
        # Deleting an already-removed Git ref returns 422 rather than 404.
        # Do not suppress other validation, permission, or protection errors.
        if (missing_ok and method == 'DELETE' and '/git/refs/heads/' in endpoint
                and 'HTTP 422' in result.stderr):
            try:
                message = json.loads(result.stdout).get('message')
            except (ValueError, AttributeError):
                message = None
            if message == 'Reference does not exist':
                return None
        raise RuntimeError(f'GitHub API {method} {endpoint}: {result.stderr.strip()}')
    return json.loads(result.stdout) if result.stdout.strip() else None


def repo_api(path, **kwargs):
    return api(f'repos/{REPOSITORY}/{path}', **kwargs)


def git(*args):
    return run('git', *args).stdout.strip()


def evidence_dir():
    return Path(os.environ['RUNNER_TEMP']) / 'release-evidence'


def prefix():
    value = f"release-tests/{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    if not re.fullmatch(r'release-tests/[0-9]+-[0-9]+', value):
        raise ValueError('Invalid release test namespace')
    return value


def context_guard():
    if os.environ.get('GITHUB_REPOSITORY', '').lower() != REPOSITORY.lower():
        raise ValueError('Release automation is restricted to ' + REPOSITORY)
    if os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('Release must be dispatched from main')
    if os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        raise ValueError('Release requires workflow_dispatch')


def next_version(tags):
    versions = [(tuple(map(int, m.groups())), tag) for tag in tags if (m := VERSION.fullmatch(tag))]
    if not versions:
        return 'v1.0.0', None
    (minor, patch), previous = max(versions)
    return f'v1.{minor}.{patch + 1}', previous


def harness_inputs(sites, ref, repository):
    """Validate dispatch data against the committed production caller template."""
    match = re.fullmatch(r'refs/heads/(release-tests/[0-9]+-[0-9]+)/harness', ref)
    if repository != REPOSITORY or not match:
        raise ValueError('Harness requires an isolated branch in ' + REPOSITORY)
    expected = json.loads(yaml_read(ROOT / 'import-zensical-sites.yml')['jobs']['import']['with']['sites'])
    defaults = {'use-bun': False, 'submodules': False, 'venv-path': '.venv', 'build-script': ''}
    normalized = []
    for site in expected:
        normalized.append({**defaults, **site})
    if not isinstance(sites, list) or len(sites) != len(normalized):
        raise ValueError('Harness sites must match the production caller')
    for site, wanted in zip(sites, normalized):
        if (not isinstance(site, dict) or not isinstance(site.get('sha'), str) or
                not SHA.fullmatch(site['sha']) or
                {k: v for k, v in site.items() if k != 'sha'} != wanted):
            raise ValueError('Harness may only add pinned SHAs to production site definitions')
    return {'sites': json.dumps(sites), 'matrix': json.dumps({'include': sites}),
            'base': match[1] + '/base', 'branch': match[1] + '/import'}


def wait_ci(source):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        runs = repo_api(f'actions/workflows/local-ci.yml/runs?event=push&head_sha={source}&per_page=100')['workflow_runs']
        runs = [r for r in runs if r['head_branch'] == 'main' and r['head_sha'] == source]
        if runs:
            latest = max(runs, key=lambda r: r['id'])
            if latest['status'] == 'completed':
                if latest['conclusion'] != 'success':
                    raise RuntimeError('CI did not pass for source commit: ' + latest['html_url'])
                return latest['html_url']
        print('Waiting for successful push CI at ' + source, flush=True)
        time.sleep(15)
    raise RuntimeError('No successful main push CI for this exact source SHA within 10 minutes')


def resolve_sites():
    sites = json.loads(yaml_read(ROOT / 'import-zensical-sites.yml')['jobs']['import']['with']['sites'])
    defaults = {'use-bun': False, 'submodules': False, 'venv-path': '.venv', 'build-script': ''}
    # Reuse production validation, including unique IDs and non-overlapping paths.
    import importlib.util
    spec = importlib.util.spec_from_file_location('importer', ROOT / 'import-zensical-sites/zensical-import.py')
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    sites = importer.sites_from_json(json.dumps(sites))
    urls = {}
    for site in sites:
        for key, value in defaults.items():
            site.setdefault(key, value)
        site['sha'] = api(f"repos/{site['repository']}/commits/main")['sha']
        config = api(f"repos/{site['repository']}/contents/zensical.toml?ref={site['sha']}")
        toml = tomllib.loads(base64.b64decode(config['content']).decode())
        urls[site['id']] = toml.get('project', toml)['site_url']
    return sites, urls


def commit(message):
    git('add', '-A')
    git('commit', '-m', message)
    return git('rev-parse', 'HEAD')


def push_branch(sha, branch):
    if git('diff', '--name-only', os.environ['GITHUB_SHA'], sha, '--', '.github/workflows'):
        raise RuntimeError('Release automation must not push modified workflow files')
    # Never replace an existing branch, even after a partially failed previous run.
    git('push', f'--force-with-lease=refs/heads/{branch}:', 'origin', f'{sha}:refs/heads/{branch}')


def dispatch_and_wait(phase, branch, harness_sha, state):
    repo_api('actions/workflows/local-release-e2e.yml/dispatches', method='POST',
             data={'ref': branch, 'inputs': {'sites': json.dumps(state['sites']), 'urls': json.dumps(state['urls']),
                                           'parent-run': os.environ['GITHUB_RUN_ID']}})
    deadline = time.monotonic() + 300
    known = {r['id'] for r in state['runs']}
    selected = None
    while time.monotonic() < deadline:
        runs = repo_api(f'actions/workflows/local-release-e2e.yml/runs?branch={branch}&event=workflow_dispatch&per_page=100')['workflow_runs']
        matches = [r for r in runs if r['head_sha'] == harness_sha and r['id'] not in known]
        if matches:
            selected = max(matches, key=lambda r: r['id'])
            break
        time.sleep(5)
    if not selected:
        raise RuntimeError('Dispatched harness did not appear within five minutes')
    record = {'id': selected['id'], 'phase': phase, 'url': selected['html_url']}
    state['runs'].append(record)
    output_json(evidence_dir() / 'state.json', state)
    print('Running ' + phase + ': ' + record['url'], flush=True)
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
        stream.write(f"- [{PHASE_LABELS.get(phase, 'Full integration pipeline')}]({record['url']}) started.\n")
    # Leave time for controller cleanup before its 180-minute timeout.
    deadline = time.monotonic() + 150 * 60
    while time.monotonic() < deadline:
        result = repo_api(f"actions/runs/{record['id']}")
        if result['status'] == 'completed':
            record['conclusion'] = result['conclusion']
            state['pipeline_attempt'] = result['run_attempt']
            output_json(evidence_dir() / 'state.json', state)
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
                stream.write(f"- [{PHASE_LABELS.get(phase, 'Full integration pipeline')}]({record['url']}): {result['conclusion']}.\n")
            if result['conclusion'] != 'success':
                raise RuntimeError('Release E2E failed: ' + record['url'])
            return record['id']
        print('Waiting for ' + record['url'], flush=True)
        time.sleep(20)
    raise RuntimeError('Release E2E exceeded 150 minutes: ' + record['url'])


def test_pr(namespace):
    prs = repo_api(f"pulls?state=open&base={namespace}/base&head=FedRAMP:{namespace}/import")
    if len(prs) != 1:
        raise RuntimeError('Expected exactly one isolated draft import PR')
    pr = prs[0]
    if not pr['draft'] or pr['base']['ref'] != namespace + '/base' or pr['head']['repo']['full_name'] != REPOSITORY:
        raise RuntimeError('Unexpected test PR scope or draft status')
    return pr


def verify_pr(namespace, sites, expected_number=None):
    pr = test_pr(namespace)
    if expected_number and pr['number'] != expected_number:
        raise RuntimeError('Refresh created a different PR')
    git('fetch', 'origin', f'{namespace}/base', f'{namespace}/import')
    head = pr['head']['sha']
    paths = git('diff', '--name-only', pr['base']['sha'], head).splitlines()
    if not paths or any(not any(p.startswith(s['destination'] + '/') for s in sites) for p in paths):
        raise RuntimeError('Import PR changed files outside expected destinations')
    for site in sites:
        dest = site['destination']
        if git('show', f'{head}:{dest}/.zensical-source-sha') != site['sha']:
            raise RuntimeError('PR has incorrect source marker')
        if not git('show', f'{head}:{dest}/index.html'):
            raise RuntimeError('PR is missing generated HTML')
        if git('ls-tree', head, dest + '/release-stale.txt'):
            raise RuntimeError('Import failed to delete stale output')
        if site['sha'] not in pr['body'] or site['repository'] not in pr['body']:
            raise RuntimeError('PR body lacks source evidence')
    if re.search(r'(?<![\w/&#=.%+-])#[0-9]+\b', pr['body']):
        raise RuntimeError('PR notes contain an unqualified source PR reference')
    return pr


class CanonicalParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'link' and attrs.get('rel') == 'canonical':
            self.urls.append(attrs.get('href', ''))


def verify_artifacts(run_id, sites, urls, include_preview=True, artifact_prefix=""):
    """Verify only this phase's artifacts when several phases share a run."""
    artifacts = repo_api(f'actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
    names = {a['name'] for a in artifacts if not artifact_prefix or a['name'].startswith(artifact_prefix)}
    expected = {'collected-sites'} | {'import-' + s['id'] for s in sites}
    if include_preview:
        expected |= {'preview-' + s['id'] for s in sites}
    expected = {artifact_prefix + name for name in expected}
    if names != expected:
        raise RuntimeError(f'Unexpected build artifacts: {names ^ expected}')
    environments = {}
    with tempfile.TemporaryDirectory() as directory:
        args = ['gh', 'run', 'download', str(run_id), '--repo', REPOSITORY, '--dir', directory]
        for name in sorted(expected):
            args += ['--name', name]
        run(*args)
        for site in sites:
            info = Path(directory) / (artifact_prefix + 'import-' + site['id']) / 'build-info.txt'
            environments[site['id']] = info.read_text()
            checks = [('import-' + site['id'], 'site.tar.gz', 'r:gz', urls[site['id']])]
            if include_preview:
                checks.append(('preview-' + site['id'], 'artifact.tar', 'r:*',
                               urls[site['id']].replace('www.fedramp.gov', 'preview.fedramp.gov')))
            for name, filename, mode, expected_url in checks:
                with tarfile.open(Path(directory) / (artifact_prefix + name) / filename, mode) as archive:
                    member = next((m for m in archive if m.name.removeprefix('./') == 'index.html'), None)
                    if member is None or not member.isfile() or member.size == 0:
                        raise RuntimeError('Missing generated index in ' + name)
                    parser = CanonicalParser()
                    parser.feed(archive.extractfile(member).read().decode())
                    if not any(u.rstrip('/') == expected_url.rstrip('/') for u in parser.urls):
                        raise RuntimeError(f'{name}: canonical URL does not match {expected_url}')
    return environments


def cleanup():
    context_guard()
    namespace = prefix()
    # Cancel children first so they cannot recreate branches after cleanup.
    runs = repo_api(f'actions/workflows/local-release-e2e.yml/runs?branch={namespace}/harness&per_page=100')['workflow_runs']
    active = [r for r in runs if r['status'] != 'completed']
    for child in active:
        try:
            repo_api(f"actions/runs/{child['id']}/cancel", method='POST')
        except RuntimeError:
            # Completion can race the cancellation request.
            if repo_api(f"actions/runs/{child['id']}")['status'] != 'completed':
                raise
    for child in active:
        deadline = time.monotonic() + 180
        while repo_api(f"actions/runs/{child['id']}")['status'] != 'completed':
            if time.monotonic() >= deadline:
                raise RuntimeError('Child still running; preserve test branches for safe cleanup')
            time.sleep(5)
    cleanup_branches(namespace)


def cleanup_branches(namespace, suffixes=('import', 'base', 'harness', 'candidate')):
    if not re.fullmatch(r'release-tests/[0-9]+-[0-9]+', namespace):
        raise ValueError('Invalid cleanup namespace')
    for pr in repo_api(f'pulls?state=open&base={namespace}/base&head=FedRAMP:{namespace}/import'):
        repo_api(f"pulls/{pr['number']}", method='PATCH', data={'state': 'closed'})
    for suffix in suffixes:
        repo_api(f'git/refs/heads/{namespace}/{suffix}', method='DELETE', missing_ok=True)
    print('Closed isolated test PRs and removed this attempt’s temporary branches.', flush=True)


def release_notes(state, commits):
    lines = [f"# {state['version']}", '', '## Changes', '']
    for sha, subject in commits:
        subject = re.sub(r'(?<![\w/])#([0-9]+)\b', lambda m: REPOSITORY + '#' + m[1], subject)
        lines.append(f'- {subject} ([{sha[:7]}](https://github.com/{REPOSITORY}/commit/{sha}))')
    lines += ['', '## Release evidence', '',
              f"- Source commit: `{state['source_sha']}`",
              f"- Builder commit: `{state['builder_sha']}`",
              f"- Release commit: `{state['release_sha']}`",
              f"- Previous v1 target: `{state['previous_v1_oid']}`",
              f"- Previous v1 commit: `{state['previous_v1_commit']}`",
              f"- requirements.txt SHA-256: `{state['requirements_sha256']}`",
              f"- [Source CI]({state['ci_url']})",
              f"- [Release run]({state['release_url']})"]
    lines.append(f"- [Inspect the test PR (closed without merging)]({state['test_pr_url']})")
    for record in state['runs']:
        lines.append(f"- [{PHASE_LABELS[record['phase']]}]({record['url']}): {record['conclusion']}")
    lines += ['', BUILD_POLICY, '']
    if state['previous_version']:
        lines.append(f"- [Changes since {state['previous_version']}](https://github.com/{REPOSITORY}/compare/{state['previous_version']}...{state['version']})")
    lines += ['', '## Build inputs', '', '```json', json.dumps(state['sites'], indent=2), '```',
              '', '## Build environments (including resolved submodules)', '', '```',
              '\n'.join(k + ':\n' + v for k, v in state['environments'].items()), '```',
              '', 'The preview checks build Pages artifacts without deploying. Test PRs were closed without merging.',
              '', '## Rollback', '', '```bash',
              f"git push --force-with-lease=refs/tags/v1:{state['release_sha']} origin {state['previous_v1_commit']}:refs/tags/v1",
              '```', '', 'Fetch the previous commit first if it is not available locally.']
    return '\n'.join(lines) + '\n'


def release_ready(state):
    if ([r['phase'] for r in state['runs']] != ['create', 'refresh', 'skip'] or
            any(r.get('conclusion') != 'success' for r in state['runs']) or
            not state.get('cleanup_complete') or not state.get('test_pr_url') or
            not state.get('environments')):
        raise RuntimeError('Release evidence is incomplete; refusing to publish or promote')


def previous_source_commit(tag):
    if not tag:
        return None
    metadata = run('git', 'show', tag + ':release-management/release.json', check=False)
    return (json.loads(metadata.stdout)['source_sha'] if metadata.returncode == 0
            else git('rev-parse', tag + '^{commit}'))


def latest_published_version():
    tags = []
    page = 1
    while True:
        releases = repo_api(f'releases?per_page=100&page={page}')
        tags.extend(r['tag_name'] for r in releases if not r['draft'] and not r['prerelease']
                    and VERSION.fullmatch(r['tag_name']))
        if len(releases) < 100:
            break
        page += 1
    return next_version(tags)[1]


def run_e2e(state, urls):
    """Dispatch one complete native-job pipeline at the immutable candidate."""
    namespace = prefix()
    candidate = state['release_sha']
    state['urls'] = urls
    git('checkout', '--detach', candidate)
    push_branch(candidate, namespace + '/candidate')
    push_branch(candidate, namespace + '/harness')
    run_id = dispatch_and_wait('pipeline', namespace + '/harness', candidate, state)
    with tempfile.TemporaryDirectory() as directory:
        run('gh', 'run', 'download', str(run_id), '--repo', REPOSITORY,
            '--name', f"pipeline-{state['pipeline_attempt']}-result", '--dir', directory)
        result = json.loads((Path(directory) / 'test-manifest.json').read_text())
    if (result['release_sha'] != candidate or result['sites'] != state['sites'] or
            result['namespace'] != namespace or result['pipeline_run_id'] != str(run_id) or
            result['pipeline_run_attempt'] != str(state['pipeline_attempt'])):
        raise RuntimeError('Pipeline evidence does not match the candidate and test scope')
    release_ready(result)
    state.update({key: result[key] for key in ('runs', 'environments', 'test_pr_url', 'cleanup_complete')})
    # Child cleans its PR/base; controller removes candidate/harness after it exits.
    cleanup()
    release_ready(state)


def main():
    context_guard()
    source = os.environ['GITHUB_SHA']
    if not SHA.fullmatch(source) or git('rev-parse', 'HEAD') != source:
        raise ValueError('Checkout must match the dispatched source commit')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise ValueError('Tracked source files must be clean')
    evidence_dir().mkdir(parents=True, exist_ok=True)
    ci_url = wait_ci(source)
    git('fetch', 'origin', '--tags')
    version, newest_tag = next_version(git('tag', '--list', 'v1.*').splitlines())
    previous = latest_published_version()
    previous_source = previous_source_commit(previous)
    resume_candidate = None
    newest_metadata = (run('git', 'show', newest_tag + ':release-management/release.json', check=False)
                       if newest_tag else None)
    metadata = (json.loads(newest_metadata.stdout)
                if newest_metadata and newest_metadata.returncode == 0 else {})
    if metadata.get('source_sha') == source:
        # A failure after tagging or publishing can be retried without replacing
        # the immutable version. Rerun all E2E gates against that exact candidate.
        version = newest_tag
        resume_candidate = git('rev-parse', version + '^{commit}')
        if repo_api('git/ref/tags/v1')['object']['sha'] == resume_candidate:
            print('This source is already released and promoted as ' + version)
            return
        previous = metadata['previous_version']
        previous_source = previous_source_commit(previous)
    elif previous_source == source:
        raise RuntimeError('No source changes since the last published release')
    if previous_source:
        run('git', 'merge-base', '--is-ancestor', previous_source, source)
    old_ref = repo_api('git/ref/tags/v1')['object']['sha']
    # Resolve annotated and lightweight tags to a commit, and record both identities.
    old_commit = git('rev-parse', old_ref + '^{commit}')
    run('git', 'merge-base', '--is-ancestor', previous_source_commit(old_commit), source)
    commits_raw = git('log', '--no-merges', '--reverse', '--format=%H%x09%s',
                      (previous_source + '..' if previous_source else '') + source)
    commits = [line.split('\t', 1) for line in commits_raw.splitlines()]
    if not commits:
        raise RuntimeError('No new source commits to release')
    sites, urls = resolve_sites()
    namespace = prefix()
    state = {'version': version, 'previous_version': previous, 'source_sha': source,
             'previous_v1_oid': old_ref, 'previous_v1_commit': old_commit,
             'requirements_sha256': hashlib.sha256((ROOT / 'requirements.txt').read_bytes()).hexdigest(),
             'ci_url': ci_url, 'release_url': f"https://github.com/{REPOSITORY}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
             'sites': sites, 'runs': []}
    git('config', 'user.name', 'github-actions[bot]')
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    output_json(ROOT / 'release-management/release.json',
                {k: state[k] for k in ('version', 'source_sha', 'requirements_sha256', 'previous_version')})
    run('python3', 'release-management/check.py', '--actionlint')
    if resume_candidate:
        git('add', '-A')
        if git('write-tree') != git('rev-parse', resume_candidate + '^{tree}'):
            raise RuntimeError('Prepared tree differs from existing immutable release tag')
        candidate = resume_candidate
        git('checkout', '--detach', candidate)
    else:
        candidate = commit('Release ' + version + ': record source ' + source)
    if git('diff', '--name-only', source, candidate, '--', '.github/workflows'):
        raise RuntimeError('Release preparation must not modify workflow files')
    state['release_sha'] = candidate
    state['builder_sha'] = candidate
    output_json(evidence_dir() / 'state.json', state)
    run_e2e(state, urls)
    output_json(evidence_dir() / 'release-manifest.json', state)
    notes_path = evidence_dir() / 'release-notes.md'
    notes_path.write_text(release_notes(state, commits))
    # Numbered tags are create-only. Never force or overwrite one on a retry.
    if not resume_candidate:
        git('tag', version, candidate)
        git('push', 'origin', 'refs/tags/' + version)
    published = repo_api('releases/tags/' + version, missing_ok=True)
    if not published:
        run('gh', 'release', 'create', version, '--repo', REPOSITORY, '--verify-tag',
            '--title', version, '--notes-file', str(notes_path), str(evidence_dir() / 'release-manifest.json'))
    else:
        if published['draft'] or published['prerelease']:
            raise RuntimeError('Existing release is not a published stable release')
        # Preserve existing release notes and assets on retries. Repair an upload
        # interrupted after release creation without replacing any existing asset.
        if not any(a['name'] == 'release-manifest.json' for a in published['assets']):
            run('gh', 'release', 'upload', version, str(evidence_dir() / 'release-manifest.json'), '--repo', REPOSITORY)
    # Promote last, using compare-and-swap so unrelated changes to v1 are not lost.
    git('push', f'--force-with-lease=refs/tags/v1:{old_ref}', 'origin', f'{candidate}:refs/tags/v1')
    if repo_api('git/ref/tags/v1')['object']['sha'] != candidate:
        raise RuntimeError('Remote v1 verification failed')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
        stream.write(notes_path.read_text())
    print('Published ' + version + ' and promoted v1 to ' + candidate, flush=True)


if __name__ == '__main__':
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('command', nargs='?', default='release', choices=['release', 'cleanup'])
        command = parser.parse_args().command
        {'release': main, 'cleanup': cleanup}[command]()
    except Exception as error:
        print(f'::error::{error}', file=sys.stderr)
        sys.exit(1)
