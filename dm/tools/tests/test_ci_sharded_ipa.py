"""Synthetic end-to-end shard coverage and staged-build boundaries, without game data."""
from contextlib import ExitStack, redirect_stdout
import errno
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('ci_sharded_ipa', REPO / 'dm/tools/ci_sharded_ipa.py')
ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)


class ShardPlanTests(unittest.TestCase):
    def test_primary_coverage_is_exact_and_dependencies_stay_with_vertex(self):
        keys = [f'{i:016x}' for i in range(10)]
        pairs = f'{keys[0]} {keys[5]}\n{keys[5]} {keys[8]}\n'
        parts = ci.partition(keys, pairs, 3)
        self.assertEqual(sorted(k for p in parts for k in p['primary']), keys)
        self.assertEqual(sum(len(p['primary']) for p in parts), len(keys))
        self.assertTrue({keys[0], keys[5], keys[8]} <= set(parts[0]['keys']))
        self.assertEqual(parts, ci.partition(list(reversed(keys)), pairs, 3))

    def test_unknown_pair_or_invalid_count_cannot_silently_drop_a_shader(self):
        with self.assertRaises(ci.BuildError): ci.partition(['0'*16], '0'*16+' '+'1'*16, 1)
        for count in (0, 33):
            with self.assertRaises(ci.BuildError): ci.partition(['0'*16], '', count)

    def test_compiler_fingerprints_must_match_preparation(self):
        actual = {'spirv-cross': 'installed', 'metal_macosx': 'compiler-A'}
        modules = {'dmsh_chain': SimpleNamespace(tools_now=lambda: actual),
                   'own_compilers': SimpleNamespace(check_tools=lambda: [])}
        with patch.dict(sys.modules, modules):
            self.assertEqual(ci.toolchain_context(), actual)
            self.assertEqual(ci.toolchain_context({'toolchain': actual}), actual)
            with self.assertRaisesRegex(ci.BuildError, 'compilers differ'):
                ci.toolchain_context({'toolchain': {**actual, 'metal_macosx': 'compiler-B'}})
        modules['own_compilers'] = SimpleNamespace(check_tools=lambda: ['translator changed'])
        with patch.dict(sys.modules, modules), self.assertRaisesRegex(ci.BuildError, 'Unsupported shader toolchain'):
            ci.toolchain_context()


def shader_fixture(root):
    keys = ['0'*16, '1'*16, '2'*16]; parts = ci.partition(keys, '', 2)
    assets = root / 'assets'; assets.mkdir()
    plan = {'format': 2, 'title': '0100000000000000', 'package': 'v0', 'chain': 'abcd1234',
            'inputs': 'fixture', 'toolchain': {'compiler': 'fixture'}, 'keys': keys, 'shards': parts}
    (assets / 'plan.json').write_text(json.dumps(plan))
    shards = []
    for part in parts:
        source = root / str(part['index']); shards.append(source); files = {}
        for platform in ('mac', 'ios'):
            folder = source / platform / 'b-abcd1234'; folder.mkdir(parents=True)
            for key in part['keys']:
                record = {'chain': plan['chain'], 'dm_ctl': key, 'route': 'B', 'stage': 'vs'}
                for suffix, data in [('_b.json', json.dumps(record).encode()), ('_b.metallib', b'library'), *([('_b.metal', b'source')] if platform == 'mac' else [])]:
                    name = key + suffix; (folder / name).write_bytes(data)
                    files[f'{platform}/b-abcd1234/{name}'] = ci.digest(folder / name)
        manifest = {**part, 'format': 2, 'chain': plan['chain'], 'inputs': plan['inputs'], 'toolchain': plan['toolchain'], 'files': files, 'complete': True}
        (source / 'shard.json').write_text(json.dumps(manifest))
        results = [{'key': k, 'st': 'ok'} for k in part['keys']]
        (source / 'mac/b-abcd1234/_b_build.json').write_text(json.dumps(results))
    return assets, plan, shards


