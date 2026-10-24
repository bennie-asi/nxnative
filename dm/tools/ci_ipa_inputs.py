#!/usr/bin/env python3
"""Prepare private build inputs for .github/workflows/build-ipa.yml. Standard library only."""
import argparse
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

GIB = 2**30
DOWNLOAD_ATTEMPTS = 12


class InputError(Exception):
    pass


def load_inputs(env):
    for key in ('GAME_FILES_JSON', 'PROD_KEYS', 'IPA_PASSWORD'):
        if not env.get(key, '').strip():
            raise InputError(f'Missing repository secret: {key}. See docs/github-actions.md.')
    if '\n' in env['IPA_PASSWORD'] or '\r' in env['IPA_PASSWORD']:
        raise InputError('IPA_PASSWORD must be a single line.')
    try:
        files = json.loads(env['GAME_FILES_JSON'])
    except (ValueError, TypeError):
        raise InputError('GAME_FILES_JSON must be a JSON array of name/url objects.') from None
    if not isinstance(files, list) or not files:
        raise InputError('GAME_FILES_JSON must contain at least one file.')
    seen = set()
    for i, entry in enumerate(files, 1):
        if not isinstance(entry, dict):
            raise InputError(f'File {i}: expected an object with name and url.')
        name, url = entry.get('name'), entry.get('url')
        if (not isinstance(name, str) or not name or '/' in name or '\\' in name
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or not name.endswith(('.nsp', '.xci'))):
            raise InputError(f'File {i}: name must be a plain filename ending in .nsp or .xci.')
        if name.casefold() in seen:
            raise InputError(f'File {i}: duplicate filename (case insensitive).')
        seen.add(name.casefold())
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid_url = (parsed and parsed.scheme == 'https' and parsed.hostname
                         and not parsed.username and not parsed.password
                         and not any(c.isspace() for c in url))
        except ValueError:
            valid_url = False
        if not valid_url:
            raise InputError(f'File {i}: url must be a direct HTTPS download URL.')
        sha = entry.get('sha256')
        if sha is not None and (not isinstance(sha, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', sha)):
            raise InputError(f'File {i}: sha256 must contain 64 hexadecimal characters.')
    return files


def verify_file(path, sha256=None):
    with path.open('rb') as stream:
        header = stream.read(0x104)
    valid = header.startswith(b'PFS0') if path.suffix == '.nsp' else header[0x100:0x104] == b'HEAD'
    if not valid:
        raise InputError('Downloaded content is not an NSP/XCI. Use a direct file URL, not a share page or ZIP.')
    if sha256 is not None:
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest.lower() != sha256.lower():
            raise InputError('Downloaded file does not match its sha256.')


def download_file(entry, directory, index, opener=urlopen, pause=time.sleep):
    target = directory / entry['name']
    partial = target.with_name(target.name + '.part')
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            # Resume only with a checksum that verifies the complete input. A server that
            # ignores Range restarts the file; malformed partial responses never get appended.
            offset = partial.stat().st_size if entry.get('sha256') and partial.exists() else 0
            headers = {'User-Agent': 'nxnative-actions', 'Accept-Encoding': 'identity'}
            if offset: headers['Range'] = f'bytes={offset}-'
            request = Request(entry['url'], headers=headers)
            with opener(request, timeout=60) as response:
                expected = response.headers.get('Content-Length')
                expected = int(expected) if expected and expected.isdigit() else None
                status = getattr(response, 'status', 200)
                if status == 200: offset = 0
                elif status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if (not match or int(match[1]) != offset or int(match[2]) != int(match[3]) - 1
                            or expected is not None and int(match[2]) - offset + 1 != expected):
                        raise InputError(f'File {index}: invalid HTTP resume response.')
                    expected = int(match[3]) - offset
                else: raise InputError(f'File {index}: unexpected HTTP response status {status}.')
                if expected is not None and expected + GIB > shutil.disk_usage(directory).free:
                    raise InputError(f'File {index}: insufficient disk space for this download.')
                written, last_report = offset, time.monotonic()
                with partial.open('ab' if offset else 'wb') as output:
                    while chunk := response.read(8 * 2**20):
                        output.write(chunk)
                        written += len(chunk)
                        if time.monotonic() - last_report >= 30:
                            print(f'File {index}: downloaded {written / GIB:.2f} GiB', flush=True)
                            last_report = time.monotonic()
                    if expected is not None and written != expected + offset:
                        raise OSError('Incomplete download')
            partial.replace(target)
            verify_file(target, entry.get('sha256'))
            print(f'File {index}: ready ({target.stat().st_size / GIB:.2f} GiB)', flush=True)
            return
        except InputError:
            partial.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        except (OSError, URLError, HTTPException) as error:
            # Exception messages can contain signed URLs. Report only the HTTP status or error type.
            reason = f'HTTP {error.code}' if isinstance(error, HTTPError) else type(error).__name__
            if isinstance(error, HTTPError): error.close()
            if attempt == DOWNLOAD_ATTEMPTS:
                partial.unlink(missing_ok=True)
                raise InputError(f'File {index}: download failed ({reason}); check URL access and expiry.') from None
            # A tunnel or object-storage gateway may need tens of seconds to reconnect.
            # Keep verified partial data through that outage instead of exhausting retries
            # before the server recovers (a 12 GiB transfer hit HTTP 530 during recovery).
            delay = min(5 * 2**(attempt - 1), 30)
            print(f'File {index}: retrying download in {delay}s ({reason}, attempt {attempt}/{DOWNLOAD_ATTEMPTS})', flush=True)
            pause(delay)


def prepare_game(directory, files, keys):
    directory.mkdir(parents=True, exist_ok=True)
    for i, entry in enumerate(files, 1):
        download_file(entry, directory, i)
    size = sum((directory / entry['name']).stat().st_size for entry in files)
    free = shutil.disk_usage(directory).free
    # The README's 2 * input size + 10 GiB estimate includes the inputs already downloaded.
    required = size + 10 * GIB
    if free < required:
        raise InputError(f'Not enough build space: {free / GIB:.1f} GiB free; estimated '
                         f'{required / GIB:.1f} GiB needed after download. See docs/github-actions.md.')
    key_file = directory / 'prod.keys'
    with key_file.open('w', encoding='utf-8', newline='\n') as stream:
        os.chmod(key_file, 0o600)
        stream.write(keys.strip() + '\n')
    print(f'Prepared {len(files)} game file(s); {free / GIB:.1f} GiB free. Keys written without logging values.')


def check_encryption_space(directory):
    ipa = directory / 'game.ipa'
    if not ipa.is_file() or not ipa.stat().st_size:
        raise InputError('The build did not produce a non-empty game.ipa.')
    required = ipa.stat().st_size + GIB
    free = shutil.disk_usage(directory).free
    if free < required:
        raise InputError(f'Not enough space for encrypted IPA: {free / GIB:.1f} GiB free, '
                         f'{required / GIB:.1f} GiB needed for another copy and a 1 GiB margin.')


def collect_diagnostics(directory, output):
    """Archive only allowlisted textual diagnostics; game binaries and keys are excluded."""
    patterns = ('*_build/logs/*.log', '*_build/logs/*.json', '*_survey/logs/*.log',
                '*_survey/survey.md', '*_build/profile/*/game.tsv')
    files = sorted({p for pattern in patterns for p in directory.glob(pattern)
                    if p.is_file() and not p.is_symlink()})
    with tarfile.open(output, 'w:gz') as archive:
        for path in files: archive.add(path, arcname=path.relative_to(directory), recursive=False)
    print(f'Collected {len(files)} diagnostic file(s); archive must be encrypted before upload.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'download', 'check-encryption-space', 'diagnostics'))
    parser.add_argument('--game-dir', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            files = load_inputs(os.environ)
            print(f'Build secrets are configured for {len(files)} file(s).')
        else:
            if args.game_dir is None:
                raise InputError('--game-dir is required.')
            if args.command == 'download':
                files = load_inputs(os.environ)
                prepare_game(args.game_dir, files, os.environ['PROD_KEYS'])
            elif args.command == 'diagnostics':
                if args.output is None: raise InputError('--output is required for diagnostics.')
                collect_diagnostics(args.game_dir, args.output)
            else:
                check_encryption_space(args.game_dir)
    except (InputError, OSError) as error:
        # Only InputError messages are safe to print; filesystem errors can contain private names.
        message = str(error) if isinstance(error, InputError) else type(error).__name__
        print(f'[RED] {message}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
