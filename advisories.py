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
import math
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile

SOURCE = 'https://osv-vulnerabilities.storage.googleapis.com'
ECOSYSTEMS = ('Go', 'PyPI', 'crates.io', 'npm')
METADATA = 'metadata.json'
DATABASE_DIRECTORY = 'osv-scalibr'  # OSV-Scanner's cache layout: <dir>/osv-scalibr/<ecosystem>/all.zip
MAX_DATABASE_BYTES = 1 << 30
MAX_LOCKFILE_BYTES = 5 << 20
MAX_LOCKFILES = 200
MAX_TOTAL_LOCKFILE_BYTES = 64 << 20
INVENTORY = '.sandbox-inventory.json'
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
    root.mkdir(parents=True, exist_ok=True)
    databases = {}
    # Stage under root (same filesystem for os.replace) and replace nothing until every
    # requested database downloaded and validated, so a partial failure changes nothing.
    with tempfile.TemporaryDirectory(prefix='.advisories-', dir=root) as temporary:
        for ecosystem in ecosystems:
            staged = Path(temporary) / f'{ecosystem}.zip'
            sha, size, modified = download(ecosystem, staged, opener)
            databases[ecosystem] = {'sha256': sha, 'size': size, 'last_modified': modified,
                                    'fetched_at': utc_now().isoformat()}
        for ecosystem in ecosystems:
            target = database_path(root, ecosystem)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(Path(temporary) / f'{ecosystem}.zip', target)
    # Metadata describes only databases that were fully written and verified. Each
    # database keeps its own fetch time: refreshing one must not renew the others' age.
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


# Runs inside the prepared image with python3 -I. It reports every recognised lockfile
# it leaves out, so the host can mark discovery incomplete instead of reporting clean.
DISCOVERY_SCRIPT = r"""
import io, json, os, stat, sys, tarfile
options = json.loads(sys.argv[1])
names, pruned = set(options['names']), set(options['pruned'])
found, omitted, errors, total = [], [], [], 0
def recognised(name):
    return name in names or (name.startswith('requirements') and name.endswith('.txt'))
def failed(error):
    errors.append(f'{os.path.relpath(error.filename or ".", options["root"])}: {error.strerror}')
for directory, dirs, files in os.walk(options['root'], onerror=failed):
    dirs[:] = sorted(d for d in dirs if d not in pruned)
    for name in sorted(files):
        if not recognised(name):
            continue
        path = os.path.join(directory, name)
        relative = os.path.relpath(path, options['root'])
        try:
            info = os.lstat(path)
        except OSError as error:
            failed(error)
            continue
        reason = None
        if not stat.S_ISREG(info.st_mode):
            reason = 'not a regular file'
        elif info.st_size > options['max_bytes']:
            reason = f'larger than {options["max_bytes"]} bytes'
        elif len(found) >= options['max_files']:
            reason = f'more than {options["max_files"]} lockfiles'
        elif total + info.st_size > options['max_total']:
            reason = f'lockfiles exceed {options["max_total"]} bytes in total'
        if reason:
            omitted.append({'lockfile': relative, 'reason': reason})
        else:
            found.append(relative)
            total += info.st_size
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
    for relative in found:
        archive.add(os.path.join(options['root'], relative), arcname=relative, recursive=False)
    data = json.dumps({'omitted': omitted, 'errors': errors}).encode()
    member = tarfile.TarInfo(options['inventory'])
    member.size = len(data)
    archive.addfile(member, io.BytesIO(data))
"""


