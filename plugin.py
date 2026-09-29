# -*- coding: utf-8 -*-
"""Code Review Copilot — plugin entry point.

Registers three read-only tools plus a ``/review`` control command:

* ``review_git_diff``   — rule-based review of pending changes
* ``review_rev_range``  — rule-based review of a commit range
* ``review_context``    — dump changed lines for an AI semantic pass
* ``/review``           — the same review, as a slash command

The heavy lifting lives in :mod:`reviewer` (pure logic, unit-tested
standalone). This module only adapts that engine to QwenPaw tools.
"""

from __future__ import annotations

import importlib.util
import logging
import os

from qwenpaw.plugins.api import PluginApi

logger = logging.getLogger(__name__)

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# Tool names — kept in one place so config lookups cannot drift.
TOOL_REVIEW_DIFF = "review_git_diff"
TOOL_REVIEW_RANGE = "review_rev_range"
TOOL_REVIEW_CONTEXT = "review_context"

COMMAND_NAME = "review"


def _load_reviewer():
    """Load ``reviewer.py`` from this plugin directory.

    Plugins are loaded under an isolated namespace, so a bare
    ``import reviewer`` is not guaranteed to resolve. Using an explicit
    file location keeps the plugin self-contained and avoids clashing
    with any module of the same name elsewhere.
    """
    module_name = "code_review_copilot_reviewer"
    existing = __import__("sys").modules.get(module_name)
    if existing is not None:
        return existing

    path = os.path.join(_PLUGIN_DIR, "reviewer.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"Cannot load reviewer module from {path}")

    module = importlib.util.module_from_spec(spec)
    # Make the sibling `rules` module importable from reviewer.py.
    import sys

    sys.modules[module_name] = module
    added_path = False
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
        added_path = True
    try:
        spec.loader.exec_module(module)
    finally:
        if added_path:
            try:
                sys.path.remove(_PLUGIN_DIR)
            except ValueError:  # pragma: no cover
                pass
    return module


def _resolve_repo_dir(repo_dir: str) -> str:
    """Resolve the repository path, defaulting to the agent workspace."""
    if repo_dir and repo_dir.strip():
        return os.path.abspath(os.path.expanduser(repo_dir.strip()))
    try:
        from qwenpaw.constant import WORKING_DIR

        return str(WORKING_DIR)
    except Exception:  # pragma: no cover - very old versions
        return os.getcwd()


def _language_preference() -> str:
    """Read the user's language preference, defaulting to auto."""
    try:
        from qwenpaw.config.config import load_agent_config
        from qwenpaw.app.agent_context import get_current_agent_id

        agent_id = get_current_agent_id()
        if agent_id:
            cfg = load_agent_config(agent_id)
            lang = getattr(cfg, "language", None)
            if lang:
                return "zh" if str(lang).lower().startswith("zh") else "en"
    except Exception:  # pragma: no cover - optional dependency
        pass
    return "auto"


def _chunk_success(text: str):
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.tool import ToolChunk

    return ToolChunk(
        state=ToolResultState.SUCCESS,
        content=[TextBlock(type="text", text=text)],
    )


def _chunk_error(text: str):
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.tool import ToolChunk

    return ToolChunk(
        state=ToolResultState.ERROR,
        content=[TextBlock(type="text", text=text)],
    )


def _run_review(
    repo_dir: str,
    rev_range: str,
    *,
    staged: bool,
    max_files: int,
    ignore_patterns: list[str],
) -> str:
    """Run the engine and return a rendered report (or an error text)."""
    reviewer = _load_reviewer()
    resolved = _resolve_repo_dir(repo_dir)

    try:
        summary, findings = reviewer.review_diff(
            resolved,
            rev_range,
            staged=staged,
            max_files=max_files,
            ignore_patterns=ignore_patterns,
        )
    except reviewer.ReviewError as exc:
        return f"ERROR: {exc}"

    scope = rev_range or ("staged changes" if staged else "working tree vs HEAD")
    return reviewer.render_report(
        summary,
        findings,
        scope=f"{scope} ({resolved})",
        language=_language_preference(),
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def review_git_diff(
    repo_dir: str = "",
    staged: bool = False,
    max_files: int = 50,
    ignore: str = "",
) -> object:
    """Review uncommitted code changes for common defects.

    Use this when the user asks to review their current work, check a
    diff before committing, or look for problems in pending changes.
    It runs read-only ``git diff`` and applies static rules.

    This tool covers: hardcoded secrets, dangerous calls (eval/exec,
    shell=True, pickle, SQL string building), swallowed exceptions,
    leftover debug output, weak hashes, and committed ``.env`` files.

    Static rules cannot judge logic or intent. If the user wants a
    deeper semantic review, call ``review_context`` afterwards and
    analyse the returned diff yourself.

    Args:
        repo_dir (str, optional):
            Path to the git repository. Leave empty to use the agent's
            current workspace.
        staged (bool, optional):
            When True, review staged changes (``git diff --cached``)
            instead of the whole working tree. Defaults to False.
        max_files (int, optional):
            Maximum number of changed files to scan. Defaults to 50.
        ignore (str, optional):
            Comma-separated regexes for paths to skip, e.g.
            ``"^vendor/,^third_party/"``. Defaults to empty.

    Returns:
        object: A ``ToolChunk`` containing a Markdown review report with
        findings grouped by severity, each with file:line and a fix
        suggestion.

    Example:
        >>> await review_git_diff()
        >>> await review_git_diff(staged=True, ignore="^vendor/")
    """
    patterns = [p.strip() for p in ignore.split(",") if p.strip()]
    report = _run_review(
        repo_dir, "", staged=staged,
        max_files=max_files, ignore_patterns=patterns,
    )
    return (
        _chunk_error(report)
        if report.startswith("ERROR:")
        else _chunk_success(report)
    )


async def review_rev_range(
    rev_range: str = "HEAD~1..HEAD",
    repo_dir: str = "",
    max_files: int = 50,
    ignore: str = "",
) -> object:
    """Review a commit or branch range, e.g. before merging a PR.

    Use this to review already-committed work such as ``main..HEAD``,
    a single commit, or a release range. Runs read-only ``git diff``
    between two revisions and applies the same static rules as
    ``review_git_diff``.

    Args:
        rev_range (str, optional):
            Revision range in git syntax, e.g. ``"HEAD~1..HEAD"``,
            ``"main..feature"``, or a single revision like ``"HEAD"``.
            Defaults to ``"HEAD~1..HEAD"``.
        repo_dir (str, optional):
            Path to the git repository. Empty uses the workspace.
        max_files (int, optional):
            Maximum number of changed files to scan. Defaults to 50.
        ignore (str, optional):
            Comma-separated regexes for paths to skip.

    Returns:
        object: A ``ToolChunk`` with the Markdown review report.

    Example:
        >>> await review_rev_range("main..HEAD")
        >>> await review_rev_range("HEAD~3..HEAD")
    """
    if not rev_range or not rev_range.strip():
        return _chunk_error(
            "ERROR: rev_range must not be empty. "
            "Use review_git_diff for uncommitted changes.",
        )
    patterns = [p.strip() for p in ignore.split(",") if p.strip()]
    report = _run_review(
        repo_dir, rev_range.strip(),
        staged=False, max_files=max_files, ignore_patterns=patterns,
    )
    return (
        _chunk_error(report)
        if report.startswith("ERROR:")
        else _chunk_success(report)
    )


async def review_context(
    rev_range: str = "",
    repo_dir: str = "",
    staged: bool = False,
    max_files: int = 30,
) -> object:
    """Fetch changed lines for an AI semantic code review.

    Static rules only catch known patterns. This tool returns the actual
    diff content (with line numbers) so YOU can review it semantically —
    judging logic errors, edge cases, naming, missing tests, and intent.

    Recommended workflow after calling this:
    1. Read the returned lines carefully.
    2. Report concrete issues with ``file:line`` references.
    3. Separate real bugs from style nits, and say which is which.
    4. If nothing is wrong, say so plainly instead of inventing issues.

    Args:
        rev_range (str, optional):
            Revision range to review, e.g. ``"main..HEAD"``. Empty
            reviews uncommitted changes.
        repo_dir (str, optional):
            Path to the git repository. Empty uses the workspace.
        staged (bool, optional):
            Review only staged changes when *rev_range* is empty.
        max_files (int, optional):
            Maximum files to include. Defaults to 30.

    Returns:
        object: A ``ToolChunk`` whose text contains the changed files and
        their added lines, each prefixed with its line number.

    Example:
        >>> await review_context()
        >>> await review_context("main..HEAD")
    """
    reviewer = _load_reviewer()
    resolved = _resolve_repo_dir(repo_dir)
    try:
        ctx = reviewer.review_context(
            resolved,
            rev_range.strip() if rev_range else "",
            staged=staged,
            max_files=max_files,
        )
    except reviewer.ReviewError as exc:
        return _chunk_error(f"ERROR: {exc}")

    header = (
        "The following are the changed lines. Review them semantically: "
        "look for logic errors, unhandled edge cases, resource leaks, "
        "race conditions, missing tests, and unclear naming. "
        "Report issues with file:line references.\n\n"
    )
    return _chunk_success(header + ctx)


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class CodeReviewCopilotPlugin:
    """Registers read-only code review tools and the /review command."""

    def register(self, api: PluginApi) -> None:
        """Register tools and the slash command.

        Args:
            api: PluginApi instance provided by QwenPaw.
        """
        # `tool_type="shell"` is deliberate and honest: these tools invoke
        # git through subprocess. Declaring them as "file" or "internal"
        # would bypass the governance layer's destructive-command checks.
        # The engine enforces a hard read-only subcommand allowlist, so
        # declaring "shell" does not widen what can actually run.
        api.register_tool(
            tool_name=TOOL_REVIEW_DIFF,
            tool_func=review_git_diff,
            description="Review uncommitted changes with static rules",
            icon="🔍",
            tool_type="shell",
            target_param="command",
        )
        api.register_tool(
            tool_name=TOOL_REVIEW_RANGE,
            tool_func=review_rev_range,
            description="Review a commit or branch range with static rules",
            icon="🔎",
            tool_type="shell",
            target_param="command",
        )
        api.register_tool(
            tool_name=TOOL_REVIEW_CONTEXT,
            tool_func=review_context,
            description="Fetch changed lines for AI semantic review",
            icon="🧠",
            tool_type="shell",
            target_param="command",
        )

        self._register_command(api)

        logger.info(
            "Code Review Copilot registered: 3 tools + /%s",
            COMMAND_NAME,
        )

    def _register_command(self, api: PluginApi) -> None:
        """Register the ``/review`` control command.

        If the host does not expose ``BaseControlCommandHandler`` the
        tools still work, but we log loudly rather than failing silently,
        so a missing command is diagnosable from the plugin log.
        """
        try:
            from qwenpaw.runtime.commands.control.base import (
                BaseControlCommandHandler,
            )
        except Exception as exc:
            logger.warning(
                "Cannot register /%s: BaseControlCommandHandler is "
                "unavailable (%s). The review tools remain usable.",
                COMMAND_NAME,
                exc,
            )
            return

        class ReviewCommandHandler(BaseControlCommandHandler):
            """``/review [range]`` — print a rule-based review report."""

            command_name = COMMAND_NAME
            help_text = "Review code changes, e.g. /review main..HEAD"

            async def handle(self, ctx, args: str):  # noqa: D102
                from agentscope.message import Msg

                target = (args or "").strip()
                report = _run_review(
                    "",
                    target,
                    staged=False,
                    max_files=50,
                    ignore_patterns=[],
                )
                return Msg(
                    name="system",
                    role="assistant",
                    content=report,
                )

        api.register_control_command(
            handler=ReviewCommandHandler(),
            priority_level=10,
        )


# Export plugin instance
plugin = CodeReviewCopilotPlugin()
