# -*- coding: utf-8 -*-
"""Unit tests for the Code Review Copilot engine.

Run with::

    python -m unittest discover -s tests -v

These tests deliberately avoid requiring QwenPaw: the engine is pure
logic plus read-only git calls, so it can be validated standalone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reviewer  # noqa: E402
import rules  # noqa: E402


SAMPLE_DIFF = """\
diff --git a/src/auth.py b/src/auth.py
new file mode 100644
index 0000000..1234567
--- /dev/null
+++ b/src/auth.py
@@ -0,0 +1,8 @@
+import os
+
+API_KEY = "sk-abcdefghijklmnopqrst"
+DEBUG = True
+
+def check(user_input):
+    return eval(user_input)
+
diff --git a/src/old.py b/src/old.py
deleted file mode 100644
index 1234567..0000000
--- a/src/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-print("gone")
-print("bye")
"""


class TestDiffParsing(unittest.TestCase):
    """The diff parser is the highest-risk component."""

    def test_parses_file_and_line_numbers(self):
        summary = reviewer.parse_unified_diff(SAMPLE_DIFF)
        paths = [f.path for f in summary.files]
        self.assertEqual(paths, ["src/auth.py", "src/old.py"])

        auth = summary.files[0]
        self.assertTrue(auth.is_new)
        self.assertEqual(auth.added_count, 8)
        # hunk starts at new-line 1
        self.assertEqual(auth.added_lines[0][0], 1)
        self.assertEqual(auth.added_lines[0][1], "import os")
        self.assertEqual(auth.added_lines[2][1], 'API_KEY = "sk-abcdefghijklmnopqrst"')

    def test_counts_additions_and_deletions(self):
        summary = reviewer.parse_unified_diff(SAMPLE_DIFF)
        self.assertEqual(summary.additions, 8)
        self.assertEqual(summary.deletions, 2)

    def test_marks_deleted_file(self):
        summary = reviewer.parse_unified_diff(SAMPLE_DIFF)
        self.assertTrue(summary.files[1].is_deleted)

    def test_handles_hunk_with_line_offsets(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -10,3 +10,4 @@\n"
            " ctx\n"
            "+added\n"
            " ctx2\n"
        )
        summary = reviewer.parse_unified_diff(diff)
        self.assertEqual(summary.files[0].added_lines, [(11, "added")])

    def test_empty_diff(self):
        summary = reviewer.parse_unified_diff("")
        self.assertEqual(summary.file_count, 0)
        self.assertEqual(summary.additions, 0)

    def test_file_body_lines_are_not_treated_as_headers(self):
        # A literal 'diff --git a/x b/y' inside content must not split files.
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,2 @@\n"
            " keep\n"
            "+text containing diff --git a/x b/y inline\n"
        )
        summary = reviewer.parse_unified_diff(diff)
        self.assertEqual(summary.file_count, 1)
        self.assertEqual(summary.files[0].added_count, 1)


class TestRuleScanning(unittest.TestCase):

    def test_detects_secret_and_eval(self):
        summary, findings = reviewer.analyze_diff(SAMPLE_DIFF)
        ids = {f.rule_id for f in findings}
        self.assertIn("secret.openai-key", ids)
        self.assertIn("danger.eval", ids)

    def test_deleted_files_are_not_scanned(self):
        _, findings = reviewer.analyze_diff(SAMPLE_DIFF)
        self.assertFalse([f for f in findings if f.path == "src/old.py"])

    def test_findings_have_line_numbers(self):
        _, findings = reviewer.analyze_diff(SAMPLE_DIFF)
        secret = next(f for f in findings if f.rule_id == "secret.openai-key")
        self.assertEqual(secret.path, "src/auth.py")
        self.assertEqual(secret.line_no, 3)

    def test_binary_and_lockfiles_skipped(self):
        diff = (
            "diff --git a/logo.png b/logo.png\n"
            "--- a/logo.png\n"
            "+++ b/logo.png\n"
            "@@ -1,1 +1,2 @@\n"
            "+binaryblob\n"
            "diff --git a/package-lock.json b/package-lock.json\n"
            "--- a/package-lock.json\n"
            "+++ b/package-lock.json\n"
            "@@ -1,1 +1,2 @@\n"
            '+{"api_key": "sk-shouldnotbeflagged"}\n'
        )
        _, findings = reviewer.analyze_diff(diff)
        self.assertEqual(findings, [])

    def test_findings_sorted_by_severity(self):
        _, findings = reviewer.analyze_diff(SAMPLE_DIFF)
        ranks = [rules.severity_rank(f.severity) for f in findings]
        self.assertEqual(ranks, sorted(ranks))

    def test_max_files_limit_respected(self):
        parts = []
        for i in range(5):
            parts.append(
                f"diff --git a/f{i}.py b/f{i}.py\n"
                f"--- a/f{i}.py\n"
                f"+++ b/f{i}.py\n"
                "@@ -1,1 +1,2 @@\n"
                "+x = eval(input())\n"
            )
        summary, findings = reviewer.analyze_diff("".join(parts), max_files=2)
        self.assertEqual(summary.file_count, 5)
        self.assertEqual(len({f.path for f in findings}), 2)


class TestRules(unittest.TestCase):

    def _matches(self, rule_id, line):
        rule = next(r for r in rules.RULES if r.id == rule_id)
        import re
        return bool(re.search(rule.pattern, line.strip()))

    def test_no_false_positive_on_similar_names(self):
        self.assertFalse(self._matches("danger.eval", "self.evaluate(x)"))
        self.assertFalse(self._matches("danger.eval", "evaluate = 1"))
        self.assertFalse(self._matches("cleanup.debug-print", "sprint('x')"))
        self.assertFalse(self._matches("cleanup.debug-print", "printf('x')"))

    def test_true_positives(self):
        self.assertTrue(self._matches("danger.eval", "eval(user_input)"))
        self.assertTrue(self._matches("error.bare-except-pass", "except: pass"))
        self.assertTrue(self._matches("danger.shell-true", "subprocess.run(c, shell=True)"))
        self.assertTrue(self._matches("cleanup.breakpoint", "breakpoint()"))

    def test_test_paths_excluded_for_secrets(self):
        rule = next(r for r in rules.RULES if r.id == "secret.openai-key")
        self.assertFalse(rule.applies_to("tests/test_auth.py"))
        self.assertFalse(rule.applies_to("src/__tests__/a.ts"))
        self.assertTrue(rule.applies_to("src/auth.py"))

    def test_skip_path(self):
        self.assertTrue(rules.skip_path("package-lock.json"))
        self.assertTrue(rules.skip_path("assets/logo.png"))
        self.assertFalse(rules.skip_path("src/main.py"))

    def test_env_file_path_rule(self):
        diff = (
            "diff --git a/.env b/.env\n"
            "--- a/.env\n"
            "+++ b/.env\n"
            "@@ -0,0 +1,1 @@\n"
            "+TOKEN=abc\n"
        )
        _, findings = reviewer.analyze_diff(diff)
        self.assertIn("secret.env-file", {f.rule_id for f in findings})


class TestGitSafety(unittest.TestCase):
    """Only read-only subcommands may ever be executed."""

    def test_rejects_write_subcommands(self):
        for bad in ("push", "commit", "reset", "checkout", "clean", "rm"):
            with self.assertRaises(reviewer.ReviewError):
                reviewer.run_git([bad], os.getcwd())

    def test_rejects_empty_args(self):
        with self.assertRaises(reviewer.ReviewError):
            reviewer.run_git([], os.getcwd())

    def test_rejects_missing_directory(self):
        with self.assertRaises(reviewer.ReviewError):
            reviewer.run_git(["status"], os.path.join(os.getcwd(), "no-such-dir-xyz"))

    def test_non_repo_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(reviewer.ReviewError):
                reviewer.review_diff(tmp)


@unittest.skipUnless(
    __import__("shutil").which("git"),
    "git not available",
)
class TestEndToEndGit(unittest.TestCase):
    """Create a real temporary repository and review it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("checkout", "-b", "main")

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, *args):
        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            check=True,
        )

    def _write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path) or self.repo, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_reviews_uncommitted_change(self):
        self._write("app.py", "print('hello')\n")
        self._git("add", ".")
        self._git("commit", "-m", "init")

        self._write("app.py", "print('hello')\npassword = 'hunter22'\n")
        summary, findings = reviewer.review_diff(self.repo)
        self.assertGreaterEqual(summary.file_count, 1)
        self.assertTrue(findings)
        self.assertIn("secret.generic-assignment", {f.rule_id for f in findings})

    def test_reviews_rev_range(self):
        self._write("app.py", "x = 1\n")
        self._git("add", ".")
        self._git("commit", "-m", "base")
        self._write("app.py", "x = 1\ny = eval(input())\n")
        self._git("add", ".")
        self._git("commit", "-m", "add eval")

        _, findings = reviewer.review_diff(self.repo, "HEAD~1..HEAD")
        self.assertIn("danger.eval", {f.rule_id for f in findings})

    def test_staged_only(self):
        self._write("app.py", "x = 1\n")
        self._git("add", ".")
        self._git("commit", "-m", "base")
        self._write("app.py", "x = 1\nimport pickle\npickle.loads(b'')\n")
        self._git("add", "app.py")

        _, findings = reviewer.review_diff(self.repo, staged=True)
        self.assertIn("danger.pickle-load", {f.rule_id for f in findings})

    def test_ignore_patterns(self):
        self._write("vendor/lib.py", "x = 1\n")
        self._git("add", ".")
        self._git("commit", "-m", "init")
        self._write("vendor/lib.py", "x = 1\neval(y)\n")

        _, all_findings = reviewer.review_diff(self.repo)
        self.assertTrue(all_findings)

        _, filtered = reviewer.review_diff(
            self.repo, ignore_patterns=[r"^vendor/"],
        )
        self.assertEqual(filtered, [])

    def test_review_context_contains_added_lines(self):
        self._write("app.py", "x = 1\n")
        self._git("add", ".")
        self._git("commit", "-m", "init")
        self._write("app.py", "x = 1\n# MARKER_LINE\n")

        ctx = reviewer.review_context(self.repo)
        self.assertIn("MARKER_LINE", ctx)
        self.assertIn("app.py", ctx)


