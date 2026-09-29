# -*- coding: utf-8 -*-
"""Review engine for code-review-copilot.

Pure-logic module: it shells out to **read-only** git commands, parses
the unified diff, applies the rule set, and renders a structured report.

Two consumers:

* :func:`review_diff` — deterministic rule hit list (for humans).
* :func:`review_context` — compact context bundle for the *agent* to run
  AI semantic review on top of (see the plugin's tool docstrings).

Security notes
--------------
* Only a hard-coded whitelist of read-only git subcommands is executed.
* Arguments are always passed as a list (never `shell=True`), so user
  input cannot be interpolated into a shell command.
* No network access, no writes to the repository.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from rules import (
    PATH_RULES,
    Rule,
    iter_rules,
    severity_label,
    severity_rank,
    skip_path,
)

# --------------------------------------------------------------------------
# Constants / limits
# --------------------------------------------------------------------------

GIT_TIMEOUT_SECONDS = 30
MAX_DIFF_BYTES = 2 * 1024 * 1024          # 2 MiB of diff text
DEFAULT_MAX_FILES = 50
MAX_LINE_LENGTH_FOR_SCAN = 500            # skip minified / generated lines
LARGE_FILE_ADDED_LINES = 800              # path-rule threshold (info)

#: Read-only git subcommands this engine is allowed to run.
ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {"diff", "log", "show", "status", "rev-parse", "ls-files", "rev-list"},
)


class ReviewError(RuntimeError):
    """Raised for expected, user-facing failures (bad repo, no git…)."""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Finding:
    """A single rule hit."""

    rule_id: str
    severity: str
    title: str
    path: str
    line_no: int | None
    snippet: str
    message: str
    suggestion: str = ""

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "title": self.title,
            "path": self.path,
            "line": self.line_no,
            "snippet": self.snippet,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class FileChange:
    """One changed file inside a diff."""

    path: str
    added_lines: list[tuple[int, str]] = field(default_factory=list)
    removed_count: int = 0
    is_new: bool = False
    is_deleted: bool = False

    @property
    def added_count(self) -> int:
        return len(self.added_lines)


@dataclass
class DiffSummary:
    """Aggregate stats over a parsed diff."""

    files: list[FileChange] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0

    @property
    def file_count(self) -> int:
        return len(self.files)


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------


def _git_available() -> bool:
    return shutil.which("git") is not None


def run_git(
    args: Sequence[str],
    repo_dir: str,
    *,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> str:
    """Run a read-only git command and return stdout.

    Args:
        args: Arguments *after* ``git`` (e.g. ``["diff", "HEAD"]``).
        repo_dir: Repository working directory.
        timeout: Seconds before the call is aborted.

    Raises:
        ReviewError: On disallowed subcommand, missing git, timeout, or a
            non-zero exit from git.
    """
    if not args:
        raise ReviewError("No git arguments supplied.")
    sub = args[0]
    if sub not in ALLOWED_GIT_SUBCOMMANDS:
        raise ReviewError(
            f"Refusing to run non-read-only git subcommand: {sub!r}. "
            f"Allowed: {', '.join(sorted(ALLOWED_GIT_SUBCOMMANDS))}.",
        )
    if not _git_available():
        raise ReviewError(
            "git was not found on PATH. Install git or pass a repository "
            "that you can inspect another way.",
        )
    if not os.path.isdir(repo_dir):
        raise ReviewError(f"Directory does not exist: {repo_dir}")

    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(
            f"git {sub} timed out after {timeout}s.",
        ) from exc
    except OSError as exc:  # pragma: no cover - environment dependent
        raise ReviewError(f"Failed to run git {sub}: {exc}") from exc

    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise ReviewError(
            f"git {sub} failed (exit {proc.returncode}): {err or 'unknown error'}",
        )

    return (proc.stdout or b"").decode("utf-8", errors="replace")


def _is_git_repo(repo_dir: str) -> bool:
    try:
        out = run_git(["rev-parse", "--is-inside-work-tree"], repo_dir)
    except ReviewError:
        return False
    return out.strip() == "true"


# --------------------------------------------------------------------------
# Diff parsing
# --------------------------------------------------------------------------

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_NEW_FILE_RE = re.compile(r"^new file mode")
_DELETED_FILE_RE = re.compile(r"^deleted file mode")


def parse_unified_diff(diff_text: str) -> DiffSummary:
    """Parse a unified diff into per-file added lines.

    Tracks the *new* file line number so findings can point at the line
    a reviewer will actually see.

    Args:
        diff_text: Output of ``git diff`` (or ``git show``).

    Returns:
        A :class:`DiffSummary` with added lines and stats.
    """
    summary = DiffSummary()
    current: FileChange | None = None
    new_line_no = 0

    for raw in diff_text.splitlines():
        header = _DIFF_HEADER_RE.match(raw)
        if header:
            current = FileChange(path=header.group(2))
            summary.files.append(current)
            new_line_no = 0
            continue

        if current is None:
            continue

        if _NEW_FILE_RE.match(raw):
            current.is_new = True
            continue
        if _DELETED_FILE_RE.match(raw):
            current.is_deleted = True
            continue

        hunk = _HUNK_RE.match(raw)
        if hunk:
            new_line_no = int(hunk.group(1))
            continue

        if raw.startswith("+++") or raw.startswith("---"):
            continue

        if raw.startswith("+"):
            content = raw[1:]
            current.added_lines.append((new_line_no, content))
            summary.additions += 1
            new_line_no += 1
        elif raw.startswith("-"):
            current.removed_count += 1
            summary.deletions += 1
        elif raw.startswith(" "):
            new_line_no += 1

    return summary


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


def _scan_file(change: FileChange, rule_list: Iterable[Rule]) -> list[Finding]:
    """Apply content rules to one file's added lines."""
    findings: list[Finding] = []
    applicable = [r for r in rule_list if r.applies_to(change.path)]
    compiled = [(r, r.compiled()) for r in applicable]

    for line_no, content in change.added_lines:
        stripped = content.strip()
        if not stripped or len(stripped) > MAX_LINE_LENGTH_FOR_SCAN:
            continue
        for rule, pattern in compiled:
            if pattern.search(stripped):
                findings.append(
                    Finding(
                        rule_id=rule.id,
                        severity=rule.severity,
                        title=rule.title,
                        path=change.path,
                        line_no=line_no,
                        snippet=stripped[:200],
                        message=rule.message,
                        suggestion=rule.suggestion,
                    ),
                )

    findings.extend(_scan_multiline(change))
    return findings


