"""Cross-job scope, failure gates, and artifact separation in the native pipeline."""
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'release-management'))
import pipeline
import release
from common import yaml_read


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        env = patch.dict(os.environ, {
            'GITHUB_REPOSITORY': release.REPOSITORY, 'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_SHA': 'a' * 40, 'RUNNER_TEMP': str(self.folder),
            'GITHUB_STEP_SUMMARY': str(self.folder / 'summary'),
            'GITHUB_WORKFLOW_REF': release.REPOSITORY + '/.github/workflows/local-test.yml@refs/heads/main',
        })
        env.start()
        self.addCleanup(env.stop)
        self.state = {'namespace': 'release-tests/123-1', 'release_sha': 'a' * 40,
                      'pipeline_run_id': '123', 'pipeline_run_attempt': '1',
                      'sites': [], 'urls': {}, 'runs': [], 'environments': {}}

    def test_scope_rejects_arbitrary_main_callers_and_other_repositories(self):
        self.assertEqual(pipeline.namespace(), 'release-tests/123-1')
        for key, value in [('GITHUB_WORKFLOW_REF', 'other'), ('GITHUB_REPOSITORY', 'FedRAMP/fedramp.gov'),
                           ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REF', 'refs/heads/develop')]:
            with patch.dict(os.environ, {key: value}), self.assertRaises(ValueError):
                pipeline.namespace()
        with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/release-tests/456-2/harness'}):
            self.assertEqual(pipeline.namespace(), 'release-tests/456-2')

    def test_state_cannot_cross_candidate_run_attempt_or_namespace(self):
        for key, value in [('release_sha', 'b' * 40), ('pipeline_run_id', '999'),
                           ('pipeline_run_attempt', '2'), ('namespace', 'release-tests/999-1')]:
            pipeline.save({**self.state, key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                pipeline.load()

    def test_final_gate_rejects_failed_or_skipped_cleanup_and_verification(self):
        for check, cleanup in [('failure', 'success'), ('success', 'failure'), ('skipped', 'success'),
                               ('success', 'skipped'), ('cancelled', 'success')]:
            with patch.dict(os.environ, CHECK_RESULT=check, CLEANUP_RESULT=cleanup), self.assertRaises(RuntimeError):
                pipeline.finish()
        self.assertFalse((release.evidence_dir() / 'test-manifest.json').exists())

    def test_skip_rejects_builds_and_pr_changes(self):
        state = {**self.state, 'runs': [{'phase': p} for p in ('create', 'refresh')],
                 'pr_number': 3, 'pr_head': 'b' * 40}
        for changed, head, artifacts in [('true', 'b' * 40, []), ('false', 'c' * 40, []),
                                         ('false', 'b' * 40, [{'name': 'attempt-1-skip-import-join'}])]:
            pipeline.save(state)
            with patch.dict(os.environ, IMPORT_CHANGED=changed), \
                    patch.object(release, 'verify_pr', return_value={'number': 3, 'head': {'sha': head}}), \
                    patch.object(release, 'repo_api', return_value={'artifacts': artifacts}), \
                    self.assertRaises(RuntimeError):
                pipeline.verify('skip')
            self.assertEqual(len(pipeline.load()['runs']), 2)

    def test_phase_artifacts_are_verified_without_consuming_other_phases(self):
        prefix = 'attempt-1-create-'
        names = [prefix + n for n in ('import-join', 'preview-join', 'collected-sites')]
        artifacts = [{'name': n} for n in names + ['attempt-1-refresh-import-join', 'pipeline-1-prepare']]
        def download(*args):
            requested = [args[i+1] for i, value in enumerate(args) if value == '--name']
            self.assertEqual(set(requested), set(names))
            directory = Path(args[args.index('--dir')+1])
            for name, filename, mode, url in [
                ('import-join', 'site.tar.gz', 'w:gz', 'https://www.fedramp.gov/join/'),
                ('preview-join', 'artifact.tar', 'w', 'https://preview.fedramp.gov/join/'),
            ]:
                folder = directory / (prefix + name)
                folder.mkdir()
                (folder / 'build-info.txt').write_text('Python 3.13')
                data = f'<link rel="canonical" href="{url}">'.encode()
                with tarfile.open(folder / filename, mode) as archive:
                    entry = tarfile.TarInfo('index.html')
                    entry.size = len(data)
                    archive.addfile(entry, io.BytesIO(data))
        with patch.object(release, 'repo_api', return_value={'artifacts': artifacts}), \
                patch.object(release, 'run', side_effect=download):
            self.assertEqual(release.verify_artifacts('123', [{'id': 'join'}],
                             {'join': 'https://www.fedramp.gov/join/'}, artifact_prefix=prefix),
                             {'join': 'Python 3.13'})
        with patch.object(release, 'repo_api', return_value={'artifacts': artifacts[1:]}), \
                self.assertRaisesRegex(RuntimeError, 'Unexpected build artifacts'):
            release.verify_artifacts('123', [{'id': 'join'}], {}, artifact_prefix=prefix)

    def test_workflow_gates_cleanup_after_all_writers_and_results_after_cleanup(self):
        jobs = yaml_read(ROOT / '.github/workflows/local-release-e2e.yml')['jobs']
        self.assertEqual(jobs['cleanup']['if'], 'always()')
        self.assertEqual(set(jobs['cleanup']['needs']), set(jobs) - {'cleanup', 'results'})
        self.assertEqual(jobs['results']['if'], 'always()')
        self.assertEqual(set(jobs['results']['needs']), {'cleanup', 'verify-skip'})
        self.assertIn('verify-create', jobs['refresh']['needs'])
        self.assertIn('verify-refresh', jobs['skip']['needs'])
        self.assertEqual(jobs['verify-create']['needs'], ['import', 'preview'])
        prefixes = [jobs[j]['with']['artifact-prefix'] for j in ('import', 'refresh', 'skip')]
        self.assertEqual(len(set(prefixes)), 3)
        self.assertTrue(all('github.run_attempt' in p for p in prefixes))
        self.assertFalse(jobs['preview']['with']['deploy'])
        self.assertFalse(any('dispatch' in step.get('run', '') for job in jobs.values()
                             for step in job.get('steps', [])))

    def test_release_dispatches_one_complete_pipeline_at_candidate(self):
        calls = []
        def api(path, **kwargs):
            calls.append((path, kwargs))
            if path.endswith('/dispatches'):
                return None
            if path.startswith('actions/workflows/'):
                return {'workflow_runs': [
                    {'id': 11, 'head_sha': 'b' * 40, 'html_url': 'https://example.test/wrong'},
                    {'id': 12, 'head_sha': 'a' * 40, 'html_url': 'https://example.test/pipeline'}]}
            self.assertEqual(path, 'actions/runs/12')
            return {'status': 'completed', 'conclusion': 'success', 'run_attempt': 1}
        state = {'sites': [], 'urls': {}, 'runs': []}
        with patch.object(release, 'repo_api', side_effect=api):
            result = release.dispatch_and_wait('pipeline', 'release-tests/123-1/harness', 'a' * 40, state)
        self.assertEqual(result, 12)
        posts = [kwargs['data'] for path, kwargs in calls if path.endswith('/dispatches')]
        self.assertEqual(posts, [{'ref': 'release-tests/123-1/harness',
                                 'inputs': {'sites': '[]', 'urls': '{}', 'parent-run': '123'}}])
        self.assertEqual(state['pipeline_attempt'], 1)
