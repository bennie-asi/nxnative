"""Exercise real encrypted round trips and fail-closed publication boundaries."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('ci_encrypt_ipa', Path(__file__).resolve().parents[1] / 'ci_encrypt_ipa.py')
ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)


class EncryptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'game.ipa'; self.source.write_bytes(os.urandom(128 * 1024))
        self.output = self.root / 'output/game.ipa.gpg'
        home = self.root / 'gpg'; home.mkdir(mode=0o700)
        environment = mock.patch.dict(os.environ, {'GNUPGHOME': str(home), 'IPA_PASSWORD': 'test-secret'})
        environment.start(); self.addCleanup(environment.stop)

    def test_real_gpg_roundtrip_and_ciphertext_tampering(self):
        gpg = os.environ.get('NX_TEST_GPG') or shutil.which('gpg')
        if not gpg: self.skipTest('GnuPG is required for the integration test')
        output = io.StringIO()
        with redirect_stdout(output):
            checksum = ci.encrypt(self.source, self.output, password='test-secret', gpg=gpg, poll_seconds=0.02)
        self.assertNotIn('test-secret', output.getvalue())
        self.assertEqual(checksum, hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertIn(checksum, (self.output.parent / 'SHA256SUMS.txt').read_text())
        command = [gpg, '--no-options', '--batch', '--yes', '--no-tty', '--pinentry-mode', 'loopback',
                   '--no-symkey-cache', '--passphrase-fd', '0', '--decrypt', str(self.output)]
        result = subprocess.run(command, input=b'test-secret\n', capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, 'The normal GnuPG reader must decrypt the result')
        self.assertEqual(result.stdout, self.source.read_bytes())
        data = bytearray(self.output.read_bytes()); data[-40] ^= 1; self.output.write_bytes(data)
        result = subprocess.run(command, input=b'test-secret\n', capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0, 'Ciphertext corruption must fail authentication')

    def test_no_space_does_not_start_encryption(self):
        with mock.patch.object(ci.shutil, 'disk_usage', return_value=mock.Mock(free=1)), \
                mock.patch.object(ci.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(ci.EncryptionError, 'free space'):
                ci.encrypt(self.source, self.output, password='test-secret')
        launch.assert_not_called(); self.assertFalse(self.output.exists())

    def test_stalled_child_is_terminated_without_publishing_partial_data(self):
        partial = self.output.with_name(self.output.name + '.partial')
        child = mock.Mock(stdin=io.BytesIO(), returncode=None)
        running = [True]
        child.poll.side_effect = lambda: None if running[0] else 1
        child.terminate.side_effect = lambda: running.__setitem__(0, False)
        def launch(*args, **kwargs):
            partial.write_bytes(b'unfinished')
            return child
        with mock.patch.object(ci.subprocess, 'Popen', side_effect=launch), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ci.EncryptionError, 'progress'):
                ci.encrypt(self.source, self.output, password='test-secret', stall_seconds=0.02, poll_seconds=0.01)
        child.terminate.assert_called_once()
        self.assertFalse(partial.exists()); self.assertFalse(self.output.exists())
        self.assertFalse((self.output.parent / 'SHA256SUMS.txt').exists())

    def test_child_error_does_not_publish_or_print_private_stderr(self):
        partial = self.output.with_name(self.output.name + '.partial')
        child = mock.Mock(stdin=io.BytesIO(), returncode=2)
        child.poll.return_value = 2
        def launch(*args, **kwargs):
            self.assertNotIn('test-secret', ' '.join(args[0]))
            self.assertNotIn('IPA_PASSWORD', kwargs['env'])
            kwargs['stderr'].write(b'test-secret private-file-name')
            partial.write_bytes(b'partial')
            return child
        output = io.StringIO()
        with mock.patch.object(ci.subprocess, 'Popen', side_effect=launch), redirect_stdout(output):
            with self.assertRaises(ci.EncryptionError) as error:
                ci.encrypt(self.source, self.output, password='test-secret')
        for secret in ('test-secret', 'private-file-name'):
            self.assertNotIn(secret, str(error.exception) + output.getvalue())
        self.assertFalse(partial.exists()); self.assertFalse(self.output.exists())

    def test_total_timeout_also_applies_while_output_is_growing(self):
        partial = self.output.with_name(self.output.name + '.partial')
        child = mock.Mock(stdin=io.BytesIO(), returncode=None)
        child.poll.return_value = None
        def launch(*args, **kwargs):
            partial.write_bytes(b'growing')
            return child
        with mock.patch.object(ci.subprocess, 'Popen', side_effect=launch), \
                mock.patch.object(ci.time, 'monotonic', side_effect=[0, 10]), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ci.EncryptionError, 'time limit'):
                ci.encrypt(self.source, self.output, password='test-secret', timeout_seconds=5)
        child.terminate.assert_called_once()
        self.assertFalse(partial.exists()); self.assertFalse(self.output.exists())


if __name__ == '__main__': unittest.main()
