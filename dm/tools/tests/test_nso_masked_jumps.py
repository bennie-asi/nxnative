"""Bounds and scheduling in the older compiler's signed-offset jump tables."""
import ast
from pathlib import Path
import re
import struct
import types
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'patch_nso.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
helpers = {'masked_index_count', 'jump_offsets', 'check_before_copy', 'table_base', 'copied_base'}
module = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in helpers], type_ignores=[])
scope = {'re': re, 'struct': struct, 'num': lambda r: int(r[1:])}
exec(compile(module, str(SOURCE), 'exec'), scope)


class Ins:
    def __init__(self, address, mnemonic, operands='', writes=(), detail=()):
        self.address, self.mnemonic, self.op_str = address, mnemonic, operands
        self.writes, self.operands = writes, detail
    def reg_name(self, register): return register
    def regs_access(self): return (), self.writes


class Mask(unittest.TestCase):
    def instructions(self, mask=15, guard_register='w17', guard_target=0x200):
        guard = Ins(0x100, 'tbnz', f'{guard_register}, #3, #{guard_target:#x}',
                    detail=[types.SimpleNamespace(reg=guard_register), types.SimpleNamespace(imm=3), types.SimpleNamespace(imm=guard_target)])
        mask_ins = Ins(0x104, 'and', f'x18, x17, #{mask:#x}', ('x18',))
        load = Ins(0x108, 'ldrsw', 'x18, [x13, x18, lsl #2]', ('x18',))
        return [guard, mask_ins, load], load

    def test_low_bit_mask_alone_bounds_table(self):
        instructions, load = self.instructions()
        self.assertEqual(scope['masked_index_count'](instructions[1:], load, 'x18'), 16)

    def test_tbnz_fallthrough_clears_high_bit(self):
        instructions, load = self.instructions()
        self.assertEqual(scope['masked_index_count'](instructions, load, 'w18'), 8)

    def test_unrelated_guard_does_not_reduce_table(self):
        instructions, load = self.instructions(guard_register='w1')
        self.assertEqual(scope['masked_index_count'](instructions, load, 'x18'), 16)

    def test_guard_that_jumps_back_into_index_sequence_is_not_used(self):
        instructions, load = self.instructions(guard_target=0x104)
        self.assertEqual(scope['masked_index_count'](instructions, load, 'x18'), 16)

    def test_sparse_or_unbounded_mask_is_not_guessed(self):
        for mask in (0xA, 0x7FFF8, 0x1FFFF):
            instructions, load = self.instructions(mask)
            with self.subTest(mask=mask):
                self.assertIsNone(scope['masked_index_count'](instructions[1:], load, 'x18'))

    def test_later_index_write_invalidates_mask(self):
        instructions, load = self.instructions()
        instructions.insert(-1, Ins(0x106, 'add', 'x18, x18, #1', ('x18',)))
        self.assertIsNone(scope['masked_index_count'](instructions, load, 'x18'))


class Scheduled(unittest.TestCase):
    def sequence(self):
        instructions = [Ins(a, 'nop') for a in range(0x100, 0x200, 4)]
        def place(ins): instructions[(ins.address-0x100)//4] = ins
        place(Ins(0x100, 'cmp', 'w18, #1'))
        place(Ins(0x104, 'b.hi', '#0x1f0'))
        place(Ins(0x108, 'adrp', 'x0, #0x3000', ('x0',), [None, types.SimpleNamespace(imm=0x3000)]))
        place(Ins(0x10C, 'add', 'x0, x0, #0', ('x0',)))
        place(Ins(0x110, 'ldrsw', 'x18, [x0, x18, lsl #2]', ('x18',)))
        place(Ins(0x164, 'add', 'x18, x18, x0', ('x18',)))
        place(Ins(0x190, 'br', 'x18'))
        image = bytearray(0x3100)
        struct.pack_into('<ii', image, 0x3000, 0x1A0-0x3000, 0x1A4-0x3000)
        return instructions, (0x190-0x100)//4, image

    def test_wider_attempt_handles_scheduled_dispatch(self):
        instructions, branch, image = self.sequence()
        with mock.patch.dict(scope, {'IMG': image}):
            self.assertIsNone(scope['jump_offsets'](instructions, branch))
            self.assertEqual(scope['jump_offsets'](instructions, branch, far_base=True), [0x1A0, 0x1A4])

    def test_target_write_after_add_is_rejected(self):
        instructions, branch, image = self.sequence()
        instructions[(0x180-0x100)//4] = Ins(0x180, 'mov', 'x18, x9', ('x18',))
        with mock.patch.dict(scope, {'IMG': image}):
            self.assertIsNone(scope['jump_offsets'](instructions, branch, far_base=True))

    def test_call_between_load_and_add_is_rejected(self):
        instructions, branch, image = self.sequence()
        instructions[(0x140-0x100)//4] = Ins(0x140, 'bl', '#0x800', ('x30',))
        with mock.patch.dict(scope, {'IMG': image}):
            self.assertIsNone(scope['jump_offsets'](instructions, branch, far_base=True))


if __name__ == '__main__':
    unittest.main()
