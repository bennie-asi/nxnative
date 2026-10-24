"""Private CI input handling, without real game files, credentials, networking or Xcode."""
import contextlib
import hashlib
from http.client import IncompleteRead
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import tarfile
import types
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

SCRIPT = Path(__file__).resolve().parents[1] / 'ci_ipa_inputs.py'
spec = importlib.util.spec_from_file_location('ci_ipa_inputs', SCRIPT)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)

ENTRY = {'name': 'Base [010015100B514000][v0].nsp',
         'url': 'https://storage.example.com/base?signature=private-token'}
KEYS = 'master_key_00 = synthetic-key-value'
PASSWORD = 'synthetic-password'
NSP = b'PFS0' + b'\0' * 64


def environment(files=None):
    return {'GAME_FILES_JSON': json.dumps([ENTRY] if files is None else files),
            'PROD_KEYS': KEYS, 'IPA_PASSWORD': PASSWORD}


class Response(io.BytesIO):
    def __init__(self, data, length=None, status=200, content_range=None):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data) if length is None else length)}
        self.status = status
        if content_range: self.headers['Content-Range'] = content_range


class Inputs(unittest.TestCase):
    def test_probe_requires_https_and_checksum_without_game_keys(self):
        value = {'url': ENTRY['url'], 'sha256': 'a' * 64, 'name': '../ignored'}
        checked = ci.probe_input({'CI_PROBE_JSON': json.dumps(value)})
        self.assertEqual(checked, {'url': ENTRY['url'], 'sha256': 'a' * 64, 'name': 'probe.tar.gz.gpg'})
        for invalid in ({}, [], None, {**value, 'sha256': ''}, {**value, 'url': 'http://example.com'}):
            with self.subTest(invalid=invalid), self.assertRaises(ci.InputError):
                ci.probe_input({'CI_PROBE_JSON': json.dumps(invalid)})

    def test_manifest_and_optional_checksum(self):
        entry = {**ENTRY, 'sha256': hashlib.sha256(NSP).hexdigest()}
        self.assertEqual(ci.load_inputs(environment([entry])), [entry])

    def test_invalid_manifest(self):
        for value in ('not JSON', '{}', '[]', 'null', '[null]'):
            with self.subTest(value=value), self.assertRaises(ci.InputError):
                ci.load_inputs({**environment(), 'GAME_FILES_JSON': value})

    def test_reject_unsafe_names_urls_and_checksums(self):
        bad_entries = [
            {**ENTRY, 'name': name} for name in
            ('../base.nsp', '/base.nsp', 'folder/base.nsp', 'folder\\base.nsp',
             'base.nsp\n', 'base.zip', 'BASE.NSP')
        ] + [
            {**ENTRY, 'url': url} for url in
            ('http://example.com/base.nsp', 'file:///base.nsp', 'https://',
             'https://example.com/has space.nsp', 'https://user:pass@example.com/base.nsp', None)
        ] + [{**ENTRY, 'sha256': 'invalid'}]
        for entry in bad_entries:
            with self.subTest(entry=entry), self.assertRaises(ci.InputError):
                ci.load_inputs(environment([entry]))

    def test_case_insensitive_duplicate(self):
        with self.assertRaisesRegex(ci.InputError, 'duplicate'):
            ci.load_inputs(environment([ENTRY, {**ENTRY, 'name': ENTRY['name'].upper()[:-4] + '.nsp'}]))

    def test_missing_secrets_and_multiline_password(self):
        for name in environment():
            with self.subTest(name=name), self.assertRaisesRegex(ci.InputError, name):
                ci.load_inputs({**environment(), name: ''})
        with self.assertRaisesRegex(ci.InputError, 'single line'):
            ci.load_inputs({**environment(), 'IPA_PASSWORD': 'first\nsecond'})


