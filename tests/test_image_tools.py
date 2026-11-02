"""Guards that analysis tools stay pinned in the Dockerfile and probed after each build."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).parents[1]
DOCKERFILE = (ROOT / 'Dockerfile').read_text()
PROFILES = json.loads((ROOT / 'docker/images.json').read_text())


class ImageToolTests(unittest.TestCase):
    def test_tools_are_pinned_to_exact_versions(self):
        for pattern in (r'staticcheck@v\d+\.\d+\.\d+\b', r'eslint@\d+\.\d+\.\d+\b',
                        r'ruff==\d+\.\d+\.\d+\b', r'hypothesis==\d+\.\d+\.\d+\b'):
            self.assertRegex(DOCKERFILE, pattern)
        self.assertNotRegex(DOCKERFILE, r'@latest|(ruff|hypothesis|eslint)\s*$')

    def test_each_build_probes_its_tools_offline(self):
        expected = {'go': ['staticcheck'], 'node': ['eslint'],
                    'python313': ['ruff', 'hypothesis'], 'python314': ['ruff', 'hypothesis']}
        for profile, tools in expected.items():
            for tool in tools:
                self.assertIn(tool, PROFILES[profile]['probe'], (profile, tool))

    def test_python_tools_install_from_wheels(self):
        self.assertRegex(DOCKERFILE, r'--only-binary :all: hypothesis==')


if __name__ == '__main__':
    unittest.main()
