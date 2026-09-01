#!/usr/bin/env python3
"""
Tests for scripts/generate-skill.py.

Run: python3 scripts/tests/test_generate_skill.py
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..")
sys.path.insert(0, SCRIPTS)

spec = importlib.util.spec_from_file_location(
    "generate_skill", os.path.join(SCRIPTS, "generate-skill.py")
)
gs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gs)

CLI = os.path.join(SCRIPTS, "generate-skill.py")


class RenderTest(unittest.TestCase):
    def test_name_defaults_to_filename_stem(self):
        source = (
            "---\n"
            'description: "Does a thing."\n'
            'argument-hint: "[--flag]"\n'
            'allowed-tools: ["Bash"]\n'
            "---\n"
            "\n"
            "# Do The Thing\n"
            "\n"
            "Body instructions.\n"
        )
        out = gs.render(source, "do-thing")
        self.assertTrue(out.startswith("---\n"))
        self.assertIn("name: do-thing\n", out)
        self.assertIn('description: "Does a thing."\n', out)
        self.assertNotIn("allowed-tools", out)
        self.assertNotIn("argument-hint", out)
        self.assertTrue(out.endswith("# Do The Thing\n\nBody instructions.\n"))

    def test_explicit_name_field_wins_over_filename(self):
        # Fallback deliberately differs from the explicit `name:` field —
        # if render() ever silently ignored the frontmatter name and used
        # the fallback instead, this must fail, not pass by coincidence.
        source = (
            "---\n"
            "name: guard\n"
            'description: "Manage protection."\n'
            "allowed-tools: Bash(python3:*)\n"
            "---\n"
            "\n"
            "Body.\n"
        )
        out = gs.render(source, "totally-different-filename")
        self.assertIn("name: guard\n", out)
        self.assertNotIn("totally-different-filename", out)

    def test_folded_multiline_description_collapses_to_one_line(self):
        source = (
            "---\n"
            "description:\n"
            "  Line one of the description spans\n"
            "  onto line two.\n"
            'argument-hint: "[--x]"\n'
            "---\n"
            "\n"
            "Body.\n"
        )
        out = gs.render(source, "review")
        self.assertIn(
            'description: "Line one of the description spans onto line two."\n',
            out,
        )
        # Exactly one description line in the output frontmatter — not folded.
        front, _ = out.split("---\n", 2)[1:]
        self.assertEqual(
            sum(1 for line in out.splitlines() if line.startswith("description:")), 1
        )

    def test_description_with_embedded_quote_is_safely_escaped(self):
        source = (
            "---\n"
            "description:\n"
            "  A description with a \"quoted phrase\" inside it.\n"
            "---\n"
            "\n"
            "Body.\n"
        )
        out = gs.render(source, "x")
        # The output must still be parseable frontmatter — round-trip it.
        import frontmatter as fm

        front_text, body = fm.split(out)
        data = fm.parse(front_text)
        self.assertEqual(
            data["description"], 'A description with a "quoted phrase" inside it.'
        )
        self.assertEqual(body, "\nBody.\n")

    def test_missing_description_raises(self):
        source = '---\nargument-hint: "[--x]"\n---\n\nBody.\n'
        with self.assertRaises(ValueError):
            gs.render(source, "x")

    def test_non_string_description_raises_rather_than_guessing(self):
        # No real command file writes description as an array — if one ever
        # does, fail loudly instead of silently joining it into prose.
        source = '---\ndescription: ["a", "b"]\n---\n\nBody.\n'
        with self.assertRaises(ValueError):
            gs.render(source, "x")

    def test_non_string_name_raises(self):
        source = '---\nname: ["a", "b"]\ndescription: "Does a thing."\n---\n\nBody.\n'
        with self.assertRaises(ValueError):
            gs.render(source, "x")

    def test_non_ascii_description_is_preserved_literally(self):
        # json.dumps defaults to ensure_ascii=True, which would turn a real
        # em dash into a — escape — several actual command descriptions
        # in this repo use em dashes, so this must stay literal.
        source = '---\ndescription: "Uses an em dash — like this."\n---\n\nBody.\n'
        out = gs.render(source, "x")
        self.assertIn("—", out)
        self.assertNotIn("\\u2014", out)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cmd_dir = os.path.join(self.root, "commands")
        os.makedirs(self.cmd_dir)
        with open(os.path.join(self.cmd_dir, "foo.md"), "w") as f:
            f.write('---\ndescription: "Does foo."\n---\n\nBody.\n')

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, CLI, *args], capture_output=True, cwd=self.root
        )

    def test_generates_skill_md_next_to_commands_dir(self):
        r = self.run_cli(os.path.join(self.cmd_dir, "foo.md"))
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        out_path = os.path.join(self.root, "skills", "foo", "SKILL.md")
        self.assertTrue(os.path.isfile(out_path))
        with open(out_path) as f:
            content = f.read()
        self.assertIn("name: foo", content)
        self.assertIn('description: "Does foo."', content)

    def test_check_mode_passes_when_up_to_date(self):
        self.run_cli(os.path.join(self.cmd_dir, "foo.md"))
        r = self.run_cli(os.path.join(self.cmd_dir, "foo.md"), "--check")
        self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_check_mode_fails_when_stale(self):
        self.run_cli(os.path.join(self.cmd_dir, "foo.md"))
        with open(os.path.join(self.cmd_dir, "foo.md"), "w") as f:
            f.write('---\ndescription: "Does foo, now differently."\n---\n\nBody.\n')
        r = self.run_cli(os.path.join(self.cmd_dir, "foo.md"), "--check")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stale", r.stdout.decode() + r.stderr.decode())

    def test_check_mode_fails_when_missing(self):
        r = self.run_cli(os.path.join(self.cmd_dir, "foo.md"), "--check")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("missing", r.stdout.decode())

    def test_cli_reports_a_malformed_command_file_via_stderr(self):
        # main()'s `except ValueError` path (render()/check_one() raising on
        # a source error), exercised end-to-end through the CLI rather than
        # just unit-tested against render() directly.
        with open(os.path.join(self.cmd_dir, "broken.md"), "w") as f:
            f.write("---\nargument-hint: \"[--x]\"\n---\n\nBody.\n")  # no description
        r = self.run_cli(os.path.join(self.cmd_dir, "broken.md"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no `description`", r.stderr.decode())

    def test_write_all_in_generates_every_command(self):
        with open(os.path.join(self.cmd_dir, "bar.md"), "w") as f:
            f.write('---\ndescription: "Does bar."\n---\n\nBody.\n')
        r = self.run_cli("--write-all-in", self.root)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertTrue(os.path.isfile(os.path.join(self.root, "skills", "foo", "SKILL.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "skills", "bar", "SKILL.md")))


if __name__ == "__main__":
    unittest.main()
