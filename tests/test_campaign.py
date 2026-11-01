import json
import tempfile
import unittest
from pathlib import Path

import campaign


class CampaignTests(unittest.TestCase):
    def test_resume_retries_and_retains_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'state').write_text('{}')
            (root / 'tasks').write_text(json.dumps(['one', 'one', 'two']))
            directory = root / 'campaign'
            campaign.create(directory, root / 'state', root / 'tasks')
            statuses = iter(['failed', 'completed', 'completed'])
            def runner(record, task, artifacts, *args, **kwargs):
                result = Path(artifacts) / 'run'
                result.mkdir()
                status = next(statuses)
                (result / 'result.json').write_text(json.dumps({'status': status}))
                return 0 if status == 'completed' else 1
            self.assertEqual(campaign.resume(directory, 3, 1, 1, runner=runner), 'attempt_failed')
            self.assertEqual(campaign.resume(directory, 3, 1, 1, runner=runner), 'all_completed')
            data = json.loads((directory / 'campaign.json').read_text())
            self.assertEqual([len(t['attempts']) for t in data['tasks']], [2, 1])
            self.assertTrue((directory / data['tasks'][0]['attempts'][0]['result']).exists())
