# -*- coding: utf-8 -*-
"""Release helper for code-review-copilot.

Reads ``.github/release-notes/v<version>.md`` and creates (or updates) a
GitHub Release with a correctly UTF-8 encoded body, then uploads the
built plugin ZIP.

Why this script exists
----------------------
Creating a release with PowerShell's ``Invoke-RestMethod`` silently
replaces every non-ASCII character with ``?``, because the JSON body is
encoded using the local ANSI code page. That bug shipped once already
(see ``.github/RELEASE.md``). Python encodes UTF-8 correctly, so this
script is the supported way to publish.

Usage
-----
    # 1. build the ZIP yourself (see .github/RELEASE.md), then:
    set GITHUB_TOKEN=github_pat_xxx
    python .github/release.py --version 1.0.0 --zip dist/code-review-copilot-1.0.0.zip

    # dry run: only verify notes + manifest, touch nothing
    python .github/release.py --version 1.0.0 --check

Requires Python 3.9+ and the standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = REPO_ROOT / ".github" / "release-notes"
MANIFEST = REPO_ROOT / "plugin.json"

CJK_RE = re.compile(r"[\u4e00-\u9fa5]")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

DEFAULT_REPO = "hxx0611/code-review-copilot"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def api(method: str, url: str, token: str, payload: dict | None = None):
    """Call the GitHub API with a UTF-8 encoded JSON body."""
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "code-review-copilot-release",
        "Accept": "application/vnd.github+json",
    }
    if payload is not None:
        # The critical line: JSON must be UTF-8 bytes, not locale-encoded.
        data = encode_body(payload)
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API error {exc.code} on {method} {url}:\n{detail}")


def upload_asset(upload_url: str, token: str, zip_path: Path) -> dict:
    """Upload a release asset from raw file bytes."""
    url = f"{upload_url.split('{')[0]}?name={zip_path.name}"
    req = urllib.request.Request(
        url,
        data=zip_path.read_bytes(),
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "code-review-copilot-release",
            "Content-Type": "application/zip",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Asset upload failed {exc.code}:\n{detail}")


def load_notes(version: str, notes_dir: Path | None = None) -> str:
    """Read release notes for *version*.

    Args:
        version: Version without the leading ``v``.
        notes_dir: Override for the notes directory (used by tests).

    Returns:
        The notes text.

    Raises:
        SystemExit: When the notes file is missing.
    """
    directory = notes_dir or NOTES_DIR
    path = directory / f"v{version}.md"
    if not path.is_file():
        raise SystemExit(
            f"Release notes not found: {path}\n"
            f"Create it first (UTF-8, no BOM).",
        )
    return path.read_text(encoding="utf-8")


def collect_problems(
    version: str,
    notes: str,
    *,
    notes_path: Path | None = None,
    manifest_path: Path | None = None,
    expected_author: str = "hxx0611",
    expected_id: str = "code-review-copilot",
) -> list[str]:
    """Return a list of validation problems (empty means all good).

    Kept side-effect free so the rules can be unit-tested directly;
    :func:`check` handles the printing and exit code.
    """
    problems: list[str] = []
    path = notes_path or (NOTES_DIR / f"v{version}.md")
    man_path = manifest_path or MANIFEST

    # --- notes encoding -------------------------------------------------
    if path.is_file():
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            problems.append(
                f"{path.name} starts with a UTF-8 BOM; re-save without BOM.",
            )
    if "????" in notes:
        problems.append("Notes already contain '????' — encoding is broken.")

    # --- manifest -------------------------------------------------------
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    mver = manifest.get("version", "")
    if not SEMVER_RE.match(mver):
        problems.append(f"plugin.json version {mver!r} is not semver.")
    if mver != version:
        problems.append(
            f"plugin.json version is {mver!r} but --version is {version!r}.",
        )
    if manifest.get("id") != expected_id:
        problems.append(
            f"plugin.json id is {manifest.get('id')!r}, "
            f"expected {expected_id!r}.",
        )
    if manifest.get("author") != expected_author:
        problems.append(
            f"plugin.json author is {manifest.get('author')!r}, "
            f"expected {expected_author!r}.",
        )

    # --- round-trip through JSON, the step that failed before -----------
    encoded = json.dumps({"body": notes}, ensure_ascii=False).encode("utf-8")
    decoded = json.loads(encoded.decode("utf-8"))["body"]
    if decoded != notes:
        problems.append("Notes do not survive a UTF-8 JSON round-trip.")

    return problems


def encode_body(payload: dict) -> bytes:
    """Serialise *payload* as UTF-8 JSON bytes.

    This is the single most important line in the file: PowerShell's
    ``Invoke-RestMethod`` encoded the body with the local ANSI code page
    and turned every Chinese character into ``?``. Python must always
    hand raw UTF-8 bytes to the HTTP layer.
    """
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def check(
    version: str,
    notes: str,
    repo: str,
    token: str | None,
    *,
    notes_path: Path | None = None,
    manifest_path: Path | None = None,
) -> None:
    """Validate everything that has broken before, then print a report.

    Raises:
        SystemExit: With code 1 when any problem is found.
    """
    path = notes_path or (NOTES_DIR / f"v{version}.md")
    problems = collect_problems(
        version, notes, notes_path=path, manifest_path=manifest_path,
    )

    encoded = encode_body({"body": notes})
    cjk = bool(CJK_RE.search(notes))

    if not cjk:
        print(
            "  note: notes contain no CJK characters "
            "(fine for English releases)",
        )

    print(f"  repo        : {repo}")
    try:
        mver = json.loads(
            (manifest_path or MANIFEST).read_text(encoding="utf-8"),
        ).get("version", "?")
    except Exception:  # pragma: no cover - manifest already validated
        mver = "?"
    print(f"  version     : {version} (manifest: {mver})")
    print(f"  notes file  : {path.name} ({len(notes)} chars)")
    print(f"  notes CJK   : {'yes' if cjk else 'no'}")
    print(f"  utf-8 bytes : {len(encoded)}")
    print(f"  token       : {'present' if token else 'MISSING'}")

    if problems:
        print("\nFAILED:")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(1)
    print("\nAll checks passed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="e.g. 1.0.0")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name")
    parser.add_argument("--zip", dest="zip_path", help="plugin ZIP to upload")
    parser.add_argument(
        "--check", action="store_true",
        help="validate only; do not touch GitHub",
    )
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--prerelease", action="store_true")
    args = parser.parse_args()

    version = args.version.lstrip("v")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    notes = load_notes(version)

    print(f"Validating release v{version}...")
    check(version, notes, args.repo, token)

    if args.check:
        print("\n--check specified; nothing was published.")
        return 0

    if not token:
        raise SystemExit(
            "GITHUB_TOKEN is not set.\n"
            "  PowerShell: $env:GITHUB_TOKEN = 'github_pat_...'",
        )

    base = f"https://api.github.com/repos/{args.repo}"
    tag = f"v{version}"

    # Reuse an existing release if the tag is already published.
    existing = None
    try:
        existing = api("GET", f"{base}/releases/tags/{tag}", token)
    except SystemExit:
        existing = None

    payload = {
        "tag_name": tag,
        "target_commitish": "main",
        "name": f"Code Review Copilot v{version}",
        "body": notes,
        "draft": args.draft,
        "prerelease": args.prerelease,
    }

    if existing:
        print(f"\nUpdating existing release {tag} (id={existing['id']})...")
        release = api(
            "PATCH", f"{base}/releases/{existing['id']}", token, payload,
        )
    else:
        print(f"\nCreating release {tag}...")
        release = api("POST", f"{base}/releases", token, payload)

    # Verify the server really stored readable text.
    stored = release.get("body", "")
    if notes.strip() and stored.strip() != notes.strip():
        print("WARNING: stored body differs from local notes.")
    flag = "yes" if CJK_RE.search(stored) else "no"
    print(f"  release url : {release['html_url']}")
    print(f"  server CJK  : {flag}")
    if CJK_RE.search(notes) and not CJK_RE.search(stored):
        raise SystemExit(
            "Encoding check FAILED: server body lost its CJK characters.",
        )

    if args.zip_path:
        zip_path = Path(args.zip_path)
        if not zip_path.is_file():
            raise SystemExit(f"ZIP not found: {zip_path}")
        print(f"\nUploading {zip_path.name} ({zip_path.stat().st_size} B)...")
        asset = upload_asset(release["upload_url"], token, zip_path)
        print(f"  asset       : {asset['name']} ({asset['size']} B)")
        print(f"  download    : {asset['browser_download_url']}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
