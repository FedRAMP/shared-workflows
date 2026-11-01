import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'release-management'))
import release
from common import yaml_read

SHA = 'a' * 40


class ReleaseTests(unittest.TestCase):
    def test_cleanup_can_run_twice_and_handle_absent_branches(self):
        branches = {'base', 'harness', 'candidate'}  # import may never be created
        env = {'GITHUB_REPOSITORY': release.REPOSITORY, 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
        def command(*args, **kwargs):
            endpoint = args[4]
            if 'actions/workflows/' in endpoint:
                body = {'workflow_runs': []}
            elif '/pulls?' in endpoint:
                body = []
            else:
                self.assertEqual(args[3], 'DELETE')
                self.assertIn('/git/refs/heads/release-tests/123-2/', endpoint)
                branch = endpoint.rsplit('/', 1)[1]
                if branch not in branches:
                    return subprocess.CompletedProcess(args, 1,
                        json.dumps({'message': 'Reference does not exist'}),
                        'gh: Reference does not exist (HTTP 422)')
                branches.remove(branch)
                body = None
            return subprocess.CompletedProcess(args, 0, json.dumps(body), '')
        with patch.dict(os.environ, env), patch.object(release, 'run', side_effect=command):
            release.cleanup()
            release.cleanup()
        self.assertFalse(branches)

    def test_missing_ref_exception_does_not_hide_other_errors(self):
        endpoint = 'repos/FedRAMP/shared-workflows/git/refs/heads/release-tests/123-2/import'
        for method, path, status, message, missing_ok in [
            ('DELETE', endpoint, 422, 'Cannot delete a protected branch', True),
            ('DELETE', endpoint, 403, 'Resource not accessible by integration', True),
            ('PATCH', endpoint, 422, 'Reference does not exist', True),
            ('DELETE', endpoint, 422, 'Reference does not exist', False),
            ('DELETE', 'repos/FedRAMP/shared-workflows/releases/1', 422, 'Reference does not exist', True),
        ]:
            with self.subTest(method=method, path=path, status=status, missing_ok=missing_ok):
                result = subprocess.CompletedProcess([], 1, json.dumps({'message': message}),
                                                     f'gh: {message} (HTTP {status})')
                with patch.object(release, 'run', return_value=result), self.assertRaises(RuntimeError):
                    release.api(path, method=method, missing_ok=missing_ok)

    def test_next_patch_ignores_major_tag_and_prereleases(self):
        self.assertEqual(release.next_version(['v1', 'v1.0.2', 'v1.1.9', 'v1.2.0-rc1', 'v2.0.0']), ('v1.1.10', 'v1.1.9'))
        self.assertEqual(release.next_version([]), ('v1.0.0', None))

    def test_fixed_harness_uses_same_commit_workflows(self):
        harness = yaml_read(ROOT / '.github/workflows/local-release-e2e.yml')
        imp, pages = harness['jobs']['import'], harness['jobs']['preview']
        self.assertEqual(imp['uses'], './.github/workflows/import-zensical-sites.yml')
        self.assertEqual(pages['uses'], './.github/workflows/build-fr-zensical.yml')
        self.assertNotIn('secrets', imp)
        self.assertNotIn('pages', imp['permissions'])
        self.assertNotIn('id-token', imp['permissions'])
        self.assertTrue(imp['with']['release-test'])
        self.assertFalse(pages['with']['deploy'])
        for name in ('import-zensical-sites.yml', 'build-fr-zensical.yml'):
            self.assertEqual(yaml_read(ROOT / '.github/workflows' / name)['jobs']['build']['uses'], release.BUILDER)

    def test_harness_accepts_only_production_sites_and_isolated_branches(self):
        sites = json.loads(yaml_read(ROOT / 'import-zensical-sites.yml')['jobs']['import']['with']['sites'])
        defaults = {'use-bun': False, 'submodules': False, 'venv-path': '.venv', 'build-script': ''}
        sites = [{**defaults, **s, 'sha': SHA} for s in sites]
        ref = 'refs/heads/release-tests/1-1/harness'
        values = release.harness_inputs(sites, ref, release.REPOSITORY)
        self.assertEqual(json.loads(values['matrix'])['include'], sites)
        self.assertEqual(values['base'], 'release-tests/1-1/base')
        for bad_ref in ('refs/heads/main', 'refs/heads/release-tests/1-1/base'):
            with self.assertRaises(ValueError):
                release.harness_inputs(sites, bad_ref, release.REPOSITORY)
        for key, value in [('repository', 'Example/other'), ('destination', 'static/other'),
                           ('build-script', 'evil.sh'), ('sha', 'main')]:
            bad = copy.deepcopy(sites)
            bad[0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                release.harness_inputs(bad, ref, release.REPOSITORY)
        with self.assertRaises(ValueError):
            release.harness_inputs(sites, ref, 'FedRAMP/fedramp.gov')

    def test_release_refuses_other_repositories_branches_and_events(self):
        env = {'GITHUB_REPOSITORY': 'FedRAMP/shared-workflows', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_EVENT_NAME': 'workflow_dispatch'}
        with patch.dict(os.environ, env):
            release.context_guard()
            for key, bad in [('GITHUB_REPOSITORY', 'FedRAMP/fedramp.gov'), ('GITHUB_REF', 'refs/heads/test'),
                             ('GITHUB_EVENT_NAME', 'push')]:
                with patch.dict(os.environ, {key: bad}), self.assertRaises(ValueError):
                    release.context_guard()

    def test_no_successful_ci_means_no_release(self):
        run = {'head_branch': 'main', 'head_sha': SHA, 'id': 1, 'status': 'completed',
               'conclusion': 'failure', 'html_url': 'https://example.test/run'}
        with patch.object(release, 'repo_api', return_value={'workflow_runs': [run]}), self.assertRaises(RuntimeError):
            release.wait_ci(SHA)

    def test_notes_include_direct_commits_and_audit_fields(self):
        state = {'version': 'v1.0.3', 'source_sha': SHA, 'release_sha': 'b' * 40, 'builder_sha': 'b' * 40,
                 'previous_v1_oid': 'c' * 40, 'previous_v1_commit': 'd' * 40,
                 'requirements_sha256': 'e' * 64, 'ci_url': 'https://example.test/ci',
                 'release_url': 'https://example.test/release', 'previous_version': 'v1.0.2',
                 'sites': [], 'environments': {'2026': 'Python 3.13'}, 'test_pr_url': 'https://example.test/pr',
                 'runs': [{'phase': 'create', 'url': 'https://example.test/e2e', 'conclusion': 'success'}]}
        notes = release.release_notes(state, [(SHA, 'Fix qualified references (#12)')])
        for value in ('FedRAMP/shared-workflows#12', SHA, state['release_sha'],
                      state['requirements_sha256'], 'https://example.test/e2e', '--force-with-lease'):
            self.assertIn(value, notes)

    def test_promotion_gate_rejects_failed_missing_and_unclean_runs(self):
        valid = {'runs': [{'phase': phase, 'conclusion': 'success'} for phase in ('create', 'refresh', 'skip')],
                 'cleanup_complete': True, 'test_pr_url': 'https://example.test/pr',
                 'environments': {'join': 'Python 3.13'}}
        release.release_ready(valid)
        for key, value in [('cleanup_complete', False), ('test_pr_url', ''), ('environments', {}),
                           ('runs', valid['runs'][:2]),
                           ('runs', [{'phase': p, 'conclusion': 'failure'} for p in ('create', 'refresh', 'skip')])]:
            state = copy.deepcopy(valid)
            state[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                release.release_ready(state)

    def test_cleanup_never_targets_production_or_another_attempt(self):
        calls = []
        def api(path, **kwargs):
            calls.append((path, kwargs))
            if path.startswith('actions/workflows/'):
                return {'workflow_runs': []}
            if path.startswith('pulls?'):
                return [{'number': 42}]
            return None
        env = {'GITHUB_REPOSITORY': 'FedRAMP/shared-workflows', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
        with patch.dict(os.environ, env), patch.object(release, 'repo_api', side_effect=api):
            release.cleanup()
        deleted = [path for path, opts in calls if opts.get('method') == 'DELETE']
        self.assertEqual(set(deleted), {'git/refs/heads/release-tests/123-2/' + s
                                      for s in ('import', 'base', 'harness', 'candidate')})
        self.assertIn(('pulls/42', {'method': 'PATCH', 'data': {'state': 'closed'}}), calls)

    def test_cleanup_failure_blocks_deletion_while_child_is_running(self):
        def api(path, **kwargs):
            if path.startswith('actions/workflows/'):
                return {'workflow_runs': [{'id': 99, 'status': 'in_progress'}]}
            if path.endswith('/cancel'):
                raise RuntimeError('Cancellation denied')
            return {'status': 'in_progress'}
        env = {'GITHUB_REPOSITORY': 'FedRAMP/shared-workflows', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
        with patch.dict(os.environ, env), patch.object(release, 'repo_api', side_effect=api) as mock:
            with self.assertRaises(RuntimeError):
                release.cleanup()
        self.assertFalse(any(c.kwargs.get('method') == 'DELETE' for c in mock.call_args_list))

    def test_only_published_stable_releases_set_notes_baseline(self):
        releases = [{'tag_name': tag, 'draft': draft, 'prerelease': pre}
                    for tag, draft, pre in [('v1.0.2', False, False), ('v1.0.3', True, False),
                                            ('v1.1.0', False, True), ('v2.0.0', False, False)]]
        with patch.object(release, 'repo_api', return_value=releases):
            self.assertEqual(release.latest_published_version(), 'v1.0.2')