class TestMultilineDetection(unittest.TestCase):
    """Regression: the most common defects span two lines."""

    def _diff(self, body):
        lines = body.splitlines()
        hunk = f"@@ -0,0 +1,{len(lines)} @@\n"
        added = "".join(f"+{ln}\n" for ln in lines)
        return (
            "diff --git a/m.py b/m.py\n"
            "--- a/m.py\n"
            "+++ b/m.py\n"
            f"{hunk}{added}"
        )

    def test_except_then_pass_on_next_line(self):
        diff = self._diff(
            "def f():\n"
            "    try:\n"
            "        do()\n"
            "    except:\n"
            "        pass\n"
        )
        _, findings = reviewer.analyze_diff(diff)
        ids = [f.rule_id for f in findings]
        self.assertIn("error.bare-except-pass", ids)
        hit = next(f for f in findings if f.rule_id == "error.bare-except-pass")
        self.assertEqual(hit.line_no, 4)  # the `except:` line

    def test_except_with_logging_is_not_flagged(self):
        diff = self._diff(
            "def f():\n"
            "    try:\n"
            "        do()\n"
            "    except ValueError as exc:\n"
            "        logger.warning('failed: %s', exc)\n"
        )
        _, findings = reviewer.analyze_diff(diff)
        self.assertNotIn(
            "error.bare-except-pass", [f.rule_id for f in findings],
        )

    def test_blank_line_between_except_and_pass(self):
        diff = self._diff(
            "try:\n"
            "    do()\n"
            "except Exception:\n"
            "\n"
            "    pass\n"
        )
        _, findings = reviewer.analyze_diff(diff)
        self.assertIn(
            "error.bare-except-pass", [f.rule_id for f in findings],
        )

    def test_sql_fstring_detected(self):
        diff = self._diff('cursor.execute(f"SELECT * FROM {table}")\n')
        _, findings = reviewer.analyze_diff(diff)
        self.assertIn("danger.sql-concat", [f.rule_id for f in findings])

    def test_sql_concatenation_detected(self):
        diff = self._diff(
            'cur.execute("SELECT * FROM t WHERE n = " + name)\n',
        )
        _, findings = reviewer.analyze_diff(diff)
        self.assertIn("danger.sql-concat", [f.rule_id for f in findings])


