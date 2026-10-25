"""Compiler variation must keep the translator lock and reject missing tools."""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[2] / 'gfx/tools'
sys.path.insert(0, str(TOOLS))
import own_compilers as own


class OwnCompilersTests(unittest.TestCase):
    def test_import_does_not_start_a_shader_build(self):
        spec = importlib.util.spec_from_file_location('own_compilers_import_test', TOOLS / 'own_compilers.py')
        with patch.object(own.runpy, 'run_path') as run:
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
        run.assert_not_called()

    def test_available_compiler_differences_are_reported_and_allowed(self):
        changes = ['spirv-cross: lock old / actual new', 'metal_macosx: lock tested / actual installed',
                   'metal_iphoneos: lock tested / actual installed']
        with patch.object(own, 'strict', return_value=changes), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(own.check_tools(), [])
        self.assertIn("built with this Mac's compilers", output.getvalue())

    def test_missing_compilers_and_modified_translators_still_fail(self):
        for fatal in ('spirv-cross: lock old / actual missing', 'metal_iphoneos: lock old / actual missing',
                      'dmsh_xlate.dll: lock old / actual new', 'the translator was not built from the in-repo source'):
            with self.subTest(error=fatal), patch.object(own, 'strict', return_value=[fatal]), redirect_stdout(io.StringIO()):
                self.assertEqual(own.check_tools(), [fatal])

    def test_invocation_forwards_arguments_and_restores_the_module(self):
        original, argv = own.dmsh_chain.check_tools, sys.argv
        seen = []
        def build(path, run_name):
            seen.append((path, list(sys.argv), own.dmsh_chain.check_tools))
        with patch.object(own.runpy, 'run_path', side_effect=build): own.main(['packages', 'output', '0'*16])
        self.assertEqual(seen[0][1][1:], ['packages', 'output', '0'*16])
        self.assertIs(seen[0][2], own.check_tools)
        self.assertIs(own.dmsh_chain.check_tools, original)
        self.assertIs(sys.argv, argv)


if __name__ == '__main__': unittest.main()
