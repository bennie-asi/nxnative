#!/usr/bin/env python3
"""Rebuild only the host of a previous unsigned NVN IPA, preserving game resources.

Requires the previous build's decrypted diagnostics and the exact prepared module
bundle. Shader-chain changes and different rewritten modules require a full build.
This tool never signs, installs, or claims runtime compatibility.
"""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import zipfile


class RebuildError(Exception):
    pass


def unpack_ipa(source, destination):
    """Accept one flat iOS application, rejecting paths outside its Payload."""
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        roots = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in entry.orig_filename or '\0' in entry.orig_filename:
                raise RebuildError('Unsafe path in source IPA')
            if len(path.parts) >= 2:
                if path.parts[0] != 'Payload' or not path.parts[1].endswith('.app'):
                    raise RebuildError('Source IPA must contain only one Payload application')
                roots.add(path.parts[1])
            elif path.parts != ('Payload',):
                raise RebuildError('Unexpected entry outside the application')
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise RebuildError('Source IPA symlinks are not supported by this rebuild tool')
        if len(roots) != 1:
            raise RebuildError('Expected one source application')
        archive.extractall(destination)
        for entry in entries:
            mode = entry.external_attr >> 16 & 0o777
            if mode: (destination / entry.filename).chmod(mode)
    app = destination / 'Payload' / roots.pop()
    if (app / '_CodeSignature').exists() or (app / 'embedded.mobileprovision').exists():
        raise RebuildError('Use an unsigned nxnative build, before sideloading')
    return app


def macho_sections(path):
    """Read section file extents, without loading the embedded modules into RAM."""
    size = path.stat().st_size
    with path.open('rb') as source:
        header = source.read(32)
        if len(header) != 32:
            raise RebuildError('Truncated executable')
        magic, cpu, _, _, count, length, _, _ = struct.unpack('<8I', header)
        if magic != 0xfeedfacf or cpu != 0x100000c or length > 16 * 2**20:
            raise RebuildError('Expected an ARM64 Mach-O executable')
        data = source.read(length)
    if len(data) != length:
        raise RebuildError('Truncated Mach-O commands')
    sections = {}; at = 0
    for _ in range(count):
        if at + 8 > length:
            raise RebuildError('Invalid Mach-O command table')
        command, n = struct.unpack_from('<II', data, at)
        if n < 8 or at + n > length:
            raise RebuildError('Invalid Mach-O command size')
        if command == 0x19:
            if n < 72:
                raise RebuildError('Truncated segment command')
            nsections = struct.unpack_from('<I', data, at + 64)[0]
            if 72 + nsections * 80 > n:
                raise RebuildError('Truncated section table')
            for i in range(nsections):
                offset = at + 72 + i * 80
                name, segment, _, size_, file_offset = struct.unpack_from('<16s16sQQI', data, offset)
                name = name.split(b'\0')[0].decode('ascii'); segment = segment.split(b'\0')[0].decode('ascii')
                flags = struct.unpack_from('<I', data, offset + 64)[0]
                if flags & 0xff in (1, 12, 18): continue  # zero-fill sections have no file extent
                if file_offset + size_ > size or (segment, name) in sections:
                    raise RebuildError('Invalid or duplicate Mach-O section extent')
                sections[segment, name] = (file_offset, size_)
        at += n
    return sections


