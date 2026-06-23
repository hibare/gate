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

"""Sync versions.env digests from the registry using go.mod for image tags.

Exit codes:
    0  Already in sync.
    1  Drift detected (--check) or auto-fixed (default); pre-commit re-stages.
    2  Missing prerequisite (versions.env, go.mod).
    3  Upstream image isn't multi-platform; needs manual attention.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from _registry import (
    RegistryError,
    fetch_digest,
    fetch_manifest,
    multiplatform_errors,
)

VERSIONS_FILE = Path("versions.env")
GO_MOD = Path("go.mod")

GO_DIRECTIVE = re.compile(r"^go\s+(?P<version>\d+\.\d+(?:\.\d+)?)\s*$")
KEY_LINE = re.compile(r"^([A-Z_]+)=")


@dataclass(frozen=True)
class Image:
    key: str
    repository: str
    tag_template: str  # may contain `{go_version}`

    def reference(self, go_version: str) -> str:
        tag = self.tag_template.format(go_version=go_version)
        return f"{self.repository}:{tag}"


IMAGES: tuple[Image, ...] = (
    Image(
        key="GOLANG_DIGEST",
        repository="public.ecr.aws/docker/library/golang",
        tag_template="{go_version}-alpine",
    ),
    Image(
        key="DISTROLESS_DIGEST",
        repository="gcr.io/distroless/static-debian13",
        tag_template="nonroot",
    ),
)


def parse_env(path: Path) -> dict[str, str]:
    """Return KEY=VALUE pairs from a dotenv-style file."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")

        if sep:
            values[key.strip()] = value.strip()

    return values


def read_go_version() -> str:
    """Return the `go X.Y[.Z]` directive from go.mod."""
    for line in GO_MOD.read_text().splitlines():
        match = GO_DIRECTIVE.match(line)
        if match:
            return match.group("version")

    raise RuntimeError(f"no `go X.Y` directive found in {GO_MOD}")


def rewrite_keys(content: str, new_values: dict[str, str]) -> str:
    """Rewrite KEY=value lines in content for keys present in new_values."""
    lines = []
    for line in content.splitlines(keepends=True):
        match = KEY_LINE.match(line)
        if match and match.group(1) in new_values:
            trailing = "\n" if line.endswith("\n") else ""
            lines.append(f"{match.group(1)}={new_values[match.group(1)]}{trailing}")
        else:
            lines.append(line)

    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Read-only: report drift and exit non-zero without writing.",
    )
    args = parser.parse_args()

    if not VERSIONS_FILE.is_file():
        print(f"error: {VERSIONS_FILE} not found", file=sys.stderr)
        return 2

    if not GO_MOD.is_file():
        print(f"error: {GO_MOD} not found", file=sys.stderr)
        return 2

    env = parse_env(VERSIONS_FILE)
    go_version = read_go_version()
    print(f"Go version (go.mod): {go_version}")

    current: dict[str, str] = {}
    bad = False

    for image in IMAGES:
        reference = image.reference(go_version)
        try:
            digest = fetch_digest(reference)
            manifest = fetch_manifest(f"{image.repository}@{digest}")
        except (RegistryError, ValueError) as err:
            print(f"error: could not resolve {reference}: {err}", file=sys.stderr)
            return 2

        errors = multiplatform_errors(manifest)
        if errors:
            bad = True
            print(f"  ✗ {image.key}: {reference}")
            for err in errors:
                print(f"      {err}")
            continue

        current[image.key] = digest

    if bad:
        print()
        print("Upstream image is not multi-platform; refusing to update.")
        return 3

    drift = {k: v for k, v in current.items() if env.get(k) != v}
    if not drift:
        for image in IMAGES:
            print(f"  ✓ {image.key}: fresh and multi-platform")
        return 0

    if args.check:
        print()
        print("Drift detected (run without --check to auto-update):")
        for key, new in drift.items():
            print(f"  {key}")
            print(f"    stored:  {env.get(key, '<missing>')}")
            print(f"    current: {new}")
        return 1

    original = VERSIONS_FILE.read_text(encoding="utf-8")
    updated = rewrite_keys(original, current)

    VERSIONS_FILE.write_text(updated, encoding="utf-8")
    print(f"Updated {VERSIONS_FILE}:")

    for key, new in drift.items():
        print(f"  {key} → {new}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
