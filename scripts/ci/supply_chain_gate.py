#!/usr/bin/env python3
"""Generate local-image SBOM evidence and fail closed on supply-chain risk."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any


SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA64 = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
COMPONENT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
SBOM_SCAN_IMAGE = re.compile(r"aiops-sbom-scan:(?:backend|frontend)-[0-9a-f]{16}\Z")
VULNERABILITY_ID = re.compile(r"(?:CVE|GHSA)-[A-Z0-9-]+\Z", re.IGNORECASE)
FORBIDDEN_LICENSES = {
    "AGPL-3.0",
    "AGPL-3.0-only",
    "AGPL-3.0-or-later",
    "SSPL-1.0",
}
REQUIRED_EXCEPTION_FIELDS = (
    "component",
    "vulnerabilityId",
    "packagePurl",
    "packageVersion",
    "owner",
    "reason",
    "mitigation",
    "expiresOn",
)
CONTENT_CACHE_COMPONENTS = {
    "tests",
    "test",
    "test-results",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "coverage",
    "coverage-analysis",
}
SECRET_FILE_NAMES = {
    ".env",
    ".npmrc",
    ".netrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}
UNNECESSARY_TOOL_PATHS = {
    "usr/bin/gcc",
    "usr/bin/g++",
    "usr/bin/make",
    "usr/bin/git",
    "usr/local/bin/node",
    "usr/local/bin/npm",
    "usr/local/bin/yarn",
    "usr/local/bin/pnpm",
}
PRIVATE_KEY_BLOCK = re.compile(
    rb"-----BEGIN (?P<label>[A-Z0-9 ]+PRIVATE KEY)-----\r?\n"
    rb"(?P<body>(?:(?:[A-Za-z0-9+/]{4,}={0,2})\r?\n)+)"
    rb"-----END (?P=label)-----"
)
AWS_ACCESS_KEY = re.compile(rb"AKIA[0-9A-Z]{16}")
HIGH_CONFIDENCE_MARKER = re.compile(
    rb"(?:"
    rb"gh[pousr]_[A-Za-z0-9]{36,}"
    rb"|github_pat_[A-Za-z0-9_]{40,}"
    rb"|sk-(?:proj|svcacct)-[A-Za-z0-9_-]{20,}"
    rb"|xox[baprs]-[A-Za-z0-9-]{20,}"
    rb"|AIza[0-9A-Za-z_-]{35}"
    rb")"
)
BASE64_CONTEXT = re.compile(rb"[A-Za-z0-9+/=]")
CONTENT_SCAN_CHUNK_BYTES = 64 * 1024
CONTENT_SCAN_OVERLAP_BYTES = 64 * 1024
MAX_ARCHIVE_MANIFEST_BYTES = 1024 * 1024
MAX_ARCHIVE_CONFIG_BYTES = 16 * 1024 * 1024
MAX_SCOUT_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_OCI_LAYOUT_BYTES = 4096
OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.v1.tar+zstd",
}


class SupplyChainGateError(RuntimeError):
    pass


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> Any:
    try:
        if path.is_symlink() or not path.is_file():
            raise SupplyChainGateError(f"{label} is invalid")
        return json.loads(path.read_text(encoding="utf-8"))
    except SupplyChainGateError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SupplyChainGateError(f"{label} is invalid") from exc


def platform_key() -> str:
    machine = platform.machine().lower()
    if machine == "aarch64":
        machine = "arm64"
    return f"{sys.platform}/{machine}"


def validate_tool_lock(path: Path) -> dict[str, dict[str, str]]:
    payload = load_json(path, "supply-chain tool lock")
    key = platform_key()
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise SupplyChainGateError("supply-chain tool lock is invalid")
    platforms = payload.get("platforms")
    record = platforms.get(key) if isinstance(platforms, dict) else None
    if not isinstance(record, dict):
        raise SupplyChainGateError(f"supply-chain tools are not locked for {key}")
    expected = {
        "dockerSbom": ("version", "provider", "sha256"),
        "dockerScout": ("version", "sha256"),
    }
    normalized: dict[str, dict[str, str]] = {}
    for tool, fields in expected.items():
        item = record.get(tool)
        if not isinstance(item, dict) or set(item) != set(fields):
            raise SupplyChainGateError("supply-chain tool lock is invalid")
        if any(not isinstance(item[field], str) or not item[field].strip() for field in fields):
            raise SupplyChainGateError("supply-chain tool lock is invalid")
        if SHA64.fullmatch(item["sha256"]) is None:
            raise SupplyChainGateError("supply-chain tool lock is invalid")
        normalized[tool] = {field: item[field] for field in fields}
    return normalized


def resolve_plugin(explicit: str | None, name: str) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env_name = f"{name.upper().replace('-', '_')}_PLUGIN"
    if os.environ.get(env_name):
        candidates.append(Path(os.environ[env_name]))
    candidates.extend(
        (
            Path("/Applications/Docker.app/Contents/Resources/cli-plugins") / name,
            Path.home() / ".docker" / "cli-plugins" / name,
        )
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            mode = resolved.stat().st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode) and os.access(resolved, os.X_OK):
            return resolved
    raise SupplyChainGateError(f"{name} plugin is unavailable")


def validate_plugin(path: Path, locked: dict[str, str], label: str) -> dict[str, str]:
    actual_sha = sha256_file(path)
    if actual_sha != locked["sha256"]:
        raise SupplyChainGateError(f"{label} binary SHA does not match the lock")
    return {**locked, "binaryPath": str(path), "binarySha256": actual_sha}


def package_version_from_purl(purl: str) -> str:
    if not purl.startswith("pkg:") or any(character.isspace() for character in purl):
        raise SupplyChainGateError("package PURL is invalid")
    versionless = purl.split("#", 1)[0].split("?", 1)[0]
    _name, separator, version = versionless.rpartition("@")
    if separator != "@" or not version:
        raise SupplyChainGateError("package PURL is invalid")
    return version


def validate_exceptions(path: Path, today: dt.date) -> list[dict[str, str]]:
    payload = load_json(path, "supply-chain exception contract")
    records = payload.get("exceptions") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(records, list):
        raise SupplyChainGateError("supply-chain exception contract is invalid")
    normalized = []
    identities = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != set(REQUIRED_EXCEPTION_FIELDS):
            raise SupplyChainGateError("supply-chain exception contract is invalid")
        if any(
            not isinstance(record[field], str) or not record[field].strip()
            for field in REQUIRED_EXCEPTION_FIELDS
        ):
            raise SupplyChainGateError("supply-chain exception contract is invalid")
        if COMPONENT.fullmatch(record["component"]) is None:
            raise SupplyChainGateError("supply-chain exception contract is invalid")
        vulnerability_id = record["vulnerabilityId"].upper()
        if VULNERABILITY_ID.fullmatch(vulnerability_id) is None:
            raise SupplyChainGateError("supply-chain exception contract is invalid")
        try:
            package_version = package_version_from_purl(record["packagePurl"])
        except SupplyChainGateError as exc:
            raise SupplyChainGateError("supply-chain exception contract is invalid") from exc
        if package_version != record["packageVersion"]:
            raise SupplyChainGateError("supply-chain exception contract is invalid")
        try:
            expires_on = dt.date.fromisoformat(record["expiresOn"])
        except ValueError as exc:
            raise SupplyChainGateError("supply-chain exception contract is invalid") from exc
        if expires_on < today:
            raise SupplyChainGateError("supply-chain exception contract contains an expired exception")
        identity = (
            record["component"],
            vulnerability_id,
            record["packagePurl"],
            record["packageVersion"],
        )
        if identity in identities:
            raise SupplyChainGateError("supply-chain exception contract contains duplicates")
        identities.add(identity)
        normalized.append({**record, "vulnerabilityId": vulnerability_id})
    return normalized


def parse_images(values: list[str], release_sha: str) -> dict[str, str]:
    images = {}
    for value in values:
        component, separator, reference = value.partition("=")
        if (
            separator != "="
            or COMPONENT.fullmatch(component) is None
            or not reference
            or any(character in reference for character in "\r\n\t\0")
            or not reference.endswith(f":{release_sha}")
            or component in images
        ):
            raise SupplyChainGateError("local image argument is invalid")
        images[component] = reference
    if set(images) != {"backend", "frontend"} or len(set(images.values())) != 2:
        raise SupplyChainGateError("local image arguments are incomplete")
    return images


def inspect_image_id(docker_bin: str, image: str) -> str:
    result = subprocess.run(
        [docker_bin, "image", "inspect", "--format", "{{.Id}}", image],
        text=True,
        capture_output=True,
        check=False,
    )
    image_id = result.stdout.strip()
    if result.returncode != 0 or IMAGE_ID.fullmatch(image_id) is None:
        raise SupplyChainGateError("local image identity inspection failed")
    return image_id


def package_type_counts(packages: list[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for package in packages:
        package_type = "unknown"
        refs = package.get("externalRefs") if isinstance(package, dict) else None
        if isinstance(refs, list):
            for ref in refs:
                locator = ref.get("referenceLocator") if isinstance(ref, dict) else None
                if isinstance(locator, str) and locator.startswith("pkg:"):
                    package_type = locator[4:].split("/", 1)[0].split("@", 1)[0].lower()
                    break
        counts[package_type] = counts.get(package_type, 0) + 1
    return dict(sorted(counts.items()))


def validate_sbom(path: Path, image: str) -> dict[str, object]:
    payload = load_json(path, "SPDX SBOM")
    packages = payload.get("packages") if isinstance(payload, dict) else None
    expected_document_name = (
        image.replace(":", "-", 1) if SBOM_SCAN_IMAGE.fullmatch(image) else None
    )
    document_name = payload.get("name") if isinstance(payload, dict) else None
    document_namespace = (
        payload.get("documentNamespace") if isinstance(payload, dict) else None
    )
    if (
        not isinstance(payload, dict)
        or expected_document_name is None
        or not isinstance(payload.get("spdxVersion"), str)
        or not payload["spdxVersion"].startswith("SPDX-2.")
        or payload.get("dataLicense") != "CC0-1.0"
        or document_name != expected_document_name
        or not isinstance(document_namespace, str)
        or f"/{expected_document_name}" not in document_namespace.split("?", 1)[0]
        or not isinstance(packages, list)
        or not packages
    ):
        raise SupplyChainGateError("SPDX SBOM is invalid")
    licenses = set()
    for package in packages:
        if not isinstance(package, dict):
            raise SupplyChainGateError("SPDX SBOM package record is invalid")
        for field in ("licenseDeclared", "licenseConcluded"):
            value = package.get(field)
            if not isinstance(value, str) or not value:
                raise SupplyChainGateError("SPDX SBOM license record is invalid")
            licenses.add(value)
            if any(forbidden.lower() in value.lower() for forbidden in FORBIDDEN_LICENSES):
                raise SupplyChainGateError(f"forbidden license detected: {value}")
    return {
        "documentName": document_name,
        "documentNamespace": document_namespace,
        "format": "spdx-json",
        "sha256": sha256_file(path),
        "packageCount": len(packages),
        "packageTypes": package_type_counts(packages),
        "licenses": sorted(licenses),
    }


def _rootfs_path_is_forbidden(component: str, path: str) -> bool:
    parts = tuple(part for part in path.split("/") if part)
    if not parts:
        return False
    if component == "backend" and parts[:3] == ("app", "pi-runtime", "node_modules"):
        return any(part in CONTENT_CACHE_COMPONENTS - {"node_modules"} for part in parts[3:])
    if component == "backend" and parts[:1] == ("app",) and any(
        part in CONTENT_CACHE_COMPONENTS for part in parts[1:]
    ):
        return True
    if component == "frontend" and parts[:3] == ("usr", "share", "nginx") and any(
        part in CONTENT_CACHE_COMPONENTS for part in parts[3:]
    ):
        return True
    return False


def _rootfs_path_is_secret(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in SECRET_FILE_NAMES or name.endswith((".key", ".p12"))


def _scan_stream_for_secret(stream: Any) -> tuple[bool, int]:
    def contains_private_key(window: bytes) -> bool:
        return any(
            len(b"".join(match.group("body").splitlines())) >= 64
            for match in PRIVATE_KEY_BLOCK.finditer(window)
        )

    def embedded_in_base64_context(window: bytes, start: int, end: int) -> bool:
        left = start
        while left > 0 and BASE64_CONTEXT.fullmatch(window[left - 1 : left]):
            left -= 1
        right = end
        while right < len(window) and BASE64_CONTEXT.fullmatch(window[right : right + 1]):
            right += 1
        return start > left and right > end

    overlap = b""
    scanned_bytes = 0
    while True:
        chunk = stream.read(CONTENT_SCAN_CHUNK_BYTES)
        if not chunk:
            return False, scanned_bytes
        scanned_bytes += len(chunk)
        window = overlap + chunk
        if contains_private_key(window) or HIGH_CONFIDENCE_MARKER.search(window):
            return True, scanned_bytes
        for match in AWS_ACCESS_KEY.finditer(window):
            if match.group() == b"AKIAIOSFODNN7EXAMPLE":
                continue
            if not embedded_in_base64_context(window, *match.span()):
                return True, scanned_bytes
        overlap = window[-CONTENT_SCAN_OVERLAP_BYTES:]


def _cleanup_flattened_sbom_resources(
    docker_bin: str,
    container_name: str,
    scan_image: str,
    export_path: Path,
    *,
    container_created: bool = True,
    scan_image_created: bool = True,
    extra_paths: tuple[Path, ...] = (),
) -> bool:
    cleanup_failed = False
    if container_created:
        remove_container = subprocess.run(
            [docker_bin, "rm", "-f", container_name],
            text=True,
            capture_output=True,
            check=False,
        )
        cleanup_failed = remove_container.returncode != 0
    if scan_image_created:
        remove_image = subprocess.run(
            [docker_bin, "image", "rm", "-f", scan_image],
            text=True,
            capture_output=True,
            check=False,
        )
        cleanup_failed = cleanup_failed or remove_image.returncode != 0
    for path in (export_path, *extra_paths):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            cleanup_failed = True
    return cleanup_failed


def _oci_descriptor(
    value: object,
    *,
    allowed_media_types: set[str],
    max_size: int,
) -> tuple[str, int, str, str]:
    if not isinstance(value, dict):
        raise SupplyChainGateError("Scout archive identity is invalid")
    digest = value.get("digest")
    size = value.get("size")
    media_type = value.get("mediaType")
    if (
        not isinstance(digest, str)
        or IMAGE_ID.fullmatch(digest) is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not (0 < size <= max_size)
        or media_type not in allowed_media_types
    ):
        raise SupplyChainGateError("Scout archive identity is invalid")
    blob_name = "blobs/sha256/" + digest.removeprefix("sha256:")
    return digest, size, media_type, blob_name


def _read_archive_member(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
    *,
    max_size: int,
) -> bytes:
    member = members.get(name)
    if member is None or not member.isfile() or not (0 < member.size <= max_size):
        raise SupplyChainGateError("Scout archive identity is invalid")
    stream = archive.extractfile(member)
    if stream is None:
        raise SupplyChainGateError("Scout archive identity is invalid")
    payload = stream.read(max_size + 1)
    if len(payload) != member.size or len(payload) > max_size:
        raise SupplyChainGateError("Scout archive identity is invalid")
    return payload


def _validate_archive_blob(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
    *,
    expected_digest: str,
    expected_size: int,
    max_size: int,
) -> None:
    member = members.get(name)
    if (
        member is None
        or not member.isfile()
        or member.size != expected_size
        or not (0 < member.size <= max_size)
    ):
        raise SupplyChainGateError("Scout archive identity is invalid")
    stream = archive.extractfile(member)
    if stream is None:
        raise SupplyChainGateError("Scout archive identity is invalid")
    digest = hashlib.sha256()
    total = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        total += len(chunk)
        if total > expected_size:
            raise SupplyChainGateError("Scout archive identity is invalid")
        digest.update(chunk)
    if total != expected_size or "sha256:" + digest.hexdigest() != expected_digest:
        raise SupplyChainGateError("Scout archive identity is invalid")


def _load_archive_json(payload: bytes) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SupplyChainGateError("Scout archive identity is invalid")
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=reject_duplicate_keys)
    except SupplyChainGateError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SupplyChainGateError("Scout archive identity is invalid") from exc


def validate_scan_archive(
    path: Path, expected_image_id: str
) -> dict[str, object]:
    if (
        IMAGE_ID.fullmatch(expected_image_id) is None
        or path.is_symlink()
        or not path.is_file()
        or not (0 < path.stat().st_size <= MAX_SCOUT_ARCHIVE_BYTES)
    ):
        raise SupplyChainGateError("Scout scan archive is invalid")
    try:
        with tarfile.open(path, "r:*") as archive:
            members: dict[str, tarfile.TarInfo] = {}
            seen_names: set[str] = set()
            declared_size_total = 0
            for member in archive.getmembers():
                canonical_name = (
                    member.name.rstrip("/") if member.isdir() else member.name
                )
                path_parts = canonical_name.split("/")
                if (
                    not canonical_name
                    or canonical_name.startswith("/")
                    or "\\" in canonical_name
                    or any(part in {"", ".", ".."} for part in path_parts)
                    or canonical_name in seen_names
                ):
                    raise SupplyChainGateError("Scout archive identity is invalid")
                seen_names.add(canonical_name)
                if member.isdir():
                    if (
                        canonical_name not in {"blobs", "blobs/sha256"}
                        or member.size != 0
                    ):
                        raise SupplyChainGateError("Scout archive identity is invalid")
                    continue
                if (
                    not member.isfile()
                    or not (0 < member.size <= MAX_SCOUT_ARCHIVE_BYTES)
                ):
                    raise SupplyChainGateError("Scout archive identity is invalid")
                declared_size_total += member.size
                if declared_size_total > MAX_SCOUT_ARCHIVE_BYTES:
                    raise SupplyChainGateError("Scout archive identity is invalid")
                members[canonical_name] = member

            layout = _load_archive_json(
                _read_archive_member(
                    archive,
                    members,
                    "oci-layout",
                    max_size=MAX_OCI_LAYOUT_BYTES,
                )
            )
            if layout != {"imageLayoutVersion": "1.0.0"}:
                raise SupplyChainGateError("Scout archive identity is invalid")

            index = _load_archive_json(
                _read_archive_member(
                    archive,
                    members,
                    "index.json",
                    max_size=MAX_ARCHIVE_MANIFEST_BYTES,
                )
            )
            index_manifests = (
                index.get("manifests") if isinstance(index, dict) else None
            )
            if (
                not isinstance(index, dict)
                or index.get("schemaVersion") != 2
                or index.get("mediaType") != OCI_INDEX_MEDIA_TYPE
                or not isinstance(index_manifests, list)
                or len(index_manifests) != 1
            ):
                raise SupplyChainGateError("Scout archive identity is invalid")

            (
                manifest_digest,
                manifest_size,
                manifest_media_type,
                manifest_blob_name,
            ) = _oci_descriptor(
                index_manifests[0],
                allowed_media_types={OCI_MANIFEST_MEDIA_TYPE},
                max_size=MAX_ARCHIVE_MANIFEST_BYTES,
            )
            if manifest_digest != expected_image_id:
                raise SupplyChainGateError("Scout archive identity is invalid")
            manifest_bytes = _read_archive_member(
                archive,
                members,
                manifest_blob_name,
                max_size=MAX_ARCHIVE_MANIFEST_BYTES,
            )
            if (
                len(manifest_bytes) != manifest_size
                or "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
                != manifest_digest
            ):
                raise SupplyChainGateError("Scout archive identity is invalid")

            oci_manifest = _load_archive_json(manifest_bytes)
            config_descriptor = (
                oci_manifest.get("config") if isinstance(oci_manifest, dict) else None
            )
            layer_descriptors = (
                oci_manifest.get("layers") if isinstance(oci_manifest, dict) else None
            )
            if (
                not isinstance(oci_manifest, dict)
                or oci_manifest.get("schemaVersion") != 2
                or oci_manifest.get("mediaType") != manifest_media_type
                or not isinstance(layer_descriptors, list)
                or not layer_descriptors
            ):
                raise SupplyChainGateError("Scout archive identity is invalid")

            (
                config_digest,
                config_size,
                config_media_type,
                config_blob_name,
            ) = _oci_descriptor(
                config_descriptor,
                allowed_media_types={OCI_CONFIG_MEDIA_TYPE},
                max_size=MAX_ARCHIVE_CONFIG_BYTES,
            )
            config_bytes = _read_archive_member(
                archive,
                members,
                config_blob_name,
                max_size=MAX_ARCHIVE_CONFIG_BYTES,
            )
            if (
                len(config_bytes) != config_size
                or "sha256:" + hashlib.sha256(config_bytes).hexdigest()
                != config_digest
            ):
                raise SupplyChainGateError("Scout archive identity is invalid")
            _load_archive_json(config_bytes)

            layer_digests: list[str] = []
            layer_media_types: list[str] = []
            layer_blob_names: list[str] = []
            for descriptor in layer_descriptors:
                layer_digest, layer_size, layer_media_type, layer_blob_name = (
                    _oci_descriptor(
                        descriptor,
                        allowed_media_types=OCI_LAYER_MEDIA_TYPES,
                        max_size=MAX_SCOUT_ARCHIVE_BYTES,
                    )
                )
                if layer_digest in layer_digests:
                    raise SupplyChainGateError("Scout archive identity is invalid")
                _validate_archive_blob(
                    archive,
                    members,
                    layer_blob_name,
                    expected_digest=layer_digest,
                    expected_size=layer_size,
                    max_size=MAX_SCOUT_ARCHIVE_BYTES,
                )
                layer_digests.append(layer_digest)
                layer_media_types.append(layer_media_type)
                layer_blob_names.append(layer_blob_name)

            compatibility_manifest = _load_archive_json(
                _read_archive_member(
                    archive,
                    members,
                    "manifest.json",
                    max_size=MAX_ARCHIVE_MANIFEST_BYTES,
                )
            )
            if (
                not isinstance(compatibility_manifest, list)
                or len(compatibility_manifest) != 1
                or not isinstance(compatibility_manifest[0], dict)
                or compatibility_manifest[0].get("Config") != config_blob_name
                or compatibility_manifest[0].get("Layers") != layer_blob_names
            ):
                raise SupplyChainGateError("Scout archive identity is invalid")

            expected_members = {
                "oci-layout",
                "index.json",
                "manifest.json",
                manifest_blob_name,
                config_blob_name,
                *layer_blob_names,
            }
            if set(members) != expected_members:
                raise SupplyChainGateError("Scout archive identity is invalid")
    except SupplyChainGateError:
        raise
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        tarfile.TarError,
    ) as exc:
        raise SupplyChainGateError(
            "Scout archive identity is invalid"
        ) from exc
    return {
        "archiveConfigDigest": config_digest,
        "archiveConfigMediaType": config_media_type,
        "archiveFormat": "oci-image-layout-v1",
        "archiveLayerDigests": layer_digests,
        "archiveLayerMediaTypes": layer_media_types,
        "archiveManifestImageId": manifest_digest,
        "archiveManifestMediaType": manifest_media_type,
        "archivePath": path.name,
        "archiveSha256": sha256_file(path),
    }


def generate_flattened_sbom(
    docker_bin: str,
    sbom_plugin: Path,
    image: str,
    image_id: str,
    component: str,
    temporary_dir: Path,
    sbom_path: Path,
    *,
    cleanup: bool = True,
) -> tuple[dict[str, object], dict[str, str]]:
    token = secrets.token_hex(8)
    container_name = f"aiops-sbom-export-{component}-{token}"
    scan_image = f"aiops-sbom-scan:{component}-{token}"
    export_path = temporary_dir / f"{component}.sbom-rootfs.tar"
    container_created = False
    scan_image_created = False
    primary_error: SupplyChainGateError | None = None
    try:
        create_result = subprocess.run(
            [
                docker_bin,
                "create",
                "--name",
                container_name,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--entrypoint",
                "/bin/true",
                image_id,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        container_created = create_result.returncode == 0
        container_id = create_result.stdout.strip()
        if not container_created or not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise SupplyChainGateError("SBOM export container creation failed")

        export_result = subprocess.run(
            [docker_bin, "export", "--output", str(export_path), container_id],
            text=True,
            capture_output=True,
            check=False,
        )
        if (
            export_result.returncode != 0
            or export_path.is_symlink()
            or not export_path.is_file()
            or export_path.stat().st_size <= 0
        ):
            raise SupplyChainGateError("SBOM rootfs export failed")
        rootfs_export_sha256 = sha256_file(export_path)
        import_result = subprocess.run(
            [
                docker_bin,
                "import",
                "--message",
                (
                    "aiops scan-only "
                    f"source-image-id={image_id} "
                    f"rootfs-export-sha256={rootfs_export_sha256}"
                ),
                str(export_path),
                scan_image,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        scan_image_created = import_result.returncode == 0
        scan_image_id = import_result.stdout.strip()
        if not scan_image_created or IMAGE_ID.fullmatch(scan_image_id) is None:
            raise SupplyChainGateError("SBOM scan image import failed")

        sbom_result = subprocess.run(
            [
                str(sbom_plugin),
                "sbom",
                "--format",
                "spdx-json",
                "--output",
                str(sbom_path),
                scan_image,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if sbom_result.returncode != 0:
            raise SupplyChainGateError("SBOM scanner failed")
        return validate_sbom(sbom_path, scan_image), {
            "rootfsExportSha256": rootfs_export_sha256,
            "scanImageId": scan_image_id,
            "scanImageReference": scan_image,
            "scanMethod": "docker-export-import",
            "sourceImageId": image_id,
            "_cleanupContainerName": container_name,
            "_cleanupExportPath": str(export_path),
        }
    except SupplyChainGateError as exc:
        primary_error = exc
        raise
    finally:
        cleanup_failed = False
        if cleanup or primary_error is not None:
            if container_created or scan_image_created or export_path.exists():
                cleanup_failed = _cleanup_flattened_sbom_resources(
                    docker_bin,
                    container_name,
                    scan_image,
                    export_path,
                    container_created=container_created,
                    scan_image_created=scan_image_created,
                )
        if cleanup_failed:
            if primary_error is not None:
                raise SupplyChainGateError(
                    f"{primary_error}; temporary SBOM resource cleanup failed"
                ) from primary_error
            raise SupplyChainGateError("temporary SBOM resource cleanup failed")


def audit_rootfs(
    docker_bin: str,
    image_id: str,
    component: str,
    temporary_dir: Path,
) -> dict[str, object]:
    create_result = subprocess.run(
        [
            docker_bin,
            "create",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--entrypoint",
            "/bin/true",
            image_id,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    container_id = create_result.stdout.strip()
    if create_result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise SupplyChainGateError("image content audit container creation failed")
    export_path = temporary_dir / f"{component}.rootfs.tar"
    primary_error: SupplyChainGateError | None = None
    try:
        export_result = subprocess.run(
            [docker_bin, "export", "--output", str(export_path), container_id],
            text=True,
            capture_output=True,
            check=False,
        )
        if export_result.returncode != 0:
            raise SupplyChainGateError("image content audit export failed")
        forbidden_paths: list[str] = []
        embedded_secrets: list[str] = []
        unnecessary_tools: list[str] = []
        regular_file_count = 0
        scanned_bytes = 0
        try:
            archive = tarfile.open(export_path, "r:")
        except (OSError, tarfile.TarError) as exc:
            raise SupplyChainGateError("image content audit archive is invalid") from exc
        with archive:
            for member in archive:
                raw_path = member.name.lstrip("/")
                if not raw_path or raw_path == ".":
                    continue
                if ".." in Path(raw_path).parts:
                    raise SupplyChainGateError("image content audit archive path is invalid")
                if _rootfs_path_is_forbidden(component, raw_path):
                    forbidden_paths.append(raw_path)
                if not member.isdir() and _rootfs_path_is_secret(raw_path):
                    embedded_secrets.append(raw_path)
                if raw_path in UNNECESSARY_TOOL_PATHS:
                    unnecessary_tools.append(raw_path)
                if not member.isfile():
                    continue
                regular_file_count += 1
                stream = archive.extractfile(member)
                if stream is None:
                    raise SupplyChainGateError("image content audit file is unreadable")
                has_secret, file_scanned_bytes = _scan_stream_for_secret(stream)
                scanned_bytes += file_scanned_bytes
                if has_secret:
                    embedded_secrets.append(raw_path)
        forbidden_paths = sorted(set(forbidden_paths))
        embedded_secrets = sorted(set(embedded_secrets))
        unnecessary_tools = sorted(set(unnecessary_tools))
        if embedded_secrets:
            raise SupplyChainGateError(
                "embedded secret detected: " + ",".join(embedded_secrets)
            )
        if forbidden_paths:
            raise SupplyChainGateError(
                "forbidden image content: " + ",".join(forbidden_paths)
            )
        if unnecessary_tools:
            raise SupplyChainGateError(
                "unnecessary runtime tool: " + ",".join(unnecessary_tools)
            )
        return {
            "embeddedSecrets": embedded_secrets,
            "exportSha256": sha256_file(export_path),
            "forbiddenPaths": forbidden_paths,
            "regularFileCount": regular_file_count,
            "scannedBytes": scanned_bytes,
            "status": "clean",
            "unnecessaryTools": unnecessary_tools,
        }
    except SupplyChainGateError as exc:
        primary_error = exc
        raise
    finally:
        remove_result = subprocess.run(
            [docker_bin, "rm", "-f", container_id],
            text=True,
            capture_output=True,
            check=False,
        )
        if remove_result.returncode != 0:
            if primary_error is not None:
                raise SupplyChainGateError(
                    f"{primary_error}; image content audit container cleanup failed"
                ) from primary_error
            raise SupplyChainGateError("image content audit container cleanup failed")


def sarif_findings(path: Path) -> list[dict[str, str]]:
    payload = load_json(path, "Docker Scout SARIF report")
    runs = payload.get("runs") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != "2.1.0"
        or not isinstance(runs, list)
    ):
        raise SupplyChainGateError("Docker Scout SARIF report is invalid")
    findings: dict[tuple[str, str, str], dict[str, str]] = {}
    for run in runs:
        results = run.get("results") if isinstance(run, dict) else None
        if not isinstance(results, list):
            raise SupplyChainGateError("Docker Scout SARIF report is invalid")
        for result in results:
            vulnerability_id = result.get("ruleId") if isinstance(result, dict) else None
            if not isinstance(vulnerability_id, str):
                raise SupplyChainGateError("Docker Scout SARIF finding is invalid")
            vulnerability_id = vulnerability_id.upper()
            if VULNERABILITY_ID.fullmatch(vulnerability_id) is None:
                raise SupplyChainGateError("Docker Scout SARIF finding is invalid")
            message = result.get("message")
            message_text = message.get("text") if isinstance(message, dict) else None
            if not isinstance(message_text, str):
                raise SupplyChainGateError("Docker Scout SARIF finding is invalid")
            package_lines = []
            for line in message_text.splitlines():
                label, separator, value = line.partition(":")
                if label.strip() == "Package" and separator == ":":
                    package_lines.append(value.strip())
            if len(package_lines) != 1:
                raise SupplyChainGateError("Docker Scout SARIF finding is invalid")
            package_purl = package_lines[0]
            try:
                package_version = package_version_from_purl(package_purl)
            except SupplyChainGateError as exc:
                raise SupplyChainGateError("Docker Scout SARIF finding is invalid") from exc
            identity = (vulnerability_id, package_purl, package_version)
            findings[identity] = {
                "vulnerabilityId": vulnerability_id,
                "packagePurl": package_purl,
                "packageVersion": package_version,
            }
    return [findings[identity] for identity in sorted(findings)]


def write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.write_bytes(canonical_json(evidence))
    path.chmod(0o600)


def run_gate(args: argparse.Namespace) -> dict[str, object]:
    if SHA40.fullmatch(args.release_sha) is None:
        raise SupplyChainGateError("release SHA is invalid")
    if SHA64.fullmatch(args.artifact_manifest_sha256) is None:
        raise SupplyChainGateError("artifact manifest SHA is invalid")
    images = parse_images(args.image, args.release_sha)
    tools = validate_tool_lock(args.tool_lock)
    exceptions = validate_exceptions(args.exceptions, dt.date.today())
    sbom_plugin = resolve_plugin(args.sbom_plugin, "docker-sbom")
    scout_plugin = resolve_plugin(args.scout_plugin, "docker-scout")
    tool_evidence = {
        "dockerSbom": validate_plugin(sbom_plugin, tools["dockerSbom"], "docker-sbom"),
        "dockerScout": validate_plugin(scout_plugin, tools["dockerScout"], "docker-scout"),
        "platform": platform_key(),
    }
    docker_bin = shutil.which(args.docker_bin) if os.sep not in args.docker_bin else args.docker_bin
    if not docker_bin or not os.access(docker_bin, os.X_OK):
        raise SupplyChainGateError("docker CLI is unavailable")

    output_dir = args.output_dir
    if not output_dir.is_absolute() or output_dir.is_symlink() or output_dir.exists():
        raise SupplyChainGateError("supply-chain output directory is invalid")
    output_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=str(output_dir.parent))
    )
    try:
        image_evidence = {}
        for component, image in sorted(images.items()):
            image_id = inspect_image_id(docker_bin, image)
            sbom_path = temporary_dir / f"{component}.spdx.json"
            sbom, sbom_source = generate_flattened_sbom(
                docker_bin,
                sbom_plugin,
                image,
                image_id,
                component,
                temporary_dir,
                sbom_path,
                cleanup=False,
            )
            cleanup_container_name = sbom_source.pop("_cleanupContainerName")
            cleanup_export_path = Path(sbom_source.pop("_cleanupExportPath"))
            cleanup_scan_image = sbom_source["scanImageReference"]
            scout_archive_path = temporary_dir / f"{component}.scout-image.tar"
            primary_error: SupplyChainGateError | None = None
            try:
                save_result = subprocess.run(
                    [
                        docker_bin,
                        "image",
                        "save",
                        "--output",
                        str(scout_archive_path),
                        sbom_source["scanImageId"],
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if save_result.returncode != 0:
                    raise SupplyChainGateError(
                        "Scout scan archive creation failed"
                    )
                archive_evidence = validate_scan_archive(
                    scout_archive_path, sbom_source["scanImageId"]
                )
                sarif_path = temporary_dir / f"{component}.critical-high.sarif.json"
                scout_result = subprocess.run(
                    [
                        str(scout_plugin),
                        "cves",
                        "--only-severity",
                        "critical,high",
                        "--exit-code",
                        "--format",
                        "sarif",
                        "--output",
                        str(sarif_path),
                        f"archive://{scout_archive_path}",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if scout_result.returncode not in {0, 2}:
                    raise SupplyChainGateError("vulnerability scanner failed")
                findings = sarif_findings(sarif_path)
                if (scout_result.returncode == 2) != bool(findings):
                    raise SupplyChainGateError(
                        "vulnerability scanner result is inconsistent"
                    )
                approved = {
                    (
                        record["vulnerabilityId"],
                        record["packagePurl"],
                        record["packageVersion"],
                    )
                    for record in exceptions
                    if record["component"] == component
                }
                finding_identities = {
                    (
                        finding["vulnerabilityId"],
                        finding["packagePurl"],
                        finding["packageVersion"],
                    )
                    for finding in findings
                }
                unapproved = sorted(finding_identities - approved)
                if unapproved:
                    raise SupplyChainGateError(
                        "unapproved critical/high vulnerabilities: "
                        + ",".join(
                            f"{vulnerability_id}@{package_purl}"
                            for vulnerability_id, package_purl, _version in unapproved
                        )
                    )
                content_audit = audit_rootfs(
                    docker_bin, image_id, component, temporary_dir
                )
                image_evidence[component] = {
                    "contentAudit": content_audit,
                    "exceptionsApplied": [
                        finding
                        for finding in findings
                        if (
                            finding["vulnerabilityId"],
                            finding["packagePurl"],
                            finding["packageVersion"],
                        )
                        in approved
                    ],
                    "imageId": image_id,
                    "imageReference": image,
                    "licenses": sbom["licenses"],
                    "packages": sbom["packageTypes"],
                    "sbom": {
                        "documentName": sbom["documentName"],
                        "documentNamespace": sbom["documentNamespace"],
                        "format": sbom["format"],
                        "packageCount": sbom["packageCount"],
                        "path": sbom_path.name,
                        "sha256": sbom["sha256"],
                        **sbom_source,
                    },
                    "vulnerabilities": {
                        "criticalHighFindings": len(findings),
                        "reportPath": sarif_path.name,
                        "reportSha256": sha256_file(sarif_path),
                        "rootfsExportSha256": sbom_source[
                            "rootfsExportSha256"
                        ],
                        "scanImageId": sbom_source["scanImageId"],
                        "scanImageReference": cleanup_scan_image,
                        "sourceImageId": image_id,
                        "status": "excepted" if findings else "clean",
                        **archive_evidence,
                    },
                }
                sbom_path.chmod(0o600)
                sarif_path.chmod(0o600)
            except SupplyChainGateError as exc:
                primary_error = exc
                raise
            finally:
                cleanup_failed = _cleanup_flattened_sbom_resources(
                    docker_bin,
                    cleanup_container_name,
                    cleanup_scan_image,
                    cleanup_export_path,
                    extra_paths=(scout_archive_path,),
                )
                if cleanup_failed:
                    if primary_error is not None:
                        raise SupplyChainGateError(
                            f"{primary_error}; temporary SBOM resource cleanup failed"
                        ) from primary_error
                    raise SupplyChainGateError(
                        "temporary SBOM resource cleanup failed"
                    )

        evidence = {
            "artifactManifestSha256": args.artifact_manifest_sha256,
            "exceptionContractSha256": sha256_file(args.exceptions),
            "images": image_evidence,
            "releaseSha": args.release_sha,
            "signature": {
                "requiredBeforeRegistryPublish": True,
                "status": "not-signed-local-candidate",
            },
            "tools": tool_evidence,
        }
        write_evidence(temporary_dir / "supply-chain-evidence.json", evidence)
        os.rename(temporary_dir, output_dir)
        return evidence
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--artifact-manifest-sha256", required=True)
    parser.add_argument("--image", action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--tool-lock", type=Path, default=script_dir / "supply_chain_tools.json"
    )
    parser.add_argument(
        "--exceptions", type=Path, default=script_dir / "supply_chain_exceptions.json"
    )
    parser.add_argument("--sbom-plugin")
    parser.add_argument("--scout-plugin")
    parser.add_argument("--docker-bin", default="docker")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        evidence = run_gate(build_parser().parse_args(argv))
    except SupplyChainGateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical_json(evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