#: ``except ...:`` with nothing after the colon (body on the next line).
_BARE_EXCEPT_RE = re.compile(r"^except\b[^:]*:\s*(#.*)?$")
#: A block that swallows the error entirely.
_SWALLOW_BODY_RE = re.compile(r"^(pass|\.\.\.|return\s+None)\s*$")
#: An ``except ...:`` that already has a body on the same line.
_INLINE_EXCEPT_PASS_RE = re.compile(r"^except\b[^:]*:\s*(pass|\.\.\.)\s*$")


def _scan_multiline(change: FileChange) -> list[Finding]:
    """Detect patterns that span lines within the *added* lines.

    The single-line rule pass cannot see ``except:\\n    pass``, which is
    one of the most common real-world defects. This walks consecutive
    added lines (skipping blanks and comments) and reports the ``except``
    line as the location.
    """
    if not change.path.endswith(".py"):
        return []

    findings: list[Finding] = []
    lines = change.added_lines
    for idx, (line_no, content) in enumerate(lines):
        stripped = content.strip()
        if _INLINE_EXCEPT_PASS_RE.match(stripped):
            findings.append(
                Finding(
                    rule_id="error.bare-except-pass",
                    severity="major",
                    title="Exception silently swallowed",
                    path=change.path,
                    line_no=line_no,
                    snippet=stripped[:200],
                    message=(
                        "A bare `except: pass` hides failures and makes bugs "
                        "very hard to diagnose."
                    ),
                    suggestion=(
                        "Catch the specific exception and log it, or re-raise "
                        "with context."
                    ),
                ),
            )
            continue

        if not _BARE_EXCEPT_RE.match(stripped):
            continue

        # Look ahead for the first meaningful body line.
        for _, follow in lines[idx + 1:]:
            follow_stripped = follow.strip()
            if not follow_stripped or follow_stripped.startswith("#"):
                continue
            if _SWALLOW_BODY_RE.match(follow_stripped):
                findings.append(
                    Finding(
                        rule_id="error.bare-except-pass",
                        severity="major",
                        title="Exception silently swallowed",
                        path=change.path,
                        line_no=line_no,
                        snippet=f"{stripped} / {follow_stripped}",
                        message=(
                            "This handler discards the exception, hiding the "
                            "real failure."
                        ),
                        suggestion=(
                            "Log the exception (with traceback) or handle it "
                            "explicitly; avoid `pass` in an except block."
                        ),
                    ),
                )
            break

    findings.extend(_scan_sql(change))
    return findings


