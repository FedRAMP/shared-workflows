"""Native-job integration pipeline; never publishes releases or tags."""
import base64
import json
import os
import re
import sys

import release as r
from common import output_json


def namespace():
    if (os.environ.get('GITHUB_REPOSITORY') != r.REPOSITORY or
            os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'):
        raise ValueError('Pipeline is restricted to manual shared-workflows runs')
    ref = os.environ.get('GITHUB_REF', '')
    match = re.fullmatch(r'refs/heads/(release-tests/[0-9]+-[0-9]+)/harness', ref)
    if match:
        return match[1]
    if (ref == 'refs/heads/main' and os.environ.get('GITHUB_WORKFLOW_REF') ==
            r.REPOSITORY + '/.github/workflows/local-test.yml@refs/heads/main'):
        return r.prefix()
    raise ValueError('Pipeline requires the local test entry point or a candidate harness branch')


def load():
    state = json.loads((r.evidence_dir() / 'state.json').read_text())
    if (state['namespace'] != namespace() or state['release_sha'] != os.environ['GITHUB_SHA'] or
            state['pipeline_run_id'] != os.environ['GITHUB_RUN_ID'] or
            state['pipeline_run_attempt'] != os.environ['GITHUB_RUN_ATTEMPT']):
        raise ValueError('Pipeline state does not belong to this run')
    return state


def save(state):
    output_json(r.evidence_dir() / 'state.json', state)


def summary(text):
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
        stream.write(text + '\n\n')


def prepare():
    scope = namespace()
    if r.git('rev-parse', 'HEAD') != os.environ['GITHUB_SHA']:
        raise ValueError('Checkout must match pipeline commit')
    if os.environ['GITHUB_REF'] == 'refs/heads/main':
        ci_url = r.wait_ci(os.environ['GITHUB_SHA'])
        sites, urls = r.resolve_sites()
    else:
        sites = json.loads(os.environ['SITES'])
        r.harness_inputs(sites, os.environ['GITHUB_REF'], r.REPOSITORY)
        urls = json.loads(os.environ['URLS'])
        if set(urls) != {s['id'] for s in sites} or any(not isinstance(v, str) for v in urls.values()):
            raise ValueError('Missing expected canonical URLs')
        ci_url = ''  # The release controller already verified source CI.
    state = {'mode': 'test-only' if os.environ['GITHUB_REF'] == 'refs/heads/main' else 'release-candidate',
             'namespace': scope, 'source_sha': os.environ['GITHUB_SHA'],
             'release_sha': os.environ['GITHUB_SHA'], 'pipeline_run_id': os.environ['GITHUB_RUN_ID'],
             'pipeline_run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
             'sites': sites, 'urls': urls, 'runs': [], 'environments': {}, 'ci_url': ci_url}
    save(state)
    r.git('config', 'user.name', 'github-actions[bot]')
    r.git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    for site in sites:
        dest = r.ROOT / site['destination']
        dest.mkdir(parents=True, exist_ok=True)
        (dest / '.zensical-source-sha').unlink(missing_ok=True)
        (dest / 'release-stale.txt').write_text('Must be removed by import\n')
    base = r.commit('Prepare isolated import test base')
    r.push_branch(base, scope + '/base')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        for key, value in {'sites': json.dumps(sites), 'matrix': json.dumps({'include': sites}),
                           'base': scope + '/base', 'branch': scope + '/import'}.items():
            stream.write(f'{key}={value}\n')
    summary('## Full pipeline test plan\n\n' + r.BUILD_POLICY +
            f'\n\nExpected: {len(sites) * 3} builds, then zero builds in the unchanged-source check.')
    if os.environ['GITHUB_REF'] != 'refs/heads/main':
        parent = scope.split('/')[1].split('-')[0]
        summary(f'[Parent release run](https://github.com/{r.REPOSITORY}/actions/runs/{parent})')


def verify(phase):
    state = load()
    if [record['phase'] for record in state['runs']] != list(r.PHASE_LABELS)[:list(r.PHASE_LABELS).index(phase)]:
        raise ValueError('Pipeline phases are out of order')
    expected_change = 'false' if phase == 'skip' else 'true'
    if os.environ['IMPORT_CHANGED'] != expected_change:
        raise RuntimeError('Unexpected importer change detection in ' + phase)
    sites, scope = state['sites'], state['namespace']
    pr = r.verify_pr(scope, sites, state.get('pr_number'))
    run_id = state['pipeline_run_id']
    artifact_prefix = 'attempt-' + state['pipeline_run_attempt'] + '-' + phase + '-'
    if phase == 'skip':
        if state['pr_head'] != pr['head']['sha']:
            raise RuntimeError('Unchanged-source check modified the PR')
        artifacts = r.repo_api(f'actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
        if any(a['name'].startswith(artifact_prefix) for a in artifacts):
            raise RuntimeError('Unchanged-source check unexpectedly produced build artifacts')
    else:
        environments = r.verify_artifacts(run_id, sites, state['urls'],
                                           include_preview=phase == 'create', artifact_prefix=artifact_prefix)
        state['environments'].update({phase + '/' + k: v for k, v in environments.items()})
    state.update(pr_number=pr['number'], pr_head=pr['head']['sha'], test_pr_url=pr['html_url'])
    if phase == 'create':
        marker = sites[0]['destination'] + '/.zensical-source-sha'
        content = r.repo_api(f'contents/{marker}?ref={pr["head"]["sha"]}')
        r.repo_api('contents/' + marker, method='PUT', data={
            'message': 'Exercise pending import refresh', 'branch': scope + '/import',
            'sha': content['sha'], 'content': base64.b64encode(('0' * 40 + '\n').encode()).decode()})
    record = {'id': int(run_id), 'phase': phase, 'conclusion': 'success',
              'url': f'https://github.com/{r.REPOSITORY}/actions/runs/{run_id}'}
    state['runs'].append(record)
    save(state)
    summary(f"## {r.PHASE_LABELS[phase]}\n\nPassed. [Inspect test PR]({pr['html_url']}).")


def cleanup():
    # This job depends on every build/verifier, so none can recreate branches.
    r.cleanup_branches(namespace(), ('import', 'base'))
    summary('## Cleanup passed\n\nTest PR closed without merging; isolated import and base branches removed.')


def finish():
    if os.environ.get('CHECK_RESULT') != 'success' or os.environ.get('CLEANUP_RESULT') != 'success':
        raise RuntimeError('Verification and cleanup must both succeed')
    state = load()
    state['cleanup_complete'] = True  # Workflow gate requires successful cleanup.
    r.release_ready(state)
    output_json(r.evidence_dir() / 'test-manifest.json', state)
    summary('## Full tests passed\n\n' + r.BUILD_POLICY)
    for record in state['runs']:
        summary(f"- [{r.PHASE_LABELS[record['phase']]}]({record['url']}): success")
    summary(f"[Inspect test PR (closed without merging)]({state['test_pr_url']})\n\n"
            'This pipeline did not deploy Pages, publish releases, or change tags.')


if __name__ == '__main__':
    try:
        command = sys.argv[1]
        if command in r.PHASE_LABELS:
            verify(command)
        else:
            {'prepare': prepare, 'cleanup': cleanup, 'finish': finish}[command]()
    except Exception as error:
        print(f'::error::{error}', file=sys.stderr)
        sys.exit(1)
