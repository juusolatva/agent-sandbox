"""Durable, bounded campaigns built from independent investigation runs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


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


def resume(directory, max_runs, run_timeout, steps, base_url=None, model=None, text_actions=False, runner=None):
    if max_runs <= 0 or run_timeout <= 0 or steps <= 0:
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
            if count >= max_runs:
                break
            attempt_dir = directory / 'runs' / str(campaign['tasks'].index(item)) / str(len(item['attempts']))
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
            campaign['stop_reason'] = 'all_completed'
        save(path, campaign)
        return campaign['stop_reason']