class MergeTests(unittest.TestCase):
    def test_all_outputs_match_the_plan_and_compile_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); assets, plan, shards = shader_fixture(root)
            for i, shard in enumerate(shards):
                manifest = ci.validate_shard(shard, plan, i)
                self.assertEqual(manifest['primary'], plan['shards'][i]['primary'])
            self.assertEqual(ci.merge_shards(assets, shards, root / 'out'), plan)
            self.assertEqual(len(list((root / 'out/ios/b-abcd1234').glob('*_b.metallib'))), 3)

    def test_merged_libraries_share_storage_and_survive_removing_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); assets, plan, shards = shader_fixture(root)
            ci.merge_shards(assets, shards, root / 'out')
            relative = f'ios/b-abcd1234/{plan["keys"][0]}_b.metallib'
            source, target = shards[0] / relative, root / 'out' / relative
            self.assertEqual(source.stat().st_ino, target.stat().st_ino)
            for shard in shards: shutil.rmtree(shard)
            self.assertEqual(target.read_bytes(), b'library')

    def test_merge_copies_when_storage_cannot_be_shared(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); assets, plan, shards = shader_fixture(root)
            with patch.object(ci.os, 'link', side_effect=OSError(errno.EXDEV, 'Different volume')):
                ci.merge_shards(assets, shards, root / 'out')
            for shard in shards: shutil.rmtree(shard)
            for key in plan['keys']:
                self.assertEqual((root / f'out/ios/b-abcd1234/{key}_b.metallib').read_bytes(), b'library')

    def test_missing_shard_bad_hash_and_stale_inputs_are_rejected(self):
        for failure in ('missing', 'checksum', 'inputs', 'compiler', 'coverage', 'incomplete', 'duplicate'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); assets, plan, shards = shader_fixture(root)
                manifest = json.loads((shards[0] / 'shard.json').read_text())
                if failure == 'missing': shards.pop()
                elif failure == 'checksum': next((shards[0] / 'ios').rglob('*.metallib')).write_bytes(b'changed')
                elif failure == 'inputs': manifest['inputs'] = 'changed'
                elif failure == 'compiler': manifest['toolchain'] = {'compiler': 'different'}
                elif failure == 'coverage': manifest['keys'].pop()
                elif failure == 'incomplete': manifest['complete'] = False
                elif failure == 'duplicate': shards.append(shards[0])
                (shards[0] / 'shard.json').write_text(json.dumps(manifest))
                with self.assertRaises(ci.BuildError): ci.merge_shards(assets, shards, root / 'out')

    def test_a_manifest_cannot_omit_guard_sources_or_reflection_dependencies(self):
        for platform, suffix in (('ios', '_b.metallib'), ('mac', '_b.metal'),
                                 ('ios', '_bv.metallib'), ('ios', '_bf.metallib'),
                                 ('ios', '_bg4.metallib'), ('mac', '_bgv.metal')):
            with self.subTest(platform=platform, suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); assets, plan, shards = shader_fixture(root)
                path = shards[0] / 'shard.json'; manifest = json.loads(path.read_text())
                key = plan['shards'][0]['keys'][0]
                for target in ('mac', 'ios'):
                    folder = shards[0] / target / 'b-abcd1234'
                    record = {'chain': plan['chain'], 'dm_ctl': key, 'route': 'B',
                              'vin': True, 'fetch': True, 'geometry': {'vertices': 4}}
                    for name in ci.shader_outputs(key, record, target):
                        file = folder / name
                        file.write_bytes(json.dumps(record).encode() if name.endswith('.json') else b'compiled')
                        manifest['files'][f'{target}/b-abcd1234/{name}'] = ci.digest(file)
                ci.validate_shard(shards[0], plan, 0)
                omitted = f'{platform}/b-abcd1234/{key}{suffix}'
                del manifest['files'][omitted]; path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ci.BuildError, 'shader output is missing'):
                    ci.merge_shards(assets, shards, root / 'out')

    def test_geometry_libraries_survive_merge_and_slice_stubs_need_no_library(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); assets, plan, shards = shader_fixture(root)
            path = shards[0] / 'shard.json'; manifest = json.loads(path.read_text())
            geometry, stub = plan['shards'][0]['keys']
            for platform in ('mac', 'ios'):
                folder = shards[0] / platform / 'b-abcd1234'
                for key, extra in ((geometry, {'geometry': {'vertices': 4}}), (stub, {'gs_slice': {'layer': 1}})):
                    record = {'chain': plan['chain'], 'dm_ctl': key, 'route': 'B', **extra}
                    for name in ci.shader_outputs(key, record, platform):
                        file = folder / name
                        file.write_bytes(json.dumps(record).encode() if name.endswith('.json') else b'compiled')
                    if key == stub:
                        (folder / (key + '_b.metallib')).unlink()
                        if platform == 'mac': (folder / (key + '_b.metal')).unlink()
            manifest = ci.validate_shard(shards[0], plan, 0)
            path.write_text(json.dumps(manifest))
            ci.merge_shards(assets, shards, root / 'out')
            self.assertTrue((root / f'out/ios/b-abcd1234/{geometry}_bg4.metallib').is_file())
            self.assertTrue((root / f'out/mac/b-abcd1234/{geometry}_bgv.metal').is_file())
            self.assertFalse((root / f'out/ios/b-abcd1234/{stub}_b.metallib').exists())

    def test_compiler_exit_success_does_not_hide_failed_shader_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); assets, plan, shards = shader_fixture(root)
            path = shards[0] / 'mac/b-abcd1234/_b_build.json'; results = json.loads(path.read_text())
            results[0]['st'] = 'metal failed'; path.write_text(json.dumps(results))
            with self.assertRaisesRegex(ci.BuildError, 'incomplete compilation'):
                ci.validate_shard(shards[0], plan, 0)


