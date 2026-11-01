#!/usr/bin/env python3
"""Operator CLI for language images and prepared, offline project environments."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
PROFILES = json.loads((ROOT / 'docker/images.json').read_text())
EXCLUDED = {'.git', '.ssh', '.aws', '.codex', '.agents', '__pycache__', '.venv',
            'node_modules', 'target', 'artifacts', '.npmrc', '.pypirc', '.netrc'}


def docker(*args, capture=False, timeout=None):
    result = subprocess.run(['docker', *args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None, timeout=timeout)
    return result.stdout.strip() if capture else None


def load_project(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    allowed = {'name', 'source', 'profile', 'prepare', 'check', 'memory', 'cpus', 'pids', 'timeout', 'verification'}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError('Project manifest must be an object with documented fields')
    for field in ('name', 'source', 'profile', 'check'):
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise ValueError(f'{field} must be a nonempty string')
    if config['profile'] not in PROFILES:
        raise ValueError('Unknown language profile')
    if not isinstance(config.get('prepare', []), list) or any(
            not isinstance(c, str) or not c.strip() for c in config.get('prepare', [])):
        raise ValueError('prepare must be a list of nonempty shell commands')
    for field, default in (('cpus', 2), ('pids', 256), ('timeout', 600)):
        value = config.get(field, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 100000:
            raise ValueError(f'{field} must be a positive bounded number')
    source = (path.parent / config['source']).resolve()
    if not source.is_dir():
        raise ValueError(f'Source directory does not exist: {source}')
    verification = config.get('verification')
    if verification is not None:
        if not isinstance(verification, dict) or set(verification) != {'source', 'command'}:
            raise ValueError('verification requires source and command')
        if any(not isinstance(v, str) or not v.strip() for v in verification.values()):
            raise ValueError('verification fields must be nonempty strings')
        if not (path.parent / verification['source']).resolve().is_dir():
            raise ValueError('Verification source directory does not exist')
    return config, source


def ignored(directory, names):
    return {name for name in names if name in EXCLUDED or name.startswith('.env')}


def snapshot(source, destination):
    # Do not follow symlinks into host directories, including credential stores.
    shutil.copytree(source, destination, symlinks=True, ignore=ignored)
    for path in destination.rglob('*'):
        if path.is_symlink():
            target = path.resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError(f'Source contains an external symlink: {path.relative_to(destination)}')
        elif not path.is_file() and not path.is_dir():
            raise ValueError('Source contains a special file')


def fingerprint(directory):
    digest = hashlib.sha256()
    for path in sorted(directory.rglob('*')):
        digest.update(str(path.relative_to(directory)).encode() + b'\0')
        digest.update(str(path.lstat().st_mode).encode() + b'\0')
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(65536), b''):
                    digest.update(block)
        digest.update(b'\0')
    return digest.hexdigest()


def build(profile, ca=None):
    spec = PROFILES[profile]
    args = ['build', '--target', spec['target'], '-t', f'agent-sandbox/{profile}:stage2']
    for key, value in spec.get('args', {}).items():
        args.extend(['--build-arg', f'{key}={value}'])
    if ca:
        args.extend(['--secret', f'id=proxy_ca,src={Path(ca).resolve()}'])
    docker(*args, str(ROOT))
    image = docker('image', 'inspect', f'agent-sandbox/{profile}:stage2', '--format', '{{.Id}}', capture=True)
    docker('run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
           '--security-opt', 'no-new-privileges:true', '--entrypoint', '/bin/bash',
           image, '-e', '-c', spec['probe'])


def prepare(manifest, state, ca=None):
    config, source = load_project(manifest)
    base = docker('image', 'inspect', f"agent-sandbox/{config['profile']}:stage2",
                  '--format', '{{.Id}}', capture=True)
    with tempfile.TemporaryDirectory(prefix='sandbox-prepare-') as temporary:
        context = Path(temporary)
        snapshot(source, context / 'source')
        source_hash = fingerprint(context / 'source')
        verification_hash = None
        if config.get('verification'):
            verification_source = (Path(manifest).resolve().parent / config['verification']['source']).resolve()
            snapshot(verification_source, context / 'verification')
            verification_hash = fingerprint(context / 'verification')
        key = hashlib.sha256(json.dumps(["stage3-v1", config, source_hash, verification_hash, base], sort_keys=True).encode()).hexdigest()
        base_tag = f"agent-sandbox/{config['profile']}:stage2"
        lines = [f'FROM {base_tag}', 'USER root', 'RUN chmod 0755 /opt/sandbox',
                 'COPY --chown=agent:agent source/ /home/agent/workspace/']
        if config.get('verification'):
            lines.append('COPY --chown=root:root verification/ /opt/sandbox/verification/')
        lines.append('USER agent')
        for command in config.get('prepare', []):
            # A secret mount exists only during preparation and is never copied.
            trust = ('if [ -f /run/secrets/proxy_ca ]; then '
                     'export PIP_CERT=/run/secrets/proxy_ca SSL_CERT_FILE=/run/secrets/proxy_ca '
                     'GIT_SSL_CAINFO=/run/secrets/proxy_ca CARGO_HTTP_CAINFO=/run/secrets/proxy_ca '
                     'NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; ')
            lines.append('RUN --mount=type=secret,id=proxy_ca ' + json.dumps(
                ['/bin/bash', '-e', '-c', trust + command]))
        (context / 'Dockerfile').write_text('\n'.join(lines) + '\n')
        tag = f'agent-sandbox/prepared:{key}'
        args = ['build', '-t', tag]
        if ca:
            args.extend(['--secret', f'id=proxy_ca,src={Path(ca).resolve()}'])
        docker(*args, str(context))
        image = docker('image', 'inspect', tag, '--format', '{{.Id}}', capture=True)
        if docker('image', 'inspect', base_tag, '--format', '{{.Id}}', capture=True) != base:
            raise ValueError('Base image changed during preparation; retry')
    record = {'image': image, 'base': base, 'source_hash': source_hash, 'verification_hash': verification_hash, 'config': config}
    # Store state only after a genuinely offline baseline succeeds.
    check_image(record)
    if verification_hash:
        from execution import verify
        code, output = verify(record)
        if code:
            raise ValueError(f'Protected preparation baseline failed: {output}')
    Path(state).parent.mkdir(parents=True, exist_ok=True)
    Path(state).write_text(json.dumps(record, indent=2) + '\n')
    print(f'Prepared and checked {image}; state saved to {state}')


def check_image(record):
    config = record['config']
    name = f'sandbox-check-{uuid.uuid4().hex}'
    try:
        docker('run', '--name', name, '--rm', '--init', '--network', 'none', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges:true', '--memory', str(config.get('memory', '4g')),
               '--cpus', str(config.get('cpus', 2)), '--pids-limit', str(config.get('pids', 256)),
               '--entrypoint', '/bin/bash', record['image'], '-e', '-c', config['check'],
               timeout=config.get('timeout', 600))
    finally:
        # A host-side timeout must not leave a build/test running in Docker.
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)



def main():
    import signal
    from agent import interrupt
    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build_parser = commands.add_parser('build')
    build_parser.add_argument('profile', choices=PROFILES)
    build_parser.add_argument('--ca', help='Optional combined CA bundle for build proxy trust')
    prepare_parser = commands.add_parser('prepare')
    prepare_parser.add_argument('manifest')
    prepare_parser.add_argument('--state', required=True)
    prepare_parser.add_argument('--ca')
    run_parser = commands.add_parser('run')
    run_parser.add_argument('state')
    run_parser.add_argument('task')
    run_parser.add_argument('--artifacts', default='artifacts/runs')
    run_parser.add_argument('--base-url', default=os.environ.get('OPENAI_BASE_URL'))
    run_parser.add_argument('--text-actions', action='store_true', help='Use explicit XML actions without native tools')
    run_parser.add_argument('--model', default=os.environ.get('LLM_MODEL'))
    run_parser.add_argument('--steps', type=int, default=20)
    run_parser.add_argument('--timeout', type=float, default=1800)
    campaign_parser = commands.add_parser('campaign')
    campaign_commands = campaign_parser.add_subparsers(dest='campaign_command', required=True)
    campaign_create = campaign_commands.add_parser('create')
    campaign_create.add_argument('directory')
    campaign_create.add_argument('state')
    campaign_create.add_argument('tasks', help='JSON array of task strings')
    campaign_run = campaign_commands.add_parser('resume')
    campaign_run.add_argument('directory')
    campaign_run.add_argument('--max-runs', type=int, required=True)
    campaign_run.add_argument('--timeout', type=float, default=1800)
    campaign_run.add_argument('--steps', type=int, default=20)
    campaign_run.add_argument('--model', default=os.environ.get('LLM_MODEL'))
    campaign_run.add_argument('--base-url', default=os.environ.get('OPENAI_BASE_URL'))
    campaign_run.add_argument('--text-actions', action='store_true')
    verify_parser = commands.add_parser('verify')
    verify_parser.add_argument('state')
    verify_parser.add_argument('--candidate')
    check_parser = commands.add_parser('check')
    check_parser.add_argument('state')
    args = parser.parse_args()
    try:
        if args.command == 'campaign':
            import campaign
            if args.campaign_command == 'create':
                campaign.create(args.directory, args.state, args.tasks)
            else:
                reason = campaign.resume(args.directory, args.max_runs, args.timeout, args.steps,
                                         args.base_url, args.model, args.text_actions)
                print(f'Campaign stopped: {reason}')
                raise SystemExit(0 if reason == 'all_completed' else 1)
        elif args.command == 'build':
            build(args.profile, args.ca)
        elif args.command == 'prepare':
            prepare(args.manifest, args.state, args.ca)
        elif args.command == 'verify':
            from execution import verify
            from controller import transfer
            record = json.loads(Path(args.state).read_text())
            candidate = None
            if args.candidate:
                candidate = transfer.validate(json.loads(Path(args.candidate).read_text()))
            code, output = verify(record, candidate)
            print(output)
            raise SystemExit(code)
        elif args.command == 'check':
            check_image(json.loads(Path(args.state).read_text()))
        else:
            from controller import run
            record = json.loads(Path(args.state).read_text())
            outcome = run(record, args.task, args.artifacts, args.base_url,
                          args.model, args.steps, args.timeout, text_actions=args.text_actions)
            raise SystemExit(outcome)

    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(exc.returncode if isinstance(exc, subprocess.CalledProcessError) else 1, f'{exc}\n')


if __name__ == '__main__':
    main()