def verify_modules(app, executable, patched, modules):
    if (patched / 'nro.tsv').exists() or (app / 'dm/nro.tsv').exists():
        raise RebuildError('Embedded NRO modules require a full build')
    sections = macho_sections(executable)
    with executable.open('rb') as old:
        for module in modules:
            if not re.fullmatch('[A-Za-z0-9_]+', module): raise RebuildError('Invalid prepared module name')
            table = patched / f'{module}.fixups.tsv'
            if not table.exists() or table.read_text() != (app / 'dm' / table.name).read_text():
                raise RebuildError(f'Rewriter metadata differs for {module}; run a full build')
            header = dict(line[2:].split() for line in table.read_text().splitlines() if line.startswith('# '))
            text_end, patch = int(header['TEXT_END'], 16), int(header['PATCH_OFF'], 16)
            binary = patched / f'{module}.patched.bin'; size = binary.stat().st_size
            align = lambda value: (value + 0x3fff) & ~0x3fff
            start = align(text_end)
            split = patch if patch >= 0 else int(header['FILE_IMAGE_LEN'], 16)
            parts = [(0, start), (start, split), (split, align(size))]
            labels = [('__TEXT', f'__dm{module}x0'), (f'__DM{module}W', '__img'), ('__TEXT', f'__dm{module}x1')]
            with binary.open('rb') as new:
                for label, (lo, hi) in zip(labels, parts):
                    extent = sections.get(label)
                    if hi < lo or extent is None or extent[1] != hi-lo:
                        raise RebuildError(f'Embedded section differs for {module}; run a full build')
                    old.seek(extent[0]); new.seek(lo); pos = lo
                    while pos < hi:
                        n = min(2**20, hi-pos); available = max(0, min(n, size-pos))
                        wanted = new.read(available) + b'\0' * (n-available)
                        if old.read(n) != wanted:
                            raise RebuildError(f'Embedded code differs for {module}; run a full build')
                        pos += n
    return len(modules)


def require_chain(app, repo, title=None):
    match = re.search(r'#define DMSH_CHAIN "([0-9a-f]+)"', (repo / 'dm/gfx/nvn/dmsh_chain.h').read_text())
    if not match: raise RebuildError('No current shader-chain version')
    chain = match[1]; directory = app / 'msl' / ('b-' + chain)
    if not directory.is_dir() or not (directory / 'inputs.tsv').is_file() or not any(directory.glob('*_b.metallib')):
        raise RebuildError('Source IPA uses another shader chain; run a full build')
    if title:
        stamp = dict(line.split('\t', 1) for line in (directory / 'inputs.tsv').read_text().splitlines() if '\t' in line and not line.startswith('#'))
        if stamp.get('game') != title or not stamp.get('inputs'):
            raise RebuildError('Shader libraries belong to another game')
        lines = (app / 'dm/c1_full.tsv').read_text().splitlines(); first = lines[0] if lines else ''
        if not first.startswith(f'# chain b-{chain} inputs {stamp["inputs"]} '):
            raise RebuildError('Shader reflection stamp differs from the source libraries')
    return chain


