#!/usr/bin/env python3
"""Stage, shard and assemble an offline NVN build using the canonical translator.

Every artifact containing game data must be encrypted by the calling workflow.
Shader keys are passed in Python's argv, avoiding the operating system's argument
length limit. Each shard retains its paired pixel shaders for vertex padding.
"""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import time

REPO = Path(__file__).resolve().parents[2]
HEXKEY = re.compile(r'[0-9a-f]{16}')


class BuildError(Exception):
    pass


def read_keys(profile):
    keys = set()
    for line in (profile / 'shader_map.tsv').read_text().splitlines():
        if line.startswith('#'): continue
        fields = line.split('\t')
        if len(fields) > 4 and fields[0].endswith('.bin'):
            if not HEXKEY.fullmatch(fields[3]): raise BuildError('Invalid shader-map key')
            keys.add(fields[3])
    if not keys: raise BuildError('No shader packages in the profile')
    return sorted(keys)


def partition(keys, pairs, count):
    """Disjoint primary keys; dependency closure stays in the same compile task."""
    if not 1 <= count <= 32: raise BuildError('Shard count must be between 1 and 32')
    known = set(keys); edges = defaultdict(set)
    for line in pairs.splitlines():
        if not line.strip() or line.startswith('#'): continue
        source, target = line.split()
        if source not in known or target not in known: raise BuildError('Shader pair references a missing key')
        edges[source].add(target)
    out = []
    for i in range(count):
        primary = sorted(keys)[i::count]; selected = set(primary); pending = list(primary)
        while pending:
            for target in edges[pending.pop()] - selected: selected.add(target); pending.append(target)
        out.append({'index': i, 'primary': primary, 'keys': sorted(selected)})
    return out


def digest(path):
    with path.open('rb') as source: return hashlib.file_digest(source, 'sha256').hexdigest()


def load_plan(root):
    plan = json.loads((root / 'plan.json').read_text())
    if (plan.get('format') != 2 or not re.fullmatch('[0-9A-F]{16}', plan.get('title', '')) or
            not re.fullmatch('v[0-9]+', plan.get('package', '')) or not isinstance(plan.get('toolchain'), dict) or
            not plan['toolchain']): raise BuildError('Invalid prepared build plan; prepare new inputs with compiler fingerprints')
    return plan


def toolchain_context(plan=None):
    sys.path.insert(0, str(REPO / 'dm/gfx/tools'))
    import dmsh_chain, own_compilers
    errors = own_compilers.check_tools()
    if errors: raise BuildError('Unsupported shader toolchain: ' + '; '.join(errors))
    actual = dmsh_chain.tools_now()
    if plan is not None and actual != plan['toolchain']:
        raise BuildError('Shard compilers differ from preparation; use the same compiler versions for every shard')
    return actual


def chain_context(root, plan, check_tools=False):
    profile = root / 'profile' / plan['title']
    os.environ.update(DM_GAME=plan['title'], DM_GAMES_DIR=str(profile.parent), DM_GAMES_FIRST='1')
    sys.path[:0] = [str(REPO / 'dm/gfx/tools'), str(REPO / 'dm/games')]
    import dmsh_chain, game
    g = game.Game(profile)
    if check_tools: toolchain_context(plan)
    if dmsh_chain.version() != plan['chain'] or dmsh_chain.inputs(g) != plan['inputs']:
        raise BuildError('Prepared shader chain/profile changed; prepare a new build')
    if read_keys(profile) != plan['keys']: raise BuildError('Prepared shader-map coverage changed')
    return dmsh_chain, g


def archive(output, files):
    with tarfile.open(output, 'w:gz', compresslevel=1) as target:
        for source, name in files: target.add(source, arcname=name, recursive=True)


