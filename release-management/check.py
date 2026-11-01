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
            if 'uses' in job and job['uses'].startswith(REPOSITORY + '/'):
                match = re.fullmatch(re.escape(REPOSITORY) + r'/([\w./-]+)@(v1|[0-9a-f]{40})', job['uses'])
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
    assert '/build-zensical-artifact.yml@' in importer['jobs']['build']['uses']
    assert '/build-zensical-artifact.yml@' in docs['build-fr-zensical.yml']['jobs']['build']['uses']
    deployment = docs['build-fr-zensical.yml']['jobs']['deploy']
    assert deployment['permissions'] == {'pages': 'write', 'id-token': 'write'}
    assert deployment['if'] == 'inputs.deploy'
    assert docs['local-ci.yml']['on']['push']['branches'] == ['main']
    assert docs['local-ci.yml']['on']['pull_request']['branches'] == ['main']
    assert set(docs['local-release.yml']['on']) == {'workflow_dispatch'}
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