class StageOnlyTests(unittest.TestCase):
    def test_staging_writes_manifest_and_never_calls_shader_compiler_or_ipa_builder(self):
        sys.path[:0] = [str(REPO / 'scripts'), str(REPO / 'dm/games'), str(REPO / 'dm/tools')]
        import build_ipa_from_nsp as nsp, game, nacp, profile_guess
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary); (root / 'prod.keys').touch(); base = root / 'base.nsp'; base.touch()
            registered = game.load('010015100B514000'); version = 'v0'
            profile = SimpleNamespace(id=registered.id, dir=root / 'profile' / registered.id,
                table=lambda name: root / 'profile' / registered.id / name,
                get=lambda name: version if name == 'package' else registered.get(name),
                words=lambda name: [version] if name == 'shader_versions' else ['main'] if name == 'modules' else registered.words(name),
                builds=lambda: {version: {'display': '1.0.0'}})
            extraction = root / 'extract'; extraction.mkdir()
            sources = {key: (False, '') for key in ('glslc_capture', 'vk_capture', 'gl_capture', 'ue_decode', 'oodle_packs', 'ue_legacy')}
            sources['graphics'] = ('nvn', 'fixture')
            mocks = {}
            for name, value in {'scan_nsps': (int(profile.id, 16), {'base': base, 'updates': [], 'dlc': []}),
                    'xci_updates': None, 'check_prereqs': None, 'free_gb': 10000,
                    'build_nx_extract': root / 'nx_extract', 'extract': extraction, 'can_clone': False,
                    'build_mismatch': [], 'extract_aoc': None, 'rewrite': None, 'chain_header': None,
                    'chain_of': 'abcd1234', 'split_shaders': 'fixture-key', 'build_shaders': root / 'shaders', 'run': None}.items():
                mocks[name] = stack.enter_context(patch.object(nsp, name, return_value=value))
            stack.enter_context(patch.object(game, 'by_title', return_value=profile))
            stack.enter_context(patch.object(nacp, 'name', return_value='Fixture'))
            stack.enter_context(patch.object(nacp, 'version', return_value='1.0.0'))
            stack.enter_context(patch.object(nacp, 'languages', return_value=['en-US']))
            stack.enter_context(patch.object(profile_guess, 'build_sources', return_value=sources))
            stack.enter_context(patch.object(nsp, 'PROG', nsp.Progress('off')))
            with redirect_stdout(io.StringIO()):
                code = nsp.cli([str(root), '-k', str(root / 'prod.keys'), '--stage-only', '--package', version, '--progress', 'off'])
            self.assertEqual(code, 0)
            manifest = json.loads((root / f'{profile.id}_build/ci_stage.json').read_text())
            self.assertEqual(manifest['title'], profile.id); self.assertEqual(manifest['chain'], 'abcd1234')
            mocks['split_shaders'].assert_called_once(); mocks['rewrite'].assert_called_once()
            mocks['build_shaders'].assert_not_called(); mocks['run'].assert_not_called()


if __name__ == '__main__': unittest.main()
