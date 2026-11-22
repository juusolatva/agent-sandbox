"""Durable, bounded campaigns built from independent investigation runs."""
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

from investigation import REPORT_NAME, REPORT_PROMPT, import_report

CAMPAIGN_FILE = 'campaign.json'
PREPARED_FILE = 'prepared.json'
PROPOSALS_FILE = 'proposals.json'
LOCK_FILE = '.lock'
REPLAY_FILE = 'replay.json'
RUN_STATUSES = ('completed', 'failed', 'exhausted', 'interrupted')
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
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prepared_digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def check_prepared(campaign, record):
    """Reject a version 2 campaign whose prepared state no longer matches creation."""
    if campaign.get('version') == 2 and prepared_digest(record) != campaign['prepared_sha256']:
        raise ValueError('Campaign prepared state has changed')


def parse_target(task):
    """Normalize a task string or {task, scope, priority} object into a campaign target."""
    task = {'task': task} if isinstance(task, str) else task
    if not isinstance(task, dict) or not isinstance(task.get('task'), str) or not task['task'].strip():
        raise ValueError('Every target requires a nonempty task')
    scope = task.get('scope', task['task'])
    priority = task.get('priority', 0)
    if not isinstance(scope, str) or not scope.strip() or type(priority) is not int:
        raise ValueError('Scope must be text and priority must be an integer')
    return {'task': task['task'], 'scope': scope, 'priority': priority, 'attempts': []}


def validate_state(state):
    """Fail at creation, not mid-campaign, when prepared state lacks protected verification."""
    if (not isinstance(state, dict) or not isinstance(state.get('image'), str)
            or not isinstance(state.get('config'), dict) or not state['config'].get('verification')
            or not isinstance(state.get('verification_hash'), str)):
        raise ValueError('Prepared state requires image, config.verification, and verification_hash')


def create(directory, state_path, tasks_path):
    directory = Path(directory)
    state = json.loads(Path(state_path).read_bytes())
    validate_state(state)
    tasks = json.loads(Path(tasks_path).read_text())
    if not isinstance(tasks, list) or not tasks or len(tasks) > 1000:
        raise ValueError('Tasks must be a nonempty JSON array of at most 1000 targets')
    targets = []
    for task in tasks:
        target = parse_target(task)
        if not any(t['task'] == target['task'] and t['scope'] == target['scope'] for t in targets):
            targets.append(target)
    directory.mkdir(parents=True, exist_ok=False)
    save(directory / PREPARED_FILE, state)
    save(directory / CAMPAIGN_FILE, {'version': 2, 'prepared_sha256': prepared_digest(state),
         'tasks': targets, 'stop_reason': 'created'})


def collect_report(directory, attempt):
    """Import a bounded proposal from a retained snapshot, never from host paths."""
    run = directory / attempt['result']
    candidate = run.parent / 'candidate.json'
    if not candidate.exists():
        return {'status': 'missing', 'findings': []}
    from execution import transfer
    bundle = None
    try:
        with candidate.open('rb') as stream:
            bundle = transfer.load(stream)
        report = import_report(bundle, str(candidate.relative_to(directory)))
    except (ValueError, TypeError, RecursionError) as exc:
        report = {'status': 'invalid', 'error': str(exc)[:1000], 'findings': []}
    save(run.parent / PROPOSALS_FILE, report)
    attempt['proposals'] = str((run.parent / PROPOSALS_FILE).relative_to(directory))
    if isinstance(bundle, dict):
        # Exclude report wording from progress detection: only project changes count.
        project = {name: entry for name, entry in bundle.items() if name != REPORT_NAME}
        attempt['candidate_sha256'] = hashlib.sha256(json.dumps(project, sort_keys=True).encode()).hexdigest()
    return report


def is_completed(item):
    return bool(item['attempts']) and item['attempts'][-1]['status'] == 'completed'


def is_eligible(item, max_attempts):
    return not is_completed(item) and not item.get('stalled') and len(item['attempts']) < max_attempts


def mark_stale_attempts(campaign):
    """Record attempts left running by a stopped controller as interrupted."""
    for item in campaign['tasks']:
        for attempt in item['attempts']:
            if attempt['status'] == 'running':
                attempt['status'] = 'interrupted'
                attempt['reason'] = 'Controller stopped before recording an outcome'


