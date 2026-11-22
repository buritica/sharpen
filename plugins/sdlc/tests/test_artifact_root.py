#!/usr/bin/env python3
"""Per-branch artifacts live under `.sharpen/`, and both plugins agree on it.

sdlc reads the receipts grumpy writes, and grumpy reads the plan and stack
base sdlc writes. The two plugins can't import each other, so each names the
other's root on its own (`receipt.GRUMPY_ROOT` and `diff_base.SDLC_ROOT` in
grumpy, `artifact_dirs` in sdlc). These tests pin them together, run the real
writers against the real readers, and check that files an older version left
under `.claude/` are still read.

Run: python3 plugins/sdlc/tests/test_artifact_root.py
"""

import glob
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
PLUGINS = os.path.normpath(os.path.join(HERE, "..", ".."))
RECEIPT = os.path.join(PLUGINS, "grumpy", "scripts", "receipt.py")
DIFF_BASE = os.path.join(PLUGINS, "grumpy", "scripts", "diff_base.py")
sys.path.insert(0, SCRIPTS)

import artifact_dirs  # noqa: E402
import gate_base  # noqa: E402
import gate_receipts  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receipt = _load("grumpy_receipt", RECEIPT)
diff_base = _load("grumpy_diff_base", DIFF_BASE)

BRANCH = "feat/x"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, check=True, text=True
    )


class PluginsAgreeTest(unittest.TestCase):
    def test_grumpy_and_sdlc_name_the_same_roots(self):
        self.assertEqual(receipt.GRUMPY_ROOT, artifact_dirs.GRUMPY_ROOT)
        self.assertEqual(diff_base.SDLC_ROOT, artifact_dirs.SDLC_ROOT)
        self.assertEqual(diff_base.LEGACY_SDLC_ROOT, artifact_dirs.LEGACY_SDLC_ROOT)

    def test_roots_are_under_sharpen_and_never_sharpen_itself(self):
        for root in (artifact_dirs.GRUMPY_ROOT, artifact_dirs.SDLC_ROOT):
            self.assertEqual(root[0], ".sharpen")
            self.assertEqual(len(root), 2)
            self.assertIn(root, artifact_dirs.OWNED)
        self.assertNotIn((".", ".sharpen"), artifact_dirs.OWNED)


