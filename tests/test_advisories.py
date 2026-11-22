import contextlib
import datetime
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import advisories

OSV_RECORD = {'id': 'GO-TEST-0001', 'modified': '2020-01-01T00:00:00Z', 'summary': 'Test advisory',
              'affected': [{'package': {'name': 'golang.org/x/text', 'ecosystem': 'Go'},
                            'ranges': [{'type': 'SEMVER', 'events': [{'introduced': '0'}, {'fixed': '0.3.3'}]}]}]}


def database_bytes(records=(OSV_RECORD,)):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for record in records:
            archive.writestr(record['id'] + '.json', json.dumps(record))
    return stream.getvalue()


class Response(io.BytesIO):
    headers = {'Last-Modified': 'Fri, 30 Oct 2026 23:33:17 GMT'}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def opener(payloads):
    def open_url(url, timeout=None):
        ecosystem = url.rsplit('/', 2)[-2]
        return Response(payloads[ecosystem])
    return open_url


class AdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='advisories-test-'))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.snapshot = self.directory / 'snapshot'
        self.snapshot.mkdir()

    def fetch(self, *ecosystems, payload=None):
        payload = payload or database_bytes()
        return advisories.fetch(self.snapshot, ecosystems, opener({e: payload for e in ecosystems}))

    def test_fetch_records_hash_size_and_time(self):
        payload = database_bytes()
        metadata = self.fetch('Go', payload=payload)
        self.assertEqual(metadata['databases']['Go']['sha256'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(metadata['databases']['Go']['size'], len(payload))
        self.assertTrue(advisories.database_path(self.snapshot, 'Go').is_file())
        _, usable, problems = advisories.verify_snapshot(self.snapshot)
        self.assertEqual((usable, problems), (['Go'], {}))

    def test_fetch_merges_ecosystems_and_rejects_unknown(self):
        self.fetch('Go')
        self.fetch('npm')
        self.assertEqual(sorted(advisories.load_metadata(self.snapshot)['databases']), ['Go', 'npm'])
        unused = opener({})
        with self.assertRaisesRegex(ValueError, 'Unsupported ecosystems'):
            advisories.fetch(self.snapshot, ('Hackage',), unused)
        with self.assertRaisesRegex(ValueError, 'none selected'):
            advisories.fetch(self.snapshot, (), unused)

    def test_corrupt_download_leaves_existing_snapshot_untouched(self):
        self.fetch('Go')
        before = advisories.database_path(self.snapshot, 'Go').read_bytes()
        corrupt = opener({'Go': b'not a zip'})
        with self.assertRaises(zipfile.BadZipFile):
            advisories.fetch(self.snapshot, ('Go',), corrupt)
        self.assertEqual(advisories.database_path(self.snapshot, 'Go').read_bytes(), before)
        self.assertEqual(advisories.verify_snapshot(self.snapshot)[2], {})

    def test_oversized_download_is_rejected(self):
        with patch.object(advisories, 'MAX_DATABASE_BYTES', 10), self.assertRaises(ValueError):
            self.fetch('Go')

    def test_tampered_or_missing_database_is_unusable(self):
        self.fetch('Go', 'PyPI')
        advisories.database_path(self.snapshot, 'Go').write_bytes(database_bytes([{**OSV_RECORD, 'id': 'X'}]))
        advisories.database_path(self.snapshot, 'PyPI').unlink()
        _, usable, problems = advisories.verify_snapshot(self.snapshot)
        self.assertEqual(usable, [])
        self.assertIn('hash', problems['Go'])
        self.assertIn('missing', problems['PyPI'])

    def test_missing_metadata_is_an_error(self):
        with self.assertRaises(ValueError):
            advisories.verify_snapshot(self.snapshot)

    def test_lockfile_recognition(self):
        for name, expected in {'go.mod': 'Go', 'Cargo.lock': 'crates.io', 'package-lock.json': 'npm',
                               'requirements.txt': 'PyPI', 'requirements-dev.txt': 'PyPI', 'uv.lock': 'PyPI',
                               'package.json': None, 'notes.txt': None}.items():
            self.assertEqual(advisories.lockfile_ecosystem(name), expected, name)

    def report(self, scan, lockfiles=('go.mod',), **options):
        return advisories.build_report(self.directory, lockfiles, self.snapshot, scan=scan, **options)

    def test_report_is_unavailable_never_clean_without_a_database(self):
        self.fetch('Go')
        called = []
        report = self.report(lambda *a: called.append(a), ('Cargo.lock',))
        self.assertEqual(called, [])
        self.assertEqual(report['checks'][0]['status'], 'unavailable')
        self.assertEqual(advisories.report_status(report), 2)
        self.assertIn('UNAVAILABLE', advisories.summarize(report))

    def test_status_distinguishes_clean_findings_and_stale(self):
        self.fetch('Go')
        clean = lambda *a: {'status': 'scanned', 'vulnerabilities': []}
        dirty = lambda *a: {'status': 'scanned', 'vulnerabilities': [
            {'id': 'GO-1', 'package': 'p', 'version': '1', 'aliases': [], 'summary': ''}]}
        self.assertEqual(advisories.report_status(self.report(clean)), 0)
        self.assertEqual(advisories.report_status(self.report(dirty)), 1)
        later = advisories.utc_now() + datetime.timedelta(days=30)
        stale = self.report(clean, now=later)
        self.assertTrue(stale['snapshot']['stale'])
        self.assertEqual(advisories.report_status(stale), 2)
        self.assertEqual(advisories.report_status(self.report(clean, lockfiles=())), 2)

    def test_scan_handles_missing_scanner_and_bad_output(self):
        def runner(result):
            return lambda *a, **k: result
        missing = advisories.scan_lockfile(self.directory, 'go.mod', self.snapshot,
                                           runner=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        self.assertEqual(missing['status'], 'unavailable')
        for completed in (subprocess.CompletedProcess([], 2, '', 'boom'),
                          subprocess.CompletedProcess([], 0, 'not json', '')):
            self.assertEqual(advisories.scan_lockfile(self.directory, 'go.mod', self.snapshot,
                                                      runner=runner(completed))['status'], 'unavailable')

    def test_scan_command_is_offline(self):
        seen = {}
        def runner(command, **kwargs):
            seen.update(command=command, env=kwargs['env'])
            return subprocess.CompletedProcess(command, 0, '{"results": []}', '')
        advisories.scan_lockfile(self.directory, 'go.mod', self.snapshot, runner=runner)
        self.assertIn('--offline', seen['command'])
        self.assertIn('--offline-vulnerabilities', seen['command'])
        self.assertEqual(seen['env']['OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY'], str(self.snapshot))

    def extract(self, members, inventory=None):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as bundle:
            for name, data in members:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                bundle.addfile(member, io.BytesIO(data))
            link = tarfile.TarInfo('./Cargo.lock')
            link.type, link.linkname = tarfile.SYMTYPE, '/etc/passwd'
            bundle.addfile(link)
            if inventory is not None:
                data = json.dumps(inventory).encode()
                member = tarfile.TarInfo(advisories.INVENTORY)
                member.size = len(data)
                bundle.addfile(member, io.BytesIO(data))
        commands = []

        def fake_docker(command, stdout=None, **kwargs):
            commands.append(command)
            if command[1] == 'run':
                stdout.write(stream.getvalue())
            return subprocess.CompletedProcess(command, 0)
        with patch.object(advisories.subprocess, 'run', side_effect=fake_docker):
            return advisories.extract_lockfiles('sha256:image', self.directory), commands

    def test_extraction_keeps_only_regular_recognised_lockfiles(self):
        (root, found, problems), commands = self.extract(
            (('go.mod', b'module x\n'), ('sub/requirements.txt', b'a==1\n'),
             ('notes.txt', b'ignored'), ('sub/../../escape/go.mod', b'x')),
            inventory={'omitted': [], 'errors': []})
        command = commands[0]
        self.assertEqual(command[command.index('--network') + 1], 'none')
        for flag in ('--read-only', '--init', '--pids-limit', '--memory', '--user', '--log-driver'):
            self.assertIn(flag, command)
        self.assertIn('FTP_PROXY=', command)
        self.assertEqual(commands[1][:3], ['docker', 'rm', '-f'])
        self.assertEqual(commands[1][3], command[command.index('--name') + 1])
        self.assertEqual(found, ['go.mod', 'sub/requirements.txt'])
        self.assertFalse((self.directory.parent / 'escape').exists())
        self.assertFalse((root / 'Cargo.lock').exists())
        # Rejected members are reported, not silently dropped.
        self.assertEqual(sorted(p['lockfile'] for p in problems),
                         ['Cargo.lock', 'notes.txt', 'sub/../../escape/go.mod'])

    def test_omitted_lockfiles_make_an_otherwise_clean_scan_incomplete(self):
        self.fetch('Go')
        clean = lambda *a: {'status': 'scanned', 'vulnerabilities': []}
        for inventory in ({'omitted': [{'lockfile': 'big/go.mod', 'reason': 'larger than 5242880 bytes'}],
                           'errors': []},
                          {'omitted': [], 'errors': ['locked: Permission denied']},
                          None):
            with self.subTest(inventory=inventory):
                shutil.rmtree(self.directory / 'lockfiles', ignore_errors=True)
                (_, found, problems), _ = self.extract((('go.mod', b'module x\n'),), inventory)
                problems = [p for p in problems if p['lockfile'] != 'Cargo.lock']
                self.assertTrue(problems)
                report = self.report(clean, found, discovery=problems)
                self.assertEqual(advisories.report_status(report), 2)
                self.assertIn('NOT SCANNED', advisories.summarize(report))

    def test_discovery_script_reports_every_limit_and_error(self):
        import os
        import sys
        project = self.directory / 'workspace'
        (project / 'big').mkdir(parents=True)
        (project / 'go.mod').write_text('module x\n')
        (project / 'big' / 'Cargo.lock').write_bytes(b'x' * 50)
        (project / 'requirements.txt').symlink_to('go.mod')
        (project / 'node_modules').mkdir()
        (project / 'node_modules' / 'package-lock.json').write_text('{}')
        locked = project / 'locked'
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        options = {'root': str(project), 'names': sorted(advisories.EXACT_LOCKFILES),
                   'pruned': list(advisories.PRUNED), 'max_bytes': 20, 'max_files': 200,
                   'max_total': 1000, 'inventory': advisories.INVENTORY}
        output = subprocess.run([sys.executable, '-I', '-c', advisories.DISCOVERY_SCRIPT, json.dumps(options)],
                                capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(output)) as bundle:
            names = bundle.getnames()
            inventory = json.loads(bundle.extractfile(advisories.INVENTORY).read())
        self.assertEqual(sorted(names), sorted(['go.mod', advisories.INVENTORY]))
        reasons = {item['lockfile']: item['reason'] for item in inventory['omitted']}
        self.assertIn('larger than', reasons['big/Cargo.lock'])
        self.assertEqual(reasons['requirements.txt'], 'not a regular file')
        if os.geteuid() != 0:
            self.assertTrue(any('locked' in error for error in inventory['errors']))
        options.update(max_bytes=100, max_files=1)
        output = subprocess.run([sys.executable, '-I', '-c', advisories.DISCOVERY_SCRIPT, json.dumps(options)],
                                capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(output)) as bundle:
            inventory = json.loads(bundle.extractfile(advisories.INVENTORY).read())
        self.assertTrue(any('more than 1 lockfiles' in item['reason'] for item in inventory['omitted']))

    def test_refreshing_one_ecosystem_does_not_renew_another(self):
        old = advisories.utc_now() - datetime.timedelta(days=30)
        with patch.object(advisories, 'utc_now', return_value=old):
            self.fetch('Go')
        self.fetch('npm')
        clean = lambda *a: {'status': 'scanned', 'vulnerabilities': []}
        go = self.report(clean, ('go.mod',))
        self.assertEqual(go['snapshot']['stale_databases'], ['Go'])
        self.assertEqual(advisories.report_status(go), 2)
        npm = self.report(clean, ('package-lock.json',))
        self.assertFalse(npm['snapshot']['stale'])
        self.assertEqual(advisories.report_status(npm), 0)
        # Snapshots written before per-database fetch times have unknown age: not clean.
        metadata = json.loads((self.snapshot / 'metadata.json').read_text())
        del metadata['databases']['npm']['fetched_at']
        (self.snapshot / 'metadata.json').write_text(json.dumps(metadata))
        self.assertEqual(advisories.report_status(self.report(clean, ('package-lock.json',))), 2)

    def test_invalid_max_age_is_rejected(self):
        self.fetch('Go')
        for value in (float('nan'), float('inf'), -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.report(lambda *a: {'status': 'scanned', 'vulnerabilities': []}, max_age_days=value)

    def test_scanner_exit_one_without_parsed_findings_is_unavailable(self):
        result = subprocess.CompletedProcess([], 1, '{"results": []}', '')
        check = advisories.scan_lockfile(self.directory, 'go.mod', self.snapshot, runner=lambda *a, **k: result)
        self.assertEqual(check['status'], 'unavailable')

    def test_partial_multi_ecosystem_fetch_replaces_nothing(self):
        self.fetch('Go', 'PyPI')
        before = advisories.database_path(self.snapshot, 'Go').read_bytes()
        updated = database_bytes([{**OSV_RECORD, 'id': 'GO-NEW'}])
        partly_corrupt = opener({'Go': updated, 'PyPI': b'not a zip'})
        with self.assertRaises(zipfile.BadZipFile):
            advisories.fetch(self.snapshot, ('Go', 'PyPI'), partly_corrupt)
        self.assertEqual(advisories.database_path(self.snapshot, 'Go').read_bytes(), before)
        self.assertEqual(advisories.verify_snapshot(self.snapshot)[2], {})

    def test_repeated_ecosystem_downloads_once(self):
        self.fetch('Go')
        calls = []
        payload = database_bytes([{**OSV_RECORD, 'id': 'GO-NEW'}])

        def counting(url, timeout=None):
            calls.append(url)
            return Response(payload)
        metadata = advisories.fetch(self.snapshot, ('Go', 'Go'), counting)
        self.assertEqual(len(calls), 1)
        self.assertEqual(metadata['databases']['Go']['sha256'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(advisories.verify_snapshot(self.snapshot)[2], {})

    def test_freshness_compares_exact_age_not_rounded_display(self):
        self.fetch('Go')
        fetched = datetime.datetime.fromisoformat(
            advisories.load_metadata(self.snapshot)['databases']['Go']['fetched_at'])
        clean = lambda *a: {'status': 'scanned', 'vulnerabilities': []}
        # 7.004 days displays as 7.0 but is past a 7-day limit.
        report = self.report(clean, now=fetched + datetime.timedelta(days=7.004))
        self.assertEqual(report['snapshot']['database_age_days']['Go'], 7.0)
        self.assertTrue(report['snapshot']['stale'])
        just_fetched = self.report(clean, max_age_days=0, now=fetched + datetime.timedelta(minutes=5))
        self.assertTrue(just_fetched['snapshot']['stale'])

    def test_fetch_creates_missing_parent_directories(self):
        nested = self.directory / 'a' / 'b' / 'snapshot'
        advisories.fetch(nested, ('Go',), opener({'Go': database_bytes()}))
        self.assertEqual(advisories.verify_snapshot(nested)[1], ['Go'])

    @unittest.skipUnless(shutil.which('osv-scanner'), 'osv-scanner is not installed')
    def test_real_scanner_finds_advisory_from_snapshot_only(self):
        self.fetch('Go')
        project = self.directory / 'project'
        project.mkdir()
        (project / 'go.mod').write_text('module x\n\ngo 1.26\n\nrequire golang.org/x/text v0.3.0\n')
        report = advisories.build_report(project, ['go.mod'], self.snapshot)
        (check,) = report['checks']
        self.assertEqual(check['status'], 'scanned', check)
        self.assertEqual([v['id'] for v in check['vulnerabilities']], ['GO-TEST-0001'])
        self.assertEqual(advisories.report_status(report), 1)


if __name__ == '__main__':
    unittest.main()
