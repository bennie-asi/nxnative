"""Extractor selection and embedded-XCI updates, using synthetic metadata only."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('build_ipa_from_nsp', REPO / 'scripts/build_ipa_from_nsp.py')
nsp = importlib.util.module_from_spec(spec); spec.loader.exec_module(nsp)
spec_game = importlib.util.spec_from_file_location('game_profiles', REPO / 'dm/games/game.py')
game = importlib.util.module_from_spec(spec_game); spec_game.loader.exec_module(game)
TITLE = 0x0100643002136000


class EmbeddedUpdates(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.xci = self.root / 'Combined [0100643002136000][v65536].xci'
        self.xci.write_bytes(b'')
        self.family = {'base': self.xci, 'updates': [], 'dlc': []}
        self.records = [{'title_id': f'{TITLE:016X}', 'type': 'Application', 'version': 0},
                        {'title_id': f'{TITLE + 0x800:016X}', 'type': 'Patch', 'version': 65536}]
        for cm in (mock.patch.object(nsp, 'PROG', mock.Mock()), mock.patch.object(nsp, 'say'),
                   mock.patch.object(nsp.shutil, 'which', return_value='dotnet'),
                   mock.patch.object(nsp, 'build_nx_extract', return_value=self.root / 'nx_extract'),
                   mock.patch.object(nsp, 'run', self.catalog)):
            cm.__enter__(); self.addCleanup(cm.__exit__, None, None, None)

    def catalog(self, command, log, **kwargs):
        self.assertEqual(command[1], '--inspect')
        Path(log).write_text(json.dumps(self.records), encoding='utf-8')

    def inspect(self):
        nsp.xci_updates(REPO, self.root, TITLE, self.family, self.root / 'prod.keys', {'PATH': ''})

    def test_embedded_patch_is_selected_from_same_container(self):
        self.inspect()
        self.assertEqual(self.family['updates'], [(65536, self.xci)])
        self.assertEqual(nsp.nsp_versions(self.family, {}), {'v0': None, 'v65536': self.xci})

    def test_filename_version_does_not_invent_patch(self):
        self.records = self.records[:1]
        self.inspect()
        self.assertEqual(self.family['updates'], [])

    def test_explicit_update_takes_precedence_at_same_version(self):
        update = self.root / 'update.nsp'
        self.family['updates'] = [(65536, update)]
        self.inspect()
        self.assertEqual(self.family['updates'], [(65536, update)])

    def test_different_embedded_game_is_rejected(self):
        self.records[0]['title_id'] = '010015100B514000'
        with self.assertRaisesRegex(nsp.BuildError, 'does not describe just game'):
            self.inspect()

    def test_nsp_only_needs_no_extra_extractor_build(self):
        self.xci.unlink()
        self.inspect()
        nsp.build_nx_extract.assert_not_called()


class ExtractorBuild(unittest.TestCase):
    def test_executable_suffix_and_preserve_supporting_files(self):
        for platform, executable in [('win32', 'nx_extract.exe'), ('darwin', 'nx_extract')]:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                def build(command, log, **kwargs):
                    out = Path(command[command.index('-o') + 1]); out.mkdir(parents=True)
                    (out / executable).write_bytes(b'executable')
                    (out / 'LibHac.dll').write_bytes(b'dependency')
                with mock.patch.object(nsp.sys, 'platform', platform), mock.patch.object(nsp, 'run', build):
                    result = nsp.build_nx_extract(REPO, root, root / 'logs', {})
                self.assertEqual(result.name, executable)
                self.assertTrue(result.is_file())
                self.assertEqual((result.parent / 'LibHac.dll').read_bytes(), b'dependency')
                self.assertFalse((result.parent / 'obj').exists())


class ProfilePaths(unittest.TestCase):
    def test_windows_drive_letters_survive_search_path(self):
        folders = [r'E:\games\profiles', r'C:\extra\profiles']
        with mock.patch.object(game.os, 'pathsep', ';'), mock.patch.dict(game.os.environ, {'DM_GAMES_DIR': ';'.join(folders)}):
            self.assertEqual(game.extra_dirs(), [Path(p) for p in folders])

    def test_mac_colon_separated_folders(self):
        folders = ['/Volumes/Games/profiles', '/tmp/profiles']
        with mock.patch.object(game.os, 'pathsep', ':'), mock.patch.dict(game.os.environ, {'DM_GAMES_DIR': ':'.join(folders)}):
            self.assertEqual(game.extra_dirs(), [Path(p) for p in folders])


if __name__ == '__main__':
    unittest.main()