# --------------------------------------------------------------------------
# SQL injection heuristic
# --------------------------------------------------------------------------

_SQL_CALL_RE = re.compile(
    r"(?i)\b(execute|executemany|query|raw)\s*\(\s*(?P<arg>.+)$",
)
_SQL_KEYWORD_RE = re.compile(
    r"(?i)\b(select|insert|update|delete|replace|with)\b.*\b(from|into|set|values|where)\b",
)
_STRING_LITERAL_RE = re.compile(r"^[rbfu]{0,2}('''|\"\"\"|'|\")")
_SAFE_PLACEHOLDER_RE = re.compile(r"%s|\?|:\w+|\$\d+")


def _scan_sql(change: FileChange) -> list[Finding]:
    """Flag SQL that is assembled dynamically instead of parameterised.

    Deliberately conservative: it only fires when a string literal that
    looks like SQL is concatenated, interpolated, or ``%``-formatted.
    The correct parameterised form ``execute("... %s", (x,))`` is **not**
    flagged, because the SQL string there is static.
    """
    if not change.path.endswith((".py", ".js", ".ts")):
        return []

    findings: list[Finding] = []
    for line_no, content in change.added_lines:
        stripped = content.strip()
        if len(stripped) > MAX_LINE_LENGTH_FOR_SCAN:
            continue
        match = _SQL_CALL_RE.search(stripped)
        if not match:
            continue
        arg = match.group("arg")
        if not _SQL_KEYWORD_RE.search(arg):
            continue

        dynamic = False
        reason = ""

        # f-string / template literal interpolation directly in the call.
        if re.search(r"(?i)\b(f|rf|fr)('''|\"\"\"|'|\")", arg):
            dynamic, reason = True, "f-string interpolation"
        # Implicit/explicit concatenation of a string with anything else.
        elif re.search(r"[\"']\s*\+", arg) or re.search(r"\+\s*[\"']", arg):
            dynamic, reason = True, "string concatenation"
        # "..." % value  (but NOT a bare "%s" placeholder + params tuple)
        elif re.search(r"[\"']\s*%\s*[^s%\s]", arg) and not re.search(
            r"[\"']\s*%\s*\(", arg,
        ):
            dynamic, reason = True, "% formatting"
        # "...".format(...)
        elif re.search(r"[\"']\s*\.format\s*\(", arg):
            dynamic, reason = True, ".format()"
        # JS template literal interpolation: `SELECT ... ${x}`
        elif "`" in arg and "${" in arg:
            dynamic, reason = True, "template literal interpolation"

        if not dynamic:
            continue

        # A fully parameterised call has a static SQL string plus a params
        # argument; only skip if the ONLY dynamics are safe placeholders.
        if _SAFE_PLACEHOLDER_RE.search(arg) and "?" in arg and "${" not in arg:
            if not re.search(r"\+|\.format\s*\(|\bformat\s*\(", arg):
                continue

        findings.append(
            Finding(
                rule_id="danger.sql-concat",
                severity="major",
                title="SQL built by string concatenation",
                path=change.path,
                line_no=line_no,
                snippet=stripped[:200],
                message=(
                    f"SQL is assembled dynamically ({reason}), which is "
                    "vulnerable to injection."
                ),
                suggestion=(
                    "Use parameterised queries, e.g. "
                    "`cursor.execute('SELECT * FROM t WHERE id = %s', (uid,))`."
                ),
            ),
        )

    return findings