class WriterReaderTest(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git(self.repo, "init", "-q", "-b", BRANCH)
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")

    def _legacy(self, name="review"):
        path = os.path.join(
            self.repo, ".claude", "grumpy", BRANCH, "receipts", f"{name}.json"
        )
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("{}\n")
        return path

    def _write(self, pass_name="review", stage=None):
        """What a pass does: save its report, then leave the receipt."""
        folder = os.path.join(self.repo, *receipt.GRUMPY_ROOT, BRANCH)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, receipt.REPORT_FOR[pass_name]), "w") as f:
            f.write(f"# {pass_name}\n")
        path, _ = receipt.write_receipt(pass_name, self.repo, stage=stage)
        return path

    def test_reader_finds_what_the_writer_wrote(self):
        written = self._write()
        self.assertEqual(
            gate_receipts.receipt_path(self.repo, BRANCH, "grumpy:review"), written
        )
        self.assertIn(os.sep + os.path.join(".sharpen", "grumpy") + os.sep, written)

    def test_a_receipt_only_at_the_legacy_root_is_read(self):
        legacy = self._legacy()
        self.assertEqual(
            gate_receipts.receipt_path(self.repo, BRANCH, "grumpy:review"), legacy
        )

    def test_the_new_root_wins_when_both_exist(self):
        self._legacy()
        written = self._write()
        self.assertEqual(
            gate_receipts.receipt_path(self.repo, BRANCH, "grumpy:review"), written
        )

    def test_with_neither_the_reader_points_at_the_new_root(self):
        path = gate_receipts.receipt_path(self.repo, BRANCH, "grumpy:review")
        self.assertTrue(
            path.startswith(os.path.join(self.repo, *artifact_dirs.GRUMPY_ROOT))
        )

    def test_a_stage_fix_receipt_is_found_at_the_new_root(self):
        written = self._write("fix", stage="review")
        self.assertEqual(
            gate_receipts.receipt_path(
                self.repo, BRANCH, "grumpy:fix", "grumpy-fix-post-review"
            ),
            written,
        )

    def test_a_legacy_stage_receipt_is_found_when_the_new_root_has_none(self):
        legacy = self._legacy("fix-imagine")
        self.assertEqual(
            gate_receipts.receipt_path(
                self.repo, BRANCH, "grumpy:fix", "grumpy-fix-post-imagine"
            ),
            legacy,
        )

    def test_writer_never_writes_the_legacy_root(self):
        self._write()
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude")))

    def test_grumpy_reads_the_base_file_sdlc_writes(self):
        git(self.repo, "branch", "parent")
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        path = gate_base.write_base_file(
            BRANCH, {"ref": "parent", "commit": head}, self.repo
        )
        self.assertTrue(
            path.startswith(os.path.join(self.repo, *artifact_dirs.SDLC_ROOT))
        )
        self.assertEqual(diff_base._from_sdlc(self.repo, self.repo, BRANCH), "parent")
        self.assertTrue(
            os.path.isfile(os.path.join(self.repo, ".sharpen", "sdlc", ".gitignore"))
        )

    def test_sdlc_and_grumpy_folders_ignore_themselves(self):
        for name in ("sdlc", "grumpy"):
            folder = os.path.join(self.repo, ".sharpen", name)
            os.makedirs(folder)
            artifact_dirs.ensure_self_ignored(folder)
            self.assertTrue(os.path.isfile(os.path.join(folder, ".gitignore")))
        artifact_dirs.ensure_self_ignored(os.path.join(self.repo, ".sharpen"))
        self.assertFalse(
            os.path.exists(os.path.join(self.repo, ".sharpen", ".gitignore"))
        )


class CommandsTest(unittest.TestCase):
    """Commands write under `.sharpen/` and only read the legacy folders."""

    def _commands(self):
        return sorted(glob.glob(os.path.join(PLUGINS, "*", "commands", "*.md")))

    def test_no_command_writes_an_artifact_under_claude(self):
        for path in self._commands():
            with open(path) as f:
                for n, line in enumerate(f, 1):
                    where = f"{os.path.relpath(path, PLUGINS)}:{n}"
                    for folder in ("grumpy", "sdlc"):
                        legacy = f'"$GIT_ROOT/.claude/{folder}/'
                        if legacy not in line:
                            continue
                        # The one allowed shape: a read fallback, taken only
                        # when the new location is missing.
                        self.assertRegex(
                            line, r"^\[ -[fd] \"\$(PLAN|ARTIFACT_DIR)\" \] \|\| ", where
                        )

    def test_every_artifact_dir_fallback_points_at_the_legacy_root(self):
        seen = 0
        for path in self._commands():
            with open(path) as f:
                for n, line in enumerate(f, 1):
                    if line.startswith('[ -d "$ARTIFACT_DIR" ] || '):
                        seen += 1
                        self.assertIn(
                            'ARTIFACT_DIR="$GIT_ROOT/.claude/grumpy/$BRANCH"',
                            line,
                            f"{os.path.relpath(path, PLUGINS)}:{n}",
                        )
        self.assertGreaterEqual(seen, 1)  # fix reads earlier passes' reports

    def test_every_plan_read_falls_back_to_the_legacy_plan(self):
        seen = 0
        for path in self._commands():
            with open(path) as f:
                lines = f.read().splitlines()
            for i, line in enumerate(lines):
                if line == 'PLAN="$GIT_ROOT/.sharpen/sdlc/$BRANCH/plan.md"':
                    seen += 1
                    self.assertTrue(
                        lines[i + 1].startswith(
                            '[ -f "$PLAN" ] || PLAN="$GIT_ROOT/.claude/sdlc/'
                        ),
                        f"{os.path.relpath(path, PLUGINS)}:{i + 2}",
                    )
        self.assertGreaterEqual(seen, 4)  # review, imagine, simplify, ship


if __name__ == "__main__":
    unittest.main()
