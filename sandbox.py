#!/usr/bin/env python3
"""Operator CLI for language images and prepared, offline project environments."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

MIN_PYTHON = (3, 13)
ROOT = Path(__file__).resolve().parent
PROFILES = json.loads((ROOT / 'docker/images.json').read_text())
BASH = '/bin/bash'
EXCLUDED = {'.git', '.ssh', '.aws', '.codex', '.agents', '__pycache__', '.venv',
            'node_modules', 'target', 'artifacts', '.npmrc', '.pypirc', '.netrc'}


def docker(*args, capture=False, timeout=None):
    result = subprocess.run(['docker', *args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None, timeout=timeout)
    return result.stdout.strip() if capture else None


def image_id(tag):
    return docker('image', 'inspect', tag, '--format', '{{.Id}}', capture=True)


def validate_fields(config):
    """Validate a manifest's scalar fields, preparation commands, and resource limits."""
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


def validate_verification(base, verification):
    """Validate an optional protected-verification section relative to the manifest."""
    if verification is None:
        return
    if not isinstance(verification, dict) or set(verification) != {'source', 'command'}:
        raise ValueError('verification requires source and command')
    if any(not isinstance(v, str) or not v.strip() for v in verification.values()):
        raise ValueError('verification fields must be nonempty strings')
    if not (base / verification['source']).resolve().is_dir():
        raise ValueError('Verification source directory does not exist')


def load_project(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    validate_fields(config)
    source = (path.parent / config['source']).resolve()
    if not source.is_dir():
        raise ValueError(f'Source directory does not exist: {source}')
    validate_verification(path.parent, config.get('verification'))
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
    image = image_id(f'agent-sandbox/{profile}:stage2')
    docker('run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
           '--security-opt', 'no-new-privileges:true', '--entrypoint', BASH,
           image, '-e', '-c', spec['probe'])


def prepare(manifest, state, ca=None):
    config, source = load_project(manifest)
    base = image_id(f"agent-sandbox/{config['profile']}:stage2")
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
                [BASH, '-e', '-c', trust + command]))
        (context / 'Dockerfile').write_text('\n'.join(lines) + '\n')
        tag = f'agent-sandbox/prepared:{key}'
        args = ['build', '-t', tag]
        if ca:
            args.extend(['--secret', f'id=proxy_ca,src={Path(ca).resolve()}'])
        docker(*args, str(context))
        image = image_id(tag)
        if image_id(base_tag) != base:
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
               '--entrypoint', BASH, record['image'], '-e', '-c', config['check'],
               timeout=config.get('timeout', 600))
    finally:
        # A host-side timeout must not leave a build/test running in Docker.
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)



def main():
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(f'sandbox.py requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; '
                         f'found {sys.version_info[0]}.{sys.version_info[1]}')
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
    campaign_finding = campaign_commands.add_parser('finding')
    campaign_finding.add_argument('directory')
    campaign_finding.add_argument('finding', help='Operator-reviewed finding JSON')
    campaign_run = campaign_commands.add_parser('resume')
    campaign_run.add_argument('directory')
    campaign_run.add_argument('--max-runs', type=int, required=True)
    campaign_run.add_argument('--max-attempts', type=int, default=3)
    campaign_run.add_argument('--budget', type=float, help='Overall invocation deadline in seconds')
    campaign_run.add_argument('--continue-on-failure', action='store_true')
    campaign_run.add_argument('--stall-limit', type=int, default=2)
    campaign_status = campaign_commands.add_parser('status')
    campaign_status.add_argument('directory')
    campaign_review = campaign_commands.add_parser('review')
    campaign_review.add_argument('directory')
    campaign_review.add_argument('target', type=int)
    campaign_review.add_argument('attempt', type=int)
    campaign_review.add_argument('finding', type=int)
    campaign_review.add_argument('--status', required=True,
                                 choices=['confirmed', 'suspected', 'rejected', 'blocked', 'no_finding'])
    campaign_replay = campaign_commands.add_parser('replay')
    campaign_replay.add_argument('directory')
    campaign_replay.add_argument('run', help='Run artifact directory relative to campaign')
    campaign_replay.add_argument('reproducer', help='Operator-owned file containing the replay command')
    campaign_replay.add_argument('--timeout', type=float, default=600)
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
        run_command(args)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(exc.returncode if isinstance(exc, subprocess.CalledProcessError) else 1, f'{exc}\n')


def run_campaign_command(args):
    import campaign
    if args.campaign_command == 'create':
        campaign.create(args.directory, args.state, args.tasks)
    elif args.campaign_command == 'status':
        print(json.dumps(campaign.status(args.directory), indent=2))
    elif args.campaign_command == 'review':
        print(campaign.review(args.directory, args.target, args.attempt, args.finding, args.status))
    elif args.campaign_command == 'replay':
        print(campaign.replay(args.directory, args.run, Path(args.reproducer).read_text(), args.timeout))
    elif args.campaign_command == 'finding':
        print(campaign.record_finding(args.directory, json.loads(Path(args.finding).read_text())))
    else:
        reason = campaign.resume(args.directory, args.max_runs, args.timeout, args.steps,
                                 args.base_url, args.model, args.text_actions, max_attempts=args.max_attempts,
                                 budget=args.budget, continue_on_failure=args.continue_on_failure,
                                 stall_limit=args.stall_limit)
        print(f'Campaign stopped: {reason}')
        raise SystemExit(0 if reason == 'all_completed' else 1)


def run_verify_command(args):
    from execution import verify
    from controller import transfer
    record = json.loads(Path(args.state).read_text())
    candidate = None
    if args.candidate:
        candidate = transfer.validate(json.loads(Path(args.candidate).read_text()))
    code, output = verify(record, candidate)
    print(output)
    raise SystemExit(code)


def run_controller_command(args):
    from controller import run
    record = json.loads(Path(args.state).read_text())
    outcome = run(record, args.task, args.artifacts, args.base_url,
                  args.model, args.steps, args.timeout, text_actions=args.text_actions)
    raise SystemExit(outcome)


def run_command(args):
    if args.command == 'campaign':
        run_campaign_command(args)
    elif args.command == 'build':
        build(args.profile, args.ca)
    elif args.command == 'prepare':
        prepare(args.manifest, args.state, args.ca)
    elif args.command == 'verify':
        run_verify_command(args)
    elif args.command == 'check':
        check_image(json.loads(Path(args.state).read_text()))
    else:
        run_controller_command(args)


if __name__ == '__main__':
    main()
