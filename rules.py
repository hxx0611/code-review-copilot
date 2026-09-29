# -*- coding: utf-8 -*-
"""Built-in review rules for code-review-copilot.

Rules are pure data (no side effects) so they can be unit-tested in
isolation and extended by users without touching the engine.

Design notes
------------
* Every rule matches against **added lines only** (diff lines starting
  with ``+``), because reviewing unchanged code is noise.
* ``pattern`` is a compiled-on-demand regex string. Anchors like ``^``
  apply to the *stripped code content*, not the raw diff line.
* A rule may declare ``exclude_paths`` (regex against the file path) to
  avoid false positives, e.g. secret-scanners skipping test fixtures.
* ``severity`` is one of: critical / major / minor / info.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Pattern

# --------------------------------------------------------------------------
# Severity levels (ordered; higher index = less urgent)
# --------------------------------------------------------------------------

SEVERITY_ORDER = ("critical", "major", "minor", "info")

SEVERITY_LABEL = {
    "critical": "🔴 Critical",
    "major": "🟠 Major",
    "minor": "🟡 Minor",
    "info": "🔵 Info",
}

#: ASCII fallbacks, used when the active stream cannot encode emoji.
SEVERITY_LABEL_ASCII = {
    "critical": "[CRITICAL]",
    "major": "[MAJOR]",
    "minor": "[MINOR]",
    "info": "[INFO]",
}


def severity_label(severity: str, *, ascii_only: bool = False) -> str:
    """Return a display label for *severity*.

    Args:
        severity: One of critical/major/minor/info.
        ascii_only: Force the plain-text variant. Callers that write to a
            legacy console (GBK/cp1252) use this to avoid
            ``UnicodeEncodeError``; everything else keeps the emoji.
    """
    if ascii_only:
        return SEVERITY_LABEL_ASCII.get(severity, severity)
    return SEVERITY_LABEL.get(severity, severity)


@dataclass(frozen=True)
class Rule:
    """A single static-analysis rule."""

    id: str
    severity: str
    title: str
    pattern: str
    message: str
    suggestion: str = ""
    # Only apply to files whose path matches this regex (None = any).
    include_paths: str | None = None
    # Skip files whose path matches this regex (None = skip nothing).
    exclude_paths: str | None = None
    # Optional language hint for documentation purposes.
    languages: tuple[str, ...] = field(default_factory=tuple)

    def compiled(self) -> Pattern[str]:
        return re.compile(self.pattern)

    def applies_to(self, path: str) -> bool:
        """Return True when this rule should run against *path*."""
        if self.include_paths and not re.search(self.include_paths, path):
            return False
        if self.exclude_paths and re.search(self.exclude_paths, path):
            return False
        return True


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------

# Files that commonly contain deliberate fake secrets.
_TEST_PATH_RE = r"(^|/)(tests?|spec|__tests__|__mocks__|fixtures?|examples?|samples?)(/|$)"
_DOC_PATH_RE = r"\.(md|mdx|rst|txt)$"
_LOCK_PATH_RE = r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|Cargo\.lock)$"

# Binary / generated files we never want to scan.
_SKIP_PATH_RE = (
    r"(" + _LOCK_PATH_RE + r")|"
    r"\.(png|jpe?g|gif|ico|svg|webp|pdf|zip|tar|gz|whl|exe|dll|so|dylib|"
    r"class|jar|pyc|woff2?|ttf|eot|mp[34]|mov|avi)$"
)


# --------------------------------------------------------------------------
# Rule set
# --------------------------------------------------------------------------

RULES: tuple[Rule, ...] = (
    # ---------------------------------------------------------------- secrets
    Rule(
        id="secret.aws-key",
        severity="critical",
        title="Hardcoded AWS access key",
        pattern=r"\b(AKIA|ASIA)[0-9A-Z]{16}\b",
        message="An AWS access key ID appears to be committed.",
        suggestion="Move it to an environment variable or a secret manager, and rotate the key immediately.",
        exclude_paths=_TEST_PATH_RE,
        languages=("any",),
    ),
    Rule(
        id="secret.openai-key",
        severity="critical",
        title="Hardcoded API key (sk-…)",
        pattern=r"""["'\s]sk-[A-Za-z0-9_\-]{16,}""",
        message="A literal API key looks hardcoded.",
        suggestion="Read it from the environment (e.g. `os.environ['API_KEY']`) and rotate the exposed key.",
        exclude_paths=_TEST_PATH_RE,
        languages=("any",),
    ),
    Rule(
        id="secret.private-key-block",
        severity="critical",
        title="Private key material committed",
        pattern=r"-----BEGIN (RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----",
        message="A private key block was added to the repository.",
        suggestion="Remove it from history (filter-repo/BFG), store it in a secret manager, and rotate it.",
        exclude_paths=_TEST_PATH_RE,
        languages=("any",),
    ),
    Rule(
        id="secret.generic-assignment",
        severity="major",
        title="Possible hardcoded credential",
        pattern=(
            r"(?i)\b(api[_-]?key|secret|passwd|password|access[_-]?token|"
            r"auth[_-]?token|private[_-]?key|client[_-]?secret)\b\s*[:=]\s*"
            r"[\"'][^\"'\s]{8,}[\"']"
        ),
        message="A credential-looking value is assigned a string literal.",
        suggestion="Use an environment variable or config file excluded from version control.",
        exclude_paths=_TEST_PATH_RE + r"|" + _DOC_PATH_RE,
        languages=("any",),
    ),
    Rule(
        id="secret.env-file",
        severity="major",
        title="Environment file added to the repository",
        pattern=r"^$",  # path-based rule; see PATH_RULES
        message="A `.env`-style file appears in the change set.",
        suggestion="Add it to `.gitignore` and commit a `.env.example` with placeholder values instead.",
        languages=("any",),
    ),

    # ------------------------------------------------------- dangerous calls
    Rule(
        id="danger.eval",
        severity="major",
        title="Use of eval/exec",
        pattern=r"(?<![\w.])(eval|exec)\s*\(",
        message="Dynamic code execution can run untrusted input.",
        suggestion="Use a safe parser (e.g. `ast.literal_eval`, `json.loads`) or an explicit dispatch table.",
        languages=("python",),
    ),
    Rule(
        id="danger.pickle-load",
        severity="major",
        title="Unsafe deserialization",
        pattern=r"\bpickle\.loads?\s*\(|\byaml\.load\s*\((?![^)]*Loader\s*=\s*(Safe|CSafe))",
        message="Deserializing untrusted data can execute arbitrary code.",
        suggestion="Use `yaml.safe_load()` or a non-executable format such as JSON.",
        languages=("python",),
    ),
    Rule(
        id="danger.shell-true",
        severity="major",
        title="shell=True in subprocess",
        pattern=r"subprocess\.\w+\([^)]*shell\s*=\s*True",
        message="`shell=True` enables shell injection when input is interpolated.",
        suggestion="Pass an argument list and leave `shell=False`.",
        languages=("python",),
    ),
    Rule(
        id="danger.sql-concat",
        severity="major",
        title="SQL built by string concatenation",
        # Intentionally handled by a dedicated multi-line pass in the
        # engine (`_scan_sql`) rather than a single regex: deciding whether
        # SQL is dynamic requires looking at the whole call, and a naive
        # regex wrongly flags the *correct* parameterised form
        # `execute("... %s", params)`. See reviewer._scan_sql.
        pattern=r"^$",
        message="SQL assembled from strings is vulnerable to injection.",
        suggestion="Use parameterised queries (`cursor.execute(sql, params)`).",
        languages=("python",),
    ),
    Rule(
        id="danger.js-innerhtml",
        severity="major",
        title="Unsanitised innerHTML / dangerouslySetInnerHTML",
        pattern=r"(dangerouslySetInnerHTML|\.innerHTML\s*=|document\.write\s*\()",
        message="Writing raw HTML can introduce XSS.",
        suggestion="Use text content, or sanitise with a vetted library (e.g. DOMPurify).",
        languages=("javascript", "typescript"),
    ),
    Rule(
        id="danger.weak-hash",
        severity="minor",
        title="Weak hash algorithm",
        pattern=r"\b(md5|sha1)\s*\(",
        message="MD5/SHA-1 are broken for security purposes.",
        suggestion="Use SHA-256 or stronger; for passwords use bcrypt/argon2.",
        languages=("any",),
    ),

    # ------------------------------------------------------- error handling
    Rule(
        id="error.bare-except-pass",
        severity="major",
        title="Exception silently swallowed",
        pattern=r"except[^:]*:\s*(pass|\.\.\.)\s*$",
        message="A bare `except: pass` hides failures and makes bugs very hard to diagnose.",
        suggestion="Catch the specific exception and log it, or re-raise with context.",
        languages=("python",),
    ),
    Rule(
        id="error.broad-except",
        severity="minor",
        title="Overly broad exception handler",
        pattern=r"except\s+(Exception|BaseException)\s*:",
        message="Catching `Exception` hides unrelated programming errors.",
        suggestion="Catch the narrowest exception type that you can actually handle.",
        exclude_paths=_TEST_PATH_RE,
        languages=("python",),
    ),
    Rule(
        id="error.js-empty-catch",
        severity="major",
        title="Empty catch block",
        pattern=r"catch\s*(\([^)]*\))?\s*\{\s*\}",
        message="An empty `catch {}` discards the error entirely.",
        suggestion="At minimum log the error; ideally handle or re-throw it.",
        languages=("javascript", "typescript"),
    ),

    # ------------------------------------------------------------ leftovers
    Rule(
        id="cleanup.debug-print",
        severity="minor",
        title="Debug output left in code",
        pattern=r"(^|[^\w.])(print|console\.(log|debug|dir)|System\.out\.println)\s*\(",
        message="Leftover debug output clutters logs and can leak data.",
        suggestion="Remove it, or route it through the project's logger at debug level.",
        exclude_paths=_TEST_PATH_RE,
        languages=("python", "javascript", "typescript", "java"),
    ),
    Rule(
        id="cleanup.todo-marker",
        severity="info",
        title="TODO/FIXME marker added",
        pattern=r"(?i)(#|//|/\*|<!--)\s*(TODO|FIXME|XXX|HACK)\b",
        message="A pending-work marker was introduced.",
        suggestion="Link it to a tracked issue, or resolve it before merging.",
        languages=("any",),
    ),
    Rule(
        id="cleanup.breakpoint",
        severity="major",
        title="Debugger statement",
        pattern=r"(^|[^\w.])(breakpoint\s*\(\)|debugger\s*;?)\s*$",
        message="A debugger hook will halt execution for other developers.",
        suggestion="Remove it before committing.",
        languages=("python", "javascript", "typescript"),
    ),
)

