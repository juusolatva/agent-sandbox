"""Trusted Docker execution boundary. No model-provided Docker arguments."""
import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
import uuid

TRANSFER = Path(__file__).resolve().parent / 'docker/transfer.py'

# Our own trusted transfer implementation, also mounted into containers; not project code.
_spec = importlib.util.spec_from_file_location('trusted_transfer', TRANSFER)
transfer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(transfer)


class _Retained:
    """Bounded copies of a process's stdout and (optionally separate) stderr."""

    def __init__(self, limit, digest):
        self.limit = limit
        self.update = digest.update if digest is not None else (lambda chunk: None)
        self.output = bytearray()
        self.errors = bytearray()
        self.truncated = False

    def add_output(self, chunk):
        self.update(chunk)
        available = self.limit - len(self.output)
        self.output.extend(chunk[:available])
        self.truncated |= len(chunk) > available

    def add_errors(self, chunk):
        self.errors.extend(chunk[:8000 - len(self.errors)])


def _drain(process, retained, argv, timeout, deadline):
    """Read every stream until EOF, raising TimeoutExpired at the deadline."""
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, retained.add_output)
        if process.stderr is not None:
            selector.register(process.stderr, selectors.EVENT_READ, retained.add_errors)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in selector.select(min(remaining, .1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if chunk:
                    key.data(chunk)
                else:
                    selector.unregister(key.fileobj)
        process.wait(timeout=max(.001, deadline - time.monotonic()))


def bounded_process(argv, timeout, limit=8000, input_bytes=None, digest=None, details=None):
    """Drain arbitrary output with bounded retention and a wall-clock deadline.

    An optional hashlib digest is updated with the complete output stream, so
    callers can compare results that differ only after the retention limit.
    When a details dict is given, stderr is kept out of the returned output and
    stored (bounded) under details['errors'], and details['truncated'] records
    whether stdout exceeded the limit, so machine-readable output stays intact.
    """
    retained = _Retained(limit, digest)
    separate = details is not None
    with tempfile.TemporaryFile() as incoming:
        if input_bytes is not None:
            incoming.write(input_bytes)
        incoming.seek(0)
        process = subprocess.Popen(argv, stdin=incoming, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE if separate else subprocess.STDOUT,
                                   start_new_session=True)
        try:
            _drain(process, retained, argv, timeout, time.monotonic() + timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            code = 124
            retained.output.extend(b'\n[execution deadline exceeded]')
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            if separate:
                process.stderr.close()
    if separate:
        details.update(errors=retained.errors.decode('utf-8', errors='replace'), truncated=retained.truncated)
    elif retained.truncated:
        retained.output.extend(b'\n[output truncated]')
    return code, retained.output.decode('utf-8', errors='replace')


class Worker:
    """Disposable command containers sharing only a run-specific home volume."""
    def __init__(self, record, events=None, deadline=None):
        self.record = record
        self.volume = 'sandbox-home-' + uuid.uuid4().hex
        self.events = events or (lambda event: None)
        self.deadline = deadline
        self.created = False

    def __enter__(self):
        subprocess.run(['docker', 'volume', 'create', self.volume], check=True,
                       stdout=subprocess.DEVNULL, timeout=30)
        self.created = True
        return self

    def __exit__(self, *exc):
        if self.created:
            subprocess.run(['docker', 'volume', 'rm', '-f', self.volume], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

    def execute(self, command, *, readonly=False, transfer=None, payload=None, limit=8000, timeout=None,
                digest=None, details=None):
        config = self.record['config']
        timeout = timeout or config.get('timeout', 600)
        if transfer:
            timeout = min(timeout, 30)
        elif self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
        if timeout <= 0:
            return 124, 'Run deadline exceeded'
        name = 'sandbox-command-' + uuid.uuid4().hex
        mount = f'type=volume,src={self.volume},dst=/home/agent'
        if readonly:
            mount += ',readonly'
        args = ['docker', 'run', '--name', name, '--rm', '--init', '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                '--memory', str(config.get('memory', '4g')), '--cpus', str(config.get('cpus', 2)),
                '--pids-limit', str(config.get('pids', 256)),
                '--tmpfs', '/tmp:rw,exec,nosuid,nodev,size=1g', '--mount', mount]
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
            args += ['--env', key + '=']
        if transfer:
            args += ['--user', '1000:1000', '--mount', f'type=bind,src={TRANSFER},dst=/opt/transfer.py,readonly',
                     '--entrypoint', 'python3']
            if payload is not None:
                args += ['-i']
            args += [self.record['image'], '-I', '/opt/transfer.py', transfer]
        else:
            args += ['--user', '1000:1000', '--env', 'HOME=/tmp/home',
                     '--env', 'GOCACHE=/tmp/go-cache', '--env', 'GOPATH=/home/agent/go',
                     '--env', 'CARGO_TARGET_DIR=/tmp/cargo-target',
                     '--env', 'PYTHONDONTWRITEBYTECODE=1',
                     '--entrypoint', '/bin/bash', self.record['image'], '-e', '-c',
                     'mkdir -p /tmp/home; ' + command]
        started = time.monotonic()
        try:
            code, output = bounded_process(args, timeout, limit, payload, digest, details)
        finally:
            # Includes timeout, Ctrl-C, and detached descendants of project commands.
            subprocess.run(['docker', 'rm', '-f', name], check=False, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        event = {'kind': 'command', 'command': command if not transfer else transfer,
                 'readonly': readonly, 'code': code, 'output': output,
                 'seconds': round(time.monotonic() - started, 3)}
        if details is not None:
            event['errors'] = details.get('errors', '')
        self.events(event)
        return code, output

    def export(self):
        details = {}
        code, output = self.execute('', readonly=True, transfer='export',
                                    limit=transfer.MAX_SERIALIZED, details=details)
        if code:
            raise RuntimeError(f'Source export failed: {(details.get("errors") or output)[-1000:]}')
        if details.get('truncated'):
            raise RuntimeError(f'Source export exceeds {transfer.MAX_SERIALIZED} serialized bytes')
        return transfer.validate(json.loads(output))

    def restore(self, bundle):
        details = {}
        code, output = self.execute('', transfer='restore', payload=json.dumps(bundle).encode(),
                                    details=details)
        if code:
            raise RuntimeError(f'Source restore failed: {(details.get("errors") or output)[-1000:]}')


def verify(record, bundle=None, events=None, deadline=None):
    policy = record['config'].get('verification')
    if not policy or not record.get('verification_hash'):
        raise ValueError('Prepare this project with a protected verification source and command first')
    with Worker(record, events, deadline) as worker:
        if bundle is not None:
            worker.restore(bundle)
        return worker.execute(policy['command'], readonly=True)
