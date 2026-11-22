#!/usr/bin/env python3
"""Operator CLI for language images and prepared, offline project environments."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

MIN_PYTHON = (3, 13)
ROOT = Path(__file__).resolve().parent
PROFILES = json.loads((ROOT / 'docker/images.json').read_text())
BASH = '/bin/bash'
ADVISORY_ECOSYSTEMS = ('Go', 'PyPI', 'crates.io', 'npm')
TREE_HASH_VERSION = b'sandbox-tree-v2\0'
DEFAULT_PREPARE_TIMEOUT = 1800


def docker(*args, capture=False, timeout=None):
    result = subprocess.run(['docker', *args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None, timeout=timeout)
    return result.stdout.strip() if capture else None


def image_id(tag):
    return docker('image', 'inspect', tag, '--format', '{{.Id}}', capture=True)


def validate_fields(config):
    """Validate a manifest's scalar fields, preparation commands, and resource limits."""
    allowed = {'name', 'source', 'profile', 'prepare', 'check', 'memory', 'cpus', 'pids', 'timeout',
               'prepare_timeout', 'verification'}
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
    validate_resources(config)


def validate_resources(config):
    """Validate numeric limits and the Docker memory size."""
    for field, default in (('cpus', 2), ('pids', 256), ('timeout', 600),
                           ('prepare_timeout', DEFAULT_PREPARE_TIMEOUT)):
        value = config.get(field, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 100000:
            raise ValueError(f'{field} must be a positive bounded number')
    if not isinstance(config.get('pids', 256), int):
        raise ValueError('pids must be an integer')
    memory = config.get('memory', '4g')
    if not isinstance(memory, str) or not re.fullmatch(r'[1-9]\d{0,5}[bkmg]?', memory.lower()):
        raise ValueError('memory must be a Docker memory size such as "4g" or "512m"')


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


def _record(digest, *fields):
    """Hash one length-prefixed record so file contents can never forge later records."""
    for field in fields:
        digest.update(len(field).to_bytes(8, 'big') + field)


def snapshot(source, destination):
    """Copy source into a new destination directory and return its tree fingerprint.

    Traversal never follows links (see transfer.walk), so a concurrent writer
    cannot redirect reads outside the source tree. Symlinks and special files
    are rejected because Stage 3 snapshots cannot represent them. Credential,
    dependency and build directories in transfer.SKIP are excluded, exactly as
    they are from run snapshots.
    """
    from execution import transfer
    destination = Path(destination)
    destination.mkdir(mode=0o755)
    digest = hashlib.sha256(TREE_HASH_VERSION)
    for relative, opened in transfer.walk(source):
        target = destination / relative
        name = relative.as_posix().encode()
        if opened is None:
            target.mkdir(mode=0o755)
            _record(digest, b'd', name)
            continue
        fd, info = opened
        content, size = hashlib.sha256(), 0
        with target.open('xb') as out:
            while chunk := os.read(fd, 1 << 20):
                size += len(chunk)
                content.update(chunk)
                out.write(chunk)
            os.fchmod(out.fileno(), info.st_mode & 0o777)
        if size != info.st_size:
            raise ValueError(f'Source changed during preparation: {relative}')
        _record(digest, b'f', name, str(info.st_mode & 0o777).encode(), content.digest())
    return digest.hexdigest()


def fingerprint(directory):
    """Tree fingerprint of an existing snapshot, matching snapshot()'s return value."""
    from execution import transfer
    digest = hashlib.sha256(TREE_HASH_VERSION)
    for relative, opened in transfer.walk(directory):
        name = relative.as_posix().encode()
        if opened is None:
            _record(digest, b'd', name)
            continue
        fd, info = opened
        content = hashlib.sha256()
        while chunk := os.read(fd, 1 << 20):
            content.update(chunk)
        _record(digest, b'f', name, str(info.st_mode & 0o777).encode(), content.digest())
    return digest.hexdigest()


def proxy_build_args(proxy=None):
    """Build arguments that replace Docker's client-configured proxies.

    Without explicit values Docker passes its configured proxy URLs, including
    any embedded credentials, to every RUN step, where project build scripts can
    read them. An operator proxy must therefore be credential-free.
    """
    if proxy is not None:
        from urllib.parse import urlsplit
        parts = urlsplit(proxy)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError('--proxy must be an http(s) URL')
        if parts.username is not None or parts.password is not None:
            raise ValueError('--proxy must not contain credentials; project build steps can read it')
    from execution import PROXY_KEYS
    args = []
    for key in PROXY_KEYS:
        value = proxy if proxy and key.lower() in ('http_proxy', 'https_proxy') else ''
        args.extend(['--build-arg', f'{key}={value}'])
    return args


def build(profile, ca=None, proxy=None):
    from execution import container_isolation_args
    spec = PROFILES[profile]
    args = ['build', '--target', spec['target'], '-t', f'agent-sandbox/{profile}:stage2',
            *proxy_build_args(proxy)]
    for key, value in spec.get('args', {}).items():
        args.extend(['--build-arg', f'{key}={value}'])
    if ca:
        args.extend(['--secret', f'id=proxy_ca,src={Path(ca).resolve()}'])
    docker(*args, str(ROOT))
    image = image_id(f'agent-sandbox/{profile}:stage2')
    docker('run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
           '--security-opt', 'no-new-privileges:true', *container_isolation_args(),
           '--entrypoint', BASH, image, '-e', '-c', spec['probe'])


def prepare(manifest, state, ca=None, proxy=None):
    config, source = load_project(manifest)
    base = image_id(f"agent-sandbox/{config['profile']}:stage2")
    with tempfile.TemporaryDirectory(prefix='sandbox-prepare-') as temporary:
        context = Path(temporary)
        source_hash = snapshot(source, context / 'source')
        verification_hash = None
        if config.get('verification'):
            verification_source = (Path(manifest).resolve().parent / config['verification']['source']).resolve()
            verification_hash = snapshot(verification_source, context / 'verification')
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
        args = ['build', '-t', tag, *proxy_build_args(proxy)]
        if ca:
            args.extend(['--secret', f'id=proxy_ca,src={Path(ca).resolve()}'])
        # Project build steps are untrusted: bound them. Ending the client cancels the BuildKit job.
        docker(*args, str(context), timeout=config.get('prepare_timeout', DEFAULT_PREPARE_TIMEOUT))
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
    from execution import container_isolation_args
    config = record['config']
    name = f'sandbox-check-{uuid.uuid4().hex}'
    try:
        docker('run', '--name', name, '--rm', '--init', '--network', 'none', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges:true', *container_isolation_args(),
               '--memory', str(config.get('memory', '4g')),
               '--cpus', str(config.get('cpus', 2)), '--pids-limit', str(config.get('pids', 256)),
               '--entrypoint', BASH, record['image'], '-e', '-c', config['check'],
               timeout=config.get('timeout', 600))
    finally:
        # A host-side timeout must not leave a build/test running in Docker.
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)



