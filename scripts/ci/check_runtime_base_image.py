#!/usr/bin/env python3
"""Fail closed unless a backend runtime base image has been explicitly audited."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


_IMAGE_DIGEST = re.compile(
    r"[a-z0-9][a-z0-9._/-]*(?::[a-z0-9][a-z0-9._-]*)?@sha256:[0-9a-f]{64}\Z"
)
_OFFICIAL_PYTHON_RUNTIME = re.compile(
    r"python:3\.11-(?:slim(?:-[a-z0-9][a-z0-9._-]*)?|alpine(?:[a-z0-9][a-z0-9._-]*)?)"
    r"@sha256:[0-9a-f]{64}\Z"
)
_FROM = re.compile(r"^\s*FROM\s+(?:--platform=[^\s]+\s+)?([^\s]+)")
_CVE = re.compile(r"CVE-[0-9]{4}-[0-9]{4,}\Z")


class GateError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("runtime base image policy is invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise GateError("runtime base image policy is invalid")
    return payload


def _read_runtime_base(dockerfile: Path) -> str:
    try:
        lines = dockerfile.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise GateError("runtime Dockerfile is invalid") from exc
    matches = [match.group(1) for line in lines if (match := _FROM.match(line))]
    if len(matches) != 1:
        raise GateError("runtime Dockerfile must have exactly one FROM instruction")
    image = matches[0]
    if _IMAGE_DIGEST.fullmatch(image) is None:
        raise GateError("runtime base image must be digest-pinned")
    if _OFFICIAL_PYTHON_RUNTIME.fullmatch(image) is None:
        raise GateError("runtime base image must use an official Python 3.11 runtime digest")
    return image


def _validate_entries(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise GateError("runtime base image policy is invalid")
    result: dict[str, tuple[str, ...]] = {}
    for image, cves in value.items():
        if _OFFICIAL_PYTHON_RUNTIME.fullmatch(image) is None or not isinstance(cves, list):
            raise GateError("runtime base image policy is invalid")
        if not cves or any(not isinstance(cve, str) or _CVE.fullmatch(cve) is None for cve in cves):
            raise GateError("runtime base image policy is invalid")
        result[image] = tuple(sorted(set(cves)))
    return result


def validate_runtime_base(dockerfile: Path, policy_path: Path) -> str:
    image = _read_runtime_base(dockerfile)
    policy = _read_json(policy_path)
    blocked = _validate_entries(policy.get("blocked"))
    approved = policy.get("approved")
    if not isinstance(approved, list) or any(
        not isinstance(candidate, str) or _OFFICIAL_PYTHON_RUNTIME.fullmatch(candidate) is None
        for candidate in approved
    ):
        raise GateError("runtime base image policy is invalid")
    if len(approved) != len(set(approved)) or set(approved).intersection(blocked):
        raise GateError("runtime base image policy is invalid")
    if image in blocked:
        raise GateError(
            "runtime base image is blocked by " + ",".join(blocked[image])
        )
    if image not in approved:
        raise GateError("runtime base image is not approved")
    return image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dockerfile", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    try:
        validate_runtime_base(args.dockerfile, args.policy)
    except GateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
