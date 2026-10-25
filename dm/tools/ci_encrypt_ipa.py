#!/usr/bin/env python3
"""Encrypt a CI IPA with bounded waits and progress that never includes secrets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

GIB = 2**30


class EncryptionError(Exception):
    pass


def process_rss(pid):
    if os.name == 'nt': return None
    try:
        result = subprocess.run(['ps', '-o', 'rss=', '-p', str(pid)],
                                capture_output=True, text=True, timeout=3)
        return int(result.stdout.strip()) * 1024 if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired): return None


def encrypt(source, output, *, password, gpg='gpg', stall_seconds=300,
            timeout_seconds=1800, poll_seconds=1, report_seconds=30, log=None):
    if not password or '\n' in password or '\r' in password:
        raise EncryptionError('IPA_PASSWORD must contain one non-empty line.')
    source = Path(source).resolve(); output = Path(output).resolve()
    before = source.stat()
    if not source.is_file() or not before.st_size:
        raise EncryptionError('The IPA must be a non-empty regular file.')
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + '.partial')
    if source in (output, partial) or output.exists() or partial.exists():
        raise EncryptionError('Encryption requires a fresh output path.')
    if shutil.disk_usage(output.parent).free < before.st_size + GIB:
        raise EncryptionError('Not enough free space for encrypted IPA and a 1 GiB margin.')

    def report(stage, **fields):
        record = {'stage': stage, **fields}
        line = json.dumps(record, sort_keys=True)
        print(line, flush=True)
        if log is not None:
            with Path(log).open('a', encoding='utf-8') as stream: stream.write(line + '\n')

    # Explicit S2K count avoids agent calibration, and no symmetric-key cache
    # avoids an agent round trip. AES-256 and the authenticated OpenPGP format
    # remain readable by the normal GnuPG download instructions.
    command = [gpg, '--no-options', '--batch', '--yes', '--no-tty',
               '--pinentry-mode', 'loopback', '--passphrase-fd', '0',
               '--no-symkey-cache', '--symmetric', '--cipher-algo', 'AES256',
               '--s2k-mode', '3', '--s2k-digest-algo', 'SHA512',
               '--s2k-count', '65011712', '--compress-algo', 'none',
               '--output', str(partial), str(source)]
    env = {k: v for k, v in os.environ.items()
           if k not in ('IPA_PASSWORD', 'PROD_KEYS', 'GAME_FILES_JSON', 'CI_PROBE_JSON')}
    started = changed = last_report = time.monotonic(); previous = 0; child = None
    try:
        with tempfile.TemporaryFile() as errors:
            child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=errors, env=env)
            child.stdin.write((password + '\n').encode('utf-8')); child.stdin.close()
            report('encrypting', input_bytes=before.st_size, output_bytes=0,
                   free_bytes=shutil.disk_usage(output.parent).free, elapsed_seconds=0)
            while child.poll() is None:
                now = time.monotonic()
                size = partial.stat().st_size if partial.exists() else 0
                if size != previous: previous = size; changed = now
                if now - last_report >= report_seconds:
                    report('encrypting', input_bytes=before.st_size, output_bytes=size,
                           free_bytes=shutil.disk_usage(output.parent).free,
                           rss_bytes=process_rss(child.pid), elapsed_seconds=round(now-started))
                    last_report = now
                if now - changed >= stall_seconds:
                    raise EncryptionError('Encryption stopped making progress; partial output is not publishable.')
                if now - started >= timeout_seconds:
                    raise EncryptionError('Encryption exceeded its time limit; partial output is not publishable.')
                if shutil.disk_usage(output.parent).free < GIB // 2:
                    raise EncryptionError('Encryption reached the free-space reserve.')
                time.sleep(poll_seconds)
            if child.returncode:
                raise EncryptionError(f'GnuPG encryption failed with exit code {child.returncode}; no private diagnostics printed.')
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise EncryptionError('The source IPA changed during encryption.')
        if not partial.is_file() or partial.stat().st_size <= before.st_size:
            raise EncryptionError('GnuPG did not produce a complete uncompressed encrypted IPA.')
        report('hashing', output_bytes=partial.stat().st_size)
        with partial.open('rb') as stream: checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        partial.replace(output)
        (output.parent / 'SHA256SUMS.txt').write_text(checksum + '  ' + output.name + '\n', encoding='ascii')
        report('encrypted', input_bytes=before.st_size, output_bytes=output.stat().st_size,
               elapsed_seconds=round(time.monotonic()-started))
        return checksum
    except BaseException:
        if child is not None and child.poll() is None:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
        partial.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    try:
        encrypt(args.source, args.output, password=os.environ.get('IPA_PASSWORD', ''), log=args.log)
    except (EncryptionError, OSError) as error:
        # OSError text can contain private paths. GnuPG stderr is never emitted.
        message = str(error) if isinstance(error, EncryptionError) else type(error).__name__
        print('[RED] ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__': sys.exit(main())