def prepare(args):
    stages = list(args.game_dir.glob('*_build/ci_stage.json'))
    if len(stages) != 1: raise BuildError('Expected exactly one staged title')
    stage = json.loads(stages[0].read_text())
    title = stage['title']; profile = Path(stage['profile'])
    if not re.fullmatch('[0-9A-F]{16}', title): raise BuildError('Invalid staged title')
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    copied = output / 'profile' / title; copied.mkdir(parents=True)
    for source in profile.iterdir():
        if source.is_file() and (source.suffix == '.tsv' or source.name == 'patches.py'):
            shutil.copy2(source, copied / source.name)
    sys.path[:0] = [str(REPO / 'dm/gfx/tools'), str(REPO / 'dm/games')]
    import dmsh_chain, game
    g = game.Game(copied)
    keys = read_keys(copied); shards = partition(keys, (copied / 'shader_pairs.tsv').read_text(), args.shards)
    plan = {**{k: stage[k] for k in ('title', 'package', 'chain', 'app_name', 'app_version', 'bundle_id', 'language', 'show_fps', 'provisional')},
            'format': 2, 'toolchain': toolchain_context(), 'inputs': dmsh_chain.inputs(g), 'keys': keys, 'shards': shards, 'module_files': {}}
    modules = Path(stage['patched'])
    if (modules / 'nro.tsv').exists(): raise BuildError('This sharded workflow does not yet support embedded NRO modules')
    module_files = [p for p in sorted(modules.iterdir()) if p.is_file() and (p.name.endswith('.patched.bin') or p.suffix == '.tsv')]
    for source in module_files: plan['module_files'][source.name] = digest(source)
    (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    common = [(output / 'plan.json', 'plan.json'), (output / 'profile', 'profile')]
    print(f'Preparing {len(keys):,} shader keys in {args.shards} shards.', flush=True)
    packages = Path(stage['shader_packages'])
    archive(output / 'compile-inputs.tar.gz', common + [(packages, 'pkg')])
    extraction = Path(stage['extraction'])
    assets = common + [(p, 'modules/' + p.name) for p in module_files] + [(extraction / 'RomFS', 'romfs')]
    assets += [(extraction / n, n) for n in ('control.nacp', 'icon.jpg') if (extraction / n).exists()]
    if stage.get('aoc'): assets.append((Path(stage['aoc']), 'aoc'))
    print('Preparing the runtime assets; keys and source XCI are excluded.', flush=True)
    archive(output / 'runtime-assets.tar.gz', assets)
    (output / 'matrix.json').write_text(json.dumps({'index': list(range(args.shards))}) + '\n')
    print('Prepared archives must be encrypted before upload.', flush=True)


def shader_outputs(key, record, platform):
    """All runtime libraries and the sources used to build the guard tables."""
    names = [key + '_b.json']
    # Geometry slice stubs deliberately carry no standalone library.
    if not record.get('gs_slice'):
        names.append(key + '_b.metallib')
        if platform == 'mac': names.append(key + '_b.metal')
    if record.get('vin'): names.append(key + '_bv.metallib')
    if record.get('fetch'): names.append(key + '_bf.metallib')
    if record.get('geometry'):
        for suffix in [f'_bg{i}' for i in range(5)] + ['_bgv']:
            names += [key + suffix + ext for ext in ('.json', '.metallib', '.metal') if platform == 'mac' or ext != '.metal']
    return names


def validate_shard(root, plan, index):
    shard = plan['shards'][index]; chain = plan['chain']; paths = {}
    results = json.loads((root / 'mac' / ('b-' + chain) / '_b_build.json').read_text())
    if len(results) != len(shard['keys']) or {r['key'] for r in results} != set(shard['keys']) or any(r['st'] != 'ok' for r in results):
        failures = [r for r in results if r['st'] != 'ok']
        raise BuildError(f'Shard {index}: incomplete compilation ({len(failures)} failures); see its encrypted log')
    for key in shard['keys']:
        for platform in ('mac', 'ios'):
            directory = root / platform / ('b-' + chain)
            reflection = directory / (key + '_b.json'); record = json.loads(reflection.read_text())
            if record.get('chain') != chain or record.get('dm_ctl') != key or record.get('route') != 'B':
                raise BuildError('Shader reflection identity mismatch')
            for name in shader_outputs(key, record, platform):
                path = directory / name
                if not path.is_file() or not path.stat().st_size: raise BuildError(f'Missing compiled output {name}')
                paths[f'{platform}/b-{chain}/{name}'] = digest(path)
    return {'format': 2, 'index': index, 'chain': chain, 'inputs': plan['inputs'], 'toolchain': plan['toolchain'],
            'keys': shard['keys'], 'primary': shard['primary'], 'files': paths, 'complete': True}


def compile_shard(args):
    root = args.root.resolve(); plan = load_plan(root); chain_context(root, plan, check_tools=True)
    if not 0 <= args.index < len(plan['shards']): raise BuildError('Invalid shard index')
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=True)
    temporary = out / 'tmp'; temporary.mkdir(exist_ok=True); log = out / 'compile.log'
    keys = plan['shards'][args.index]['keys']
    if not keys: raise BuildError('Empty shader shard')
    print(f'Shard {args.index}: compiling {len(keys):,} keys, including paired shaders.', flush=True)
    env = dict(os.environ, DM_ROOT=str(out), WORK=str(temporary), IOSOUT=str(out / 'ios'), J=str(args.workers))
    # A Python-level argv is not constrained by macOS ARG_MAX.
    command = [sys.executable, __file__, 'invoke', '--root', str(root), '--output', str(out), '--index', str(args.index)]
    with log.open('w') as output:
        process = subprocess.Popen(command, env=env, stdout=output, stderr=subprocess.STDOUT)
        started = time.monotonic()
        while process.poll() is None:
            time.sleep(25)
            count = sum(1 for _ in (out / 'ios' / ('b-' + plan['chain'])).glob('*_b.json'))
            print(f'Shard {args.index}: {count:,}/{len(keys):,} reflected shaders; elapsed {int(time.monotonic()-started)} s.', flush=True)
    if process.returncode: raise BuildError(f'Shard {args.index}: translator process failed; see encrypted compile.log')
    toolchain_context(plan)   # compilers must not change during the build either
    manifest = validate_shard(out, plan, args.index)
    (out / 'shard.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Shard {args.index}: verified {len(keys):,} shaders for macOS and iOS.', flush=True)


