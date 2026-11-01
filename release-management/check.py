"""Fast static contracts, shell parsing, and optional actionlint validation."""
import ast
from pathlib import Path
import re
import sys
from common import ROOT, REPOSITORY, run, yaml_read


def check():
    files = sorted((ROOT / '.github/workflows').glob('*.yml'))
    files += [ROOT / 'import-zensical-sites.yml', ROOT / 'zensical-to-preview.yml']
    docs = {p.name: yaml_read(p) for p in files[:len(files)-2]}
    for path in files:
        workflow = yaml_read(path)
        assert 'on' in workflow, path
        for job in workflow['jobs'].values():
            if 'uses' in job and job['uses'].startswith((REPOSITORY + '/', './.github/workflows/')):
                match = re.fullmatch(r'(?:' + re.escape(REPOSITORY) + r'/|\./)([\w./-]+)(?:@(v1|[0-9a-f]{40}))?', job['uses'])
                assert match, job['uses']
                target = yaml_read(ROOT / match[1])
                spec = target['on']['workflow_call'].get('inputs', {})
                supplied = job.get('with', {})
                assert not supplied.keys() - spec.keys(), (path, supplied.keys() - spec.keys())
                for key, value in spec.items():
                    assert not value.get('required') or key in supplied, (path, key)
            for step in job.get('steps', []):
                if 'run' in step:
                    # Substitute expressions before bash parses its script.
                    script = re.sub(r'\$\{\{.*?\}\}', 'EXPRESSION', step['run'])
                    run('bash', '-n', input=script)
    builder = docs['build-zensical-artifact.yml']
    assert builder['permissions'] == {'contents': 'read'}
    assert set(builder['jobs']) == {'build'}
    assert 'permissions' not in builder['jobs']['build']
    importer = docs['import-zensical-sites.yml']
    for job in importer['jobs'].values():
        assert not {'pages', 'id-token'} & job.get('permissions', {}).keys()
    assert importer['jobs']['build']['uses'] == './.github/workflows/build-zensical-artifact.yml'
    assert docs['build-fr-zensical.yml']['jobs']['build']['uses'] == './.github/workflows/build-zensical-artifact.yml'
    deployment = docs['build-fr-zensical.yml']['jobs']['deploy']
    assert deployment['permissions'] == {'pages': 'write', 'id-token': 'write'}
    assert deployment['if'] == 'inputs.deploy'
    assert docs['local-ci.yml']['on']['push']['branches'] == ['main']
    assert docs['local-ci.yml']['on']['pull_request']['branches'] == ['main']
    assert set(docs['local-release.yml']['on']) == {'workflow_dispatch'}
    test_workflow = docs['local-test.yml']
    assert set(test_workflow['on']) == {'workflow_dispatch'}
    assert test_workflow['jobs']['test']['uses'] == './.github/workflows/local-release-e2e.yml'
    assert 'secrets.' not in (ROOT / '.github/workflows/local-test.yml').read_text()
    release_job = docs['local-release.yml']['jobs']['release']
    assert release_job['permissions'] == {'contents': 'write', 'actions': 'write', 'pull-requests': 'write'}
    assert 'secrets.' not in (ROOT / '.github/workflows/local-release.yml').read_text()
    harness = docs['local-release-e2e.yml']['jobs']
    for job_name, template, template_job in [('import', 'import-zensical-sites.yml', 'import'),
                                             ('preview', 'zensical-to-preview.yml', 'site')]:
        caller = yaml_read(ROOT / template)['jobs'][template_job]
        expected_path = './' + caller['uses'].split(REPOSITORY + '/', 1)[1].split('@', 1)[0]
        assert harness[job_name]['uses'] == expected_path
    assert harness['import']['with']['create-pr'] is True
    assert harness['import']['with']['release-test'] is True
    assert harness['preview']['with']['deploy'] is False
    assert harness['preview']['with']['preview-urls'] is True

    for path in [*ROOT.glob('release-management/**/*.py'), *ROOT.glob('import-zensical-sites/*.py')]:
        ast.parse(path.read_text(), filename=str(path))
    for path in (ROOT / 'release-management').glob('**/action.yml'):
        for step in yaml_read(path)['runs']['steps']:
            if 'run' in step:
                run('bash', '-n', input=re.sub(r'\$\{\{.*?\}\}', 'EXPRESSION', step['run']))
    if '--actionlint' in sys.argv:
        # GitHub.com's workflow identity contexts are newer than this linter's schema.
        run('actionlint', '-ignore', r'property "workflow_(repository|sha)" is not defined',
            *map(str, files))
    print('Workflow interfaces, permission boundaries, Python and shell syntax passed.')


if __name__ == '__main__':
    check()
