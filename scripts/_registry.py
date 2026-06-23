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

"""OCI registry helpers shared by versions.py and check-compose.py."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPResponse

BEARER_PARAM = re.compile(r'(\w+)="([^"]*)"')
DEFAULT_TIMEOUT = 30

REQUIRED_ARCHS = frozenset({"amd64", "arm64"})
INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
MANIFEST_ACCEPT = ", ".join(
    sorted(
        INDEX_MEDIA_TYPES
        | {
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        }
    )
)


class RegistryError(RuntimeError):
    pass


def split_reference(reference: str) -> tuple[str, str, str]:
    """Split `host/path/image[:tag][@sha256:digest]` into (host, path, ref).

    Docker Hub single-name repos resolve under `library/`. When both tag and
    digest are present, the digest wins.
    """
    if "@sha256:" in reference:
        head, _, digest = reference.partition("@sha256:")
        repo_part, _, _ = head.rpartition(":")
        repo_part = repo_part or head
        ref = f"sha256:{digest}"
    else:
        repo_part, sep, tag = reference.rpartition(":")
        if not sep or "/" in tag:
            repo_part, ref = reference, "latest"
        else:
            ref = tag

    host, slash, path = repo_part.partition("/")

    if not slash or ("." not in host and ":" not in host):
        if "/" not in repo_part:
            repo_part = f"library/{repo_part}"
        return "registry-1.docker.io", repo_part, ref

    return host, path, ref


def _bearer_token(www_authenticate: str) -> str:
    """Fetch an anonymous token from the realm in a Bearer challenge."""
    if not www_authenticate.lower().startswith("bearer "):
        raise RegistryError(f"unsupported auth scheme: {www_authenticate!r}")

    params = dict(BEARER_PARAM.findall(www_authenticate[7:]))
    realm = params.pop("realm", None)

    if not realm:
        raise RegistryError(f"missing realm in challenge: {www_authenticate!r}")

    query = urllib.parse.urlencode(params)
    url = f"{realm}?{query}" if query else realm

    with urllib.request.urlopen(url, timeout=DEFAULT_TIMEOUT) as resp:
        payload = json.loads(resp.read())

    token = payload.get("token") or payload.get("access_token")

    if not token:
        raise RegistryError(f"no token in response from {realm}")
    return token


def _request(method: str, url: str) -> HTTPResponse:
    """Send a manifest request; retry once with a bearer token on 401."""
    headers = {"Accept": MANIFEST_ACCEPT}

    def send(extra: dict[str, str] | None = None) -> HTTPResponse:
        req = urllib.request.Request(
            url, headers={**headers, **(extra or {})}, method=method
        )
        return urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT)

    try:
        return send()
    except urllib.error.HTTPError as err:
        if err.code != 401:
            raise RegistryError(
                f"{method} {url} failed: HTTP {err.code} {err.reason}"
            ) from err
        token = _bearer_token(err.headers.get("WWW-Authenticate", ""))
        return send({"Authorization": f"Bearer {token}"})


def fetch_digest(reference: str) -> str:
    """Return the manifest digest for a reference.

    Uses HEAD; public.ecr.aws only sets Docker-Content-Digest on HEAD.
    """
    host, path, ref = split_reference(reference)
    url = f"https://{host}/v2/{path}/manifests/{ref}"

    with _request("HEAD", url) as resp:
        digest = resp.headers.get("Docker-Content-Digest", "")
    if not digest:
        raise RegistryError(f"no Docker-Content-Digest header for {reference}")

    return digest


def fetch_manifest(reference: str) -> dict:
    """Fetch and parse a manifest."""
    host, path, ref = split_reference(reference)
    url = f"https://{host}/v2/{path}/manifests/{ref}"
    with _request("GET", url) as resp:
        return json.loads(resp.read())


def multiplatform_errors(manifest: dict) -> list[str]:
    """Return errors if the manifest isn't a list covering linux/amd64+arm64."""
    media_type = manifest.get("mediaType", "")
    if media_type not in INDEX_MEDIA_TYPES:
        return [f"not a manifest list (mediaType: {media_type!r})"]
    archs = _linux_archs(manifest)
    missing = REQUIRED_ARCHS - archs
    if missing:
        return [f"missing architectures {sorted(missing)} (found {sorted(archs)})"]
    return []


def platforms(manifest: dict) -> list[str]:
    return sorted(_linux_archs(manifest))


def _linux_archs(manifest: dict) -> set[str]:
    return {
        m["platform"]["architecture"]
        for m in manifest.get("manifests", [])
        if m.get("platform", {}).get("os") == "linux"
        and m.get("platform", {}).get("architecture") not in (None, "unknown")
    }