class Files(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        patcher = mock.patch.object(ci.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=64 * ci.GIB))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_download_verifies_then_publishes(self):
        entry = {**ENTRY, 'sha256': hashlib.sha256(NSP).hexdigest().upper()}
        opener = mock.Mock(return_value=Response(NSP))
        with contextlib.redirect_stdout(io.StringIO()):
            ci.download_file(entry, self.directory, 1, opener=opener)
        self.assertEqual((self.directory / ENTRY['name']).read_bytes(), NSP)
        self.assertFalse(list(self.directory.glob('*.part')))
        self.assertEqual(opener.call_args.args[0].full_url, ENTRY['url'])

    def test_encrypted_probe_blob_uses_checksum_verification(self):
        blob = b'encrypted diagnostic input'
        entry = {'name': 'probe.tar.gz.gpg', 'url': ENTRY['url'], 'sha256': hashlib.sha256(blob).hexdigest()}
        with contextlib.redirect_stdout(io.StringIO()):
            ci.download_file(entry, self.directory, 1, opener=mock.Mock(return_value=Response(blob)), verifier=ci.verify_checksum)
        self.assertEqual((self.directory / entry['name']).read_bytes(), blob)

    def test_retries_partial_download(self):
        opener = mock.Mock(side_effect=[Response(NSP[:8], len(NSP)), Response(NSP)])
        pause = mock.Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            ci.download_file(ENTRY, self.directory, 1, opener=opener, pause=pause)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual((self.directory / ENTRY['name']).read_bytes(), NSP)
        pause.assert_called_once()

    def test_checksum_download_resumes_an_interruption(self):
        entry = {**ENTRY, 'sha256': hashlib.sha256(NSP).hexdigest()}
        opener = mock.Mock(side_effect=[Response(NSP[:8], len(NSP)),
            Response(NSP[8:], status=206, content_range=f'bytes 8-{len(NSP)-1}/{len(NSP)}')])
        with contextlib.redirect_stdout(io.StringIO()):
            ci.download_file(entry, self.directory, 1, opener=opener, pause=mock.Mock())
        self.assertEqual(opener.call_args.args[0].get_header('Range'), 'bytes=8-')
        self.assertEqual((self.directory / ENTRY['name']).read_bytes(), NSP)

    def test_ignored_range_restarts_instead_of_appending(self):
        entry = {**ENTRY, 'sha256': hashlib.sha256(NSP).hexdigest()}
        opener = mock.Mock(side_effect=[Response(NSP[:8], len(NSP)), Response(NSP)])
        with contextlib.redirect_stdout(io.StringIO()):
            ci.download_file(entry, self.directory, 1, opener=opener, pause=mock.Mock())
        self.assertEqual((self.directory / ENTRY['name']).read_bytes(), NSP)

    def test_wrong_resume_range_is_rejected(self):
        entry = {**ENTRY, 'sha256': hashlib.sha256(NSP).hexdigest()}
        opener = mock.Mock(side_effect=[Response(NSP[:8], len(NSP)),
            Response(NSP[8:], status=206, content_range=f'bytes 0-{len(NSP)-9}/{len(NSP)-8}')])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ci.InputError, 'resume'):
            ci.download_file(entry, self.directory, 1, opener=opener, pause=mock.Mock())
        self.assertFalse(list(self.directory.iterdir()))

    def test_gateway_outage_keeps_the_partial_file_until_recovery(self):
        entry = {**ENTRY, 'sha256': hashlib.sha256(NSP).hexdigest()}
        unavailable = HTTPError(ENTRY['url'], 530, 'Gateway temporarily disconnected', {}, None)
        opener = mock.Mock(side_effect=[Response(NSP[:8], len(NSP)), unavailable, unavailable,
            Response(NSP[8:], status=206, content_range=f'bytes 8-{len(NSP)-1}/{len(NSP)}')])
        pause = mock.Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            ci.download_file(entry, self.directory, 1, opener=opener, pause=pause)
        self.assertEqual((self.directory / ENTRY['name']).read_bytes(), NSP)
        self.assertTrue(all(call.args[0].get_header('Range') == 'bytes=8-'
                            for call in opener.call_args_list[1:]))
        self.assertGreaterEqual(sum(call.args[0] for call in pause.call_args_list), 10)

    def test_network_errors_do_not_disclose_url(self):
        for error in (HTTPError(ENTRY['url'], 403, 'private-token', {}, None),
                      URLError(ENTRY['url']), IncompleteRead(b'partial')):
            output = io.StringIO()
            with self.subTest(error=type(error).__name__), contextlib.redirect_stdout(output):
                with self.assertRaises(ci.InputError) as failure:
                    ci.download_file(ENTRY, self.directory, 1,
                                     opener=mock.Mock(side_effect=error), pause=mock.Mock())
            self.assertNotIn('private-token', str(failure.exception) + output.getvalue())
            self.assertFalse(list(self.directory.iterdir()))

    def test_rejects_share_page_and_checksum_mismatch(self):
        for data, sha in ((b'<html>sign in</html>', None), (NSP, '0' * 64)):
            entry = {**ENTRY, **({'sha256': sha} if sha else {})}
            with self.subTest(data=data), self.assertRaises(ci.InputError):
                ci.download_file(entry, self.directory, 1, opener=mock.Mock(return_value=Response(data)))
            self.assertFalse(list(self.directory.iterdir()))

    def test_xci_header(self):
        path = self.directory / 'game.xci'
        path.write_bytes(b'\0' * 0x100 + b'HEAD')
        ci.verify_file(path)

    def test_disk_failure_does_not_write_keys(self):
        def fake_download(entry, directory, index):
            (directory / entry['name']).write_bytes(NSP)
        with mock.patch.object(ci, 'download_file', side_effect=fake_download):
            with mock.patch.object(ci.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=ci.GIB)):
                with self.assertRaisesRegex(ci.InputError, 'Not enough build space'):
                    ci.prepare_game(self.directory, [ENTRY], KEYS)
            self.assertFalse((self.directory / 'prod.keys').exists())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                ci.prepare_game(self.directory, [ENTRY], KEYS)
            self.assertEqual((self.directory / 'prod.keys').read_text(), KEYS + '\n')
            self.assertNotIn(KEYS, output.getvalue())

    def test_encryption_requires_space_for_second_copy(self):
        (self.directory / 'game.ipa').write_bytes(b'zip')
        ci.check_encryption_space(self.directory)
        with mock.patch.object(ci.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=ci.GIB)):
            with self.assertRaisesRegex(ci.InputError, 'encrypted IPA'):
                ci.check_encryption_space(self.directory)

    def test_diagnostics_include_logs_but_exclude_keys_and_game_data(self):
        for name in ('0100643002136000_build/logs/native_probe.log',
                     '0100643002136000_build/logs/native_probe.json',
                     '0100643002136000_build/profile/0100643002136000/game.tsv',
                     '0100643002136000_survey/survey.md',
                     'prod.keys', 'base.xci', '0100643002136000_build/extract/RomFS/root.arc'):
            path = self.directory / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(name)
        output = self.directory / 'diagnostics.tar.gz'
        with contextlib.redirect_stdout(io.StringIO()): ci.collect_diagnostics(self.directory, output)
        with tarfile.open(output) as archive:
            names = archive.getnames()
        self.assertEqual(len(names), 4)
        self.assertTrue(any(name.endswith('native_probe.log') for name in names))
        self.assertFalse(any(name.endswith(('.keys', '.xci', '.arc')) for name in names))


class Cli(unittest.TestCase):
    def test_validation_error_does_not_print_secret_values(self):
        env = {**environment(), 'GAME_FILES_JSON': 'private-token invalid JSON'}
        with mock.patch.dict(ci.os.environ, env), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(ci.main(['validate']), 1)
        for value in ('private-token', KEYS, PASSWORD):
            self.assertNotIn(value, error.getvalue())


if __name__ == '__main__':
    unittest.main()