def extract_lockfiles(image, destination):
    """Copy recognised lockfiles out of a prepared image, run without networking.

    Returns (root, lockfiles, problems); problems lists every recognised lockfile
    that was omitted and every traversal error, each making the scan incomplete.
    """
    from execution import container_isolation_args
    options = {'root': WORKSPACE, 'names': sorted(EXACT_LOCKFILES), 'pruned': list(PRUNED),
               'max_bytes': MAX_LOCKFILE_BYTES, 'max_files': MAX_LOCKFILES,
               'max_total': MAX_TOTAL_LOCKFILE_BYTES, 'inventory': INVENTORY}
    name = f'sandbox-advisories-{uuid.uuid4().hex}'
    archive = Path(destination) / 'lockfiles.tar'
    try:
        with open(archive, 'wb') as out:
            subprocess.run(['docker', 'run', '--name', name, '--rm', '--init', '--network', 'none',
                            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                            '--memory', '1g', '--cpus', '1', '--pids-limit', '64', '--user', '1000:1000',
                            *container_isolation_args(), '--workdir', '/', '--entrypoint', 'python3',
                            image, '-I', '-c', DISCOVERY_SCRIPT, json.dumps(options)],
                           check=True, stdout=out, timeout=120)
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False, timeout=30)
    if archive.stat().st_size > MAX_TOTAL_LOCKFILE_BYTES + (4 << 20):
        raise ValueError('Lockfile archive exceeds its size limit')
    root = Path(destination) / 'lockfiles'
    root.mkdir()
    found, problems, inventory = [], [], None
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            parts = Path(member.name).parts
            relative = Path(member.name).as_posix().removeprefix('./')
            if relative == INVENTORY and member.isfile() and member.size <= 1 << 20:
                inventory = json.loads(bundle.extractfile(member).read())
                continue
            if (not member.isfile() or lockfile_ecosystem(parts[-1] if parts else '') is None
                    or member.size > MAX_LOCKFILE_BYTES or Path(member.name).is_absolute() or '..' in parts):
                # Links, devices, oversized files and escaping paths are never extracted.
                problems.append({'lockfile': relative, 'reason': 'rejected archive member'})
                continue
            bundle.extract(member, root, filter='data')
            found.append(relative)
    if not isinstance(inventory, dict):
        problems.append({'lockfile': None, 'reason': 'lockfile discovery did not complete'})
    else:
        problems.extend(item for item in inventory.get('omitted', []) if isinstance(item, dict))
        problems.extend({'lockfile': None, 'reason': f'traversal error: {error}'}
                        for error in inventory.get('errors', []))
    return root, sorted(found), problems


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
        vulnerabilities = parse_vulnerabilities(result.stdout)
    except (ValueError, AttributeError):
        return {'status': 'unavailable', 'reason': 'osv-scanner output was not valid JSON'}
    if result.returncode == 1 and not vulnerabilities:
        # Exit 1 means findings; an empty parse means output we do not understand.
        return {'status': 'unavailable', 'reason': 'osv-scanner reported findings that could not be parsed'}
    return {'status': 'scanned', 'vulnerabilities': vulnerabilities}


def validate_max_age(max_age_days):
    if (isinstance(max_age_days, bool) or not isinstance(max_age_days, (int, float))
            or not math.isfinite(max_age_days) or max_age_days < 0):
        raise ValueError('Maximum snapshot age must be a finite, non-negative number of days')


def database_ages(metadata, now=None):
    """Days since each database was fetched; None when its age was never recorded."""
    ages = {}
    for ecosystem, recorded in metadata['databases'].items():
        stamp = recorded.get('fetched_at') if isinstance(recorded, dict) else None
        ages[ecosystem] = round(snapshot_age_days({'fetched_at': stamp}, now), 2) if stamp else None
    return ages


def build_report(root, lockfiles, directory, scan=scan_lockfile, max_age_days=DEFAULT_MAX_AGE_DAYS, now=None,
                 discovery=()):
    validate_max_age(max_age_days)
    metadata, usable, problems = verify_snapshot(directory)
    age = snapshot_age_days(metadata, now)
    ages = database_ages(metadata, now)
    used = sorted({lockfile_ecosystem(Path(relative).name) for relative in lockfiles} & set(usable))
    # Unknown age (snapshots from before per-database times) is treated as stale.
    stale_databases = [e for e in used if ages.get(e) is None or ages[e] > max_age_days]
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
                         'age_days': round(age, 2), 'database_age_days': ages,
                         'stale': bool(stale_databases), 'stale_databases': stale_databases,
                         'databases': metadata['databases'], 'problems': problems},
            'discovery': list(discovery),
            'scanned_at': (now or utc_now()).isoformat(), 'checks': checks,
            'note': 'Known-advisory matching only; this does not show the code is free of vulnerabilities.'}


def report_status(report):
    """0 clean, 1 known vulnerabilities, 2 any check unavailable, stale, incomplete, or no lockfiles."""
    checks = report['checks']
    if (any(check['status'] != 'scanned' for check in checks) or report['snapshot']['stale']
            or report.get('discovery') or not checks):
        return 2
    return 1 if any(check['vulnerabilities'] for check in checks) else 0


def scan_image(image, directory, max_age_days=DEFAULT_MAX_AGE_DAYS):
    validate_max_age(max_age_days)
    with tempfile.TemporaryDirectory(prefix='advisory-scan-') as temporary:
        root, lockfiles, discovery = extract_lockfiles(image, temporary)
        return build_report(root, lockfiles, directory, max_age_days=max_age_days, discovery=discovery)


def summarize(report):
    snapshot = report['snapshot']
    lines = [f"Advisory snapshot fetched {snapshot['fetched_at']} ({snapshot['age_days']} days old"
             f"{', STALE: ' + ', '.join(snapshot['stale_databases']) if snapshot['stale'] else ''})"]
    for problem in report.get('discovery', []):
        lines.append(f"{problem.get('lockfile') or '(discovery)'}: NOT SCANNED ({problem.get('reason')})")
    for check in report['checks']:
        if check['status'] == 'scanned':
            lines.append(f"{check['lockfile']}: {len(check['vulnerabilities'])} known advisories")
            lines.extend(f"  {v['id']} {v['package']} {v['version']}" for v in check['vulnerabilities'])
        else:
            lines.append(f"{check['lockfile']}: UNAVAILABLE ({check['reason']})")
    if not report['checks']:
        lines.append('No recognised lockfiles found; dependency advisories were not checked.')
    return '\n'.join(lines)
