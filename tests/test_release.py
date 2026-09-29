# -*- coding: utf-8 -*-
"""Unit tests for the release publisher (``.github/release.py``).

These tests never touch the network. They cover the logic that shipped a
broken release once: the JSON body was encoded with the Windows ANSI
code page, turning every Chinese character into ``?``.

The tests therefore focus on three things:

1. ``encode_body`` always produces UTF-8 bytes, on any locale.
2. ``collect_problems`` catches BOMs, ``????`` corruption, version
   drift and author drift.
3. The HTTP layer is handed UTF-8 bytes and a ``charset=utf-8`` header.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_PY = REPO_ROOT / ".github" / "release.py"

# The file lives in .github/ (not a package), so load it by path.
_spec = importlib.util.spec_from_file_location("crc_release", RELEASE_PY)
release = importlib.util.module_from_spec(_spec)
sys.modules["crc_release"] = release
_spec.loader.exec_module(release)


CJK_NOTES = "# 代码评审插件\n\n## 功能\n\n- 只读评审 🔴\n"


def _write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.write_bytes(text.encode(encoding))


def _manifest(version="1.0.0", author="hxx0611", pid="code-review-copilot"):
    return json.dumps(
        {
            "id": pid,
            "name": "Code Review Copilot",
            "version": version,
            "author": author,
            "entry": {"backend": "plugin.py"},
        },
        ensure_ascii=False,
    )


class TestEncoding(unittest.TestCase):
    """The regression that shipped v1.0.0 with mojibake."""

    def test_encode_body_is_utf8_bytes(self):
        payload = {"body": "中文测试 🔴"}
        data = release.encode_body(payload)
        self.assertIsInstance(data, bytes)
        # Must be valid UTF-8 and keep the characters intact.
        self.assertEqual(
            json.loads(data.decode("utf-8"))["body"], "中文测试 🔴",
        )

    def test_encode_body_does_not_escape_unicode(self):
        """ensure_ascii=False keeps the payload readable, not \\uXXXX."""
        data = release.encode_body({"body": "中文"})
        self.assertIn("中文".encode("utf-8"), data)

    def test_round_trip_survives_non_ascii(self):
        notes = CJK_NOTES
        data = release.encode_body({"body": notes})
        self.assertEqual(json.loads(data.decode("utf-8"))["body"], notes)

    def test_encoding_is_locale_independent(self):
        """Even when the process locale is cp1252, bytes stay UTF-8.

        This is the exact failure mode that shipped v1.0.0 broken:
        PowerShell encoded the body with the ANSI code page.
        """
        import locale as _locale

        original = _locale.getpreferredencoding
        try:
            _locale.getpreferredencoding = lambda *a, **k: "cp1252"
            data = release.encode_body({"body": "中文"})
            self.assertEqual(
                json.loads(data.decode("utf-8"))["body"], "中文",
            )
            self.assertNotIn(b"?", data)
        finally:
            _locale.getpreferredencoding = original


class TestCollectProblems(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.notes_path = self.dir / "v1.0.0.md"
        self.manifest_path = self.dir / "plugin.json"
        self._write_notes(CJK_NOTES)
        self.manifest_path.write_text(_manifest(), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_notes(self, text, encoding="utf-8"):
        self.notes_path.write_bytes(text.encode(encoding))

    def _problems(self, version="1.0.0", notes=CJK_NOTES):
        return release.collect_problems(
            version,
            notes,
            notes_path=self.notes_path,
            manifest_path=self.manifest_path,
        )

    def test_clean_input_has_no_problems(self):
        self.assertEqual(self._problems(), [])

    def test_detects_bom(self):
        self.notes_path.write_bytes(
            b"\xef\xbb\xbf" + CJK_NOTES.encode("utf-8"),
        )
        problems = self._problems()
        self.assertTrue(
            any("BOM" in p for p in problems), problems,
        )

    def test_detects_mojibake(self):
        broken = "# ????\n\n## ??\n"
        problems = self._problems(notes=broken)
        self.assertTrue(
            any("????" in p for p in problems), problems,
        )

    def test_detects_version_mismatch(self):
        problems = self._problems(version="2.0.0")
        self.assertTrue(
            any("--version" in p for p in problems), problems,
        )

    def test_detects_non_semver(self):
        self.manifest_path.write_text(
            _manifest(version="1.0"), encoding="utf-8",
        )
        problems = self._problems(version="1.0")
        self.assertTrue(
            any("semver" in p for p in problems), problems,
        )

    def test_detects_author_drift(self):
        self.manifest_path.write_text(
            _manifest(author="snooze"), encoding="utf-8",
        )
        problems = self._problems()
        self.assertTrue(
            any("author" in p for p in problems), problems,
        )

    def test_detects_id_drift(self):
        self.manifest_path.write_text(
            _manifest(pid="other-plugin"), encoding="utf-8",
        )
        problems = self._problems()
        self.assertTrue(any("id" in p for p in problems), problems)

    def test_missing_notes_file_is_tolerated_when_text_supplied(self):
        """collect_problems works on text; file presence is load_notes' job."""
        missing = self.dir / "v9.9.9.md"
        problems = release.collect_problems(
            "1.0.0",
            CJK_NOTES,
            notes_path=missing,
            manifest_path=self.manifest_path,
        )
        self.assertEqual(problems, [])


