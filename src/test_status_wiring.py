from pathlib import Path
import tempfile
import unittest

import status


class TestWiringGuard(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.src = self.root / "src"
        self.src.mkdir()
        self.justfile = self.root / "justfile"
        self.discovery = '    @{{py}} -m unittest discover -s "{{src}}" -p \'test_*.py\' -v\n'
        self.recipe = "selftest-py:\n" + self.discovery
        self.case = (
            "import unittest\n"
            "class Registered(unittest.TestCase):\n"
            "    def test_registered(self):\n"
            "        self.assertEqual(1, 1)\n"
        )
        (self.src / "test_registered.py").write_text(self.case)

    def check(self, recipe=None):
        self.justfile.write_text(self.recipe if recipe is None else recipe)
        status._check_test_wiring(str(self.justfile), str(self.src))

    def test_registered_testcase_needs_no_literal_filename(self):
        self.check()

    def test_removing_discovery_flags_testcase_without_bare_asserts(self):
        with self.assertRaisesRegex(AssertionError, "test_registered.py"):
            self.check(self.recipe.replace(self.discovery, "    @true\n"))

    def test_wrong_discovery_pattern_does_not_cover_testcase(self):
        with self.assertRaisesRegex(AssertionError, "test_registered.py"):
            self.check(self.recipe.replace("test_*.py", "test_other*.py"))

    def test_wrong_discovery_directory_does_not_cover_testcase(self):
        with self.assertRaisesRegex(AssertionError, "test_registered.py"):
            self.check(self.recipe.replace("{{src}}", "elsewhere"))

    def test_commented_or_echoed_discovery_is_not_execution(self):
        for prefix in ("    # ", "    @echo "):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(AssertionError, "test_registered.py"):
                self.check("selftest-py:\n" + prefix + self.discovery.strip().lstrip("@") + "\n")

    def test_name_filter_does_not_claim_full_module_coverage(self):
        with self.assertRaisesRegex(AssertionError, "test_registered.py"):
            self.check(self.recipe.replace(" -v", " -k one_case -v"))

    def test_standalone_assert_scripts_still_require_literal_execution(self):
        for name in ("test_stale_resolve.py", "test_cli_wheel.py"):
            with self.subTest(name=name):
                script = self.src / name
                script.write_text('def main():\n    assert False\nif __name__ == "__main__":\n    main()\n')
                with self.assertRaisesRegex(AssertionError, name):
                    self.check()
                self.check(self.recipe + "    @{{py}} {{src}}/" + name + "\n")
                script.unlink()

    def test_removing_wheel_recipe_flags_standalone_smoke(self):
        (self.src / "test_cli_wheel.py").write_text('if __name__ == "__main__":\n    assert False\n')
        call = "    @just selftest-py-wheel\n"
        wheel = "\nselftest-py-wheel:\n    @{{py}} {{src}}/test_cli_wheel.py a.whl a.tar.gz\n"
        self.check(self.recipe + call + wheel)
        with self.assertRaisesRegex(AssertionError, "test_cli_wheel.py"):
            self.check(self.recipe + call)
        with self.assertRaisesRegex(AssertionError, "test_cli_wheel.py"):
            self.check(self.recipe + wheel)

    def test_matching_filename_without_registered_testcase_is_not_covered(self):
        (self.src / "test_fake.py").write_text("class NotATest:\n    def test_fake(self):\n        assert False\n")
        with self.assertRaisesRegex(AssertionError, "test_fake.py"):
            self.check()

    def test_empty_testcase_does_not_cover_main_assertions(self):
        (self.src / "test_empty.py").write_text(
            'import unittest\nclass Empty(unittest.TestCase):\n    pass\n'
            'if __name__ == "__main__":\n    assert False\n'
        )
        with self.assertRaisesRegex(AssertionError, "test_empty.py"):
            self.check()

    def test_load_tests_returning_empty_suite_cannot_claim_coverage(self):
        (self.src / "test_registered.py").write_text(
            self.case + "def load_tests(loader, tests, pattern):\n    return unittest.TestSuite()\n"
        )
        with self.assertRaisesRegex(AssertionError, "test_registered.py"):
            self.check()

    def test_inherited_and_import_aliased_testcases_are_registered(self):
        (self.src / "test_registered.py").write_text(
            "from unittest import TestCase as Base\n"
            "class Parent(Base):\n    def test_parent(self):\n        self.assertTrue(True)\n"
            "class Child(Parent):\n    pass\n"
        )
        self.check()

    def test_standalone_selftest_and_main_guards_remain(self):
        for name, source in (
            ("function_check.py", "def _selftest():\n    assert False\n"),
            ("main_check.py", 'if __name__ == "__main__":\n    assert False\n'),
        ):
            with self.subTest(name=name):
                script = self.src / name
                script.write_text(source)
                with self.assertRaisesRegex(AssertionError, name):
                    self.check()
                self.check(self.recipe + "    @{{py}} {{src}}/" + name + " --selftest\n")
                script.unlink()

    def test_ordinary_recipe_does_not_count_as_selftest_wiring(self):
        with self.assertRaisesRegex(AssertionError, "test_registered.py"):
            self.check("selftest-py:\n    @true\n\nordinary:\n" + self.discovery)

    def test_wrapper_reaches_private_check_recipe(self):
        self.check("selftest-py:\n    @just _test-isolated just selftest-py-checks\n\n"
                   "_test-isolated +args:\n    @env HOME=temporary {{args}}\n\n"
                   "[private]\nselftest-py-checks:\n" + self.discovery)

    def test_default_pattern_and_explicit_source_directory(self):
        self.check(f"selftest-py:\n    @python3 -m unittest discover --start-directory='{self.src}'\n")

    def test_assertions_only_in_comments_or_docstrings_are_not_tests(self):
        (self.src / "test_documented.py").write_text(
            '\"\"\"assert False is documentation\"\"\"\n# assert False is a comment\n'
        )
        self.check()

    def test_main_assertions_require_literal_wiring_even_with_registered_testcase(self):
        (self.src / "test_registered.py").write_text(
            self.case + 'if __name__ == "__main__":\n    assert False\n'
        )
        with self.assertRaisesRegex(AssertionError, "test_registered.py"):
            self.check()
        self.check(self.recipe + "    @{{py}} {{src}}/test_registered.py\n")

    def test_discovery_pattern_cannot_masquerade_as_literal_script_execution(self):
        (self.src / "test_cli_wheel.py").write_text('if __name__ == "__main__":\n    assert False\n')
        with self.assertRaisesRegex(AssertionError, "test_cli_wheel.py"):
            self.check(self.recipe + self.discovery.replace("test_*.py", "test_cli_wheel.py"))

    def test_quoted_or_commented_literal_script_is_not_execution(self):
        (self.src / "test_cli_wheel.py").write_text('if __name__ == "__main__":\n    assert False\n')
        for command in ("# {{py}} {{src}}/test_cli_wheel.py", "echo '{{py}} {{src}}/test_cli_wheel.py'"):
            with self.subTest(command=command), self.assertRaisesRegex(AssertionError, "test_cli_wheel.py"):
                self.check(self.recipe + "    " + command + "\n")

    def test_empty_testcase_without_methods_needs_no_coverage(self):
        (self.src / "test_empty.py").write_text('import unittest\nclass Empty(unittest.TestCase):\n    pass\n')
        self.check()

    def test_inherited_methods_emptied_by_load_tests_are_flagged(self):
        (self.src / "base_cases.py").write_text(
            "from unittest import TestCase\n"
            "class Parent(TestCase):\n    def test_parent(self):\n        self.assertTrue(True)\n"
        )
        (self.src / "test_registered.py").write_text(
            "import unittest\nfrom base_cases import Parent\nclass Child(Parent):\n    pass\n"
            "def load_tests(loader, tests, pattern):\n    return unittest.TestSuite()\n"
        )
        with self.assertRaisesRegex(AssertionError, "test_registered.py"):
            self.check()

    def test_literal_script_does_not_excuse_empty_unittest_suite(self):
        (self.src / "test_registered.py").write_text(
            self.case + "def load_tests(loader, tests, pattern):\n    return unittest.TestSuite()\n"
            'if __name__ == "__main__":\n    unittest.main()\n'
        )
        with self.assertRaisesRegex(AssertionError, "test_registered.py"):
            self.check(self.recipe + "    @{{py}} {{src}}/test_registered.py\n")

    def test_extraction_delegate_requires_reachable_enabled_lifecycle_invocation(self):
        (self.src / "kal_extract_mcp.py").write_text("def _selftest_stdio_extract():\n    assert True\n")
        plain = "    @{{py}} {{src}}/kal_mcp.py --selftest-static\n"
        enabled = "    @KAL_MCP_WRITE=1 {{py}} {{src}}/kal_mcp.py --selftest-static\n"
        with self.assertRaisesRegex(AssertionError, "kal_extract_mcp.py"):
            self.check(self.recipe + plain)
        self.check(self.recipe + plain + enabled)
        for mutation in (
            enabled.replace("KAL_MCP_WRITE=1", "KAL_MCP_WRITE=0"),
            enabled.replace("--selftest-static", "--help"),
            enabled.replace("@KAL_MCP_WRITE", "@echo KAL_MCP_WRITE"),
            enabled.replace("@KAL_MCP_WRITE", "# KAL_MCP_WRITE"),
            enabled.replace("{{src}}/kal_mcp.py", "elsewhere/kal_mcp.py"),
        ):
            with self.subTest(mutation=mutation), self.assertRaisesRegex(AssertionError, "kal_extract_mcp.py"):
                self.check(self.recipe + plain + mutation)
        with self.assertRaisesRegex(AssertionError, "kal_extract_mcp.py"):
            self.check(self.recipe + plain + "\nselftest-lifecycle:\n" + enabled)
        self.check(self.recipe + plain + "    @just selftest-lifecycle\n\nselftest-lifecycle:\n" + enabled)

    def test_missing_selftest_recipes_fail_loudly(self):
        with self.assertRaisesRegex(AssertionError, "no selftest recipe"):
            self.check("ordinary:\n" + self.discovery)


if __name__ == "__main__":
    unittest.main()
