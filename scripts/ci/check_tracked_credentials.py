#!/usr/bin/env python3
"""Fail closed on literal credentials and unsafe production access in tracked files."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_GUIDE = Path("docs/production_deployment_guide.md")
# This legacy external-interface reference is intentionally kept out of the
# credential gate. Its examples are governed separately and may contain
# approved reference values that must not be rewritten by this baseline fix.
_CREDENTIAL_SCAN_EXEMPTIONS = {
    Path("docs/api/01-外部业务系统接口.md"),
}
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?m)^\s*(?:export\s+)?(?P<name>[A-Z][A-Z0-9_]*(?:PASS(?:WORD)?|SECRET(?:_KEY)?|CREDENTIAL|API[_-]?KEY|TOKEN))\s*=\s*(?P<value>[^\s#]+)"
)
_TCL_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?im)^\s*set\s+[A-Za-z_][A-Za-z0-9_]*(?:pass(?:word)?|secret|token|api[_-]?key|credential)[A-Za-z0-9_]*\s+(?P<value>[^\s#]+)"
)
_CODE_LITERAL_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:(?:const|let|var)\s+)?[A-Za-z_][A-Za-z0-9__]*(?:password|secret(?:_key)?|token|api[_-]?key|credential)\s*(?::[^=\n]+)?=\s*(?P<quote>['\"])(?P<value>[^'\"\r\n]+)(?P=quote)"
)
_MAPPING_LITERAL_ASSIGNMENT = re.compile(
    r"(?im)^\s*['\"]?[A-Za-z_][A-Za-z0-9_]*(?:password|secret(?:_key)?|token|api[_-]?key|credential)['\"]?\s*:\s*(?P<quote>['\"])(?P<value>[^'\"\r\n]+)(?P=quote)"
)
_MARKDOWN_LITERAL_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:[-*+]\s+)?`?"
    r"(?:password|passwd|secret(?:_key)?|token|api[_-]?key|credential|"
    r"[A-Za-z_][A-Za-z0-9_]*(?:password|secret(?:_key)?|token|api[_-]?key|credential))"
    r"`?\s*:\s*(?P<value>[A-Za-z0-9][A-Za-z0-9._@%+/:=-]*)"
)
_MARKDOWN_INLINE_CODE_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9_])`?"
    r"(?:password|passwd|secret(?:_key)?|token|api[_-]?key|credential|"
    r"[A-Za-z_][A-Za-z0-9_]*(?:password|secret(?:_key)?|token|api[_-]?key|credential))"
    r"`?\s+`(?P<value>[^`\r\n]+)`(?=[\s,.;:!?)]|$)"
)
_SAFE_TEST_ASSIGNMENT = re.compile(
    r"(?i)^(?:test|example|placeholder|changeme|replace[-_]me)(?:[-_](?:key|token|password|secret))?$"
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)\bauthorization\b\s*[:=|]\s*(?:bearer|basic)\s+(?P<value>[^\s|]+)"
)
_URL_USERINFO = re.compile(
    r"(?i)https?://(?P<username>[^/@\s:]+):(?P<value>[^/@\s]+)@"
)
_UNSAFE_RELEASE_RULES = (
    (
        "password_environment",
        re.compile(
            r"(?im)\b[A-Z][A-Z0-9_]*(?:PASS(?:WORD)?|SECRET|CREDENTIAL|TOKEN)\s*=\s*\$\{?[A-Z][A-Z0-9_]*\}?"
        ),
    ),
    (
        "password_environment",
        re.compile(
            r"(?im)^\s*set\s+[A-Za-z_][A-Za-z0-9_]*(?:pass(?:word)?|secret|credential|token)[A-Za-z0-9_]*\s+\$env\("
        ),
    ),
    ("sudo_stdin", re.compile(r"(?i)\bsudo\s+-S\b")),
    (
        "ssh_host_key_checking_disabled",
        re.compile(r"(?i)\bStrictHostKeyChecking\s*=\s*no\b"),
    ),
    (
        "expect_password_interaction",
        re.compile(
            r"(?im)\bexpect_user\b|\bsend\s+(?:--\s+)?['\"]?\$[A-Za-z_]*pass|assword\.\*['\"]?\s*\{"
        ),
    ),
    ("legacy_production_entry", re.compile(r"(?im)^\s*spawn\s+ssh\b")),
    (
        "unrestricted_root_shell",
        re.compile(r"(?i)\bsudo\s+-n(?:\s+--)?\s+bash\s+-c\b"),
    ),
)
_PRIVATE_KEY_HEADER = re.compile(
    r"(?m)^-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----\s*$"
)
_SSHPASS = re.compile(r"(?i)\bsshpass\b")


def tracked_paths(project_root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=project_root,
        check=True,
        capture_output=True,
    )
    # `git ls-files` intentionally includes deleted-but-not-yet-staged paths.
    # They are no longer repository content and cannot contain a credential;
    # omit them so cleanup commits can remove tracked files without breaking
    # the scanner's read pass.
    return [
        relative
        for item in result.stdout.split(b"\0")
        if item
        for relative in [Path(item.decode("utf-8"))]
        if (project_root / relative).is_file()
    ]


def _is_runtime_or_placeholder(value: str) -> bool:
    normalized = value.strip().strip("'\"`{}<>[](),.;:")
    if not normalized:
        return True
    if normalized.lower() in {
        "redacted",
        "masked",
        "password",
        "token",
        "access_token",
        "api_key",
        "secret",
        "credential",
        "none",
        "null",
        "string",
        "str",
    }:
        return True
    if set(normalized) <= {"*", "x", "X", "-"}:
        return True
    if normalized.startswith(("$", "os.getenv(", "os.environ", "settings.", "config.", "process.env", "System.getenv(")):
        return True
    if _SAFE_TEST_ASSIGNMENT.fullmatch(normalized):
        return True
    if re.match(
        r"(?i)^(?:test|fake|mock|dummy|example|placeholder|invalid)(?:[-_].+)?$",
        normalized,
    ):
        return True
    return any(
        marker in normalized.lower()
        for marker in (
            "example",
            "placeholder",
            "replace",
            "your_",
            "your-",
            "<",
            "secret-provider",
            "secret_provider",
            "managed-secret",
        )
    )


def _is_test_source(relative: Path) -> bool:
    value = relative.as_posix()
    return (
        "tests" in relative.parts
        or ".test." in relative.name
        or ".spec." in relative.name
        or relative.name.startswith("test_")
        or value.startswith("tests/manual_scripts/")
    )


def _literal_assignment_rules(relative: Path, content: str) -> set[str]:
    findings: set[str] = set()
    patterns = [_SENSITIVE_ASSIGNMENT, _TCL_SENSITIVE_ASSIGNMENT]
    if not _is_test_source(relative):
        patterns.extend((_CODE_LITERAL_ASSIGNMENT, _MAPPING_LITERAL_ASSIGNMENT))
    for pattern in patterns:
        for match in pattern.finditer(content):
            name = match.groupdict().get("name", "")
            if name.startswith("TEST_"):
                continue
            if not _is_runtime_or_placeholder(match.group("value")):
                findings.add("credential_assignment")
    if relative.suffix.lower() in {".md", ".mdx"}:
        for match in _MARKDOWN_LITERAL_ASSIGNMENT.finditer(content):
            if not _is_runtime_or_placeholder(match.group("value")):
                findings.add("credential_assignment")
        for match in _MARKDOWN_INLINE_CODE_ASSIGNMENT.finditer(content):
            value = match.group("value").strip()
            if len(value) >= 12 and not _is_runtime_or_placeholder(value):
                findings.add("credential_assignment")
    return findings


def _has_high_confidence_authorization_literal(content: str) -> bool:
    for match in _AUTHORIZATION_VALUE.finditer(content):
        value = match.group("value").strip("'\"`<>()[].,;:")
        if _is_runtime_or_placeholder(value):
            continue
        if len(value) >= 16 and any(character.isdigit() for character in value):
            return True
    return False


def _has_uriviera_userinfo_credential(content: str) -> bool:
    return any(
        not _is_runtime_or_placeholder(match.group("value"))
        for match in _URL_USERINFO.finditer(content)
    )


def _is_release_or_legacy_operator_path(relative: Path) -> bool:
    value = relative.as_posix()
    return (
        relative.suffix == ".exp"
        or value.startswith(("scripts/release/", "scripts/ops/"))
    )


def _unsafe_release_rules(relative: Path, content: str) -> set[str]:
    if not _is_release_or_legacy_operator_path(relative):
        return set()
    return {name for name, pattern in _UNSAFE_RELEASE_RULES if pattern.search(content)}


def scan_paths(project_root: Path, paths: list[Path]) -> list[tuple[str, tuple[str, ...]]]:
    findings: list[tuple[str, tuple[str, ...]]] = []
    for path in paths:
        relative = path if not path.is_absolute() else path.relative_to(project_root)
        if relative in _CREDENTIAL_SCAN_EXEMPTIONS:
            continue
        candidate = project_root / relative
        try:
            payload = candidate.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"unable to scan tracked file: {relative.as_posix()}") from exc
        if b"\0" in payload:
            continue
        content = payload.decode("utf-8", errors="replace")
        rules = _literal_assignment_rules(relative, content)
        if _PRIVATE_KEY_HEADER.search(content):
            rules.add("private_key_header")
        if _SSHPASS.search(content) and _is_release_or_legacy_operator_path(relative):
            rules.add("sshpass")
        if _has_high_confidence_authorization_literal(content):
            rules.add("authorization_literal")
        if _has_uriviera_userinfo_credential(content):
            rules.add("uriviera_userinfo_credential")
        rules.update(_unsafe_release_rules(relative, content))
        if rules:
            findings.append((relative.as_posix(), tuple(sorted(rules))))
    return sorted(findings)


def format_findings(findings: list[tuple[str, tuple[str, ...]]]) -> str:
    return "".join(f"{path}: {', '.join(rules)}\n" for path, rules in findings)


def main() -> int:
    paths = tracked_paths(PROJECT_ROOT)
    if PRODUCTION_GUIDE not in paths:
        print(f"{PRODUCTION_GUIDE.as_posix()}: not_git_tracked", file=sys.stderr)
        return 1
    findings = scan_paths(PROJECT_ROOT, paths)
    if findings:
        sys.stderr.write(format_findings(findings))
        return 1
    print("Git-tracked credential scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
