"""Dated, verifiable dependency advisory snapshots for offline scanning.

Operator-side only. ``fetch`` needs network access and writes OSV ecosystem
databases (https://osv.dev) in the layout OSV-Scanner uses for its local cache,
plus a metadata file with hashes and timestamps. ``scan`` extracts lockfiles from
a prepared image (networking disabled) and checks them against a verified
snapshot with OSV-Scanner in offline mode. A scan never falls back to the network;
checks that cannot run are reported as unavailable rather than as clean.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

SOURCE = 'https://osv-vulnerabilities.storage.googleapis.com'
ECOSYSTEMS = ('Go', 'PyPI', 'crates.io', 'npm')
METADATA = 'metadata.json'
DATABASE_DIRECTORY = 'osv-scalibr'  # OSV-Scanner's cache layout: <dir>/osv-scalibr/<ecosystem>/all.zip
MAX_DATABASE_BYTES = 1 << 30
MAX_LOCKFILE_BYTES = 5 << 20
MAX_LOCKFILES = 200
DEFAULT_MAX_AGE_DAYS = 7
WORKSPACE = '/home/agent/workspace'
EXACT_LOCKFILES = {'go.mod': 'Go', 'Cargo.lock': 'crates.io', 'package-lock.json': 'npm',
                   'poetry.lock': 'PyPI', 'uv.lock': 'PyPI', 'Pipfile.lock': 'PyPI'}
PRUNED = ('node_modules', '.venv', 'target', '.git', 'vendor', '__pycache__')


def lockfile_ecosystem(name):
    """Ecosystem for a recognised lockfile name, or None."""
    if name in EXACT_LOCKFILES:
        return EXACT_LOCKFILES[name]
    if name.startswith('requirements') and name.endswith('.txt'):
        return 'PyPI'
    return None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def database_path(directory, ecosystem):
    return Path(directory) / DATABASE_DIRECTORY / ecosystem / 'all.zip'


def download(ecosystem, destination, opener=urllib.request.urlopen):
    """Stream one ecosystem database to destination; return (sha256, size, last_modified)."""
    digest, size = hashlib.sha256(), 0
    with opener(f'{SOURCE}/{ecosystem}/all.zip', timeout=60) as response, open(destination, 'wb') as out:
        modified = response.headers.get('Last-Modified') if response.headers else None
        for block in iter(lambda: response.read(1 << 20), b''):
            size += len(block)
            if size > MAX_DATABASE_BYTES:
                raise ValueError(f'{ecosystem} database exceeds {MAX_DATABASE_BYTES} bytes')
            digest.update(block)
            out.write(block)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None or not archive.namelist():
            raise ValueError(f'{ecosystem} database archive is corrupt or empty')
    return digest.hexdigest(), size, modified


def fetch(directory, ecosystems=ECOSYSTEMS, opener=urllib.request.urlopen):
    """Download databases into directory and write verified metadata last."""
    unknown = sorted(set(ecosystems) - set(ECOSYSTEMS))
    if unknown or not ecosystems:
        raise ValueError(f'Unsupported ecosystems: {unknown or "none selected"}')
    root = Path(directory)
    databases = {}
    with tempfile.TemporaryDirectory(prefix='advisories-', dir=root.parent if root.parent.is_dir() else None) as temporary:
        for ecosystem in ecosystems:
            staged = Path(temporary) / f'{ecosystem}.zip'
            sha, size, modified = download(ecosystem, staged, opener)
            target = database_path(root, ecosystem)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
            databases[ecosystem] = {'sha256': sha, 'size': size, 'last_modified': modified}
    # Metadata describes only databases that were fully written and verified.
    existing = load_metadata(root) if (root / METADATA).exists() else {}
    merged = {**existing.get('databases', {}), **databases}
    metadata = {'format': 1, 'source': SOURCE, 'fetched_at': utc_now().isoformat(), 'databases': merged}
    temporary_metadata = root / (METADATA + '.tmp')
    temporary_metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n')
    os.replace(temporary_metadata, root / METADATA)
    return metadata


def load_metadata(directory):
    path = Path(directory) / METADATA
    if not path.is_file():
        raise ValueError(f'No advisory snapshot at {directory}: {METADATA} is missing')
    metadata = json.loads(path.read_text())
    if metadata.get('format') != 1 or not isinstance(metadata.get('databases'), dict):
        raise ValueError('Unsupported advisory snapshot metadata')
    return metadata


def verify_snapshot(directory):
    """Return (metadata, usable ecosystems, problems); hashes are recomputed from disk."""
    metadata = load_metadata(directory)
    usable, problems = [], {}
    for ecosystem, recorded in sorted(metadata['databases'].items()):
        path = database_path(directory, ecosystem)
        if ecosystem not in ECOSYSTEMS:
            problems[ecosystem] = 'unsupported ecosystem in snapshot'
        elif not path.is_file():
            problems[ecosystem] = 'database file is missing'
        elif sha256_file(path) != recorded.get('sha256'):
            problems[ecosystem] = 'database hash does not match metadata'
        else:
            usable.append(ecosystem)
    return metadata, usable, problems


def snapshot_age_days(metadata, now=None):
    fetched = datetime.datetime.fromisoformat(metadata['fetched_at'])
    return ((now or utc_now()) - fetched).total_seconds() / 86400


def extract_lockfiles(image, destination):
    """Copy recognised lockfiles out of a prepared image, run without networking."""
    names = ' -o '.join(f"-name '{name}'" for name in EXACT_LOCKFILES) + " -o -name 'requirements*.txt'"
    prune = ' -o '.join(f"-name '{name}'" for name in PRUNED)
    script = (f"cd {WORKSPACE} && find . \\( {prune} \\) -prune -o -type f -size -{MAX_LOCKFILE_BYTES // 1024}k "
              f"\\( {names} \\) -print0 | head -z -n {MAX_LOCKFILES} | tar --null -T - -cf -")
    archive = Path(destination) / 'lockfiles.tar'
    with open(archive, 'wb') as out:
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
                        '--security-opt', 'no-new-privileges:true', '--entrypoint', '/bin/bash',
                        image, '-e', '-c', script], check=True, stdout=out, timeout=120)
    root = Path(destination) / 'lockfiles'
    root.mkdir()
    found = []
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            parts = Path(member.name).parts
            if (not member.isfile() or lockfile_ecosystem(parts[-1] if parts else '') is None
                    or member.size > MAX_LOCKFILE_BYTES or Path(member.name).is_absolute() or '..' in parts):
                continue  # Links, devices, oversized files and escaping paths are never extracted.
            bundle.extract(member, root, filter='data')
            found.append(Path(member.name).as_posix().removeprefix('./'))
    return root, sorted(found)


def parse_vulnerabilities(output):
    """Flatten OSV-Scanner JSON into stable (id, package, version, summary) records."""
    found = {}
    for result in json.loads(output).get('results') or []:
        for entry in result.get('packages') or []:
            package = entry.get('package', {})
            for vulnerability in entry.get('vulnerabilities') or []:
                key = (vulnerability.get('id'), package.get('name'), package.get('version'))
                found[key] = {'id': key[0], 'package': key[1], 'version': key[2],
                              'aliases': sorted(vulnerability.get('aliases') or []),
                              'summary': (vulnerability.get('summary') or '')[:300]}
    return [found[key] for key in sorted(found, key=str)]


def scan_lockfile(root, relative, cache, runner=subprocess.run):
    """Offline scan of one lockfile. Exit codes 0 (clean) and 1 (findings) are valid."""
    command = ['osv-scanner', 'scan', 'source', '--offline', '--offline-vulnerabilities',
               '--format', 'json', '-L', str(root / relative)]
    environment = {**os.environ, 'OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY': str(cache)}
    try:
        result = runner(command, capture_output=True, text=True, env=environment, timeout=300, check=False)
    except FileNotFoundError:
        return {'status': 'unavailable', 'reason': 'osv-scanner is not installed'}
    except subprocess.TimeoutExpired:
        return {'status': 'unavailable', 'reason': 'osv-scanner timed out'}
    if result.returncode not in (0, 1):
        return {'status': 'unavailable', 'reason': f'osv-scanner exited with {result.returncode}'}
    try:
        return {'status': 'scanned', 'vulnerabilities': parse_vulnerabilities(result.stdout)}
    except (ValueError, AttributeError):
        return {'status': 'unavailable', 'reason': 'osv-scanner output was not valid JSON'}


def build_report(root, lockfiles, directory, scan=scan_lockfile, max_age_days=DEFAULT_MAX_AGE_DAYS, now=None):
    metadata, usable, problems = verify_snapshot(directory)
    age = snapshot_age_days(metadata, now)
    checks = []
    for relative in lockfiles:
        ecosystem = lockfile_ecosystem(Path(relative).name)
        if ecosystem in usable:
            check = scan(root, relative, directory)
        else:
            check = {'status': 'unavailable',
                     'reason': problems.get(ecosystem) or f'snapshot has no {ecosystem} database'}
        checks.append({'lockfile': relative, 'ecosystem': ecosystem, **check})
    return {'snapshot': {'directory': str(directory), 'fetched_at': metadata['fetched_at'],
                         'age_days': round(age, 2), 'stale': age > max_age_days,
                         'databases': metadata['databases'], 'problems': problems},
            'scanned_at': (now or utc_now()).isoformat(), 'checks': checks,
            'note': 'Known-advisory matching only; this does not show the code is free of vulnerabilities.'}


def report_status(report):
    """0 clean, 1 known vulnerabilities, 2 any check unavailable, stale, or no lockfiles."""
    checks = report['checks']
    if any(check['status'] != 'scanned' for check in checks) or report['snapshot']['stale'] or not checks:
        return 2
    return 1 if any(check['vulnerabilities'] for check in checks) else 0


def scan_image(image, directory, max_age_days=DEFAULT_MAX_AGE_DAYS):
    with tempfile.TemporaryDirectory(prefix='advisory-scan-') as temporary:
        root, lockfiles = extract_lockfiles(image, temporary)
        return build_report(root, lockfiles, directory, max_age_days=max_age_days)


def summarize(report):
    snapshot = report['snapshot']
    lines = [f"Advisory snapshot fetched {snapshot['fetched_at']} ({snapshot['age_days']} days old"
             f"{', STALE' if snapshot['stale'] else ''})"]
    for check in report['checks']:
        if check['status'] == 'scanned':
            lines.append(f"{check['lockfile']}: {len(check['vulnerabilities'])} known advisories")
            lines.extend(f"  {v['id']} {v['package']} {v['version']}" for v in check['vulnerabilities'])
        else:
            lines.append(f"{check['lockfile']}: UNAVAILABLE ({check['reason']})")
    if not report['checks']:
        lines.append('No recognised lockfiles found; dependency advisories were not checked.')
    return '\n'.join(lines)
