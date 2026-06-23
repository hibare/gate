#!/usr/bin/env python3
# Copyright 2026 Thomson Reuters
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Verify SHA-pinned images in compose files are multi-platform."""

from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _registry import RegistryError, fetch_manifest, multiplatform_errors, platforms

MAX_WORKERS = 8

IMAGE_LINE = re.compile(r"^\s*image:\s*(?P<ref>\S+@sha256:[0-9a-f]+)(?:\s+#.*)?\s*$")


def extract_images(path: Path) -> list[str]:
    """Return SHA-pinned image references, sorted and deduped."""
    refs = set()
    for line in path.read_text().splitlines():
        match = IMAGE_LINE.match(line)
        if match:
            refs.add(match.group("ref"))
    return sorted(refs)


def check_image(image: str) -> tuple[bool, str]:
    try:
        manifest = fetch_manifest(image)
    except (RegistryError, ValueError) as err:
        return False, f"inspect failed: {err}"

    errors = multiplatform_errors(manifest)
    if errors:
        return False, "; ".join(errors)
    return True, f"platforms: {platforms(manifest)}"


def check_file(path: Path) -> bool:
    print(f"Checking {path}")
    images = extract_images(path)
    if not images:
        print("  (no SHA-pinned images found)")
        return True

    workers = min(MAX_WORKERS, len(images))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(check_image, images))

    ok = True
    for image, (passed, message) in zip(images, results, strict=True):
        marker = "✓" if passed else "✗"
        print(f"  {marker} {image}")
        print(f"    {message}")
        if not passed:
            ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        default=[Path("local/compose.dev.yml")],
        help="Compose files to check (default: local/compose.dev.yml)",
    )
    args = parser.parse_args()

    missing_files = False
    image_failures = False
    for path in args.files:
        if not path.is_file():
            print(f"error: {path} not found", file=sys.stderr)
            missing_files = True
            continue
        if not check_file(path):
            image_failures = True

    if image_failures:
        print()
        print(
            "One or more images are not multi-platform. Pin to a manifest list "
            "digest (the OCI index, not a single-arch image) that supports "
            "linux/amd64 and linux/arm64."
        )
    return 1 if (missing_files or image_failures) else 0


if __name__ == "__main__":
    sys.exit(main())