def _scan_paths(change: FileChange) -> list[Finding]:
    """Apply path-based rules (e.g. committed .env)."""
    findings: list[Finding] = []
    for rule in PATH_RULES:
        if rule.id == "cleanup.large-file":
            if change.added_count >= LARGE_FILE_ADDED_LINES:
                findings.append(
                    Finding(
                        rule_id=rule.id,
                        severity=rule.severity,
                        title=rule.title,
                        path=change.path,
                        line_no=None,
                        snippet=f"{change.added_count} added lines",
                        message=rule.message,
                        suggestion=rule.suggestion,
                    ),
                )
            continue
        if re.search(rule.pattern, change.path):
            findings.append(
                Finding(
                    rule_id=rule.id,
                    severity=rule.severity,
                    title=rule.title,
                    path=change.path,
                    line_no=None,
                    snippet=change.path,
                    message=rule.message,
                    suggestion=rule.suggestion,
                ),
            )
    return findings


def analyze_diff(
    diff_text: str,
    *,
    max_files: int = DEFAULT_MAX_FILES,
) -> tuple[DiffSummary, list[Finding]]:
    """Parse *diff_text* and return ``(summary, findings)``.

    Files matching :func:`rules.skip_path` (binaries, lock files) are
    excluded before scanning but still counted in the summary so the
    report stays honest about the change size.
    """
    summary = parse_unified_diff(diff_text)
    findings: list[Finding] = []
    scanned = 0

    for change in summary.files:
        if scanned >= max_files:
            break
        if skip_path(change.path) or change.is_deleted:
            continue
        scanned += 1
        findings.extend(_scan_file(change, iter_rules()))
        findings.extend(_scan_paths(change))

    findings.sort(
        key=lambda f: (severity_rank(f.severity), f.path, f.line_no or 0),
    )
    return summary, findings


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


def review_diff(
    repo_dir: str,
    rev_range: str = "",
    *,
    staged: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    ignore_patterns: Sequence[str] = (),
) -> tuple[DiffSummary, list[Finding]]:
    """Review a git diff.

    Args:
        repo_dir: Path to the repository.
        rev_range: Optional revision range (e.g. ``"main..HEAD"``). When
            empty, reviews the working tree against HEAD.
        staged: When True and *rev_range* is empty, review staged changes
            (``--cached``) instead of the working tree.
        max_files: Maximum number of files to scan.
        ignore_patterns: Regexes; matching paths are excluded.

    Returns:
        ``(summary, findings)``.

    Raises:
        ReviewError: If the path is not a git repository or git fails.
    """
    if not _is_git_repo(repo_dir):
        raise ReviewError(
            f"Not a git repository: {repo_dir}. "
            "Run this tool inside a git working tree.",
        )

    args = ["diff", "--no-color", "--unified=0"]
    if rev_range:
        args.append(rev_range)
    elif staged:
        args.append("--cached")
    else:
        args.append("HEAD")

    diff_text = run_git(args, repo_dir)
    if len(diff_text) > MAX_DIFF_BYTES:
        diff_text = diff_text[:MAX_DIFF_BYTES]

    summary, findings = analyze_diff(diff_text, max_files=max_files)

    if ignore_patterns:
        compiled = [re.compile(p) for p in ignore_patterns]
        findings = [
            f for f in findings
            if not any(c.search(f.path) for c in compiled)
        ]
        summary.files = [
            fc for fc in summary.files
            if not any(c.search(fc.path) for c in compiled)
        ]

    return summary, findings


