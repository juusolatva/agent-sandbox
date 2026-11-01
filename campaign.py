"""Durable, bounded campaigns built from independent investigation runs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
import math

from investigation import REPORT_PROMPT, import_report


def save(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.campaign-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def create(directory, state_path, tasks_path):
    directory = Path(directory)
    state_bytes = Path(state_path).read_bytes()
    state = json.loads(state_bytes)
    tasks = json.loads(Path(tasks_path).read_text())
    if not isinstance(tasks, list) or not tasks or len(tasks) > 1000:
        raise ValueError('Tasks must be a nonempty JSON array of at most 1000 targets')
    targets = []
    for task in tasks:
        task = {'task': task} if isinstance(task, str) else task
        if not isinstance(task, dict) or not isinstance(task.get('task'), str) or not task['task'].strip():
            raise ValueError('Every target requires a nonempty task')
        scope = task.get('scope', task['task'])
        priority = task.get('priority', 0)
        if not isinstance(scope, str) or not scope.strip() or type(priority) is not int:
            raise ValueError('Scope must be text and priority must be an integer')
        if any(t['task'] == task['task'] and t['scope'] == scope for t in targets):
            continue
        targets.append({'task': task['task'], 'scope': scope, 'priority': priority, 'attempts': []})
    directory.mkdir(parents=True, exist_ok=False)
    save(directory / 'prepared.json', state)
    digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    save(directory / 'campaign.json', {'version': 2, 'prepared_sha256': digest,
         'tasks': targets, 'stop_reason': 'created'})


def collect_report(directory, attempt):
    """Import a bounded proposal from a retained snapshot, never from host paths."""
    run = directory / attempt['result']
    candidate = run.parent / 'candidate.json'
    if not candidate.exists():
        return {'status': 'missing', 'findings': []}
    try:
        bundle = json.loads(candidate.read_text())
        evidence = str(candidate.relative_to(directory))
        report = import_report(bundle, evidence)
    except (ValueError, TypeError, RecursionError) as exc:
        report = {'status': 'invalid', 'error': str(exc)[:1000], 'findings': []}
    save(run.parent / 'proposals.json', report)
    attempt['proposals'] = str((run.parent / 'proposals.json').relative_to(directory))
    # Exclude report wording from progress detection: only project changes count.
    bundle = json.loads(candidate.read_text())
    bundle.pop('investigation-findings.json', None)
    attempt['candidate_sha256'] = hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest()
    return report


def resume(directory, max_runs, run_timeout, steps, base_url=None, model=None, text_actions=False,
           runner=None, max_attempts=3, budget=None, continue_on_failure=False, stall_limit=2):
    budget = max_runs * run_timeout if budget is None else budget
    if any(not math.isfinite(v) or v <= 0 for v in
           (max_runs, run_timeout, steps, max_attempts, budget, stall_limit)):
        raise ValueError('Campaign limits must be finite and positive')
    directory = Path(directory)
    import fcntl
    with (directory / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / 'campaign.json'
        campaign = json.loads(path.read_text())
        record = json.loads((directory / 'prepared.json').read_text())
        if campaign.get('version') == 2:
            digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
            if digest != campaign['prepared_sha256']:
                raise ValueError('Campaign prepared state has changed')
        if runner is None:
            from controller import run as runner
        for item in campaign['tasks']:
            for attempt in item['attempts']:
                if attempt['status'] == 'running':
                    attempt['status'] = 'interrupted'
                    attempt['reason'] = 'Controller stopped before recording an outcome'
        campaign['stop_reason'] = 'run_limit'
        campaign['last_budget'] = {'runs': max_runs, 'seconds': budget, 'steps_per_run': steps,
                                   'max_attempts': max_attempts, 'stall_limit': stall_limit}
        save(path, campaign)
        deadline = time.monotonic() + budget
        # Least-attempted targets first, then operator priority and stable input order.
        queue = sorted(enumerate(campaign['tasks']), key=lambda pair:
            (len(pair[1]['attempts']), -pair[1].get('priority', 0), pair[0]))
        count = 0
        for index, item in queue:
            if item['attempts'] and item['attempts'][-1]['status'] == 'completed':
                continue
            if item.get('stalled') or len(item['attempts']) >= max_attempts:
                continue
            if count >= max_runs:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                campaign['stop_reason'] = 'time_budget'
                break
            attempt_dir = directory / 'runs' / str(index) / uuid.uuid4().hex
            attempt_dir.mkdir(parents=True)
            attempt = {'status': 'running', 'artifacts': str(attempt_dir.relative_to(directory)), 'started': time.time()}
            item['attempts'].append(attempt)
            save(path, campaign)
            try:
                prompt = item['task'] + '\nAssigned scope: ' + item.get('scope', item['task']) + REPORT_PROMPT
                code = runner(record, prompt, str(attempt_dir), base_url, model, steps, min(run_timeout, remaining),
                              text_actions=text_actions)
                results = list(attempt_dir.glob('*/result.json'))
                result = json.loads(results[0].read_text()) if len(results) == 1 else {}
                status = result.get('status', 'failed')
                if status not in ('completed', 'failed', 'exhausted', 'interrupted') or (status == 'completed' and code != 0):
                    status = 'failed'
                attempt.update(status=status, exit_code=code,
                               result=str(results[0].relative_to(directory)) if len(results) == 1 else None)
                if attempt['result']:
                    report = collect_report(directory, attempt)
                    attempt['report_status'] = report['status']
                item['attempted_at'] = time.time()
                recent = item['attempts'][-stall_limit:]
                hashes = [a.get('candidate_sha256') for a in recent]
                if status != 'completed' and (result.get('stop_reason') == 'repeated_command' or
                        (len(hashes) == stall_limit and all(hashes) and len(set(hashes)) == 1)):
                    item['stalled'] = True
                    attempt['reason'] = 'Repeated command or unchanged candidates across attempts'
            except BaseException:
                attempt.update(status='interrupted', reason='Controller interrupted during attempt')
                campaign['stop_reason'] = 'interrupted'
                save(path, campaign)
                raise
            attempt['finished'] = time.time()
            count += 1
            save(path, campaign)
            if attempt['status'] != 'completed' and not continue_on_failure:
                campaign['stop_reason'] = 'attempt_' + attempt['status']
                break
        if all(t['attempts'] and t['attempts'][-1]['status'] == 'completed' for t in campaign['tasks']):
            campaign['stop_reason'] = 'all_completed'
        elif not any(not t.get('stalled') and len(t['attempts']) < max_attempts and
                     (not t['attempts'] or t['attempts'][-1]['status'] != 'completed') for t in campaign['tasks']):
            campaign['stop_reason'] = 'no_eligible_targets'
        save(path, campaign)
        return campaign['stop_reason']


def record_finding(directory, finding):
    """Record an operator-reviewed observation, retaining duplicate reports."""
    import fcntl
    directory = Path(directory)
    required = ('title', 'location', 'root_cause', 'status', 'evidence')
    if not isinstance(finding, dict) or any(k not in finding for k in required):
        raise ValueError('Finding requires title, location, root_cause, status, and evidence')
    if any(not isinstance(finding[k], str) or not finding[k].strip() for k in required[:-1]):
        raise ValueError('Finding fields must be nonempty strings')
    if finding['status'] not in ('confirmed', 'suspected', 'rejected', 'blocked', 'no_finding'):
        raise ValueError('Invalid finding status')
    if not isinstance(finding['evidence'], list) or not finding['evidence']:
        raise ValueError('Evidence must be a nonempty list of campaign artifact paths')
    root = directory.resolve()
    for name in finding['evidence']:
        if not isinstance(name, str) or Path(name).is_absolute():
            raise ValueError('Evidence paths must be relative to the campaign')
        target = (root / name).resolve()
        if not target.is_relative_to(root / 'runs') or not target.is_file():
            raise ValueError('Evidence must refer to an existing run artifact')
    with (directory / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / 'findings.json'
        findings = json.loads(path.read_text()) if path.exists() else []
        identity = [finding['location'].strip(), finding['root_cause'].strip()]
        existing = next((f for f in findings if f['identity'] == identity), None)
        observation = {k: finding[k] for k in required}
        observation['recorded'] = time.time()
        if existing:
            existing['observations'].append(observation)
        else:
            existing = {'id': hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
                        'identity': identity, 'observations': [observation]}
            findings.append(existing)
        save(path, findings)
        return existing['id']


def replay(directory, attempt_path, command, timeout=600, worker_class=None):
    """Replay an operator-selected command on original and candidate snapshots."""
    import fcntl
    if not isinstance(command, str) or not command.strip() or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Replay requires a command and a positive timeout')
    directory = Path(directory).resolve()
    attempt = (directory / attempt_path).resolve()
    with (directory / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        campaign = json.loads((directory / 'campaign.json').read_text())
        known = [directory / a['result'] for t in campaign['tasks'] for a in t['attempts'] if a.get('result')]
        if attempt / 'result.json' not in known:
            raise ValueError('Replay must select a recorded run artifact directory')
        from controller import transfer
        bundles = {side: transfer.validate(json.loads((attempt / (side + '.json')).read_text()))
                   for side in ('original', 'candidate')}
        record = json.loads((directory / 'prepared.json').read_text())
        if campaign.get('version') == 2 and hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest() != campaign['prepared_sha256']:
            raise ValueError('Campaign prepared state has changed')
        if worker_class is None:
            from execution import Worker as worker_class
        report = {'command': command, 'timeout': timeout, 'run': attempt_path, 'results': {},
                  'status': 'running', 'image': record.get('image'),
                  'verification_hash': record.get('verification_hash'), 'started': time.time()}
        replay_dir = directory / 'runs' / 'replays' / uuid.uuid4().hex
        replay_dir.mkdir(parents=True)
        save(replay_dir / 'replay.json', report)
        try:
            for side, bundle in bundles.items():
                with worker_class(record, deadline=time.monotonic() + timeout) as worker:
                    worker.restore(bundle)
                    code, output = worker.execute(command, timeout=timeout)
                    report['results'][side] = {'code': code, 'output': output,
                        'snapshot_sha256': hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest()}
                    save(replay_dir / 'replay.json', report)
            before = report['results']['original']['code']
            after = report['results']['candidate']['code']
            report['status'] = ('failure_then_pass' if before > 0 and before != 124 and after == 0
                                else 'no_failure_then_pass')
        except BaseException:
            report['status'] = 'interrupted_or_failed'
            raise
        finally:
            report['finished'] = time.time()
            save(replay_dir / 'replay.json', report)
        return str(replay_dir.relative_to(directory))


def status(directory):
    """Return recorded progress and grouped proposals without treating claims as verdicts."""
    directory = Path(directory)
    data = json.loads((directory / 'campaign.json').read_text())
    groups = {}
    for index, target in enumerate(data['tasks']):
        for number, attempt in enumerate(target['attempts']):
            if not attempt.get('proposals'):
                continue
            report = json.loads((directory / attempt['proposals']).read_text())
            for finding in report['findings']:
                group = groups.setdefault(finding['id'], {'title': finding['title'], 'observations': []})
                group['observations'].append({'target': index, 'attempt': number, 'evidence': finding['evidence']})
    return {'campaign': data, 'proposed_findings': groups}


def review(directory, target, attempt, finding, decision):
    directory = Path(directory)
    data = json.loads((directory / 'campaign.json').read_text())
    try:
        if min(target, attempt, finding) < 0:
            raise IndexError
        entry = data['tasks'][target]['attempts'][attempt]
        report = json.loads((directory / entry['proposals']).read_text())
        proposal = report['findings'][finding]
    except (IndexError, KeyError) as exc:
        raise ValueError('Select an existing target, attempt, and proposed finding') from exc
    return record_finding(directory, dict(proposal, status=decision))
