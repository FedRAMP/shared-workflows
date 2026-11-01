import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('importer', ROOT / 'import-zensical-sites/zensical-import.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SHA = 'a' * 40
OLD = 'b' * 40


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        self.runner = self.root / 'runner'
        self.runner.mkdir()
        env = patch.dict(os.environ, {
            'GITHUB_REPOSITORY': 'FedRAMP/fedramp.gov', 'CREATE_PR': 'true',
            'BASE_BRANCH': 'develop', 'IMPORT_BRANCH': 'automation/zensical-sites',
            'RELEASE_TEST': 'false', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GITHUB_REF': 'refs/heads/main', 'GITHUB_OUTPUT': str(self.root / 'outputs'),
            'GITHUB_STEP_SUMMARY': str(self.root / 'summary'), 'RUNNER_TEMP': str(self.runner),
            'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '1',
        })
        env.start()
        self.addCleanup(env.stop)

    def sites(self, count=2, previous=OLD):
        sites = [{'id': name, 'repository': 'FedRAMP/' + name, 'destination': 'static/' + name}
                 for name in ('join', 'phase4')[:count]]
        for site in sites:
            dest = Path(site['destination'])
            dest.mkdir(parents=True)
            (dest / 'stale.html').write_text('old')
            (dest / '.zensical-source-sha').write_text(previous)
        os.environ['SITES'] = json.dumps(sites)
        return sites

    def outputs(self):
        return dict(line.split('=', 1) for line in (self.root / 'outputs').read_text().splitlines())

    def fake_run(self, pending=False, failure=False):
        def invoke(*args, **kwargs):
            if args[:2] == ('git', 'ls-remote'):
                return subprocess.CompletedProcess(args, int(failure), SHA + '\trefs/heads/main\n', '')
            if args[:3] == ('git', 'rev-parse', 'HEAD'):
                output = OLD
            elif args[:3] == ('gh', 'pr', 'list'):
                output = json.dumps([{'headRefOid': OLD, 'url': 'https://example.test/pr'}] if pending else [])
            elif args[:2] == ('gh', 'api'):
                output = SHA
            else:
                raise AssertionError(args)
            return subprocess.CompletedProcess(args, 0, output, '')
        return invoke

    def test_changed_sources_and_pending_skip(self):
        self.sites()
        with patch.object(m, 'run', side_effect=self.fake_run()):
            m.discover()
        self.assertEqual(len(json.loads(self.outputs()['matrix'])['include']), 2)
        with patch.object(m, 'run', side_effect=self.fake_run(pending=True)):
            m.discover()
        self.assertEqual(self.outputs()['changed'], 'false')

    def test_unchanged_source_skips(self):
        self.sites(previous=SHA)
        with patch.object(m, 'run', side_effect=self.fake_run()):
            m.discover()
        self.assertEqual(self.outputs()['changed'], 'false')

    def test_lookup_failure_emits_no_build_matrix(self):
        self.sites()
        with patch.object(m, 'run', side_effect=self.fake_run(failure=True)), self.assertRaises(RuntimeError):
            m.discover()
        self.assertFalse((self.root / 'outputs').exists())

    def test_pinned_sources_do_not_follow_main(self):
        sites = self.sites()
        for site in sites:
            site['sha'] = SHA
        os.environ['SITES'] = json.dumps(sites)
        with patch.object(m, 'run', side_effect=self.fake_run()) as mock:
            m.discover()
        self.assertFalse(any(c.args[:2] == ('git', 'ls-remote') for c in mock.call_args_list))
        self.assertTrue(all(s['sha'] == SHA for s in json.loads(self.outputs()['matrix'])['include']))

    def test_build_only_never_queries_pr(self):
        self.sites()
        os.environ['CREATE_PR'] = 'false'
        with patch.object(m, 'run', side_effect=self.fake_run()) as mock:
            m.discover()
        self.assertFalse(any(c.args[0] == 'gh' for c in mock.call_args_list))

    def test_release_scope_requires_exact_branches_and_repository(self):
        with self.assertRaises(ValueError):
            m.validate_pr_scope('Example/other')
        os.environ.update(RELEASE_TEST='true', GITHUB_REF='refs/heads/release-tests/123-1/harness',
                          BASE_BRANCH='release-tests/123-1/base', IMPORT_BRANCH='release-tests/123-1/import')
        m.validate_pr_scope('FedRAMP/shared-workflows')
        for key, bad in [('BASE_BRANCH', 'main'), ('IMPORT_BRANCH', 'release-tests/124-1/import'),
                         ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REF', 'refs/heads/main')]:
            with patch.dict(os.environ, {key: bad}), self.assertRaises(ValueError):
                m.validate_pr_scope('FedRAMP/shared-workflows')
        with self.assertRaises(ValueError):
            m.validate_pr_scope('FedRAMP/fedramp.gov')

    def test_unsafe_destinations_and_overlaps(self):
        for destination in ('static', '/static/x', 'static/../x', 'static/x,other'):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                m.sites_from_json(json.dumps([{'id': 'a', 'repository': 'FedRAMP/join', 'destination': destination}]))
        sites = self.sites()
        sites[1]['destination'] = 'static/join/nested'
        with self.assertRaises(ValueError):
            m.sites_from_json(json.dumps(sites))
        Path('static/link').symlink_to(self.runner, target_is_directory=True)
        with self.assertRaises(ValueError):
            m.destination_path('static/link/child')

    def make_artifacts(self, sites, broken=None):
        for site in sites:
            site['sha'] = SHA
            dest = self.runner / 'site-artifacts' / ('import-' + site['id'])
            dest.mkdir(parents=True)
            with tarfile.open(dest / 'site.tar.gz', 'w:gz') as archive:
                files = {'.hidden': b'hidden', 'tool.sh': b'#!/bin/sh\n'}
                if broken != site['id']:
                    files['index.html'] = b'new HTML'
                for name, value in files.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(value)
                    member.mode = 0o755 if name == 'tool.sh' else 0o644
                    archive.addfile(member, io.BytesIO(value))
            (dest / 'build-info.txt').write_text('Python 3.13\nZensical 0.0.73\n')
        os.environ['MATRIX'] = json.dumps({'include': sites})

    def test_round_trip_preserves_hidden_and_executable_files_removes_stale(self):
        sites = self.sites()
        self.make_artifacts(sites)
        with patch.object(m, 'commit_notes', return_value='  - Fix (FedRAMP/join#42)\n'):
            m.collect()
        collected = self.runner / 'collected'
        collected.mkdir()
        (self.runner / 'collected-sites.tar.gz').rename(collected / 'collected-sites.tar.gz')
        m.apply()
        for site in sites:
            dest = Path(site['destination'])
            self.assertFalse((dest / 'stale.html').exists())
            self.assertEqual((dest / '.hidden').read_text(), 'hidden')
            self.assertTrue((dest / 'tool.sh').stat().st_mode & 0o100)
            self.assertEqual(m.marker(dest), SHA)

    def test_bad_second_archive_prevents_all_replacements(self):
        sites = self.sites()
        self.make_artifacts(sites, broken='phase4')
        with patch.object(m, 'commit_notes', return_value=''), self.assertRaises(ValueError):
            m.collect()
        for site in sites:
            self.assertEqual(m.marker(site['destination']), OLD)
            self.assertTrue((Path(site['destination']) / 'stale.html').exists())

    def test_archive_traversal_rejected(self):
        path = self.runner / 'unsafe.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            member = tarfile.TarInfo('../escaped')
            member.size = 1
            archive.addfile(member, io.BytesIO(b'x'))
        with self.assertRaises(tarfile.FilterError):
            m.unpack(path, self.runner / 'unpacked')
        self.assertFalse((self.runner / 'escaped').exists())

    def test_commit_references_qualified_in_both_history_paths(self):
        for previous in ('', OLD):
            def invoke(*args, **kwargs):
                output = '12' if 'rev-list' in args else ('  - Fix (#164), closes #163; Other/repo#42; https://example.test/#123\n' if 'log' in args else '')
                return subprocess.CompletedProcess(args, 0, output, '')
            with patch.object(m, 'run', side_effect=invoke):
                notes = m.commit_notes({'id': '2026', 'repository': 'FedRAMP/2026', 'sha': SHA}, previous, self.runner)
            self.assertIn('(FedRAMP/2026#164)', notes)
            self.assertIn('closes FedRAMP/2026#163', notes)
            self.assertIn('Other/repo#42', notes)
            self.assertIn('https://example.test/#123', notes)

    def test_native_main_scope_requires_local_test_caller_and_current_attempt(self):
        os.environ.update(RELEASE_TEST='true', GITHUB_RUN_ATTEMPT='2',
                          GITHUB_WORKFLOW_REF='FedRAMP/shared-workflows/.github/workflows/local-test.yml@refs/heads/main',
                          BASE_BRANCH='release-tests/1-2/base', IMPORT_BRANCH='release-tests/1-2/import')
        m.validate_pr_scope('FedRAMP/shared-workflows')
        for key, value in [('GITHUB_WORKFLOW_REF', 'other'), ('GITHUB_RUN_ATTEMPT', '3'),
                           ('BASE_BRANCH', 'main'), ('IMPORT_BRANCH', 'automation/zensical-sites')]:
            with patch.dict(os.environ, {key: value}), self.assertRaises(ValueError):
                m.validate_pr_scope('FedRAMP/shared-workflows')

    def test_prefixed_collect_reads_only_the_requested_phase(self):
        sites = self.sites()
        self.make_artifacts(sites)
        artifacts = self.runner / 'site-artifacts'
        for path in list(artifacts.iterdir()):
            path.rename(path.with_name('attempt-1-refresh-' + path.name))
        # A bad archive from another phase must not be read.
        (artifacts / 'import-join').mkdir()
        with patch.dict(os.environ, ARTIFACT_PREFIX='attempt-1-refresh-'), \
                patch.object(m, 'commit_notes', return_value=''):
            m.collect()
        for site in sites:
            self.assertEqual(m.marker(site['destination']), SHA)