def interrupt(signum, frame):
    """Unwind cleanup (containers, volumes, campaign state) on SIGTERM as on Ctrl-C."""
    raise KeyboardInterrupt


def main():
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(f'sandbox.py requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; '
                         f'found {sys.version_info[0]}.{sys.version_info[1]}')
    import signal
    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build_parser = commands.add_parser('build')
    build_parser.add_argument('profile', choices=PROFILES)
    build_parser.add_argument('--ca', help='Optional combined CA bundle for build proxy trust')
    build_parser.add_argument('--proxy', help='Credential-free http(s) proxy for build steps')
    prepare_parser = commands.add_parser('prepare')
    prepare_parser.add_argument('manifest')
    prepare_parser.add_argument('--state', required=True)
    prepare_parser.add_argument('--ca')
    prepare_parser.add_argument('--proxy', help='Credential-free http(s) proxy for preparation')
    run_parser = commands.add_parser('run')
    run_parser.add_argument('state')
    run_parser.add_argument('task')
    run_parser.add_argument('--artifacts', default='artifacts/runs')
    run_parser.add_argument('--base-url', default=os.environ.get('OPENAI_BASE_URL'))
    run_parser.add_argument('--text-actions', action='store_true', help='Use explicit XML actions without native tools')
    run_parser.add_argument('--model', default=os.environ.get('LLM_MODEL'))
    run_parser.add_argument('--steps', type=int, default=20)
    run_parser.add_argument('--timeout', type=float, default=1800)
    run_parser.add_argument('--command-timeout', type=float,
                            help='Seconds per model command (default: the manifest timeout)')
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
    campaign_run.add_argument('--command-timeout', type=float,
                              help='Seconds per model command (default: the manifest timeout)')
    verify_parser = commands.add_parser('verify')
    verify_parser.add_argument('state')
    verify_parser.add_argument('--candidate')
    check_parser = commands.add_parser('check')
    check_parser.add_argument('state')
    advisories_parser = commands.add_parser('advisories')
    advisory_commands = advisories_parser.add_subparsers(dest='advisory_command', required=True)
    advisory_fetch = advisory_commands.add_parser('fetch', help='Download a dated OSV snapshot (needs network)')
    advisory_fetch.add_argument('snapshot', help='Snapshot directory to create or update')
    advisory_fetch.add_argument('--ecosystem', action='append', dest='ecosystems', choices=ADVISORY_ECOSYSTEMS,
                                help='Repeatable; defaults to all supported ecosystems')
    advisory_scan = advisory_commands.add_parser('scan', help='Scan a prepared project offline against a snapshot')
    advisory_scan.add_argument('state')
    advisory_scan.add_argument('snapshot')
    advisory_scan.add_argument('--output', help='Write the JSON report here')
    advisory_scan.add_argument('--max-age-days', type=float, default=7)
    args = parser.parse_args()
    try:
        run_command(args)
    except KeyboardInterrupt:
        parser.exit(130, 'Interrupted\n')
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(exc.returncode if isinstance(exc, subprocess.CalledProcessError) else 1, f'{exc}\n')
    except (KeyError, TypeError, AttributeError) as exc:
        # Usually a hand-edited or truncated state, campaign, or snapshot file.
        parser.exit(1, f'Malformed input file: {type(exc).__name__}: {exc}\n')


