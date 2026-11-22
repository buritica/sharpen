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
import json
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
OLDER = "2020-01-01T00:00:00+00:00"
OLD = "2021-01-01T00:00:00+00:00"
NEW = "2099-01-01T00:00:00+00:00"


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
        self.assertFalse(any(f == ".sharpen" for _, f in artifact_dirs.OWNED))


class WriterReaderTest(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git(self.repo, "init", "-q", "-b", BRANCH)
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")

    def _legacy(self, name="review", body=None):
        path = os.path.join(
            self.repo, ".claude", "grumpy", BRANCH, "receipts", f"{name}.json"
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps(body or {}) + "\n")
        return path

    def _age(self, path, written_at, mtime=None):
        """Rewrite the receipt at `path` with this written_at (None drops it)."""
        with open(path) as f:
            body = json.load(f)
        body.pop("written_at", None)
        if written_at:
            body["written_at"] = written_at
        with open(path, "w") as f:
            json.dump(body, f)
        if mtime is not None:
            os.utime(path, (mtime, mtime))

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

    def test_the_newer_receipt_wins_when_both_exist(self):
        legacy = self._legacy(body={"written_at": OLD})
        written = self._write()  # written now, so newer
        self.assertEqual(
            gate_receipts.receipt_path(self.repo, BRANCH, "grumpy:review"), written
        )
        self._age(written, OLDER)
        self.assertEqual(
            gate_receipts.receipt_path(self.repo, BRANCH, "grumpy:review"), legacy
        )

    def test_a_stale_new_receipt_does_not_shadow_a_fresh_legacy_one(self):
        written = self._write()
        self._age(written, OLD)
        legacy = self._legacy(body={"written_at": NEW})
        self.assertEqual(
            gate_receipts.receipt_path(self.repo, BRANCH, "grumpy:review"), legacy
        )

    def test_age_falls_back_to_mtime_without_written_at(self):
        written = self._write()
        self._age(written, None, mtime=1_000_000)
        legacy = self._legacy()
        os.utime(legacy, (2_000_000, 2_000_000))
        self.assertEqual(
            gate_receipts.receipt_path(self.repo, BRANCH, "grumpy:review"), legacy
        )
        os.utime(legacy, (500_000, 500_000))
        self.assertEqual(
            gate_receipts.receipt_path(self.repo, BRANCH, "grumpy:review"), written
        )

    def test_a_tie_goes_to_the_new_root(self):
        written = self._write()
        self._age(written, NEW)
        self._legacy(body={"written_at": NEW})
        self.assertEqual(
            gate_receipts.receipt_path(self.repo, BRANCH, "grumpy:review"), written
        )

    def test_a_stage_receipt_beats_a_newer_plain_one(self):
        stage = self._write("fix", stage="review")
        self._age(stage, OLD)
        self._legacy("fix", body={"written_at": NEW})
        self.assertEqual(
            gate_receipts.receipt_path(
                self.repo, BRANCH, "grumpy:fix", "grumpy-fix-post-review"
            ),
            stage,
        )

    def test_the_newer_stage_receipt_wins_across_roots(self):
        stage = self._write("fix", stage="imagine")
        self._age(stage, OLD)
        legacy = self._legacy("fix-imagine", body={"written_at": NEW})
        self.assertEqual(
            gate_receipts.receipt_path(
                self.repo, BRANCH, "grumpy:fix", "grumpy-fix-post-imagine"
            ),
            legacy,
        )

    def test_predecessor_receipt_is_found_in_the_legacy_root(self):
        fix = self._write("fix", stage="review")
        self._legacy("review", body={"written_at": NEW})
        before, ts = gate_receipts._predecessor_written(
            fix, "grumpy-fix-post-review", {}, BRANCH
        )
        self.assertEqual(before, "grumpy-review")
        self.assertEqual(ts, gate_receipts._parse_ts(NEW))

    def test_predecessor_takes_the_newer_of_the_two_roots(self):
        review = self._write("review")
        self._age(review, OLD)
        self._legacy("review", body={"written_at": NEW})
        fix = self._write("fix", stage="review")
        _, ts = gate_receipts._predecessor_written(
            fix, "grumpy-fix-post-review", {}, BRANCH
        )
        self.assertEqual(ts, gate_receipts._parse_ts(NEW))

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
                        # The allowed shapes only name a legacy file or folder;
                        # what's done with it (read, or copied into the new
                        # folder) is pinned by the tests below.
                        self.assertRegex(
                            line,
                            r'^(LEGACY_DIR|LEGACY_PLAN|LEGACY_FIX)="\$GIT_ROOT/',
                            where,
                        )

    def test_fix_falls_back_per_file_and_never_writes_the_legacy_folder(self):
        with open(os.path.join(PLUGINS, "grumpy", "commands", "fix.md")) as f:
            text = f.read()
        lines = text.splitlines()
        self.assertIn(
            'LEGACY_DIR="$GIT_ROOT/.claude/grumpy/$BRANCH"  # reports written '
            "before grumpy 2.15; read, never written",
            lines,
        )
        # No folder-level switch: that picks the new folder even when only
        # the legacy one holds review.md (a mid-chain upgrade).
        self.assertNotIn('[ -d "$ARTIFACT_DIR" ] ||', text)
        self.assertIn("per file", text)
        for line in lines:
            if "LEGACY_DIR" in line and line.startswith(("mkdir", "printf", ">")):
                self.fail(f"fix.md writes the legacy folder: {line}")
        # The persist step creates only the new folder.
        self.assertIn('mkdir -p "$ARTIFACT_DIR"', lines)
        self.assertNotIn('mkdir -p "$LEGACY_DIR"', text)

    def test_no_command_switches_folders_by_directory(self):
        for path in self._commands():
            with open(path) as f:
                self.assertNotIn(
                    '[ -d "$ARTIFACT_DIR" ] ||', f.read(), os.path.relpath(path)
                )

    def _plan_snippets(self):
        """Each command's plan block: the PLAN= line through its `fi`."""
        snippets = []
        for path in self._commands():
            with open(path) as f:
                lines = f.read().splitlines()
            for i, line in enumerate(lines):
                if line == 'PLAN="$GIT_ROOT/.sharpen/sdlc/$BRANCH/plan.md"':
                    end = lines.index("fi", i)
                    snippets.append((path, "\n".join(lines[i : end + 1])))
        return snippets

    def test_every_plan_read_carries_a_legacy_plan_over(self):
        snippets = self._plan_snippets()
        self.assertGreaterEqual(len(snippets), 4)  # review, imagine, simplify, ship
        for path, snippet in snippets:
            where = os.path.relpath(path, PLUGINS)
            self.assertIn(
                'LEGACY_PLAN="$GIT_ROOT/.claude/sdlc/$BRANCH/plan.md"', snippet, where
            )
            self.assertIn('cp "$LEGACY_PLAN" "$PLAN"', snippet, where)
            # The plan is appended to at $PLAN, which never moves to .claude/.
            self.assertNotIn('PLAN="$LEGACY_PLAN"', snippet, where)

    def test_the_plan_carry_over_runs_in_a_real_shell(self):
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        legacy = os.path.join(repo, ".claude", "sdlc", BRANCH, "plan.md")
        os.makedirs(os.path.dirname(legacy))
        with open(legacy, "w") as f:
            f.write("# old plan\n")
        _, snippet = self._plan_snippets()[0]
        env = {**os.environ, "GIT_ROOT": repo, "BRANCH": BRANCH}
        for _ in range(2):  # idempotent: a second run leaves the copy alone
            subprocess.run(["bash", "-c", snippet], env=env, check=True)
        new = os.path.join(repo, ".sharpen", "sdlc", BRANCH, "plan.md")
        with open(new) as f:
            self.assertEqual(f.read(), "# old plan\n")
        self.assertTrue(
            os.path.isfile(os.path.join(repo, ".sharpen", "sdlc", ".gitignore"))
        )

    def test_fix_carries_a_legacy_fix_report_over_before_appending(self):
        with open(os.path.join(PLUGINS, "grumpy", "commands", "fix.md")) as f:
            text = f.read()
        self.assertIn(
            '[ -f "$ARTIFACT_DIR/fix.md" ] || [ ! -f "$LEGACY_FIX" ] || '
            'cp "$LEGACY_FIX" "$ARTIFACT_DIR/fix.md"',
            text,
        )

    def test_settled_and_deferred_readers_read_both_fix_reports(self):
        for rel in (
            ("grumpy", "commands", "review.md"),
            ("grumpy", "commands", "imagine.md"),
            ("sdlc", "commands", "ship.md"),
        ):
            with open(os.path.join(PLUGINS, *rel)) as f:
                text = " ".join(f.read().split())
            self.assertIn(".claude/grumpy/", text, "/".join(rel))
            self.assertRegex(
                text,
                r"(also|plus) the (`## Settled` sections of|legacy)",
                "/".join(rel),
            )


if __name__ == "__main__":
    unittest.main()
