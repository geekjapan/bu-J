"""Run offline acceptance checks without credentials or browser discovery."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / 'vendor/contracts/source-manifest.json').read_text())
    for source in manifest['sources']:
        if 'path' in source:
            assert hashlib.sha256((root / source['path']).read_bytes()).hexdigest() == source['sha256'], source['path']
    subprocess.run([sys.executable, '-m', 'pytest', '-q'], cwd=root, check=True)
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for mode in ('shadow', 'enforced', 'observe-only'):
            for routing in ('rules', 'model'):
                output = folder / f'{mode}-{routing}.json'
                process = subprocess.run([sys.executable, '-m', 'decision_browser', '--mode', mode,
                    '--routing', routing, '--journal', str(folder / f'{mode}-{routing}.sqlite3'),
                    '--output', str(output)], cwd=root, check=True, capture_output=True, text=True)
                result = json.loads(output.read_text())
                assert result == json.loads(process.stdout)
                assert result['state'] == ('OBSERVED' if mode == 'observe-only' else 'SUCCEEDED'), result
                if mode != 'observe-only':
                    assert [r['name'] for r in result['records']] == ['Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon']
                else:
                    assert result['steps'] == 0
    print('Offline acceptance passed. Live MCP/Chrome, Azure model task completion and performance gates remain unverified.')


if __name__ == '__main__':
    main()
