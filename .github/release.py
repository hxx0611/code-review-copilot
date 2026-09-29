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
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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


def load_notes(version: str) -> str:
    path = NOTES_DIR / f"v{version}.md"
    if not path.is_file():
        raise SystemExit(
            f"Release notes not found: {path}\n"
            f"Create it first (UTF-8, no BOM).",
        )
    return path.read_text(encoding="utf-8")


def check(version: str, notes: str, repo: str, token: str | None) -> None:
    """Validate everything that has broken before, before publishing."""
    problems: list[str] = []
    notes_path = NOTES_DIR / f"v{version}.md"

    # --- notes encoding -------------------------------------------------
    raw = notes_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append(
            f"{notes_path.name} starts with a UTF-8 BOM; re-save without BOM.",
        )
    if not CJK_RE.search(notes):
        print(
            "  note: notes contain no CJK characters "
            "(fine for English releases)",
        )
    if "????" in notes:
        problems.append("Notes already contain '????' — encoding is broken.")

    # --- manifest -------------------------------------------------------
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    mver = manifest.get("version", "")
    if not SEMVER_RE.match(mver):
        problems.append(f"plugin.json version {mver!r} is not semver.")
    if mver != version:
        problems.append(
            f"plugin.json version is {mver!r} but --version is {version!r}.",
        )
    if manifest.get("id") != "code-review-copilot":
        problems.append("plugin.json id changed unexpectedly.")
    if manifest.get("author") != "hxx0611":
        problems.append(
            f"plugin.json author is {manifest.get('author')!r}, expected 'hxx0611'.",
        )

    # --- round-trip through JSON, the step that failed before -----------
    encoded = json.dumps({"body": notes}, ensure_ascii=False).encode("utf-8")
    decoded = json.loads(encoded.decode("utf-8"))["body"]
    if decoded != notes:
        problems.append("Notes do not survive a UTF-8 JSON round-trip.")

    # --- report ---------------------------------------------------------
    print(f"  repo        : {repo}")
    print(f"  version     : {version} (manifest: {mver})")
    print(f"  notes file  : {notes_path.name} ({len(notes)} chars)")
    print(f"  notes CJK   : {'yes' if CJK_RE.search(notes) else 'no'}")
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
