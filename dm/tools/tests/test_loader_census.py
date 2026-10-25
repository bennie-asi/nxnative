"""Loader pointer stores may be scheduled after the next call's name setup."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('loader_census', Path(__file__).resolve().parents[1] / 'loader_census.py')
census = importlib.util.module_from_spec(spec); spec.loader.exec_module(census)


def adrp(reg, address):
    page = address >> 12
    return 0x90000000 | ((page & 3) << 29) | ((page >> 2) << 5) | reg


def load(reg, base, offset): return 0xf9400000 | ((offset // 8) << 10) | (base << 5) | reg
def store(base, offset=0): return 0xf9000000 | ((offset // 8) << 10) | (base << 5)
def add(dst, base, offset): return 0x91000000 | (offset << 10) | (base << 5) | dst


class LoaderStoreTests(unittest.TestCase):
    def test_got_store_accepts_the_next_names_hoisted_address(self):
        for hoisted in ([], [adrp(1, 0x9000), add(1, 1, 0x120)]):
            with self.subTest(hoisted=bool(hoisted)):
                words = [0xd63f0260, adrp(8, 0x4000), load(8, 8, 0x150), *hoisted, store(8)]
                self.assertEqual(census.result_slot(words, 0, {}), 0x4150)

    def test_a_new_return_value_or_pointer_cannot_belong_to_the_old_call(self):
        for clobber in (adrp(0, 0x9000), adrp(8, 0x9000), add(0, 1, 8), add(8, 8, 8),
                        0xd63f0260, 0x14000004):
            with self.subTest(instruction=hex(clobber)):
                words = [0xd63f0260, adrp(8, 0x4000), load(8, 8, 0x150), clobber, store(8)]
                self.assertIsNone(census.result_slot(words, 0, {}))

    def test_direct_and_preloaded_globals_keep_their_original_meaning(self):
        self.assertEqual(census.result_slot([0xd63f0260, adrp(8, 0x4000), store(8, 0x150)], 0, {}), 0x4150)
        self.assertEqual(census.result_slot([0xd63f0260, store(22, 0x150), 0xd503201f], 0, {22: 0x4000}), 0x4150)

    def test_unrelated_indirect_load_and_truncated_tail_are_not_a_store(self):
        self.assertIsNone(census.result_slot([0xd63f0260, adrp(8, 0x4000), load(8, 9, 0x150), store(8)], 0, {}))
        self.assertIsNone(census.result_slot([0xd63f0260, adrp(8, 0x4000), load(8, 8, 0x150)], 0, {}))
        self.assertIsNone(census.result_slot([0xd63f0260, adrp(0, 0x4000), load(8, 0, 0x150), store(8)], 0, {}))
        self.assertIsNone(census.result_slot([0xd63f0260, adrp(8, 0x4000), load(0, 8, 0x150), store(0)], 0, {}))


if __name__ == '__main__': unittest.main()
