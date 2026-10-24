"""Check resource extraction and embedded-code identity without any game files."""
import importlib.util
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'ci_rebuild_ipa.py'
spec = importlib.util.spec_from_file_location('ci_rebuild_ipa', SCRIPT)
ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)


def fixture(root, far=False):
    app = root / 'old.app'; patched = root / 'patched'; (app / 'dm').mkdir(parents=True); patched.mkdir()
    table = '# TEXT_END\t0x2000\n# PATCH_OFF\t' + ('-0x10000' if far else '0x8000') + '\n# FILE_IMAGE_LEN\t0x8000\n'
    for directory in (app / 'dm', patched): (directory / 'main.fixups.tsv').write_text(table)
    binary = b'a' * 0x8000 + b'patchcode'
    (patched / 'main.patched.bin').write_bytes(binary)
    parts = [binary[:0x4000], binary[0x4000:0x8000], binary[0x8000:] + b'\0' * (0x4000-9)]
    labels = [('__TEXT', '__dmmainx0'), ('__DMmainW', '__img'), ('__TEXT', '__dmmainx1')]
    commands = b''; position = 0x1000
    for (segment, section), data in zip(labels, parts):
        commands += struct.pack('<II16sQQQQiiII', 0x19, 152, segment.encode(), 0, len(data), position, len(data), 7, 5, 1, 0)
        commands += struct.pack('<16s16sQQIIIIIIII', section.encode(), segment.encode(), 0, len(data), position, 14, 0, 0, 0, 0, 0, 0)
        position += len(data)
    header = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 3, len(commands), 0, 0)
    executable = app / 'Game'; executable.write_bytes((header+commands).ljust(0x1000, b'\0') + b''.join(parts))
    return app, patched, executable


class EmbeddedCodeTests(unittest.TestCase):
    def test_near_and_far_module_pieces(self):
        for far in (False, True):
            with self.subTest(far=far), tempfile.TemporaryDirectory() as temporary:
                app, patched, executable = fixture(Path(temporary), far)
                self.assertEqual(ci.verify_modules(app, executable, patched, ['main']), 1)
                with executable.open('r+b') as target: target.seek(0x9001); target.write(b'changed')
                with self.assertRaisesRegex(ci.RebuildError, 'Embedded code differs'):
                    ci.verify_modules(app, executable, patched, ['main'])

    def test_rewriter_metadata_and_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, patched, executable = fixture(Path(temporary))
            with (patched / 'main.fixups.tsv').open('a') as target: target.write('# OTHER\t0x1\n')
            with self.assertRaisesRegex(ci.RebuildError, 'Rewriter metadata differs'):
                ci.verify_modules(app, executable, patched, ['main'])
            executable.write_bytes(executable.read_bytes()[:40])
            with self.assertRaisesRegex(ci.RebuildError, 'Truncated Mach-O'):
                ci.macho_sections(executable)

    def test_unverified_nro_code_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, patched, executable = fixture(Path(temporary))
            (patched / 'nro.tsv').write_text('synthetic\n')
            with self.assertRaisesRegex(ci.RebuildError, 'NRO modules require a full build'):
                ci.verify_modules(app, executable, patched, ['main'])

    def test_chain_title_and_reflection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); app = root / 'app'; repo = root / 'repo'
            (repo / 'dm/gfx/nvn').mkdir(parents=True)
            (repo / 'dm/gfx/nvn/dmsh_chain.h').write_text('#define DMSH_CHAIN "abcd1234"\n')
            (app / 'msl/b-abcd1234').mkdir(parents=True); (app / 'dm').mkdir()
            (app / 'msl/b-abcd1234/inputs.tsv').write_text('game\t0100000000000000\ninputs\tdigest\n')
            (app / 'msl/b-abcd1234/sample_b.metallib').write_bytes(b'synthetic')
            (app / 'dm/c1_full.tsv').write_text('# chain b-abcd1234 inputs digest fixture\n')
            self.assertEqual(ci.require_chain(app, repo, '0100000000000000'), 'abcd1234')
            with self.assertRaisesRegex(ci.RebuildError, 'another game'):
                ci.require_chain(app, repo, '0100000000000001')
            (app / 'dm/c1_full.tsv').write_text('# chain b-abcd1234 inputs other fixture\n')
            with self.assertRaisesRegex(ci.RebuildError, 'reflection stamp differs'):
                ci.require_chain(app, repo, '0100000000000000')
            (repo / 'dm/gfx/nvn/dmsh_chain.h').write_text('#define DMSH_CHAIN "1234abcd"\n')
            with self.assertRaisesRegex(ci.RebuildError, 'another shader chain'):
                ci.require_chain(app, repo)


class SourceArchiveTests(unittest.TestCase):
    def test_extracts_one_application_and_preserves_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); archive = root / 'source.ipa'
            with zipfile.ZipFile(archive, 'w') as target:
                entry = zipfile.ZipInfo('Payload/Game.app/Game'); entry.external_attr = (stat.S_IFREG | 0o755) << 16
                target.writestr(entry, b'executable'); target.writestr('Payload/Game.app/romfs/marker', b'asset')
            app = ci.unpack_ipa(archive, root / 'unpacked')
            self.assertEqual((app / 'romfs/marker').read_bytes(), b'asset')
            if os.name != 'nt': self.assertEqual((app / 'Game').stat().st_mode & 0o111, 0o111)

    def test_rejects_paths_links_multiple_apps_and_signed_sources(self):
        cases = ['Payload/../escape', '/absolute', 'Payload/A.app/file\\name', 'outside',
                 'Payload/A.app/_CodeSignature/CodeResources', 'Payload/A.app/embedded.mobileprovision']
        for name in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); archive = root / 'source.ipa'
                with zipfile.ZipFile(archive, 'w') as target:
                    entry = zipfile.ZipInfo(); entry.filename = name
                    target.writestr(entry, b'data')
                with self.assertRaises(ci.RebuildError): ci.unpack_ipa(archive, root / 'out')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); archive = root / 'source.ipa'
            with zipfile.ZipFile(archive, 'w') as target:
                target.writestr('Payload/A.app/file', b'a'); target.writestr('Payload/B.app/file', b'b')
            with self.assertRaisesRegex(ci.RebuildError, 'one source application'): ci.unpack_ipa(archive, root / 'out')
            with zipfile.ZipFile(archive, 'w') as target:
                entry = zipfile.ZipInfo('Payload/A.app/link'); entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                target.writestr(entry, '../../escape')
            with self.assertRaisesRegex(ci.RebuildError, 'symlinks'): ci.unpack_ipa(archive, root / 'out')


if __name__ == '__main__': unittest.main()
