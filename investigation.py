"""Bounded handling of untrusted investigation reports and repeated actions."""
import base64
import hashlib
import json

REPORT_NAME = 'investigation-findings.json'
REPORT_LIMIT = 65536
REPORT_PROMPT = '''
Investigate only the assigned scope. State a concrete hypothesis and necessary
conditions, test it under the available budgets, and retain diagnostic evidence.
Before requesting completion, write investigation-findings.json as a JSON object:
{"summary":"examined scope and evidence", "disposition":"finding|no_finding|blocked",
 "findings":[{"title":"...", "location":"path:function", "root_cause":"...",
 "hypothesis":"...", "conditions":"...", "reproducer":"description or command for operator review"}]}.
Use an empty findings list for no_finding or blocked. This report is an untrusted
proposal; acceptance and confirmation require independent operator review.
'''


class Stalled(RuntimeError):
    pass


class RepetitionGuard:
    def __init__(self, limit=3):
        self.limit = limit
        self.counts = {}

    def observe(self, command, code, output):
        identity = (command.strip(), code, hashlib.sha256(output.encode()).hexdigest())
        self.counts[identity] = self.counts.get(identity, 0) + 1
        if self.counts[identity] >= self.limit:
            raise Stalled('Repeated identical command and result reached the stall limit')


def import_report(bundle, evidence):
    """Validate data only; never execute a report's reproducer or accept its status."""
    entry = bundle.get(REPORT_NAME)
    if entry is None:
        return {'status': 'missing', 'findings': []}
    if not isinstance(entry, dict) or not isinstance(entry.get('data'), str) or len(entry['data']) > REPORT_LIMIT * 2:
        raise ValueError('Investigation report exceeds its size limit or has an invalid entry')
    raw = base64.b64decode(entry['data'], validate=True)
    if len(raw) > REPORT_LIMIT:
        raise ValueError('Investigation report exceeds its size limit')
    report = json.loads(raw)
    if not isinstance(report, dict) or not isinstance(report.get('summary'), str) or not report['summary'].strip():
        raise ValueError('Report requires a summary')
    if report.get('disposition') not in ('finding', 'no_finding', 'blocked'):
        raise ValueError('Report requires a valid disposition')
    findings = report.get('findings')
    if not isinstance(findings, list) or len(findings) > 100:
        raise ValueError('Report requires at most 100 findings')
    if bool(findings) != (report['disposition'] == 'finding'):
        raise ValueError('Report disposition conflicts with its findings')
    proposals = []
    fields = ('title', 'location', 'root_cause', 'hypothesis', 'conditions', 'reproducer')
    for finding in findings:
        if not isinstance(finding, dict) or any(not isinstance(finding.get(k), str) or
                not finding[k].strip() or len(finding[k]) > 8000 for k in fields):
            raise ValueError('Finding requires bounded nonempty descriptive fields')
        identity = [finding['location'].strip(), finding['root_cause'].strip()]
        proposals.append(dict({k: finding[k] for k in fields}, status='suspected',
            id=hashlib.sha256(json.dumps(identity).encode()).hexdigest(), evidence=[evidence]))
    return {'status': 'imported', 'summary': report['summary'], 'claimed_disposition': report['disposition'],
            'findings': proposals}
