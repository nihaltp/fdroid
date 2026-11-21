#!/usr/bin/env python3
"""Import APKs from the latest stable GitHub release into an F-Droid repo."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path.cwd()
OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER", "nihaltp")
CONFIG_PATH = ROOT / "apps.yml"
STATE_PATH = ROOT / "release-state.json"


def api_json(url: str) -> Any:
    token = os.environ.get("RELEASES_READ_TOKEN")

    if not token:
        raise RuntimeError(
            "RELEASES_READ_TOKEN is missing. "
            "Add it to the repository's Actions secrets."
        )

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fdroid-repo-updater",
        },
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)

def latest_stable_release(repository: str) -> dict[str, Any]:
    # GitHub orders releases newest-first. Continue paging until a non-draft,
    # non-prerelease release is found, rather than assuming page 1 has one.
    page = 1
    while True:
        url = f"https://api.github.com/repos/{OWNER}/{repository}/releases?per_page=100&page={page}"
        releases = api_json(url)
        if not releases:
            raise RuntimeError(f"{repository}: no stable GitHub release found")
        for release in releases:
            if not release.get("draft") and not release.get("prerelease"):
                return release
        page += 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_asset(asset: dict[str, Any], destination: Path) -> None:
    request = urllib.request.Request(
        asset["browser_download_url"],
        headers={"User-Agent": "fdroid-repo-updater"},
    )
    with urllib.request.urlopen(request, timeout=240) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)

    expected = asset.get("digest") or ""
    if expected.startswith("sha256:"):
        actual = sha256_file(destination)
        if actual.lower() != expected.split(":", 1)[1].lower():
            raise RuntimeError(f"{asset['name']}: GitHub-provided SHA-256 digest mismatch")


def inspect_apk(path: Path, expected_package: str, expected_certificate: str) -> dict[str, Any]:
    badging = subprocess.check_output(["aapt", "dump", "badging", str(path)], text=True)
    match = re.search(
        r"^package: name='([^']+)' versionCode='([^']+)' versionName='([^']*)'",
        badging,
        re.MULTILINE,
    )
    if not match:
        raise RuntimeError(f"Cannot read package/version information from {path.name}")

    package, version_code_text, version_name = match.groups()
    if package != expected_package:
        raise RuntimeError(f"{path.name}: expected package {expected_package}, found {package}")

    cert_output = subprocess.check_output(
        ["apksigner", "verify", "--print-certs", str(path)],
        text=True,
        stderr=subprocess.STDOUT,
    )
    cert_match = re.search(
        r"Signer #1 certificate SHA-256 digest:\s*([0-9a-fA-F:]+)",
        cert_output,
    )
    if not cert_match:
        raise RuntimeError(f"{path.name}: could not determine signing certificate")

    actual_certificate = cert_match.group(1).replace(":", "").lower()
    expected_certificate = expected_certificate.replace(":", "").lower()
    if actual_certificate != expected_certificate:
        raise RuntimeError(f"{path.name}: signing certificate mismatch")

    return {
        "package": package,
        "version_code": int(version_code_text),
        "version_name": version_name,
        "sha256": sha256_file(path),
    }


def find_existing_apk(filename: str) -> Path | None:
    for directory in (ROOT / "repo", ROOT / "archive"):
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def update_metadata(metadata_path: Path, version_name: str, version_code: int) -> bool:
    if not metadata_path.is_file():
        raise RuntimeError(f"Metadata file not found: {metadata_path}")

    text = metadata_path.read_text(encoding="utf-8")
    # This repository distributes upstream binaries directly; these fields are
    # only needed for F-Droid source-build/update-check workflows.
    text = re.sub(r"(?m)^(Binaries|AutoUpdateMode|UpdateCheckMode|UpdateCheckData):.*\n?", "", text)
    text, version_count = re.subn(
        r"(?m)^CurrentVersion:.*$",
        f"CurrentVersion: {json.dumps(version_name)}",
        text,
        count=1,
    )
    text, code_count = re.subn(
        r"(?m)^CurrentVersionCode:.*$",
        f"CurrentVersionCode: {version_code}",
        text,
        count=1,
    )
    if version_count != 1 or code_count != 1:
        raise RuntimeError(f"{metadata_path}: CurrentVersion/CurrentVersionCode fields missing")

    if text != metadata_path.read_text(encoding="utf-8"):
        metadata_path.write_text(text, encoding="utf-8")
        return True
    return False


def process_app(app: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    name = app["name"]
    package = app["package"]
    release = latest_stable_release(app["github_repo"])
    release_id = str(release["id"])
    tag = release.get("tag_name", release_id)
    apk_assets = [asset for asset in release.get("assets", []) if asset.get("name", "").lower().endswith(".apk")]
    if not apk_assets:
        raise RuntimeError(f"{name} ({tag}): latest stable release has no .apk assets")

    previous = state.get(package, {})
    current_asset_ids = sorted(str(asset["id"]) for asset in apk_assets)
    previous_asset_ids = sorted(str(item["id"]) for item in previous.get("assets", []))

    # The state file lets us know the manifest-derived APK filenames without
    # downloading binaries again on every scheduled run.
    if previous.get("release_id") == release_id and current_asset_ids == previous_asset_ids:
        expected_files = [item["file"] for item in previous.get("assets", [])]
        files_are_intact = bool(expected_files) and all(
            (existing := find_existing_apk(item["file"])) is not None
            and sha256_file(existing) == item["sha256"]
            for item in previous.get("assets", [])
        )
        if files_are_intact:
            print(f"{name}: {tag} already imported; all APKs are present in repo/ or archive/. Skipping.")
            # Keep metadata aligned even if it was manually edited/stale.
            update_metadata(
                ROOT / app["metadata"],
                previous["version_name"],
                int(previous["current_version_code"]),
            )
            return previous

    print(f"{name}: importing latest stable release {tag} ({len(apk_assets)} APK asset(s))")
    ROOT.joinpath("repo").mkdir(exist_ok=True)
    ROOT.joinpath("archive").mkdir(exist_ok=True)

    imported_assets: list[dict[str, Any]] = []
    version_names: set[str] = set()
    version_codes: set[int] = set()

    with tempfile.TemporaryDirectory(prefix=f"fdroid-{package}-") as temp_dir:
        temp_root = Path(temp_dir)
        for asset in apk_assets:
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", asset["name"])
            downloaded = temp_root / f"{asset['id']}-{safe_name}"
            download_asset(asset, downloaded)
            info = inspect_apk(downloaded, package, app["signing_certificate"])
            code = info["version_code"]
            filename = f"{package}_{code}.apk"

            if code in version_codes:
                raise RuntimeError(
                    f"{name} ({tag}): multiple APK assets have versionCode {code}; "
                    "F-Droid filenames would collide"
                )
            version_codes.add(code)
            version_names.add(info["version_name"])

            existing = find_existing_apk(filename)

            if existing:
                existing_info = inspect_apk(
                    existing,
                    package,
                    app["signing_certificate"],
                )

                if (
                    existing_info["version_code"] != code
                    or existing_info["version_name"] != info["version_name"]
                ):
                    raise RuntimeError(
                        f"{name}: {filename} exists, but its version information "
                        "does not match the release. Refusing to continue."
                    )

                stored_sha256 = existing_info["sha256"]

                if stored_sha256 != info["sha256"]:
                    print(
                        f"  Warning: {filename} has different bytes from the "
                        "GitHub asset, but its package, version and signer match. "
                        "Keeping the existing APK."
                    )

                print(f"  Already present: {filename} ({existing.parent.name}/)")

            else:
                destination = ROOT / "repo" / filename
                shutil.copy2(downloaded, destination)
                stored_sha256 = info["sha256"]
                print(f"  Added: {filename} from {asset['name']}")

            imported_assets.append(
                {
                    "id": str(asset["id"]),
                    "name": asset["name"],
                    "file": filename,
                    "sha256": info["sha256"],
                    "release_sha256": info["sha256"],
                    "version_code": code,
                    "version_name": info["version_name"],
                }
            )

    if len(version_names) != 1:
        raise RuntimeError(f"{name} ({tag}): APK variants have inconsistent version names: {version_names}")

    version_name = next(iter(version_names))
    current_version_code = max(version_codes)
    update_metadata(ROOT / app["metadata"], version_name, current_version_code)

    print(f"{name}: imported/verified {len(imported_assets)} APK(s); latest {version_name} ({current_version_code})")
    return {
        "release_id": release_id,
        "tag": tag,
        "version_name": version_name,
        "current_version_code": current_version_code,
        "assets": imported_assets,
    }


def main() -> None:
    import yaml

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    state = load_state()

    for app in config["apps"]:
        package = app["package"]
        state[package] = process_app(app, state)

    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
