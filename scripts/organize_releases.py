#!/usr/bin/env python3
"""Keep complete versionName groups in repo/ and archive older groups.

Each APK is inspected with aapt. All APKs for an app that share the exact
versionName are treated as one logical release group, regardless of ABI or
individual versionCode.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path.cwd()
CONFIG_PATH = ROOT / "apps.yml"
STATE_PATH = ROOT / "release-state.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_apk(path: Path, expected_package: str) -> dict[str, Any]:
    """Read and validate the package name, version code and version name."""
    output = subprocess.check_output(
        ["aapt", "dump", "badging", str(path)],
        text=True,
    )
    match = re.search(
        r"^package: name='([^']+)' versionCode='([^']+)' versionName='([^']*)'",
        output,
        re.MULTILINE,
    )
    if not match:
        raise RuntimeError(f"Cannot read package/version information from {path}")

    package, version_code_text, version_name = match.groups()
    if package != expected_package:
        raise RuntimeError(
            f"{path}: expected package {expected_package}, found {package}"
        )
    if not version_name:
        raise RuntimeError(f"{path}: APK has an empty versionName; cannot group safely")

    try:
        version_code = int(version_code_text)
    except ValueError as exc:
        raise RuntimeError(f"{path}: invalid versionCode {version_code_text!r}") from exc

    return {
        "path": path,
        "version_name": version_name,
        "version_code": version_code,
    }


def move_without_overwrite(source: Path, destination: Path) -> bool:
    """Move a file safely; discard only byte-identical duplicate copies."""
    if source.resolve() == destination.resolve():
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(source) != sha256_file(destination):
            raise RuntimeError(
                f"Cannot move {source} to {destination}: destination exists "
                "with different contents"
            )
        source.unlink()
        return True

    shutil.move(str(source), str(destination))
    return True


def organize_app(
    app: dict[str, Any],
    state: dict[str, Any],
    keep_release_groups: int,
) -> None:
    package = app["package"]
    app_name = app.get("name", package)
    package_prefix = f"{package}_"

    # Inspect APKs in both directories so older archived variants can be
    # restored to repo/ when their versionName belongs to a retained group.
    apk_paths: list[Path] = []
    for directory_name in ("repo", "archive"):
        directory = ROOT / directory_name
        if directory.exists():
            apk_paths.extend(
                path
                for path in directory.glob(f"{package_prefix}*.apk")
                if path.is_file()
            )

    if not apk_paths:
        print(f"{app_name}: no APKs found; skipping retention")
        return

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(set(apk_paths)):
        info = inspect_apk(path, package)
        expected_filename = f"{package}_{info['version_code']}.apk"
        if path.name != expected_filename:
            raise RuntimeError(
                f"{path}: filename does not match APK versionCode; "
                f"expected {expected_filename}"
            )
        groups[info["version_name"]].append(info)

    app_state = state.get(package, {})
    current_version_name = app_state.get("version_name")
    current_version_code = app_state.get("current_version_code")

    if current_version_name:
        current_group = groups.get(current_version_name)
        if not current_group:
            raise RuntimeError(
                f"{app_name}: current versionName {current_version_name!r} "
                "is absent from repo/ and archive/"
            )
        if current_version_code is not None:
            current_codes = {item["version_code"] for item in current_group}
            if int(current_version_code) not in current_codes:
                raise RuntimeError(
                    f"{app_name}: release-state current versionCode "
                    f"{current_version_code} is absent from versionName "
                    f"{current_version_name!r}"
                )

    # Newest versionCode determines group order. The current release group is
    # explicitly first, matching F-Droid's CurrentVersionCode preference.
    ordered_names = sorted(
        groups,
        key=lambda version_name: max(
            item["version_code"] for item in groups[version_name]
        ),
        reverse=True,
    )
    if current_version_name in groups:
        ordered_names.remove(current_version_name)
        ordered_names.insert(0, current_version_name)

    retained_names = set(ordered_names[:keep_release_groups])
    moved_to_repo = 0
    moved_to_archive = 0

    for version_name, items in groups.items():
        target_directory = "repo" if version_name in retained_names else "archive"
        for item in items:
            source = item["path"]
            destination = ROOT / target_directory / source.name
            if move_without_overwrite(source, destination):
                if target_directory == "repo":
                    moved_to_repo += 1
                else:
                    moved_to_archive += 1

    group_summary = ", ".join(
        f"{name} ({len(groups[name])} APK(s))"
        for name in ordered_names
    )
    print(
        f"{app_name}: retained {len(retained_names)} versionName group(s) "
        f"in repo/; {moved_to_repo} moved into repo/, "
        f"{moved_to_archive} moved into archive/. Groups newest-first: "
        f"{group_summary}"
    )


def main() -> None:
    import yaml

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    keep = int(config.get("history", {}).get("keep_in_main_repo", 3))
    if keep < 1:
        raise SystemExit("history.keep_in_main_repo must be >= 1")

    for app in config["apps"]:
        organize_app(app, state, keep)


if __name__ == "__main__":
    main()