def attempt_status(result, code):
    """Accept only known run statuses; completion also requires a zero exit code."""
    status = result.get('status', 'failed')
    if status not in RUN_STATUSES or (status == 'completed' and code != 0):
        return 'failed'
    return status


def mark_stall(item, attempt, result, stall_limit):
    """Stop retrying a target after repeated commands or unchanged candidates."""
    if attempt['status'] == 'completed':
        return
    hashes = [a.get('candidate_sha256') for a in item['attempts'][-stall_limit:]]
    unchanged = len(hashes) == stall_limit and all(hashes) and len(set(hashes)) == 1
    if result.get('stop_reason') == 'repeated_command' or unchanged:
        item['stalled'] = True
        attempt['reason'] = 'Repeated command or unchanged candidates across attempts'


def run_attempt(directory, path, campaign, index, launch, remaining, stall_limit):
    """Run one durable attempt for target index, saving state before and after."""
    item = campaign['tasks'][index]
    attempt_dir = directory / 'runs' / str(index) / uuid.uuid4().hex
    attempt_dir.mkdir(parents=True)
    attempt = {'status': 'running', 'artifacts': str(attempt_dir.relative_to(directory)), 'started': time.time()}
    item['attempts'].append(attempt)
    save(path, campaign)
    try:
        code = launch(item, attempt_dir, remaining)
        results = list(attempt_dir.glob('*/result.json'))
        result = json.loads(results[0].read_text()) if len(results) == 1 else {}
        attempt.update(status=attempt_status(result, code), exit_code=code,
                       result=str(results[0].relative_to(directory)) if len(results) == 1 else None)
        if attempt['result']:
            attempt['report_status'] = collect_report(directory, attempt)['status']
        item['attempted_at'] = time.time()
        mark_stall(item, attempt, result, stall_limit)
    except BaseException:
        attempt.update(status='interrupted', reason='Controller interrupted during attempt')
        campaign['stop_reason'] = 'interrupted'
        save(path, campaign)
        raise
    attempt['finished'] = time.time()
    save(path, campaign)
    return attempt


def final_stop_reason(campaign, max_attempts):
    tasks = campaign['tasks']
    if all(is_completed(t) for t in tasks):
        return 'all_completed'
    if not any(is_eligible(t, max_attempts) for t in tasks):
        return 'no_eligible_targets'
    return campaign['stop_reason']


def next_target(campaign, max_attempts):
    """Least-attempted eligible target first, then operator priority and stable input order."""
    eligible = [(index, item) for index, item in enumerate(campaign['tasks']) if is_eligible(item, max_attempts)]
    if not eligible:
        return None
    return min(eligible, key=lambda pair: (len(pair[1]['attempts']), -pair[1].get('priority', 0), pair[0]))[0]


def validated_budget(max_runs, run_timeout, steps, max_attempts, budget, stall_limit):
    """Check campaign limits and return the overall time budget in seconds."""
    if any(type(v) is not int for v in (max_runs, steps, max_attempts, stall_limit)):
        raise ValueError('Run, step, attempt, and stall limits must be integers')
    if stall_limit < 2:
        raise ValueError('Stall limit must be at least 2: it compares candidates across attempts')
    budget = max_runs * run_timeout if budget is None else budget
    if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in
           (max_runs, run_timeout, steps, max_attempts, budget)):
        raise ValueError('Campaign limits must be finite and positive')
    return budget


def run_targets(directory, path, campaign, launch, deadline, max_runs, max_attempts,
                stall_limit, continue_on_failure):
    """Run attempts until a limit stops the campaign, setting its stop_reason."""
    count = 0
    # Re-select after every attempt so retries use the remaining run budget.
    while (index := next_target(campaign, max_attempts)) is not None:
        if count >= max_runs:
            campaign['stop_reason'] = 'run_limit'
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            campaign['stop_reason'] = 'time_budget'
            return
        attempt = run_attempt(directory, path, campaign, index, launch, remaining, stall_limit)
        count += 1
        if attempt['status'] == 'interrupted':
            # The controller converts Ctrl-C/SIGTERM into an interrupted run; stop everything.
            campaign['stop_reason'] = 'interrupted'
            save(path, campaign)
            raise KeyboardInterrupt
        if attempt['status'] != 'completed' and not continue_on_failure:
            campaign['stop_reason'] = 'attempt_' + attempt['status']
            return


