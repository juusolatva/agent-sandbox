import base64
import json
import unittest

from investigation import import_report, REPORT_NAME, RepetitionGuard, Stalled


def report_bundle(report):
    return {REPORT_NAME: {'data': base64.b64encode(json.dumps(report).encode()).decode(), 'executable': False}}


def finding_report():
    return {'summary': 'Examined parser', 'disposition': 'finding', 'findings': [
        {'title': 'Overflow', 'location': 'parse.c:decode', 'root_cause': 'unchecked size',
         'hypothesis': 'Size wraps', 'conditions': 'Large input', 'reproducer': 'operator review required',
         'status': 'confirmed', 'evidence': ['/etc/passwd']}]}


class InvestigationTests(unittest.TestCase):
    def test_model_cannot_confirm_findings_or_supply_host_evidence(self):
        imported = import_report(report_bundle(finding_report()), 'runs/0/run/candidate.json')
        proposal = imported['findings'][0]
        self.assertEqual(proposal['status'], 'suspected')
        self.assertEqual(proposal['evidence'], ['runs/0/run/candidate.json'])
        self.assertEqual(imported['claimed_disposition'], 'finding')

    def test_invalid_and_oversized_reports_are_rejected(self):
        for report in ([], {'summary': 'test', 'disposition': 'confirmed', 'findings': []},
                       {'summary': 'test', 'disposition': 'no_finding', 'findings': [{}]}):
            bundle = report_bundle(report)
            with self.subTest(report=report), self.assertRaises(ValueError):
                import_report(bundle, 'evidence')
        with self.assertRaises(ValueError):
            import_report({REPORT_NAME: {'data': 'x' * 200000}}, 'evidence')

    def test_repeat_guard_compares_commands_and_results(self):
        guard = RepetitionGuard()
        guard.observe('test', 1, 'failed')
        guard.observe('test', 0, 'passed')
        guard.observe('test', 1, 'failed')
        with self.assertRaises(Stalled):
            guard.observe('test', 1, 'failed')
