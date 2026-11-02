"""Durable, bounded campaigns built from independent investigation runs."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

CAMPAIGN_FILE = 'campaign.json'
PREPARED_FILE = 'prepared.json'
LOCK_FILE = '.lock'
REPLAY_FILE = 'replay.json'
FINDING_FIELDS = ('title', 'location', 'root_cause', 'status', 'evidence')
FINDING_STATUSES = ('confirmed', 'suspected', 'rejected', 'blocked', 'no_finding')


@contextlib.contextmanager
def locked(directory):
    """Hold the campaign's exclusive, non-blocking controller lock."""
    import fcntl
    with (Path(directory) / LOCK_FILE).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


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
    save(directory / PREPARED_FILE, state)
    unique = list(dict.fromkeys(tasks))
    save(directory / CAMPAIGN_FILE, {'version': 1, 'prepared_sha256': hashlib.sha256(state_bytes).hexdigest(),
         'tasks': [{'task': t, 'attempts': []} for t in unique], 'stop_reason': 'created'})


def is_completed(item):
    return bool(item['attempts']) and item['attempts'][-1]['status'] == 'completed'


def mark_stale_attempts(campaign):
    """Record attempts left running by a stopped controller as interrupted."""
    for item in campaign['tasks']:
        for attempt in item['attempts']:
            if attempt['status'] == 'running':
                attempt['status'] = 'interrupted'
                attempt['reason'] = 'Controller stopped before recording an outcome'


def run_attempt(directory, path, campaign, index, launch):
    """Run one durable attempt for task index, saving state before and after."""
    item = campaign['tasks'][index]
    attempt_dir = directory / 'runs' / str(index) / uuid.uuid4().hex
    attempt_dir.mkdir(parents=True)
    attempt = {'status': 'running', 'artifacts': str(attempt_dir.relative_to(directory)), 'started': time.time()}
    item['attempts'].append(attempt)
    save(path, campaign)
    try:
        code = launch(item['task'], attempt_dir)
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
    save(path, campaign)
    return attempt


def resume(directory, max_runs, run_timeout, steps, base_url=None, model=None, text_actions=False, runner=None, max_attempts=3):
    if max_runs <= 0 or run_timeout <= 0 or steps <= 0 or max_attempts <= 0:
        raise ValueError('Campaign limits must be positive')
    directory = Path(directory)
    # Exclusive lock prevents concurrent controllers from claiming the same task.
    with locked(directory):
        path = directory / CAMPAIGN_FILE
        campaign = json.loads(path.read_text())
        record = json.loads((directory / PREPARED_FILE).read_text())
        if runner is None:
            from controller import run as runner

        def launch(task, attempt_dir):
            return runner(record, task, str(attempt_dir), base_url, model, steps, run_timeout,
                          text_actions=text_actions)

        mark_stale_attempts(campaign)
        campaign['stop_reason'] = 'run_limit'
        save(path, campaign)
        count = 0
        for index, item in enumerate(campaign['tasks']):
            if is_completed(item):
                continue
            if len(item['attempts']) >= max_attempts:
                campaign['stop_reason'] = 'attempt_limit'
                continue
            if count >= max_runs:
                break
            attempt = run_attempt(directory, path, campaign, index, launch)
            count += 1
            if attempt['status'] != 'completed':
                campaign['stop_reason'] = 'attempt_' + attempt['status']
                break
        else:
            campaign['stop_reason'] = ('all_completed' if all(is_completed(t) for t in campaign['tasks'])
                                       else 'attempt_limit')
        save(path, campaign)
        return campaign['stop_reason']


def validate_finding(root, finding):
    """Validate an operator finding and its links to existing run artifacts under root."""
    required = FINDING_FIELDS
    if not isinstance(finding, dict) or any(k not in finding for k in required):
        raise ValueError('Finding requires title, location, root_cause, status, and evidence')
    if any(not isinstance(finding[k], str) or not finding[k].strip() for k in required[:-1]):
        raise ValueError('Finding fields must be nonempty strings')
    if finding['status'] not in FINDING_STATUSES:
        raise ValueError('Invalid finding status')
    if not isinstance(finding['evidence'], list) or not finding['evidence']:
        raise ValueError('Evidence must be a nonempty list of campaign artifact paths')
    for name in finding['evidence']:
        if not isinstance(name, str) or Path(name).is_absolute():
            raise ValueError('Evidence paths must be relative to the campaign')
        target = (root / name).resolve()
        if not target.is_relative_to(root / 'runs') or not target.is_file():
            raise ValueError('Evidence must refer to an existing run artifact')


def record_finding(directory, finding):
    """Record an operator-reviewed observation, retaining duplicate reports."""
    directory = Path(directory)
    validate_finding(directory.resolve(), finding)
    with locked(directory):
        path = directory / 'findings.json'
        findings = json.loads(path.read_text()) if path.exists() else []
        identity = [finding['location'].strip(), finding['root_cause'].strip()]
        existing = next((f for f in findings if f['identity'] == identity), None)
        observation = {k: finding[k] for k in FINDING_FIELDS}
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
    if not isinstance(command, str) or not command.strip() or timeout <= 0:
        raise ValueError('Replay requires a command and a positive timeout')
    directory = Path(directory).resolve()
    attempt = (directory / attempt_path).resolve()
    with locked(directory):
        campaign = json.loads((directory / CAMPAIGN_FILE).read_text())
        known = [directory / a['result'] for t in campaign['tasks'] for a in t['attempts'] if a.get('result')]
        if attempt / 'result.json' not in known:
            raise ValueError('Replay must select a recorded run artifact directory')
        from controller import transfer
        bundles = {side: transfer.validate(json.loads((attempt / (side + '.json')).read_text()))
                   for side in ('original', 'candidate')}
        record = json.loads((directory / PREPARED_FILE).read_text())
        if worker_class is None:
            from execution import Worker as worker_class
        report = {'command': command, 'timeout': timeout, 'run': attempt_path, 'results': {},
                  'status': 'running'}
        replay_dir = directory / 'runs' / 'replays' / uuid.uuid4().hex
        replay_dir.mkdir(parents=True)
        save(replay_dir / REPLAY_FILE, report)
        try:
            for side, bundle in bundles.items():
                with worker_class(record, deadline=time.monotonic() + timeout) as worker:
                    worker.restore(bundle)
                    code, output = worker.execute(command, timeout=timeout)
                    report['results'][side] = {'code': code, 'output': output,
                        'snapshot_sha256': hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest()}
                    save(replay_dir / REPLAY_FILE, report)
            before = report['results']['original']['code']
            after = report['results']['candidate']['code']
            report['status'] = ('failure_then_pass' if before > 0 and before != 124 and after == 0
                                else 'no_failure_then_pass')
        except BaseException:
            report['status'] = 'interrupted_or_failed'
            raise
        finally:
            save(replay_dir / REPLAY_FILE, report)
        return str(replay_dir.relative_to(directory))
