import base64
import hashlib
import hmac
import json
import zlib
from typing import Any, Dict


SNAPSHOT_TOKEN_VERSION = "v1"
MAX_SNAPSHOT_TOKEN_BYTES = 900_000
MAX_SNAPSHOT_JSON_BYTES = 8_000_000


class ReportSnapshotTokenError(ValueError):
    pass


def encode_report_snapshot(report: Dict[str, Any], secret_key: str) -> str:
    try:
        raw = json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReportSnapshotTokenError("invalid report snapshot") from exc
    if len(raw) > MAX_SNAPSHOT_JSON_BYTES:
        raise ReportSnapshotTokenError("report snapshot too large")
    payload = _base64url_encode(zlib.compress(raw))
    signed = f"{SNAPSHOT_TOKEN_VERSION}.{payload}"
    signature = _signature(signed, secret_key)
    token = f"{signed}.{_base64url_encode(signature)}"
    if len(token.encode("ascii")) > MAX_SNAPSHOT_TOKEN_BYTES:
        raise ReportSnapshotTokenError("report snapshot token too large")
    return token


def decode_report_snapshot(
    token: str,
    secret_key: str,
    expected_experiment_id: int,
) -> Dict[str, Any]:
    try:
        if not isinstance(token, str) or len(token.encode("utf-8")) > MAX_SNAPSHOT_TOKEN_BYTES:
            raise ReportSnapshotTokenError("invalid report snapshot token")
        version, payload, encoded_signature = token.split(".")
        if version != SNAPSHOT_TOKEN_VERSION:
            raise ReportSnapshotTokenError("invalid report snapshot token")
        signed = f"{version}.{payload}"
        actual_signature = _base64url_decode(encoded_signature)
        expected_signature = _signature(signed, secret_key)
        if not hmac.compare_digest(actual_signature, expected_signature):
            raise ReportSnapshotTokenError("invalid report snapshot token")
        compressed = _base64url_decode(payload)
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed, MAX_SNAPSHOT_JSON_BYTES + 1)
        if (
            len(raw) > MAX_SNAPSHOT_JSON_BYTES
            or decompressor.unconsumed_tail
            or decompressor.unused_data
            or not decompressor.eof
        ):
            raise ReportSnapshotTokenError("invalid report snapshot token")
        report = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        if not isinstance(report, dict):
            raise ReportSnapshotTokenError("invalid report snapshot token")
        experiment = report.get("experiment")
        if (
            not isinstance(experiment, dict)
            or isinstance(experiment.get("experimentId"), bool)
            or experiment.get("experimentId") != expected_experiment_id
        ):
            raise ReportSnapshotTokenError("invalid report snapshot token")
        return report
    except ReportSnapshotTokenError:
        raise
    except (UnicodeError, ValueError, TypeError, zlib.error) as exc:
        raise ReportSnapshotTokenError("invalid report snapshot token") from exc


def _signature(signed: str, secret_key: str) -> bytes:
    return hmac.new(
        str(secret_key).encode("utf-8"),
        signed.encode("ascii"),
        hashlib.sha256,
    ).digest()


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    encoded = value.encode("ascii")
    padding = b"=" * (-len(encoded) % 4)
    return base64.b64decode(encoded + padding, altchars=b"-_", validate=True)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