# --------------------------------------------------------------------------
# Path-only rules (cannot be expressed as a content regex)
# --------------------------------------------------------------------------

PATH_RULES: tuple[Rule, ...] = (
    Rule(
        id="secret.env-file",
        severity="major",
        title="Environment file added to the repository",
        pattern=r"(^|/)\.env(\.|$)",
        message="A `.env` file appears in the change set and may contain real credentials.",
        suggestion="Add it to `.gitignore`; commit a `.env.example` with placeholders instead.",
        languages=("any",),
    ),
    Rule(
        id="cleanup.large-file",
        severity="info",
        title="Very large file changed",
        pattern=r"$^",
        message="Large files bloat the repository and are hard to review.",
        suggestion="Consider Git LFS or storing the artefact outside the repo.",
        languages=("any",),
    ),
)


def severity_rank(severity: str) -> int:
    """Return the sort rank of *severity* (unknown values sort last)."""
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(SEVERITY_ORDER)


def iter_rules() -> Iterable[Rule]:
    """Yield all content rules (path rules are handled by the engine)."""
    return iter(RULES)


def skip_path(path: str) -> bool:
    """Return True when *path* should not be scanned at all."""
    return bool(re.search(_SKIP_PATH_RE, path, re.IGNORECASE))


__all__ = [
    "Rule",
    "RULES",
    "PATH_RULES",
    "SEVERITY_LABEL",
    "SEVERITY_LABEL_ASCII",
    "SEVERITY_ORDER",
    "iter_rules",
    "severity_label",
    "severity_rank",
    "skip_path",
]