class TestSqlDetection(unittest.TestCase):
    """SQL injection detection must never flag the *correct* form."""

    def _diff(self, line):
        return (
            "diff --git a/m.py b/m.py\n"
            "--- a/m.py\n"
            "+++ b/m.py\n"
            "@@ -0,0 +1,1 @@\n"
            f"+{line}\n"
        )

    def _flagged(self, line):
        _, findings = reviewer.analyze_diff(self._diff(line))
        return "danger.sql-concat" in {f.rule_id for f in findings}

    # --- must be flagged -------------------------------------------------
    def test_concatenation_with_quotes_flagged(self):
        self.assertTrue(
            self._flagged(
                "cur.execute(\"SELECT * FROM users WHERE name = '\" + name + \"'\")",
            ),
        )

    def test_fstring_flagged(self):
        self.assertTrue(
            self._flagged('cursor.execute(f"SELECT * FROM {table}")'),
        )

    def test_percent_format_flagged(self):
        self.assertTrue(
            self._flagged('cur.execute("SELECT * FROM t WHERE id = %s" % uid)'),
        )

    def test_dot_format_flagged(self):
        self.assertTrue(
            self._flagged('cur.execute("SELECT * FROM t".format(1))'),
        )

    def test_literal_concatenation_flagged(self):
        self.assertTrue(
            self._flagged('cur.execute("SELECT" + " * FROM t")'),
        )

    # --- must NOT be flagged --------------------------------------------
    def test_parameterised_percent_s_not_flagged(self):
        self.assertFalse(
            self._flagged(
                'cur.execute("SELECT * FROM users WHERE name = %s", (name,))',
            ),
        )

    def test_parameterised_qmark_not_flagged(self):
        self.assertFalse(
            self._flagged('cur.execute("SELECT * FROM users WHERE id = ?", (uid,))'),
        )

    def test_variables_only_not_flagged(self):
        self.assertFalse(self._flagged("cur.execute(sql, params)"))

    def test_non_sql_string_not_flagged(self):
        self.assertFalse(self._flagged('cache.set("user:" + uid, data)'))

    def test_orm_query_not_flagged(self):
        self.assertFalse(self._flagged("db.query(User).filter(User.id == 1)"))


