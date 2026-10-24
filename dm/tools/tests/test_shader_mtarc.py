"""MT Framework ARC layouts from supported SDK 10 and Revelations' SDK 1.6."""
import importlib.util
import os
from pathlib import Path
import struct
import unittest
from unittest import mock
import zlib

SOURCE = Path(__file__).resolve().parents[2] / 'gfx/tools/shader_split.py'
spec = importlib.util.spec_from_file_location('shader_split_mtarc', SOURCE)
shader = importlib.util.module_from_spec(spec)
with mock.patch.dict(os.environ, {'DM_GAME': '010036E00FB20000'}):
    spec.loader.exec_module(shader)


def archive(version, data, compressed=True, size=None, offset=None):
    payload = zlib.compress(data) if compressed else data
    raw_size = len(data) if size is None else size
    offset = offset if offset is not None else (0x98 if version == 7 else 0x8000)
    header = struct.pack('<4sHH', b'ARC\0', version, 1)
    if version == 7:
        entry = struct.pack('<128s4I', b'sc\\NX_64\\root', 0, len(payload), raw_size | 0x40000000, offset)
    else:
        entry = struct.pack('<64s5I', b'sc\\NX_64\\root', 0, len(payload), raw_size, 2 if compressed else 0, offset)
    return header + entry + bytes(offset-len(header)-len(entry)) + payload


class Arc(unittest.TestCase):
    def test_arc7_keeps_legacy_flags_and_path_layout(self):
        data = b'SPK\0' + b'legacy shader' * 20
        self.assertEqual(list(shader.mtarc_members(archive(7, data), 'legacy')), [('sc/NX_64/root', data)])

    def test_arc9_separate_flags_and_offset(self):
        data = b'SPK\0' + b'old SDK shader' * 20
        self.assertEqual(list(shader.mtarc_members(archive(9, data), 'sdk1')), [('sc/NX_64/root', data)])

    def test_arc9_multiple_entries_have_84_byte_stride(self):
        first, second = b'first', b'second'
        offset = 8 + 2 * 0x54
        header = struct.pack('<4sHH', b'ARC\0', 9, 2)
        entries = struct.pack('<64s5I', b'one', 0, len(first), len(first), 0, offset)
        entries += struct.pack('<64s5I', b'two', 0, len(second), len(second), 0, offset + len(first))
        self.assertEqual(list(shader.mtarc_members(header + entries + first + second, 'multi')),
                         [('one', first), ('two', second)])

    def test_arc9_stored_member(self):
        data = b'SPK\0stored'
        self.assertEqual(list(shader.mtarc_members(archive(9, data, compressed=False), 'stored')), [('sc/NX_64/root', data)])

    def test_arc9_does_not_mask_large_size_as_flags(self):
        class LargeMember:
            def __len__(self): return 651519592
        decoded = LargeMember()
        with mock.patch.object(zlib, 'decompress', return_value=decoded):
            self.assertIs(list(shader.mtarc_members(archive(9, b'compressed', size=len(decoded)), 'large'))[0][1], decoded)

    def test_truncated_table_and_payload_are_rejected(self):
        for data in (struct.pack('<4sHH', b'ARC\0', 9, 1), archive(9, b'shader')[:-1]):
            with self.subTest(length=len(data)), self.assertRaisesRegex(SystemExit, 'truncated|out of bounds'):
                list(shader.mtarc_members(data, 'bad'))


def stage(kind):
    code = bytearray(0x80)
    code[:4] = shader.GFX_MAGIC
    struct.pack_into('<I', code, 0x30, (1 if kind == 'vs' else 5) << 10 | 0x61)
    control = bytearray(0x838)
    control[:4] = shader.CTL_MAGIC
    struct.pack_into('<I', control, 0x20, len(control))
    struct.pack_into('<I', control, 0x704, len(code))
    return bytes(control), bytes(code)


class Spk(unittest.TestCase):
    def test_code_before_control_remains_supported(self):
        cv, v = stage('vs'); cp, p = stage('ps')
        result = shader.mtspk_shaders(b'SPK\0table' + v + cv + p + cp, 'new')
        self.assertEqual([(s, c, code) for _, s, c, code in result], [('vs', cv, v), ('ps', cp, p)])

    def test_control_before_code_with_record_metadata(self):
        cv, v = stage('vs'); cp, p = stage('ps')
        result = shader.mtspk_shaders(b'SPK\0table' + cv + v + b'\0'*0x44 + cp + p + b'footer', 'sdk1')
        self.assertEqual([(s, c, code) for _, s, c, code in result], [('vs', cv, v), ('ps', cp, p)])

    def test_truncated_code_is_rejected(self):
        control, code = stage('vs')
        with self.assertRaisesRegex(SystemExit, 'out of bounds'):
            shader.mtspk_shaders(b'SPK\0table' + control + code[:-1], 'bad')


if __name__ == '__main__':
    unittest.main()