def review_context(
    repo_dir: str,
    rev_range: str = "",
    *,
    staged: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    max_chars_per_file: int = 4000,
) -> str:
    """Build a compact context bundle for **AI semantic review**.

    Returns the actual added lines (not just rule hits) so the agent can
    reason about logic, naming, and intent — the parts static rules
    cannot judge.

    Args:
        repo_dir: Path to the repository.
        rev_range: Optional revision range.
        staged: Review staged changes when *rev_range* is empty.
        max_files: Maximum files to include.
        max_chars_per_file: Per-file character budget for added lines.
    """
    if not _is_git_repo(repo_dir):
        raise ReviewError(f"Not a git repository: {repo_dir}")

    args = ["diff", "--no-color", "--unified=1"]
    if rev_range:
        args.append(rev_range)
    elif staged:
        args.append("--cached")
    else:
        args.append("HEAD")

    diff_text = run_git(args, repo_dir)
    if len(diff_text) > MAX_DIFF_BYTES:
        diff_text = diff_text[:MAX_DIFF_BYTES]

    summary = parse_unified_diff(diff_text)
    parts: list[str] = [
        f"# Review context\n",
        f"Repository: {repo_dir}",
        f"Scope: {rev_range or ('staged' if staged else 'working tree vs HEAD')}",
        f"Files changed: {summary.file_count}  "
        f"(+{summary.additions} / -{summary.deletions})\n",
    ]

    emitted = 0
    for change in summary.files:
        if emitted >= max_files:
            parts.append(
                f"\n… {summary.file_count - emitted} more file(s) omitted.",
            )
            break
        if change.is_deleted or not change.added_lines:
            continue
        emitted += 1
        parts.append(f"\n## {change.path}  (+{change.added_count})")
        if change.is_new:
            parts.append("(new file)")
        buf: list[str] = []
        used = 0
        for line_no, content in change.added_lines:
            entry = f"{line_no:>6} + {content}"
            if used + len(entry) > max_chars_per_file:
                buf.append("      … (truncated)")
                break
            buf.append(entry)
            used += len(entry)
        parts.extend(buf)

    return "\n".join(parts)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _stream_supports(text: str) -> bool:
    """Return True when *text* can be written to the active stdout."""
    try:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError, TypeError):
        return False
    return True


def resolve_language(preference: str = "auto") -> str:
    """Resolve the report language.

    The report is built as UTF-8 text regardless of this value; it only
    decides which language to *write*. Per project convention the agent's
    configured language wins, with the console encoding used purely as a
    last-resort fallback so nothing turns into mojibake.

    Args:
        preference: ``"zh"``, ``"en"``, or ``"auto"``.

    Returns:
        ``"zh"`` or ``"en"``.
    """
    if preference in ("zh", "en"):
        return preference

    # Honour the host configuration first (QwenPaw stores `language`).
    for env_var in ("QWENPAW_LANGUAGE", "LANG", "LC_ALL"):
        value = (os.environ.get(env_var) or "").lower()
        if value.startswith("zh"):
            return "zh"
        if value.startswith("en"):
            return "en"

    # Fall back to what the console can actually render.
    return "zh" if _stream_supports("代码评审") else "en"


def safe_for_console(text: str, encoding: str | None = None) -> str:
    """Return *text* that is guaranteed printable on the active console.

    Reports are generated as full UTF-8 Unicode. This helper is only for
    the moment they are written to a terminal that may use a legacy code
    page (GBK/cp1252), where CJK text or emoji would raise
    ``UnicodeEncodeError`` and kill the process.

    Characters the target encoding cannot represent are dropped or
    replaced individually, so the surrounding (readable) text survives.

    Args:
        text: The report text.
        encoding: Target codec; defaults to the active stdout encoding.

    Returns:
        A string that encodes cleanly in *encoding*.
    """
    enc = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        pass

    out_chars: list[str] = []
    for ch in text:
        try:
            ch.encode(enc)
        except (UnicodeEncodeError, LookupError):
            # Emoji and other symbols outside the code page become '?'.
            out_chars.append("?")
        else:
            out_chars.append(ch)
    return "".join(out_chars)