class TestLoadNotes(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_loads_utf8_notes(self):
        (self.dir / "v1.0.0.md").write_bytes(CJK_NOTES.encode("utf-8"))
        text = release.load_notes("1.0.0", notes_dir=self.dir)
        self.assertEqual(text, CJK_NOTES)

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            release.load_notes("9.9.9", notes_dir=self.dir)
        self.assertIn("not found", str(ctx.exception))

    def test_real_release_notes_are_valid(self):
        """The notes committed for v1.0.0 must pass validation."""
        text = release.load_notes("1.0.0")
        self.assertTrue(release.CJK_RE.search(text))
        self.assertNotIn("????", text)
        raw = (REPO_ROOT / ".github" / "release-notes" / "v1.0.0.md").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))


class TestApiEncoding(unittest.TestCase):
    """Verify the bytes actually handed to urllib are UTF-8."""

    def setUp(self):
        self.captured = {}
        self._orig = release.urllib.request.urlopen

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=60):
            self.captured["data"] = req.data
            self.captured["headers"] = {
                k.lower(): v for k, v in req.header_items()
            }
            self.captured["method"] = req.get_method()
            return _Resp({"id": 1, "body": "ok", "html_url": "u"})

        release.urllib.request.urlopen = fake_urlopen

    def tearDown(self):
        release.urllib.request.urlopen = self._orig

    def test_body_is_sent_as_utf8_bytes(self):
        notes = "中文 release notes 🔴"
        release.api(
            "POST", "https://api.github.com/x", "tok", {"body": notes},
        )
        data = self.captured["data"]
        self.assertIsInstance(data, bytes)
        self.assertEqual(
            json.loads(data.decode("utf-8"))["body"], notes,
        )
        self.assertNotIn(b"\\u4e2d", data)  # not escaped

    def test_content_type_declares_utf8(self):
        release.api(
            "POST", "https://api.github.com/x", "tok", {"body": "中文"},
        )
        ctype = self.captured["headers"]["content-type"]
        self.assertIn("charset=utf-8", ctype.lower())

    def test_get_sends_no_body(self):
        release.api("GET", "https://api.github.com/x", "tok")
        self.assertIsNone(self.captured["data"])

    def test_authorization_header_present(self):
        release.api("GET", "https://api.github.com/x", "secret-token")
        self.assertEqual(
            self.captured["headers"]["authorization"], "Bearer secret-token",
        )


class TestCliSurface(unittest.TestCase):
    """The CLI contract other tooling depends on."""

    def setUp(self):
        self._orig_argv = sys.argv

    def tearDown(self):
        sys.argv = self._orig_argv

    def _run(self, *args):
        """Invoke main() with argv; return (exit_code, stdout).

        ``main`` raises ``SystemExit`` whose payload is either an int
        (argparse / sys.exit(1)) or a message string (raised with a
        message). Normalise both into an int plus captured output.
        """
        sys.argv = ["release.py", *args]
        buf = io.StringIO()
        err = io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf, err
        try:
            try:
                code = release.main()
            except SystemExit as exc:
                code = exc.code
        finally:
            sys.stdout, sys.stderr = old_out, old_err

        output = buf.getvalue() + err.getvalue()
        if code is None:
            code = 0
        elif not isinstance(code, int):
            # Raised with a message: that is a failure by convention.
            output += str(code)
            code = 1
        return code, output

    def test_check_mode_publishes_nothing(self):
        code, out = self._run("--version", "1.0.0", "--check")
        self.assertEqual(code, 0)
        self.assertIn("All checks passed", out)
        self.assertIn("nothing was published", out)

    def test_check_reports_cjk_status(self):
        _, out = self._run("--version", "1.0.0", "--check")
        self.assertIn("notes CJK   : yes", out)

    def test_missing_version_flag_fails(self):
        code, out = self._run()
        self.assertNotEqual(code, 0)
        self.assertIn("--version", out)

    def test_version_flag_accepts_leading_v(self):
        code, out = self._run("--version", "v1.0.0", "--check")
        self.assertEqual(code, 0)
        self.assertIn("v1.0.0", out)

    def test_publish_without_token_exits(self):
        original = os.environ.pop("GITHUB_TOKEN", None)
        had_gh = os.environ.pop("GH_TOKEN", None)
        try:
            code, out = self._run("--version", "1.0.0")
        finally:
            if original:
                os.environ["GITHUB_TOKEN"] = original
            if had_gh:
                os.environ["GH_TOKEN"] = had_gh
        self.assertEqual(code, 1)
        self.assertIn("GITHUB_TOKEN is not set", out)

    def test_unknown_version_notes_exit(self):
        code, out = self._run("--version", "9.9.9", "--check")
        self.assertEqual(code, 1)
        self.assertIn("not found", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
