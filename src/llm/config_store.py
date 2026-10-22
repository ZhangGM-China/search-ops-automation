"""Transactional persistence for versioned node model configuration bundles.

The store intentionally stops at persistence. API authorization, audit actor
identity, and runtime wiring belong to later slices, while this module keeps
the bundle validation and version transitions deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Sequence

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from src.persistence import NodeModelConfigEntry, NodeModelConfigVersion, SessionLocal
from .node_config import (
    NODE_REGISTRY,
    NodeConfigError,
    NodeConfigSnapshot,
    capture_snapshot,
    resolve_node_config,
)


class NodeModelConfigStoreError(RuntimeError):
    """Base error for model configuration persistence failures."""


class NodeModelConfigConflict(NodeModelConfigStoreError):
    """Raised when a version CAS or immutable transition cannot be applied."""


class NodeModelConfigNotFound(NodeModelConfigStoreError):
    """Raised when an expected draft, active bundle, or rollback target is absent."""


class NodeModelConfigValidationError(NodeModelConfigStoreError):
    """Raised when a bundle cannot be resolved by the runtime registry."""


@dataclass(frozen=True)
class NodeModelConfigBundle:
    version: int
    entries: Mapping[str, Mapping[str, object]]
    snapshot: NodeConfigSnapshot


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_note(change_note: str | None) -> str | None:
    if change_note is None:
        return None
    if not isinstance(change_note, str):
        raise NodeModelConfigValidationError("change_note must be a string or null")
    normalized = change_note.strip()
    if len(normalized) > 240:
        raise NodeModelConfigValidationError("change_note exceeds 240 characters")
    return normalized or None


def _validate_expected_version(expected_version: int | None) -> None:
    if expected_version is not None and (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version <= 0
    ):
        raise NodeModelConfigValidationError("expected_version must be a positive integer or null")


def _normalize_entries(entries: Mapping[str, Mapping[str, object]]) -> dict[str, dict[str, object]]:
    if not isinstance(entries, Mapping):
        raise NodeModelConfigValidationError("entries must be a mapping")
    if set(entries) != set(NODE_REGISTRY):
        missing = sorted(set(NODE_REGISTRY) - set(entries))
        unknown = sorted(set(entries) - set(NODE_REGISTRY))
        raise NodeModelConfigValidationError(
            f"entries must contain exactly NODE_REGISTRY; missing={missing}, unknown={unknown}"
        )

    normalized: dict[str, dict[str, object]] = {}
    try:
        for node_key, spec in NODE_REGISTRY.items():
            payload = entries[node_key]
            if not isinstance(payload, Mapping):
                raise NodeModelConfigValidationError(f"{node_key} entry must be a mapping")
            if set(payload) != {"channel", "primaryModel", "fallbackModel"}:
                raise NodeModelConfigValidationError(f"{node_key} entry fields are invalid")
            channel = payload["channel"]
            if channel != spec.channel:
                raise NodeModelConfigValidationError(
                    f"{node_key} must use {spec.channel} channel"
                )
            resolved = resolve_node_config(
                node_key,
                published={
                    "primaryModel": payload["primaryModel"],
                    "fallbackModel": payload["fallbackModel"],
                },
            )
            normalized[node_key] = {
                "channel": resolved.channel,
                "primaryModel": resolved.primary_model,
                "fallbackModel": resolved.fallback_model,
            }
    except NodeConfigError as exc:
        raise NodeModelConfigValidationError(str(exc)) from exc
    return normalized


def _next_version(db) -> int:
    current = db.query(func.max(NodeModelConfigVersion.version)).scalar()
    return int(current or 0) + 1


def _active_record(db) -> NodeModelConfigVersion | None:
    return (
        db.query(NodeModelConfigVersion)
        .filter(
            NodeModelConfigVersion.status == "published",
            NodeModelConfigVersion.active_slot == "active",
        )
        .one_or_none()
    )


def _check_expected(active: NodeModelConfigVersion | None, expected_version: int | None) -> None:
    _validate_expected_version(expected_version)
    actual = active.version if active is not None else None
    if actual != expected_version:
        raise NodeModelConfigConflict(
            f"active version changed: expected={expected_version}, actual={actual}"
        )


def _record_entries(record: NodeModelConfigVersion) -> dict[str, dict[str, object]]:
    payload = {
        entry.node_key: {
            "channel": entry.channel,
            "primaryModel": entry.primary_model,
            "fallbackModel": entry.fallback_model,
        }
        for entry in record.entries
    }
    # Bundles published before the relevance governance node existed remain
    # readable; the new node inherits its registry default until republished.
    node_key = "query_governance.relevance_governance"
    if node_key not in payload:
        resolved = resolve_node_config(node_key)
        payload[node_key] = {
            "channel": resolved.channel,
            "primaryModel": resolved.primary_model,
            "fallbackModel": resolved.fallback_model,
        }
    return _normalize_entries(payload)


def _bundle(record: NodeModelConfigVersion) -> NodeModelConfigBundle:
    entries = _record_entries(record)
    published = {
        node_key: {
            "primaryModel": payload["primaryModel"],
            "fallbackModel": payload["fallbackModel"],
        }
        for node_key, payload in entries.items()
    }
    try:
        snapshot = capture_snapshot(published=published, version=record.version)
    except NodeConfigError as exc:
        raise NodeModelConfigValidationError(str(exc)) from exc
    return NodeModelConfigBundle(
        version=record.version,
        entries=MappingProxyType(
            {node_key: MappingProxyType(payload) for node_key, payload in entries.items()}
        ),
        snapshot=snapshot,
    )


def _add_entries(record: NodeModelConfigVersion, entries: Mapping[str, Mapping[str, object]]) -> None:
    record.entries = [
        NodeModelConfigEntry(
            node_key=node_key,
            channel=payload["channel"],
            primary_model=payload["primaryModel"],
            fallback_model=payload["fallbackModel"],
        )
        for node_key, payload in entries.items()
    ]


def create_draft(
    db,
    entries: Mapping[str, Mapping[str, object]],
    *,
    change_note: str | None = None,
    commit: bool = True,
):
    normalized = _normalize_entries(entries)
    note = _normalize_note(change_note)
    record = NodeModelConfigVersion(
        version=_next_version(db),
        status="draft",
        change_note=note,
        created_at=_utc_now(),
    )
    _add_entries(record, normalized)
    try:
        db.add(record)
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(record)
        return record
    except IntegrityError as exc:
        db.rollback()
        raise NodeModelConfigConflict("node model config version allocation conflicted") from exc
    except BaseException:
        db.rollback()
        raise


def get_active_bundle(db) -> NodeModelConfigBundle:
    record = _active_record(db)
    if record is None:
        raise NodeModelConfigNotFound("no active node model config bundle")
    return _bundle(record)


def get_active_published_overrides(db) -> Mapping[str, Mapping[str, object]]:
    """Return active overrides in the shape accepted by ``capture_snapshot``.

    An empty result preserves the existing environment/default resolution until
    the first bundle is published.
    """
    record = _active_record(db)
    if record is None:
        return MappingProxyType({})
    bundle = _bundle(record)
    return MappingProxyType(
        {
            node_key: MappingProxyType(
                {
                    "primaryModel": payload["primaryModel"],
                    "fallbackModel": payload["fallbackModel"],
                }
            )
            for node_key, payload in bundle.entries.items()
        }
    )


def capture_active_snapshot(db, *, node_keys=None) -> NodeConfigSnapshot:
    """Capture a runtime snapshot using the currently published overrides."""
    record = _active_record(db)
    if record is None:
        return capture_snapshot(node_keys=node_keys)
    entries = _record_entries(record)
    published = {
        node_key: {
            "primaryModel": payload["primaryModel"],
            "fallbackModel": payload["fallbackModel"],
        }
        for node_key, payload in entries.items()
    }
    return capture_snapshot(
        node_keys=node_keys,
        published=published,
        version=record.version,
    )


def capture_runtime_snapshot(
    *,
    node_keys: Sequence[str] | None = None,
    session_factory=SessionLocal,
) -> NodeConfigSnapshot:
    """Capture a task-start snapshot using the currently published bundle."""
    db = session_factory()
    try:
        return capture_active_snapshot(db, node_keys=node_keys)
    finally:
        db.close()


def publish_draft(
    db,
    version: int,
    *,
    expected_version: int | None,
    commit: bool = True,
):
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise NodeModelConfigValidationError("version must be a positive integer")
    record = db.query(NodeModelConfigVersion).filter_by(version=version).one_or_none()
    if record is None:
        raise NodeModelConfigNotFound(f"node model config version {version} not found")
    if record.status == "published" and record.active_slot == "active":
        return record
    if record.status != "draft" or record.active_slot is not None:
        raise NodeModelConfigConflict(f"node model config version {version} is not publishable")

    _record_entries(record)
    active = _active_record(db)
    _check_expected(active, expected_version)
    if active is not None:
        active.status = "retired"
        active.active_slot = None
    record.status = "published"
    record.active_slot = "active"
    record.published_at = _utc_now()
    try:
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(record)
        return record
    except IntegrityError as exc:
        db.rollback()
        raise NodeModelConfigConflict("node model config publish conflicted") from exc
    except BaseException:
        db.rollback()
        raise


def rollback_active(
    db,
    *,
    target_version: int,
    expected_version: int | None,
    commit: bool = True,
):
    if isinstance(target_version, bool) or not isinstance(target_version, int) or target_version <= 0:
        raise NodeModelConfigValidationError("target_version must be a positive integer")
    active = _active_record(db)
    if active is None:
        raise NodeModelConfigNotFound("no active node model config bundle")
    marker = f"rollback:{target_version}"
    if active.change_note == marker:
        return active
    _check_expected(active, expected_version)
    target = db.query(NodeModelConfigVersion).filter_by(version=target_version).one_or_none()
    if target is None:
        raise NodeModelConfigNotFound(f"rollback target version {target_version} not found")
    normalized = _record_entries(target)
    record = NodeModelConfigVersion(
        version=_next_version(db),
        status="published",
        active_slot="active",
        created_at=_utc_now(),
        published_at=_utc_now(),
        change_note=marker,
    )
    _add_entries(record, normalized)
    active.status = "retired"
    active.active_slot = None
    try:
        db.add(record)
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(record)
        return record
    except IntegrityError as exc:
        db.rollback()
        raise NodeModelConfigConflict("node model config rollback conflicted") from exc
    except BaseException:
        db.rollback()
        raise
