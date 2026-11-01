#!/usr/bin/env python3
"""Import orchestration only; all site builds run in build-fr-zensical.yml."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
from urllib.parse import quote

SHA = re.compile(r"[0-9a-f]{40}")


def run(*args, check=True):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result


def summary(message):
    print(message)
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
        stream.write(message + '\n')


def relative_path(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', value):
        raise ValueError(f'Invalid relative path: {value!r}')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or str(path) != value:
        raise ValueError(f'Unsafe relative path: {value!r}')
    return path


def destination_path(value):
    path = relative_path(value)
    if len(path.parts) < 2 or path.parts[0] != 'static':
        raise ValueError('Destination must be a directory beneath static/')
    current = Path('.')
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f'Symlink in destination: {current}')
        if current.exists() and not current.is_dir():
            raise ValueError(f'Destination component is not a directory: {current}')
    return Path(value)


def sites_from_json(raw):
    sites = json.loads(raw)
    if not isinstance(sites, list) or not sites:
        raise ValueError('sites must be a nonempty JSON array')
    ids, destinations = set(), []
    defaults = {'use-bun': False, 'submodules': False,
                'venv-path': '.venv', 'build-script': ''}
    allowed = {'id', 'repository', 'destination', 'sha', *defaults}
    for site in sites:
        if not isinstance(site, dict) or set(site) - allowed:
            raise ValueError('Invalid site fields')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', site.get('id', '')):
            raise ValueError('Invalid site id')
        if site['id'].lower() in ids:
            raise ValueError('Duplicate site id')
        ids.add(site['id'].lower())
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', site.get('repository', '')):
            raise ValueError('Invalid repository')
        destination = destination_path(site.get('destination', ''))
        if any(destination == other or destination in other.parents or
               other in destination.parents for other in destinations):
            raise ValueError('Overlapping destinations')
        destinations.append(destination)
        for key, value in defaults.items():
            site.setdefault(key, value)
        for key in ('use-bun', 'submodules'):
            if type(site[key]) is not bool:
                raise ValueError(f'{key} must be boolean')
        relative_path(site['venv-path'])
        if site['build-script']:
            relative_path(site['build-script'])
        if 'sha' in site and not SHA.fullmatch(site['sha']):
            raise ValueError('Invalid source SHA')
    return sites


def marker(destination):
    path = Path(destination) / '.zensical-source-sha'
    return path.read_text().strip() if path.is_file() and not path.is_symlink() else ''


def discover():
    create_pr = os.environ['CREATE_PR'] == 'true'
    repository = os.environ['GITHUB_REPOSITORY']
    if create_pr and repository.lower() != 'fedramp/fedramp.gov':
        raise ValueError('PR creation is restricted to FedRAMP/fedramp.gov')
    sites = sites_from_json(os.environ['SITES'])
    changed, failures = [], []
    for site in sites:
        result = run('git', 'ls-remote', '--exit-code',
                     f"https://github.com/{site['repository']}.git", 'refs/heads/main', check=False)
        sha = result.stdout.split()[0] if result.stdout.split() else ''
        if result.returncode or not SHA.fullmatch(sha):
            failures.append(site['repository'])
            continue
        site['sha'] = sha
        previous = marker(site['destination'])
        summary(f"{site['repository']}: {previous or 'not imported'} → {sha}")
        if previous != sha:
            changed.append(site)
    if failures:
        raise RuntimeError('Source lookup failed; no imports: ' + ', '.join(failures))
    if changed and create_pr:
        prs = json.loads(run('gh', 'pr', 'list', '--repo', repository,
                             '--base', os.environ['BASE_BRANCH'], '--head', os.environ['IMPORT_BRANCH'],
                             '--state', 'open', '--json', 'headRefOid,url').stdout)
        if prs:
            matches = True
            for site in sites:
                endpoint = (f"repos/{repository}/contents/{site['destination']}/"
                            f".zensical-source-sha?ref={quote(prs[0]['headRefOid'], safe='')}")
                pending = run('gh', 'api', endpoint, '-H', 'Accept: application/vnd.github.raw+json', check=False)
                if pending.returncode or pending.stdout.strip() != site['sha']:
                    matches = False
            if matches:
                summary('Already awaiting review: ' + prs[0]['url'])
                changed = []
    if not changed:
        summary('No new source commits to import.')
    elif not create_pr:
        summary('Build-only run: archives will be retained; no branch or PR will be written.')
    outputs = {'changed': str(bool(changed)).lower(),
               'matrix': json.dumps({'include': changed}, separators=(',', ':')),
               'base-sha': run('git', 'rev-parse', 'HEAD').stdout.strip(),
               'paths': ','.join(site['destination'] for site in changed)}
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        for key, value in outputs.items():
            stream.write(f'{key}={value}\n')


def unpack(archive, target):
    target.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, 'r:gz') as stream:
        # data filter rejects escaping paths, unsafe links and special files.
        stream.extractall(target, filter='data')


def verify_output(path):
    index = path / 'index.html'
    if not index.is_file() or not index.stat().st_size:
        raise ValueError(f'Missing or empty generated index: {index}')


def replace(source, destination):
    destination_path(str(destination))
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns('.DS_Store'))


def commit_notes(site, previous, temp):
    source = temp / ('source-' + site['id'])
    run('git', 'clone', '--no-checkout', f"https://github.com/{site['repository']}.git", str(source))
    sha = site['sha']
    run('git', '-C', str(source), 'fetch', 'origin', sha)
    prefix = ('git', '-C', str(source))
    if SHA.fullmatch(previous) and run(*prefix, 'merge-base', '--is-ancestor', previous, sha, check=False).returncode == 0:
        revision = f'{previous}..{sha}'
        count = int(run(*prefix, 'rev-list', '--no-merges', '--count', revision).stdout)
        notes = run(*prefix, 'log', '--no-merges', '-10', '--format=  - %s (%h)', revision).stdout
        if count > 10:
            notes += f'  - …and {count - 10} more commits\n'
        return notes
    return run(*prefix, 'log', '--no-merges', '-1', '--format=  - %s (%h)', sha).stdout


def matrix_sites():
    matrix = json.loads(os.environ['MATRIX'])
    sites = sites_from_json(json.dumps(matrix['include']))
    if any('sha' not in site for site in sites):
        raise ValueError('Resolved source SHA is required')
    return sites


def collect():
    sites = matrix_sites()
    temp = Path(os.environ['RUNNER_TEMP'])
    staged = temp / 'verified-sites'
    staged.mkdir()
    body = ['Built the following upstream main commits:\n\n']
    # Validate ALL archives and prepare the review body before touching static/.
    for site in sites:
        artifact = temp / 'site-artifacts' / ('import-' + site['id'])
        output = staged / site['id']
        unpack(artifact / 'site.tar.gz', output)
        verify_output(output)
        old_marker = output / '.zensical-source-sha'
        if old_marker.exists() or old_marker.is_symlink():
            raise ValueError('Build output must not contain a source marker')
        old_marker.write_text(site['sha'] + '\n')
        body.append(f"- [{site['repository']} @ {site['sha']}](https://github.com/{site['repository']}/commit/{site['sha']}) → {site['destination']}\n")
        body.append(commit_notes(site, marker(site['destination']), temp))
        versions = (artifact / 'build-info.txt').read_text().strip().replace('\n', '; ')
        body.append(f'  - Build environment: {versions}\n')
    for site in sites:
        replace(staged / site['id'], Path(site['destination']))
    with tarfile.open(temp / 'collected-sites.tar.gz', 'w:gz') as archive:
        for site in sites:
            archive.add(site['destination'], arcname=site['destination'])
    run_url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    body.append(f'\n[Workflow run]({run_url}). Manual review and merge required.\n')
    (temp / 'import-pr.md').write_text(''.join(body))
    summary('Verified and collected ' + ', '.join(site['repository'] for site in sites))


def apply():
    sites = matrix_sites()
    temp = Path(os.environ['RUNNER_TEMP'])
    staged = temp / 'apply-sites'
    unpack(temp / 'collected' / 'collected-sites.tar.gz', staged)
    for site in sites:
        output = staged / site['destination']
        verify_output(output)
        if marker(output) != site['sha']:
            raise ValueError('Collected source marker does not match resolved SHA')
    for site in sites:
        replace(staged / site['destination'], Path(site['destination']))


if __name__ == '__main__':
    try:
        {'discover': discover, 'collect': collect, 'apply': apply}[sys.argv[1]]()
    except Exception as error:
        print(f'::error::{error}', file=sys.stderr)
        sys.exit(1)