def resume(directory, max_runs, run_timeout, steps, base_url=None, model=None, text_actions=False,
           runner=None, max_attempts=3, budget=None, continue_on_failure=False, stall_limit=2,
           command_timeout=None):
    budget = validated_budget(max_runs, run_timeout, steps, max_attempts, budget, stall_limit)
    directory = Path(directory)
    # Exclusive lock prevents concurrent controllers from claiming the same task.
    with locked(directory):
        path = directory / CAMPAIGN_FILE
        campaign = json.loads(path.read_text())
        record = json.loads((directory / PREPARED_FILE).read_text())
        check_prepared(campaign, record)
        if runner is None:
            from controller import run as runner

        def launch(item, attempt_dir, remaining):
            prompt = item['task'] + '\nAssigned scope: ' + item.get('scope', item['task']) + REPORT_PROMPT
            extra = {} if command_timeout is None else {'command_timeout': command_timeout}
            return runner(record, prompt, str(attempt_dir), base_url, model, steps, min(run_timeout, remaining),
                          text_actions=text_actions, **extra)

        mark_stale_attempts(campaign)
        campaign['stop_reason'] = 'running'
        campaign['last_budget'] = {'runs': max_runs, 'seconds': budget, 'steps_per_run': steps,
                                   'max_attempts': max_attempts, 'stall_limit': stall_limit}
        save(path, campaign)
        run_targets(directory, path, campaign, launch, time.monotonic() + budget, max_runs,
                    max_attempts, stall_limit, continue_on_failure)
        campaign['stop_reason'] = final_stop_reason(campaign, max_attempts)
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
    if not isinstance(command, str) or not command.strip() or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Replay requires a command and a positive timeout')
    directory = Path(directory).resolve()
    attempt = (directory / attempt_path).resolve()
    with locked(directory):
        campaign = json.loads((directory / CAMPAIGN_FILE).read_text())
        known = [directory / a['result'] for t in campaign['tasks'] for a in t['attempts'] if a.get('result')]
        if attempt / 'result.json' not in known:
            raise ValueError('Replay must select a recorded run artifact directory')
        from execution import transfer
        bundles = {}
        for side in ('original', 'candidate'):
            with (attempt / (side + '.json')).open('rb') as stream:
                bundles[side] = transfer.load(stream)
        record = json.loads((directory / PREPARED_FILE).read_text())
        check_prepared(campaign, record)
        if worker_class is None:
            from execution import Worker as worker_class
        report = {'command': command, 'timeout': timeout, 'run': attempt_path, 'results': {},
                  'status': 'running', 'image': record.get('image'),
                  'verification_hash': record.get('verification_hash'), 'started': time.time()}
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
            report['finished'] = time.time()
            save(replay_dir / REPLAY_FILE, report)
        return str(replay_dir.relative_to(directory))


def status(directory):
    """Return recorded progress and grouped proposals without treating claims as verdicts."""
    directory = Path(directory)
    data = json.loads((directory / CAMPAIGN_FILE).read_text())
    groups = {}
    for index, target in enumerate(data['tasks']):
        for number, attempt in enumerate(target['attempts']):
            if not attempt.get('proposals'):
                continue
            report = json.loads((directory / attempt['proposals']).read_text())
            # target/attempt/finding are exactly the indices review() expects.
            for position, finding in enumerate(report['findings']):
                group = groups.setdefault(finding['id'], {'title': finding['title'], 'observations': []})
                group['observations'].append({'target': index, 'attempt': number, 'finding': position,
                                              'evidence': finding['evidence']})
    return {'campaign': data, 'proposed_findings': groups}


def review(directory, target, attempt, finding, decision):
    directory = Path(directory)
    data = json.loads((directory / CAMPAIGN_FILE).read_text())
    try:
        if min(target, attempt, finding) < 0:
            raise IndexError
        entry = data['tasks'][target]['attempts'][attempt]
        report = json.loads((directory / entry['proposals']).read_text())
        proposal = report['findings'][finding]
    except (IndexError, KeyError) as exc:
        raise ValueError('Select an existing target, attempt, and proposed finding') from exc
    return record_finding(directory, dict(proposal, status=decision))
