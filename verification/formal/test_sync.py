import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from verification.formal.run import verified_commit


class SourceCommitTest(unittest.TestCase):
    def test_commit_must_contain_exact_covered_bytes(self):
        with tempfile.TemporaryDirectory(prefix='decision-browser-sync-') as scratch:
            root = Path(scratch)
            def git(*args):
                return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null',
                    '-c', 'commit.gpgsign=false', '-c', 'user.name=Verification',
                    '-c', 'user.email=verification@example.invalid', *args], cwd=root,
                    capture_output=True, check=True, text=True, timeout=10).stdout.strip()
            git('init')
            source = root / 'source.py'
            source.write_text('value = 1\n')
            hashes = {'source.py': hashlib.sha256(source.read_bytes()).hexdigest()}
            self.assertIsNone(verified_commit(root, hashes))
            git('add', 'source.py')
            git('commit', '-m', 'Fixture baseline')
            self.assertEqual(verified_commit(root, hashes), git('rev-parse', 'HEAD'))
            source.write_text('value = 2\n')
            dirty = {'source.py': hashlib.sha256(source.read_bytes()).hexdigest()}
            self.assertIsNone(verified_commit(root, dirty))
            self.assertIsNone(verified_commit(root, {'untracked.py': hashes['source.py']}))


if __name__ == '__main__':
    unittest.main()