def run_advisories_command(args):
    import advisories
    if args.advisory_command == 'fetch':
        metadata = advisories.fetch(args.snapshot, tuple(args.ecosystems or advisories.ECOSYSTEMS))
        print(f"Snapshot {args.snapshot} fetched at {metadata['fetched_at']}: "
              f"{', '.join(sorted(metadata['databases']))}")
        return
    record = json.loads(Path(args.state).read_text())
    report = advisories.scan_image(record['image'], args.snapshot, args.max_age_days)
    text = json.dumps(report, indent=2) + '\n'
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text)
    print(advisories.summarize(report))
    raise SystemExit(advisories.report_status(report))


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
                                 stall_limit=args.stall_limit, command_timeout=args.command_timeout)
        print(f'Campaign stopped: {reason}')
        raise SystemExit(0 if reason == 'all_completed' else 1)


def run_verify_command(args):
    from execution import transfer, verify
    record = json.loads(Path(args.state).read_text())
    candidate = None
    if args.candidate:
        # Bound the read before parsing: the candidate file's contents are untrusted.
        with open(args.candidate, 'rb') as stream:
            candidate = transfer.load(stream)
    code, output = verify(record, candidate)
    print(output)
    raise SystemExit(code)


def run_controller_command(args):
    from controller import run
    record = json.loads(Path(args.state).read_text())
    outcome = run(record, args.task, args.artifacts, args.base_url,
                  args.model, args.steps, args.timeout, text_actions=args.text_actions,
                  command_timeout=args.command_timeout)
    raise SystemExit(outcome)


def run_command(args):
    if args.command == 'campaign':
        run_campaign_command(args)
    elif args.command == 'advisories':
        run_advisories_command(args)
    elif args.command == 'build':
        build(args.profile, args.ca, args.proxy)
    elif args.command == 'prepare':
        prepare(args.manifest, args.state, args.ca, args.proxy)
    elif args.command == 'verify':
        run_verify_command(args)
    elif args.command == 'check':
        check_image(json.loads(Path(args.state).read_text()))
    else:
        run_controller_command(args)


if __name__ == '__main__':
    main()