def run(command, log, *, cwd=None, env=None, append=False):
    with log.open('a' if append else 'w') as output:
        result = subprocess.run(list(map(str, command)), cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode: raise RebuildError(f'{Path(str(command[0])).name} failed; see {log.name}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ipa', required=True, type=Path)
    parser.add_argument('--diagnostics', required=True, type=Path)
    parser.add_argument('--prepared', required=True, type=Path)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--title', required=True)
    parser.add_argument('--package', default='v0')
    args = parser.parse_args(argv)
    if not re.fullmatch('[0-9A-Fa-f]{16}', args.title) or not re.fullmatch('v[0-9]+', args.package):
        parser.error('Invalid title ID or package version')
    if sys.platform != 'darwin': parser.error('Host compilation requires a Mac runner')
    repo = Path(__file__).resolve().parents[2]; work = args.work.resolve(); title = args.title.upper()
    if work.exists(): raise RebuildError('Choose a new rebuild work directory')
    work.mkdir(parents=True); family = work / f'{title}_build'; family.mkdir()
    logs = family / 'logs'; logs.mkdir()
    print('Extracting the previous unsigned IPA; game resources are reused.', flush=True)
    app = unpack_ipa(args.ipa, work / 'source')
    info = plistlib.loads((app / 'Info.plist').read_bytes())
    executable_name = info.get('CFBundleExecutable', '')
    if not executable_name or Path(executable_name).name != executable_name:
        raise RebuildError('Invalid application executable name')
    chain = require_chain(app, repo, title)
    profile = family / 'profile' / title; profile.mkdir(parents=True)
    source_profile = args.diagnostics / f'{title}_build/profile/{title}/game.tsv'
    shutil.copy2(source_profile, profile / 'game.tsv')
    sys.path.insert(0, str(repo / 'dm/games')); import game
    g = game.Game(profile)
    if g.get('title_id').upper() != title or g.get('package') != args.package:
        raise RebuildError('Profile title/version differs from the requested source')
    patched = args.prepared / f'{title}_build' / f'patched_{args.package}'
    count = verify_modules(app, app / executable_name, patched, g.words('modules'))
    print(f'Verified all embedded sections of {count} modules and shader chain {chain}.', flush=True)
    project = work / 'project'; project.mkdir(); gen = project / 'Gen'; gen.mkdir()
    ios = repo / 'dm/ios'
    for name in ('Sources', 'tools'): (project / name).symlink_to(ios / name, target_is_directory=True)
    shutil.copy2(ios / 'project.yml', project / 'project.yml'); shutil.copytree(app / 'dm', gen / 'dm')
    (gen / 'Assets.xcassets').mkdir()
    (gen / 'Assets.xcassets/Contents.json').write_text('{"info":{"author":"xcode","version":1}}\n')
    env = dict(os.environ, GAME=title, DM_GAME=title, DM_GAMES_DIR=str(profile.parent), DM_GAMES_FIRST='1',
               DM_BUNDLE_ID=info['CFBundleIdentifier'], DM_DISPLAY_NAME=info.get('CFBundleDisplayName', g.get('name')),
               DM_APP_VERSION=info.get('CFBundleShortVersionString', '1.0'), DM_TEAM_ID='0000000000',
               DM_APP_CATEGORY=info.get('LSApplicationCategoryType', g.get('app_category')), DM_SCHEME_NAME='DmIOS',
               DM_LANG=info.get('DMLanguage', 'en-US'), DM_SHOW_FPS='YES' if str(info.get('DMShowFPS')).lower() in ('yes', 'true', '1') else 'NO')
    python = sys.executable
    run([python, ios / 'tools/gen_build.py', repo / 'dm/host', gen], gen / 'dm.xcconfig', env=env)
    flags = subprocess.check_output([python, ios / 'tools/gen_embed.py', patched, gen / 'embed.S', *g.words('modules')], env=env, text=True).strip()
    derived = work / 'derived'
    with (gen / 'dm.xcconfig').open('a') as config:
        config.write(f'OTHER_LDFLAGS = $(inherited) $(DM_FRAMEWORKS) {flags}\nDM_APPICON =\nDM_XCODE_OUTPUT_ROOT = {derived}\n')
    print('Compiling the updated iOS host without signing or shader compilation.', flush=True)
    run(['xcodegen', 'generate', '-q'], logs / 'xcodegen.log', cwd=project, env=env)
    run(['xcodebuild', '-project', 'DmIOS.xcodeproj', '-scheme', 'DmIOS', '-configuration', 'Release',
         '-destination', 'generic/platform=iOS', '-derivedDataPath', derived,
         'CODE_SIGNING_ALLOWED=NO', 'CODE_SIGNING_REQUIRED=NO', 'CODE_SIGN_IDENTITY=', 'DM_COPY_GAME_RESOURCES=NO',
         f'DM_XCODE_OUTPUT_ROOT={derived}', f'SYMROOT={derived}/Build/Products', f'OBJROOT={derived}/Build/Intermediates.noindex', 'build'],
        logs / 'xcodebuild.log', cwd=project, env=env)
    new_app = derived / 'Build/Products/Release-iphoneos/DmIOS.app'
    new_info = plistlib.loads((new_app / 'Info.plist').read_bytes())
    new_executable = new_app / new_info['CFBundleExecutable']
    verify_modules(app, new_executable, patched, g.words('modules'))
    shutil.copy2(new_executable, app / executable_name)
    print('Packing the updated executable with the original game data and shader libraries.', flush=True)
    run([python, ios / 'tools/pack_ipa.py', work / 'game.ipa', '--store', g.get('zip_store'), f'Payload/{app.name}={app}'],
        logs / 'pack_ipa.log', env=env)
    (logs / 'host_rebuild.json').write_text(json.dumps({'title':title, 'package':args.package, 'modules_verified':count,
        'chain':chain, 'game_resources_reused':True, 'signed':False, 'installed':False, 'device_tested':False}, indent=2)+'\n')
    print('Unsigned IPA repacked. Runtime behavior still requires device testing.', flush=True)
    return 0


if __name__ == '__main__':
    try: sys.exit(main())
    except (RebuildError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print('[RED] ' + str(error), file=sys.stderr); sys.exit(1)
