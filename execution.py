"""Trusted Docker execution boundary. No model-provided Docker arguments."""
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


def bounded_process(argv, timeout, limit=8000, input_bytes=None, digest=None):
    """Drain arbitrary output with bounded retention and a wall-clock deadline.

    An optional hashlib digest is updated with the complete output stream, so
    callers can compare results that differ only after the retention limit.
    """
    update = digest.update if digest is not None else (lambda chunk: None)
    output = bytearray()
    truncated = False
    with tempfile.TemporaryFile() as incoming:
        if input_bytes is not None:
            incoming.write(input_bytes)
        incoming.seek(0)
        process = subprocess.Popen(argv, stdin=incoming, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    for key, _ in selector.select(min(remaining, .1)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            update(chunk)
                            available = limit - len(output)
                            output.extend(chunk[:available])
                            truncated |= len(chunk) > available
                process.wait(timeout=max(.001, deadline - time.monotonic()))
            code = process.returncode
        except subprocess.TimeoutExpired:
            code = 124
            output.extend(b'\n[execution deadline exceeded]')
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
    if truncated:
        output.extend(b'\n[output truncated]')
    return code, output.decode('utf-8', errors='replace')


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
                digest=None):
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
            code, output = bounded_process(args, timeout, limit, payload, digest)
        finally:
            # Includes timeout, Ctrl-C, and detached descendants of project commands.
            subprocess.run(['docker', 'rm', '-f', name], check=False, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.events({'kind': 'command', 'command': command if not transfer else transfer,
                     'readonly': readonly, 'code': code, 'output': output,
                     'seconds': round(time.monotonic() - started, 3)})
        return code, output

    def export(self):
        code, output = self.execute('', readonly=True, transfer='export', limit=24 * 1024 * 1024)
        if code:
            raise RuntimeError(f'Source export failed: {output[:1000]}')
        return json.loads(output)

    def restore(self, bundle):
        code, output = self.execute('', transfer='restore', payload=json.dumps(bundle).encode())
        if code:
            raise RuntimeError(f'Source restore failed: {output[:1000]}')


def verify(record, bundle=None, events=None, deadline=None):
    policy = record['config'].get('verification')
    if not policy or not record.get('verification_hash'):
        raise ValueError('Prepare this project with a protected verification source and command first')
    with Worker(record, events, deadline) as worker:
        if bundle is not None:
            worker.restore(bundle)
        return worker.execute(policy['command'], readonly=True)
