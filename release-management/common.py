"""Small shared helpers; no third-party Python dependencies."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'FedRAMP/shared-workflows'


def run(*args, input=None, check=True, cwd=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True, cwd=cwd)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr.strip()}')
    return result


def yaml_read(path):
    # JSON preserves the string "on" with Ruby's YAML 1.1 boolean conversion fixed.
    code = '''require 'yaml'; require 'json'; d=YAML.load_file(ARGV[0]);
    d['on']=d.delete(true) if d.is_a?(Hash) && d.key?(true); puts JSON.generate(d)'''
    return json.loads(run('ruby', '-e', code, str(path)).stdout)


def output_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2) + '\n')
