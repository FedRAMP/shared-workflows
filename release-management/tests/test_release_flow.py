"""Exercise candidate/tag promotion with real Git in disposable local repositories.

Only GitHub API/build execution is simulated; no network calls or real tags/PRs
are made. This catches release-tree and retry mistakes that YAML lint cannot.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import subprocess

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'release-management'))
import release
from common import run as real_run, yaml_read


class ReleaseFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.work = self.folder / 'work'
        self.work.mkdir()
        self.remote = self.folder / 'origin.git'
        for name in ('.github', 'release-management', 'import-zensical-sites'):
            shutil.copytree(ROOT / name, self.work / name, ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('requirements.txt', '.gitignore', 'import-zensical-sites.yml', 'zensical-to-preview.yml'):
            shutil.copy(ROOT / name, self.work / name)
        self.cwd = Path.cwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self.cwd)
        real_run('git', 'init', '--bare', str(self.remote))
        real_run('git', 'init', '-b', 'main')
        real_run('git', 'config', 'user.name', 'Release test')
        real_run('git', 'config', 'user.email', 'test@example.invalid')
        real_run('git', 'add', '.')
        real_run('git', 'commit', '-m', 'Initial version')
        self.old = real_run('git', 'rev-parse', 'HEAD').stdout.strip()
        real_run('git', 'tag', 'v1.0.0')
        real_run('git', 'tag', '-a', 'v1', '-m', 'Initial moving tag')
        self.old_oid = real_run('git', 'rev-parse', 'v1').stdout.strip()
        (self.work / 'change.txt').write_text('Release change\n')
        real_run('git', 'add', 'change.txt')
        real_run('git', 'commit', '-m', 'Fix release behavior (#12)')
        self.source = real_run('git', 'rev-parse', 'HEAD').stdout.strip()
        real_run('git', 'remote', 'add', 'origin', str(self.remote))
        real_run('git', 'push', 'origin', 'main', '--tags')
        self.env = {'GITHUB_REPOSITORY': 'FedRAMP/shared-workflows', 'GITHUB_REF': 'refs/heads/main',
                    'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
                    'GITHUB_SHA': self.source, 'RUNNER_TEMP': str(self.folder / 'runner'),
                    'GITHUB_STEP_SUMMARY': str(self.folder / 'summary')}
        sites = json.loads(yaml_read(self.work / 'import-zensical-sites.yml')['jobs']['import']['with']['sites'])
        for site in sites:
            site['sha'] = 'a' * 40
            for k, v in {'use-bun': False, 'submodules': False, 'venv-path': '.venv', 'build-script': ''}.items():
                site.setdefault(k, v)
        self.sites = sites
        self.gh_calls = []
        self.fail_phase = None
        self.fail_publish = False
        self.race_promotion = False

    def remote_ref(self, ref):
        return real_run('git', '--git-dir=' + str(self.remote), 'rev-parse', ref).stdout.strip()

    def api(self, path, **kwargs):
        if path == 'git/ref/tags/v1':
            return {'object': {'sha': self.remote_ref('refs/tags/v1')}}
        if path.startswith('contents/'):
            return {'sha': 'blob-sha'}
        if '/artifacts?' in path:
            return {'artifacts': []}
        if path.startswith('releases/tags/'):
            return None
        raise AssertionError(path)

    def command(self, *args, **kwargs):
        if args[0] == 'gh':
            self.gh_calls.append(args)
            if self.fail_publish and args[:3] == ('gh', 'release', 'create'):
                raise RuntimeError('Simulated publication failure')
            if self.race_promotion and args[:3] == ('gh', 'release', 'create'):
                real_run('git', '--git-dir=' + str(self.remote), 'update-ref',
                         'refs/tags/v1', self.source, self.old_oid)
            return subprocess.CompletedProcess(args, 0, '', '')
        if args[:2] == ('python3', 'release-management/check.py'):
            return subprocess.CompletedProcess(args, 0, '', '')
        return real_run(*args, **kwargs)

    def dispatch(self, phase, branch, harness, state):
        if phase == self.fail_phase:
            raise RuntimeError('Simulated E2E failure')
        state['runs'].append({'id': len(state['runs']) + 1, 'phase': phase,
                              'url': 'https://example.test/' + phase, 'conclusion': 'success'})
        return len(state['runs'])

    def invoke(self):
        pr = {'number': 42, 'head': {'sha': 'b' * 40}, 'html_url': 'https://example.test/pr'}
        with patch.dict(os.environ, self.env), patch.object(release, 'ROOT', self.work), \
                patch.object(release, 'run', side_effect=self.command), \
                patch.object(release, 'repo_api', side_effect=self.api), \
                patch.object(release, 'wait_ci', return_value='https://example.test/ci'), \
                patch.object(release, 'latest_published_version', return_value='v1.0.0'), \
                patch.object(release, 'resolve_sites', return_value=(self.sites, {})), \
                patch.object(release, 'dispatch_and_wait', side_effect=self.dispatch), \
                patch.object(release, 'verify_pr', return_value=pr), \
                patch.object(release, 'verify_artifacts', return_value={'join': 'Python 3.13\n'}), \
                patch.object(release, 'cleanup'):
            release.main()

    def test_success_promotes_pinned_candidate_and_leaves_main_alone(self):
        self.invoke()
        candidate = self.remote_ref('refs/tags/v1.0.1')
        self.assertEqual(self.remote_ref('refs/tags/v1'), candidate)
        self.assertNotEqual(candidate, self.source)
        self.assertEqual(self.remote_ref('refs/heads/main'), self.source)
        for name in ('import-zensical-sites.yml', 'build-fr-zensical.yml'):
            text = real_run('git', 'show', candidate + ':.github/workflows/' + name).stdout
            self.assertIn(release.BUILDER + self.source, text)
        tree = real_run('git', 'ls-tree', '-r', '--name-only', candidate).stdout
        self.assertNotIn('release-stale.txt', tree)
        harness = real_run('git', 'show', candidate + ':.github/workflows/local-release-e2e.yml').stdout
        self.assertNotIn('release-tests/123-1', harness)
        self.assertTrue(any(c[:3] == ('gh', 'release', 'create') for c in self.gh_calls))

    def test_failed_e2e_never_creates_version_or_moves_v1(self):
        self.fail_phase = 'refresh'
        with self.assertRaisesRegex(RuntimeError, 'E2E failure'):
            self.invoke()
        self.assertEqual(self.remote_ref('refs/tags/v1'), self.old_oid)
        tags = real_run('git', '--git-dir=' + str(self.remote), 'tag', '--list').stdout
        self.assertNotIn('v1.0.1', tags)
        self.assertFalse(self.gh_calls)

    def test_publication_retry_reuses_identical_version_commit(self):
        self.fail_publish = True
        with self.assertRaisesRegex(RuntimeError, 'publication failure'):
            self.invoke()
        candidate = self.remote_ref('refs/tags/v1.0.1')
        self.assertEqual(self.remote_ref('refs/tags/v1'), self.old_oid)
        real_run('git', 'checkout', '--detach', self.source)
        self.env['GITHUB_RUN_ATTEMPT'] = '2'
        self.fail_publish = False
        self.invoke()
        self.assertEqual(self.remote_ref('refs/tags/v1.0.1'), candidate)
        self.assertEqual(self.remote_ref('refs/tags/v1'), candidate)

    def test_promotion_does_not_overwrite_a_concurrent_v1_change(self):
        self.race_promotion = True
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertEqual(self.remote_ref('refs/tags/v1'), self.source)
        self.assertNotEqual(self.remote_ref('refs/tags/v1.0.1'), self.source)