def invoke(args):
    plan = load_plan(args.root); chain_context(args.root, plan)
    script = REPO / 'dm/gfx/tools/own_compilers.py'
    sys.argv = [str(script), str(args.root / 'pkg'), str(args.output / 'mac'), *plan['shards'][args.index]['keys']]
    runpy.run_path(str(script), run_name='__main__')


def merge_shards(assets, shards, output):
    plan = load_plan(assets); count = len(plan['shards']); found = {}; ownership = {}; collected = {}
    for root in shards:
        manifest = json.loads((root / 'shard.json').read_text()); i = manifest.get('index')
        if i in found or not isinstance(i, int) or not 0 <= i < count: raise BuildError('Invalid or repeated shard')
        if manifest.get('format') != 2 or not manifest.get('complete') or any(manifest.get(k) != plan[k] for k in ('chain', 'inputs', 'toolchain')):
            raise BuildError('Incomplete shard or mismatched shader chain/profile')
        expected = plan['shards'][i]
        if manifest.get('keys') != expected['keys'] or manifest.get('primary') != expected['primary']:
            raise BuildError('Shard coverage differs from the prepared plan')
        for key in expected['primary']:
            if key in ownership: raise BuildError('Duplicate primary shader key')
            ownership[key] = i
        for relative, checksum in manifest['files'].items():
            path = Path(relative)
            if path.is_absolute() or '..' in path.parts or '\\' in relative: raise BuildError('Invalid shader output path')
            source = root / path
            if not source.is_file() or digest(source) != checksum: raise BuildError('Compiled shader checksum mismatch')
            if relative in collected and collected[relative] != checksum: raise BuildError('Paired shader outputs differ between shards')
            if relative not in collected:
                target = output / path; target.parent.mkdir(parents=True, exist_ok=True)
                # Compiled outputs are immutable. Sharing their storage avoids a
                # second complete shader set on the temporary runner volume.
                try: os.link(source, target)
                except OSError: shutil.copy2(source, target)
                collected[relative] = checksum
        found[i] = manifest
        print(f'Verified and merged shard {i + 1}/{count}: {len(expected["primary"]):,} primary keys.', flush=True)
    if set(found) != set(range(count)) or set(ownership) != set(plan['keys']): raise BuildError('Missing shader shards or keys')
    # Re-check reflection dependencies even when an incomplete manifest hashes correctly.
    for key in plan['keys']:
        for platform in ('mac', 'ios'):
            directory = output / platform / ('b-' + plan['chain'])
            record = json.loads((directory / (key + '_b.json')).read_text())
            if record.get('chain') != plan['chain'] or record.get('dm_ctl') != key or record.get('route') != 'B':
                raise BuildError('Merged reflection identity mismatch')
            for name in shader_outputs(key, record, platform):
                path = directory / name
                relative = f'{platform}/b-{plan["chain"]}/{name}'
                if relative not in collected or not path.is_file() or not path.stat().st_size:
                    raise BuildError(f'Merged shader output is missing or empty: {relative}')
    return plan