class TestRendering(unittest.TestCase):

    def test_report_when_clean(self):
        summary = reviewer.DiffSummary()
        out = reviewer.render_report(summary, [], language="zh")
        self.assertIn("未发现规则问题", out)

    def test_report_english_when_requested(self):
        summary = reviewer.DiffSummary()
        out = reviewer.render_report(summary, [], language="en")
        self.assertIn("No rule violations", out)

    def test_report_is_always_full_unicode(self):
        """Language C: the report is UTF-8 regardless of the console.

        A GBK terminal must not strip Chinese out of the generated text;
        degradation happens only at print time via safe_for_console().
        """
        import io

        original = sys.stdout
        try:
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
            out = reviewer.render_report(
                reviewer.DiffSummary(), [], language="zh",
            )
        finally:
            sys.stdout = original
        self.assertIn("未发现规则问题", out)

    def test_resolve_language_prefers_explicit_value(self):
        self.assertEqual(reviewer.resolve_language("zh"), "zh")
        self.assertEqual(reviewer.resolve_language("en"), "en")

    def test_resolve_language_honours_env(self):
        original = os.environ.get("QWENPAW_LANGUAGE")
        try:
            os.environ["QWENPAW_LANGUAGE"] = "zh-CN"
            self.assertEqual(reviewer.resolve_language("auto"), "zh")
            os.environ["QWENPAW_LANGUAGE"] = "en-US"
            self.assertEqual(reviewer.resolve_language("auto"), "en")
        finally:
            if original is None:
                os.environ.pop("QWENPAW_LANGUAGE", None)
            else:
                os.environ["QWENPAW_LANGUAGE"] = original

    def test_safe_for_console_never_raises(self):
        """Printing on a legacy code page must degrade, not crash."""
        out = reviewer.safe_for_console("🔴 中文报告 with emoji", encoding="gbk")
        self.assertIsInstance(out, str)
        out.encode("gbk")  # must be encodable now
        self.assertIn("中文报告", out)
        self.assertIn("with emoji", out)

    def test_safe_for_console_is_identity_for_utf8(self):
        text = "🔴 中文报告"
        self.assertEqual(
            reviewer.safe_for_console(text, encoding="utf-8"), text,
        )

    def test_report_groups_by_severity(self):
        summary, findings = reviewer.analyze_diff(SAMPLE_DIFF)
        out = reviewer.render_report(
            summary, findings, scope="HEAD", language="en",
        )
        self.assertIn("Critical", out)
        self.assertIn("src/auth.py", out)

    def test_summarize_counts(self):
        summary, findings = reviewer.analyze_diff(SAMPLE_DIFF)
        stats = reviewer.summarize(summary, findings)
        self.assertEqual(stats["findings_total"], len(findings))
        self.assertEqual(
            sum(stats["findings_by_severity"].values()),
            len(findings),
        )

    def test_report_survives_legacy_codepage_via_helper(self):
        """Regression: emoji/CJK must not crash a GBK terminal.

        Language C: the report itself stays full Unicode, so the fix is
        that `safe_for_console()` makes it printable anywhere.
        """
        summary, findings = reviewer.analyze_diff(SAMPLE_DIFF)
        out = reviewer.render_report(
            summary, findings, scope="HEAD", language="zh",
        )
        # Explicitly target GBK, as a legacy Windows console would.
        printable = reviewer.safe_for_console(out, encoding="gbk")
        printable.encode("gbk")  # must not raise
        self.assertIn("Critical", printable)  # readable text survives

    def test_severity_label_ascii_variant(self):
        import rules as rules_mod

        self.assertEqual(
            rules_mod.severity_label("critical", ascii_only=True),
            "[CRITICAL]",
        )
        self.assertIn("critical", rules_mod.severity_label("critical").lower())

    def test_findings_dict_is_json_shaped(self):
        import json
        _, findings = reviewer.analyze_diff(SAMPLE_DIFF)
        payload = json.dumps([f.to_dict() for f in findings])
        self.assertIn("src/auth.py", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
