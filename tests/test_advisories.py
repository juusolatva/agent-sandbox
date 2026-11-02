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
        with self.assertRaises(ValueError):
            advisories.fetch(self.snapshot, ('Hackage',), opener({}))
        with self.assertRaises(ValueError):
            advisories.fetch(self.snapshot, (), opener({}))

    def test_corrupt_download_leaves_existing_snapshot_untouched(self):
        self.fetch('Go')
        before = advisories.database_path(self.snapshot, 'Go').read_bytes()
        with self.assertRaises(Exception):
            advisories.fetch(self.snapshot, ('Go',), opener({'Go': b'not a zip'}))
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

    def test_extraction_keeps_only_regular_recognised_lockfiles(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as bundle:
            for name, data in (('./go.mod', b'module x\n'), ('./sub/requirements.txt', b'a==1\n'),
                               ('./notes.txt', b'ignored'), ('./sub/../../escape/go.mod', b'x')):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                bundle.addfile(member, io.BytesIO(data))
            link = tarfile.TarInfo('./Cargo.lock')
            link.type, link.linkname = tarfile.SYMTYPE, '/etc/passwd'
            bundle.addfile(link)

        def fake_docker(command, stdout=None, **kwargs):
            stdout.write(stream.getvalue())
            return subprocess.CompletedProcess(command, 0)
        with patch.object(advisories.subprocess, 'run', side_effect=fake_docker) as run:
            root, found = advisories.extract_lockfiles('sha256:image', self.directory)
        command = run.call_args.args[0]
        self.assertIn('--network', command)
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertEqual(found, ['go.mod', 'sub/requirements.txt'])
        self.assertFalse((self.directory.parent / 'escape').exists())
        self.assertFalse((root / 'Cargo.lock').exists())

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
