import re
from typing import Any, Dict


_CREDENTIAL_KEY_PATTERN = (
    r"(?:token|api[_-]?key|password|secret|client[_-]?secret|"
    r"aws[_-]?access[_-]?key[_-]?id|aws[_-]?secret[_-]?access[_-]?key)"
)


def _sanitize_action_text(value: Any, *, redact_urls: bool) -> str:
    message = str(value or "")
    message = re.sub(
        r"\b(?:postgres(?:ql)?|mysql(?:\+[a-z0-9_]+)?|mariadb|redis|mongodb(?:\+srv)?)://[^\s\"'<>]+",
        "[REDACTED_DSN]",
        message,
        flags=re.IGNORECASE,
    )
    if redact_urls:
        message = re.sub(r"https?://[^\s\"']+", "[REDACTED_URL]", message, flags=re.IGNORECASE)
    message = re.sub(
        r"(Authorization\s*[:=]\s*)Basic\s+[^\s,;]+",
        r"\1Basic [REDACTED]",
        message,
        flags=re.IGNORECASE,
    )
    message = re.sub(r"Bearer\s+[^\s\"']+", "Bearer [REDACTED]", message, flags=re.IGNORECASE)
    message = re.sub(
        rf'([\"\']?{_CREDENTIAL_KEY_PATTERN}[\"\']?\s*[:=]\s*)([\"\']?)[^\s,}}\"\']+\2',
        r'\1"[REDACTED]"',
        message,
        flags=re.IGNORECASE,
    )
    return message[:500]


def sanitize_action_error(value: Any) -> str:
    return _sanitize_action_text(value, redact_urls=True)


def sanitize_action_error_details(value: Any, *, redact_urls: bool = False) -> Any:
    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if (
                normalized_key in {
                    "apikey",
                    "password",
                    "secret",
                    "authorization",
                    "clientsecret",
                    "awsaccesskeyid",
                    "awssecretaccesskey",
                }
                or normalized_key.endswith("token")
            ):
                sanitized[key] = "[REDACTED]"
                continue
            child_redact_urls = redact_urls or normalized_key in {"error", "message", "reason", "toolresponse"}
            sanitized[key] = sanitize_action_error_details(item, redact_urls=child_redact_urls)
        return sanitized
    if isinstance(value, list):
        return [sanitize_action_error_details(item, redact_urls=redact_urls) for item in value]
    if isinstance(value, tuple):
        return [sanitize_action_error_details(item, redact_urls=redact_urls) for item in value]
    if isinstance(value, str):
        return _sanitize_action_text(value, redact_urls=redact_urls)
    return value
