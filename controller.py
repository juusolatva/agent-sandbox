"""Host controller: inference, policy, evidence, and isolated project execution."""
import base64
import contextlib
import hashlib
import subprocess
import tempfile
import importlib.util
import json
import os
from pathlib import Path
import time
import uuid

import agent
from investigation import RepetitionGuard, Stalled
from execution import Worker, verify

# This is our own trusted transfer implementation, not project code.
spec = importlib.util.spec_from_file_location('trusted_transfer', Path(__file__).parent / 'docker/transfer.py')
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


def connect(base_url=None, model=None, timeout=120):
    if agent.OpenAI is None:
        raise ValueError('Install requirements-controller.txt in a host virtual environment first')
    if not model:
        raise ValueError('Choose a model explicitly with --model or LLM_MODEL')
    urls = [base_url] if base_url else ['http://127.0.0.1:1234/v1', 'http://10.0.0.2:1234/v1']
    errors = []
    for url in urls:
        client = agent.OpenAI(base_url=url, api_key=os.environ.get('OPENAI_API_KEY', 'lm-studio'),
                              timeout=timeout, max_retries=0)
        try:
            client.models.list(timeout=min(timeout, 5))
            return client, model, url
        except Exception as exc:
            errors.append(f'{url}: {type(exc).__name__}')
            client.close()
    raise ValueError('LM Studio connection failed: ' + '; '.join(errors))


def patch_text(before, after):
    transfer.validate(before)
    transfer.validate(after)
    with tempfile.TemporaryDirectory(prefix='sandbox-patch-') as temporary:
        root = Path(temporary)
        for side, bundle in (('a', before), ('b', after)):
            (root / side).mkdir()
            for name, entry in bundle.items():
                path = root / side / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(entry['data'], validate=True))
                path.chmod(0o755 if entry['executable'] else 0o644)
        result = subprocess.run(['git', 'diff', '--no-index', '--binary', '--no-ext-diff',
                                 '--no-textconv', '--src-prefix=', '--dst-prefix=', '--', 'a', 'b'],
                                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=30)
        if result.returncode not in (0, 1):
            raise RuntimeError('Could not generate patch')
        return result.stdout.decode('utf-8', errors='replace'), []



class Evidence:
    def __init__(self, root):
        self.path = Path(root) / uuid.uuid4().hex
        self.path.mkdir(parents=True, mode=0o700)
        self.sequence = 0

    def event(self, event):
        self.sequence += 1
        # Large exports belong in snapshots, not duplicated in transcript events.
        if event.get('command') in {'export', 'restore'}:
            event = dict(event, output=event.get('output', '')[:1000])
        with (self.path / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event, sequence=self.sequence)) + '\n')

    def save(self, name, value):
        (self.path / name).write_text(json.dumps(value, indent=2) + '\n')


class DeadlineClient:
    """Minimal chat-completions client that enforces the remaining run deadline."""

    def __init__(self, client, deadline):
        self.client = client
        self.deadline = deadline
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Run deadline exceeded')
        request_client = self.client.with_options(timeout=min(agent.LLM_TIMEOUT, remaining))
        return request_client.chat.completions.create(max_tokens=4096, **kwargs)


def save_candidate(worker, before, evidence):
    """Retain the candidate snapshot, its patch, and the changed paths."""
    candidate = worker.export()
    transfer.validate(candidate)
    evidence.save('candidate.json', candidate)
    patch_data, binary = patch_text(before, candidate)
    (evidence.path / 'candidate.patch').write_text(patch_data)
    evidence.save('changes.json', {'paths': sorted(k for k in before.keys() | candidate.keys()
                  if before.get(k) != candidate.get(k)), 'binary_or_unsupported': binary})


def run(record, task, artifact_root, base_url=None, model=None, steps=20, timeout=1800,
        client=None, text_actions=False):
    if steps <= 0 or timeout <= 0:
        raise ValueError('Stage 3 requires positive step and run-time limits')
    if not record['config'].get('verification') or not record.get('verification_hash'):
        raise ValueError('Prepare this project with protected verification before running the controller')
    evidence = Evidence(artifact_root)
    deadline = time.monotonic() + timeout
    runtime_hash = hashlib.sha256(b''.join((Path(__file__).parent / name).read_bytes()
        for name in ('controller.py', 'execution.py', 'agent.py', 'investigation.py', 'docker/transfer.py'))).hexdigest()
    result = {'runtime_hash': runtime_hash, 'command_timeout': agent.COMMAND_TIMEOUT,
              'llm_timeout': agent.LLM_TIMEOUT, 'max_response_tokens': 4096, 'status': 'failed', 'image': record['image'], 'source_hash': record['source_hash'],
              'verification_hash': record['verification_hash'], 'task': task, 'steps': steps,
              'timeout': timeout, 'text_actions': text_actions, 'baseline_verified': False, 'candidate_verified': False}
    evidence.save('input.json', dict(record, task=task))
    outcome = agent.Outcome.FAILED
    worker = None
    before = None
    connected = client is None
    try:
        code, output = verify(record, events=evidence.event, deadline=deadline)
        if code:
            raise ValueError(f'Protected baseline failed: {output}')
        result['baseline_verified'] = True
        if client is None:
            client, model, base_url = connect(base_url, model, min(agent.LLM_TIMEOUT, timeout))
        result.update(model=model, base_url=base_url)
        with Worker(record, evidence.event, deadline) as worker:
            before = worker.export()
            transfer.validate(before)
            evidence.save('original.json', before)

            repetition = RepetitionGuard()

            def execute(command, label='sandbox $'):
                code, output = worker.execute(command, timeout=agent.COMMAND_TIMEOUT)
                repetition.observe(command, code, output)
                return code, f"Exit code: {code}\n{output}"

            def gate():
                candidate = worker.export()
                transfer.validate(candidate)
                code, output = verify(record, candidate, evidence.event, deadline)
                result['candidate_verified'] = code == 0
                return code == 0, output

            def infer(client, model, messages):
                bounded = DeadlineClient(client, deadline)
                if text_actions:
                    response = bounded.create(model=model,
                        messages=agent.flatten_messages_for_fallback(messages))
                    msg, fallback = response.choices[0].message, True
                else:
                    msg, fallback = agent.call_llm_resilient(bounded, model, messages)
                if msg is not None:
                    evidence.event({'kind': 'assistant', 'message': msg.model_dump(), 'fallback': fallback})
                return msg, fallback

            with (evidence.path / 'controller.log').open('w') as log, contextlib.redirect_stdout(log):
                try:
                    outcome = agent.run_agent_loop(client, model, task, execute=execute,
                                                   verify=gate, infer=infer, steps=steps)
                finally:
                    save_candidate(worker, before, evidence)
        result['status'] = outcome.name.lower()
    except KeyboardInterrupt:
        outcome = agent.Outcome.INTERRUPTED
        result['status'] = 'interrupted'
    except Exception as exc:
        outcome = agent.Outcome.FAILED
        result['status'] = 'failed'
        result['error'] = str(exc)[:4000]
        if isinstance(exc, Stalled):
            result['stop_reason'] = 'repeated_command'
    finally:
        if connected and client is not None:
            client.close()
        evidence.save('result.json', result)
        print(f"Run {result['status']}; evidence: {evidence.path}")
    return int(outcome)
