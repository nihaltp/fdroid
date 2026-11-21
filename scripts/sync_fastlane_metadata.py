#!/usr/bin/env python3
"""Mirror Fastlane Android store metadata from configured app repositories."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

ROOT = Path.cwd()
CONFIG_PATH = ROOT / "apps.yml"
OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER", "nihaltp")
FASTLANE_PREFIX = "fastlane/metadata/android/"


def api_json(url: str) -> Any:
    """Fetch JSON from GitHub's API using the read-only app-repository token."""
    token = os.environ.get("RELEASES_READ_TOKEN")
    if not token:
        raise RuntimeError(
            "RELEASES_READ_TOKEN is missing. Add it to the repository's Actions secrets."
        )

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fdroid-fastlane-metadata-sync",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def git_tree(repository: str, ref: str) -> list[dict[str, Any]]:
    """Return the recursive Git tree for one app repository/ref."""
    encoded_ref = urllib.parse.quote(ref, safe="")
    url = (
        f"https://api.github.com/repos/{OWNER}/{repository}/git/trees/"
        f"{encoded_ref}?recursive=1"
    )
    result = api_json(url)
    if result.get("truncated"):
        raise RuntimeError(
            f"{repository}@{ref}: GitHub returned a truncated tree; refusing an incomplete metadata sync."
        )
    return result.get("tree", [])


def download_raw_file(repository: str, ref: str, path: str) -> bytes:
    """Download a file from raw.githubusercontent.com."""
    token = os.environ.get("RELEASES_READ_TOKEN")
    encoded_ref = urllib.parse.quote(ref, safe="")
    encoded_path = urllib.parse.quote(path, safe="/")
    url = f"https://raw.githubusercontent.com/{OWNER}/{repository}/{encoded_ref}/{encoded_path}"
    headers = {"User-Agent": "fdroid-fastlane-metadata-sync"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def git_blob_sha(data: bytes) -> str:
    """Calculate the Git SHA-1 for a blob, to verify downloaded bytes."""
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def safe_relative_path(path: str) -> Path:
    """Convert a repository path to a safe relative local path."""
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or not parsed.parts or any(part in ("", ".", "..") for part in parsed.parts):
        raise RuntimeError(f"Unsafe path in GitHub metadata tree: {path!r}")
    return Path(*parsed.parts)


def sync_app_metadata(app: dict[str, Any], *, dry_run: bool = False) -> tuple[int, int, int]:
    """Mirror one app's Fastlane metadata into metadata/<package>/.

    The source tree is fastlane/metadata/android/<locale>/ in the configured
    metadata_ref (normally main). All locales and file types are included.
    Existing files that no longer exist upstream are pruned after every source
    file has been downloaded and integrity-checked successfully.
    """
    name = app["name"]
    repository = app["github_repo"]
    package = app["package"]
    ref = str(app.get("metadata_ref", "main"))
    if not ref.strip():
        raise RuntimeError(f"{name}: metadata_ref cannot be empty")

    print(f"{name}: syncing Fastlane metadata from {OWNER}/{repository}@{ref}")
    tree = git_tree(repository, ref)

    entries: dict[str, dict[str, Any]] = {}
    for item in tree:
        path = item.get("path", "")
        if not path.startswith(FASTLANE_PREFIX):
            continue
        # Fastlane metadata should be regular Git blobs. Refuse symlinks rather
        # than following them outside the intended metadata directory.
        if item.get("type") != "blob":
            continue
        if item.get("mode") == "120000":
            raise RuntimeError(f"{name}: symlink not supported in Fastlane metadata: {path}")
        relative = path[len(FASTLANE_PREFIX):]
        local_relative = safe_relative_path(relative)
        entries[local_relative.as_posix()] = item

    if not entries:
        raise RuntimeError(
            f"{name}: no files found under {FASTLANE_PREFIX!r} in {repository}@{ref}"
        )

    destination_root = ROOT / "metadata" / package
    downloaded: dict[str, bytes] = {}
    for relative, item in sorted(entries.items()):
        source_path = FASTLANE_PREFIX + relative
        data = download_raw_file(repository, ref, source_path)
        expected_sha = item.get("sha")
        actual_sha = git_blob_sha(data)
        if expected_sha and actual_sha.lower() != expected_sha.lower():
            raise RuntimeError(
                f"{name}: integrity check failed for {source_path}; "
                f"expected Git blob {expected_sha}, got {actual_sha}"
            )
        downloaded[relative] = data

    added = 0
    updated = 0
    unchanged = 0
    for relative, data in downloaded.items():
        destination = destination_root / safe_relative_path(relative)
        if destination.is_file():
            if destination.read_bytes() == data:
                unchanged += 1
                continue
            action = "update"
            updated += 1
        else:
            action = "add"
            added += 1

        print(f"  {action}: metadata/{package}/{relative}")
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(destination)

    # Prune files removed upstream. This is safe because this directory is the
    # Fastlane mirror; the F-Droid YAML file lives beside it, not inside it.
    stale: list[Path] = []
    if destination_root.exists():
        expected = set(downloaded)
        for existing in destination_root.rglob("*"):
            if existing.is_file():
                relative = existing.relative_to(destination_root).as_posix()
                if relative not in expected:
                    stale.append(existing)

    for path in stale:
        print(f"  remove: {path.relative_to(ROOT).as_posix()} (no longer upstream)")
        if not dry_run:
            path.unlink()

    if not dry_run and destination_root.exists():
        # Remove empty directories left behind by deleted files.
        for directory in sorted(
            (p for p in destination_root.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

    print(
        f"{name}: metadata sync {'preview' if dry_run else 'complete'} — "
        f"{added} added, {updated} updated, {unchanged} unchanged, {len(stale)} removed"
    )
    return added, updated, len(stale)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="download and verify metadata, but do not write or remove local files",
    )
    args = parser.parse_args()

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    apps = config.get("apps", [])
    if not apps:
        raise SystemExit("apps.yml does not contain any configured apps")

    totals = [0, 0, 0]
    for app in apps:
        counts = sync_app_metadata(app, dry_run=args.dry_run)
        totals = [total + count for total, count in zip(totals, counts)]

    print(
        "Fastlane metadata sync totals: "
        f"{totals[0]} added, {totals[1]} updated, {totals[2]} removed"
    )


if __name__ == "__main__":
    main()
