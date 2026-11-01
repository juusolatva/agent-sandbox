"""Durable, bounded campaigns built from independent investigation runs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid


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
    finally:
        if os.path.exists(name):
            os.unlink(name)


def create(directory, state_path, tasks_path):
    directory = Path(directory)
    state_bytes = Path(state_path).read_bytes()
    state = json.loads(state_bytes)
    tasks = json.loads(Path(tasks_path).read_text())
    if not isinstance(tasks, list) or not tasks or any(not isinstance(t, str) or not t.strip() for t in tasks):
        raise ValueError('Tasks must be a nonempty JSON array of nonempty strings')
    directory.mkdir(parents=True, exist_ok=False)
    save(directory / 'prepared.json', state)
    unique = list(dict.fromkeys(tasks))
    save(directory / 'campaign.json', {'version': 1, 'prepared_sha256': hashlib.sha256(state_bytes).hexdigest(),
         'tasks': [{'task': t, 'attempts': []} for t in unique], 'stop_reason': 'created'})


def resume(directory, max_runs, run_timeout, steps, base_url=None, model=None, text_actions=False, runner=None, max_attempts=3):
    if max_runs <= 0 or run_timeout <= 0 or steps <= 0 or max_attempts <= 0:
        raise ValueError('Campaign limits must be positive')
    directory = Path(directory)
    # Exclusive lock prevents concurrent controllers from claiming the same task.
    import fcntl
    with (directory / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / 'campaign.json'
        campaign = json.loads(path.read_text())
        record = json.loads((directory / 'prepared.json').read_text())
        if runner is None:
            from controller import run as runner
        for item in campaign['tasks']:
            for attempt in item['attempts']:
                if attempt['status'] == 'running':
                    attempt['status'] = 'interrupted'
                    attempt['reason'] = 'Controller stopped before recording an outcome'
        campaign['stop_reason'] = 'run_limit'
        save(path, campaign)
        count = 0
        for item in campaign['tasks']:
            if item['attempts'] and item['attempts'][-1]['status'] == 'completed':
                continue
            if len(item['attempts']) >= max_attempts:
                campaign['stop_reason'] = 'attempt_limit'
                continue
            if count >= max_runs:
                break
            attempt_dir = directory / 'runs' / str(campaign['tasks'].index(item)) / uuid.uuid4().hex
            attempt_dir.mkdir(parents=True)
            attempt = {'status': 'running', 'artifacts': str(attempt_dir.relative_to(directory)), 'started': time.time()}
            item['attempts'].append(attempt)
            save(path, campaign)
            try:
                code = runner(record, item['task'], str(attempt_dir), base_url, model, steps, run_timeout,
                              text_actions=text_actions)
                results = list(attempt_dir.glob('*/result.json'))
                result = json.loads(results[0].read_text()) if len(results) == 1 else {}
                attempt.update(status=result.get('status', 'failed'), exit_code=code,
                               result=str(results[0].relative_to(directory)) if len(results) == 1 else None)
            except BaseException:
                attempt.update(status='interrupted', reason='Controller interrupted during attempt')
                campaign['stop_reason'] = 'interrupted'
                save(path, campaign)
                raise
            attempt['finished'] = time.time()
            count += 1
            save(path, campaign)
            if attempt['status'] != 'completed':
                campaign['stop_reason'] = 'attempt_' + attempt['status']
                break
        else:
            campaign['stop_reason'] = ('all_completed' if all(
                t['attempts'] and t['attempts'][-1]['status'] == 'completed'
                for t in campaign['tasks']) else 'attempt_limit')
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
    if not isinstance(command, str) or not command.strip() or timeout <= 0:
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
        if worker_class is None:
            from execution import Worker as worker_class
        report = {'command': command, 'timeout': timeout, 'run': attempt_path, 'results': {},
                  'status': 'running'}
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
            save(replay_dir / 'replay.json', report)
        return str(replay_dir.relative_to(directory))