def assemble(args):
    assets = args.root.resolve(); out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    shards = [p.parent for p in args.shards.rglob('shard.json')]
    plan = merge_shards(assets, shards, out); chain, g = chain_context(assets, plan)
    for name, checksum in plan['module_files'].items():
        if Path(name).name != name or digest(assets / 'modules' / name) != checksum: raise BuildError('Rewritten module identity changed')
    for platform in ('mac', 'ios'): chain.write_stamp(out / platform / ('b-' + plan['chain']), g.id, plan['inputs'])
    logs = out / 'logs'; logs.mkdir()
    print(f'All {len(plan["keys"]):,} shaders verified across {len(plan["shards"])} shards.', flush=True)
    env = dict(os.environ, DM_ROOT=str(out), WORK=str(out / 'tmp'), GAME=g.id, W=str(out / 'ipa'),
               PATCHED=str(assets / 'modules'), ROMFS=str(assets / 'romfs'), MSL_IOS=str(out / 'ios'), GFXAPI='nvn',
               AOC=str(assets / 'aoc') if (assets / 'aoc').exists() else '', DM_DISPLAY_NAME=plan['app_name'],
               DM_APP_VERSION=plan['app_version'], DM_BUNDLE_ID=plan['bundle_id'], DM_LANG=plan['language'],
               DM_SHOW_FPS='1' if plan['show_fps'] else '0', FONT_PY=sys.executable)
    (out / 'tmp').mkdir()
    if (assets / 'icon.jpg').exists(): env['DM_ICON'] = str(assets / 'icon.jpg')
    for tool, table in (('c1_guard.py', 'c1_full.tsv'), ('cb_extent.py', 'cb_extent.tsv')):
        with (logs / (tool + '.log')).open('w') as log:
            subprocess.run([sys.executable, REPO / 'dm/gfx/tools' / tool, out / 'mac' / ('b-' + plan['chain']), g.table(table)], env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    # settings.py needs the game's control data for its language menu.
    (out / 'ipa/data/v0').mkdir(parents=True)
    if (assets / 'control.nacp').exists(): shutil.copy2(assets / 'control.nacp', out / 'ipa/data/v0/control.nacp')
    try:
        with (logs / 'build_ipa.log').open('w') as log:
            subprocess.run(['bash', REPO / 'dm/ios/tools/build_ipa.sh', out / 'game.ipa'], env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    finally:
        xcode_log = out / 'ipa/build/ios/pkg/build.log'
        if xcode_log.is_file(): shutil.copy2(xcode_log, logs / 'xcodebuild.log')
    if not (out / 'game.ipa').is_file(): raise BuildError('IPA was not produced')
    print('Unsigned IPA assembled. Device behavior remains untested.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__); commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare'); prep.add_argument('--game-dir', type=Path, required=True); prep.add_argument('--shards', type=int, default=8); prep.add_argument('--output', type=Path, required=True)
    for name in ('compile', 'invoke'):
        p = commands.add_parser(name); p.add_argument('--root', type=Path, required=True); p.add_argument('--index', type=int, required=True); p.add_argument('--output', type=Path, required=True); p.add_argument('--workers', type=int, default=3)
    assembly = commands.add_parser('assemble'); assembly.add_argument('--root', type=Path, required=True); assembly.add_argument('--shards', type=Path, required=True); assembly.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    {'prepare': prepare, 'compile': compile_shard, 'invoke': invoke, 'assemble': assemble}[args.command](args)


if __name__ == '__main__':
    try: main()
    except (BuildError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print('[RED] ' + str(error), file=sys.stderr); sys.exit(1)
