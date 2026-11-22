"""The bundled protected checks must keep the verdict out of candidate-controlled processes."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1] / 'examples/verification'


class SupervisorTests(unittest.TestCase):
    def harness(self, body):
        """Run the supervisor against a Python harness whose add() is defined by body."""
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'harness.py'
            script.write_text(body + '''
import sys
for line in sys.stdin:
    left, right = map(int, line.split())
    print(f'answer: {add(left, right)}', flush=True)
''')
            return subprocess.run([sys.executable, '-I', str(ROOT / 'check.py'),
                                   sys.executable, '-I', str(script)],
                                  capture_output=True, text=True, timeout=60).returncode

    def test_correct_candidate_passes(self):
        self.assertEqual(self.harness('def add(a, b):\n    return a + b\n'), 0)

    def test_successful_premature_exit_fails(self):
        # Exiting 0 during import or on the first call must not skip later assertions.
        self.assertNotEqual(self.harness('import os\nos._exit(0)\ndef add(a, b): return a + b\n'), 0)
        self.assertNotEqual(self.harness('import sys\ndef add(a, b):\n    sys.exit(0)\n'), 0)
        self.assertNotEqual(self.harness(
            'import os\ncalls = []\ndef add(a, b):\n    calls.append(1)\n'
            '    if len(calls) == 2: os._exit(0)\n    return a + b\n'), 0)

    def test_wrong_or_noisy_answers_fail(self):
        self.assertNotEqual(self.harness('def add(a, b):\n    return 4\n'), 0)
        self.assertNotEqual(self.harness('def add(a, b):\n    print("answer: 0")\n    return a + b\n'), 0)
        self.assertEqual(self.harness('def add(a, b):\n    print("debug")\n    return a + b\n'), 0)

    def test_noisy_candidate_output_is_bounded_and_still_judged(self):
        # 50 MB of stdout and stderr noise must not be buffered, and answers still decide.
        noisy = ('import sys\n'
                 'def add(a, b):\n'
                 '    sys.stdout.write("x" * 5_000_000 + "\\n")\n'
                 '    sys.stderr.write("e" * 2_000_000)\n'
                 '    return a + b\n')
        self.assertEqual(self.harness(noisy), 0)
        self.assertNotEqual(self.harness(noisy.replace('return a + b', 'return 0')), 0)

    def test_extra_answers_fail(self):
        self.assertNotEqual(self.harness('def add(a, b):\n    print("answer: 1")\n    return a + b\n'), 0)

    def test_every_example_uses_the_shared_supervisor(self):
        import json
        for manifest in sorted((ROOT.parent).glob('*.json')):
            with self.subTest(manifest=manifest.name):
                verification = json.loads(manifest.read_text())['verification']
                self.assertEqual(verification['source'], 'verification')
                self.assertIn('python3 -I /opt/sandbox/verification/check.py ', verification['command'])
