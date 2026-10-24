#!/usr/bin/env python3
"""Probe prepared rewritten modules without uploading a title's complete RomFS.

The encrypted bundle contains <title>_build/profile/<title>/ and patched_<version>/.
Asset access failures are expected when no RomFS was supplied. This is an SDK
startup diagnostic, not a graphics, audio, game compatibility or device test.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--title', required=True)
    parser.add_argument('--package', default='v0')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9A-Fa-f]{16}', args.title) or not re.fullmatch(r'v\d+', args.package):
        parser.error('title must be a 16-digit title ID and package must be v<version>')
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('nsp_build', repo / 'scripts/build_ipa_from_nsp.py')
    nsp = importlib.util.module_from_spec(spec); spec.loader.exec_module(nsp)
    sys.path.insert(0, str(repo / 'dm/games'))
    import game
    work = args.root.resolve() / f'{args.title.upper()}_build'
    profile = work / 'profile'
    os.environ.update(DM_GAME=args.title.upper(), DM_GAMES_DIR=str(profile), DM_GAMES_FIRST='1')
    g = game.load()
    logs = work / 'logs'; logs.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DM_ROOT=str(work), WORK=str(work / 'tmp'))
    env['DM_AUDREN_LEGACY_CAPTURE'] = str(logs / 'audren_legacy_capture.json')
    (work / 'tmp').mkdir(exist_ok=True)
    nsp.PROG.mode = 'lines'
    print('Prepared-module startup probe: RomFS assets may be absent; asset failures do not establish game compatibility.', flush=True)
    try:
        nsp.chain_header(sys.executable, repo, env, logs)
        nsp.probe_mac(repo, work, work / 'extract' / args.package,
                      work / f'patched_{args.package}', env, logs, g, 'en-US')
    except nsp.BuildError as error:
        print('[RED] ' + str(error), flush=True)
        return 1
    finally: nsp.PROG.close(ok=False)
    return 0


if __name__ == '__main__': sys.exit(main())