def render_report(
    summary: DiffSummary,
    findings: Sequence[Finding],
    *,
    scope: str = "",
    language: str = "auto",
) -> str:
    """Render a Markdown review report (always UTF-8 text).

    Args:
        summary: Parsed diff statistics.
        findings: Rule hits to report.
        scope: Human-readable description of what was reviewed.
        language: ``"zh"``, ``"en"``, or ``"auto"`` (default). See
            :func:`resolve_language`. The returned string is always
            proper Unicode; use :func:`safe_for_console` before writing
            it to a legacy terminal.
    """
    zh = resolve_language(language) == "zh"

    def t(zh_text: str, en_text: str) -> str:
        return zh_text if zh else en_text

    lines: list[str] = [
        t("# 代码评审报告", "# Code Review Report"),
        "",
    ]

    if scope:
        lines.append(t(f"**范围**: {scope}", f"**Scope**: {scope}"))
    lines.append(
        t(
            f"**变更**: {summary.file_count} 个文件, "
            f"+{summary.additions} / -{summary.deletions}",
            f"**Changes**: {summary.file_count} file(s), "
            f"+{summary.additions} / -{summary.deletions}",
        ),
    )
    lines.append("")

    if not findings:
        lines.append(
            t(
                "✅ 未发现规则问题。",
                "✅ No rule violations found.",
            ),
        )
        lines.append("")
        lines.append(
            t(
                "> 提示：静态规则无法判断逻辑正确性，"
                "建议用 `review_context` 让 AI 做语义复核。",
                "> Note: static rules cannot judge logic. "
                "Use `review_context` for an AI semantic pass.",
            ),
        )
        return "\n".join(lines)

    # Group by severity, preserving order.
    grouped: dict[str, list[Finding]] = {}
    for f in findings:
        grouped.setdefault(f.severity, []).append(f)

    lines.append(t("## 问题汇总", "## Summary"))
    lines.append("")
    lines.append(t("| 严重度 | 数量 |", "| Severity | Count |"))
    lines.append("| --- | --- |")
    for sev in ("critical", "major", "minor", "info"):
        if sev in grouped:
            lines.append(f"| {severity_label(sev)} | {len(grouped[sev])} |")
    lines.append("")

    for sev in ("critical", "major", "minor", "info"):
        items = grouped.get(sev)
        if not items:
            continue
        lines.append(f"## {severity_label(sev)} ({len(items)})")
        lines.append("")
        for f in items:
            loc = f"`{f.path}:{f.line_no}`" if f.line_no else f"`{f.path}`"
            lines.append(f"- **{f.title}** — {loc}")
            if f.snippet:
                lines.append(f"  ```\n  {f.snippet}\n  ```")
            lines.append(f"  {f.message}")
            if f.suggestion:
                lines.append(f"  → {f.suggestion}")
        lines.append("")

    return "\n".join(lines)


def summarize(summary: DiffSummary, findings: Sequence[Finding]) -> dict:
    """Return machine-readable counts (used in tool JSON output)."""
    counts = {sev: 0 for sev in ("critical", "major", "minor", "info")}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return {
        "files_changed": summary.file_count,
        "additions": summary.additions,
        "deletions": summary.deletions,
        "findings_total": len(findings),
        "findings_by_severity": counts,
    }


__all__ = [
    "ALLOWED_GIT_SUBCOMMANDS",
    "DiffSummary",
    "FileChange",
    "Finding",
    "ReviewError",
    "analyze_diff",
    "parse_unified_diff",
    "render_report",
    "resolve_language",
    "review_context",
    "review_diff",
    "run_git",
    "safe_for_console",
    "severity_label",
    "summarize",
]
