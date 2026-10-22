from __future__ import annotations

import pytest

from src.persistence import Base, SessionLocal, reset_engine_for_testing
from src.llm.node_config import NODE_REGISTRY
from src.llm.config_store import (
    NodeModelConfigConflict,
    NodeModelConfigNotFound,
    NodeModelConfigValidationError,
    _normalize_entries,
    create_draft,
    get_active_bundle,
    get_active_published_overrides,
    publish_draft,
    rollback_active,
)


def setup_function():
    engine = reset_engine_for_testing()
    Base.metadata.create_all(bind=engine)


def teardown_function():
    from src.persistence import session

    Base.metadata.drop_all(bind=session.engine)
    session.engine.dispose()


def _entries(primary_suffix: str = "") -> dict[str, dict[str, object]]:
    return {
        node_key: {
            "channel": spec.channel,
            "primaryModel": (
                spec.default_primary
                if spec.required_capabilities
                else f"{spec.default_primary}{primary_suffix}"
            ),
            "fallbackModel": spec.default_fallback,
        }
        for node_key, spec in NODE_REGISTRY.items()
    }


def test_create_draft_publish_and_read_active_bundle():
    db = SessionLocal()
    try:
        draft = create_draft(db, _entries(), change_note="initial bundle")

        assert draft.version == 1
        assert draft.status == "draft"
        with pytest.raises(NodeModelConfigNotFound):
            get_active_bundle(db)

        published = publish_draft(db, draft.version, expected_version=None)
        bundle = get_active_bundle(db)

        assert published.id == draft.id
        assert published.status == "published"
        assert published.active_slot == "active"
        assert bundle.version == draft.version
        assert bundle.entries["no_result.intent"]["primaryModel"] == "qwen3.6-plus"
    finally:
        db.close()


def test_create_draft_accepts_models_used_by_multimodal_relevance_benchmark():
    entries = _entries()
    entries["query_governance.relevance_multimodal"].update(
        {
            "primaryModel": "qwen3.7-plus",
            "fallbackModel": "qwen3.6-plus",
        }
    )
    db = SessionLocal()
    try:
        draft = create_draft(db, entries)
        relevance = next(
            entry for entry in draft.entries
            if entry.node_key == "query_governance.relevance_multimodal"
        )
        assert relevance.primary_model == "qwen3.7-plus"
        assert relevance.fallback_model == "qwen3.6-plus"
    finally:
        db.close()


def test_normalize_entries_rejects_retired_fastclaw_assistant_channel():
    import pytest

    entries = _entries()
    entries["assistant.chat"]["channel"] = "fastclaw"

    with pytest.raises(NodeModelConfigValidationError, match="pi_agent channel"):
        _normalize_entries(entries)


def test_startup_migrates_persisted_fastclaw_assistant_channel():
    from src.persistence import schema as persistence_schema
    from sqlalchemy import create_engine, text

    legacy_engine = create_engine("sqlite://")
    try:
        with legacy_engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE node_model_config_entry (
                    id INTEGER PRIMARY KEY,
                    version_id INTEGER NOT NULL,
                    node_key VARCHAR(120) NOT NULL,
                    channel VARCHAR(30) NOT NULL,
                    primary_model VARCHAR(120) NOT NULL,
                    fallback_model VARCHAR(120)
                )
            """))
            conn.execute(text("INSERT INTO node_model_config_entry (id, version_id, node_key, channel, primary_model) VALUES (1, 1, 'assistant.chat', 'fastclaw', 'gpt-5.6-sol')"))
            conn.execute(text("INSERT INTO node_model_config_entry (id, version_id, node_key, channel, primary_model) VALUES (2, 1, 'query_governance.kg_agent', 'rd_llm', 'qwen3.6-plus')"))
        original_engine = persistence_schema._session.engine
        persistence_schema._session.engine = legacy_engine
        try:
            persistence_schema._ensure_schema_upgrades()
        finally:
            persistence_schema._session.engine = original_engine
        with legacy_engine.connect() as conn:
            channels = {row[0]: row[1] for row in conn.execute(text("SELECT id, channel FROM node_model_config_entry"))}
        assert channels == {1: "pi_agent", 2: "rd_llm"}
    finally:
        legacy_engine.dispose()


def test_active_published_overrides_are_runtime_ready_and_empty_before_first_publish():
    db = SessionLocal()
    try:
        assert get_active_published_overrides(db) == {}
        draft = create_draft(db, _entries())
        assert get_active_published_overrides(db) == {}
        publish_draft(db, draft.version, expected_version=None)

        overrides = get_active_published_overrides(db)
        assert set(overrides) == set(NODE_REGISTRY)
        assert overrides["no_result.intent"] == {
            "primaryModel": "qwen3.6-plus",
            "fallbackModel": None,
        }
    finally:
        db.close()


def test_publish_uses_expected_version_cas_and_replays_same_draft():
    db = SessionLocal()
    try:
        first = create_draft(db, _entries(), change_note="first")
        publish_draft(db, first.version, expected_version=None)
        second = create_draft(db, _entries("-next"), change_note="second")

        with pytest.raises(NodeModelConfigConflict):
            publish_draft(db, second.version, expected_version=None)

        published = publish_draft(db, second.version, expected_version=first.version)
        replayed = publish_draft(db, second.version, expected_version=first.version)

        assert published.id == second.id
        assert replayed.id == second.id
        assert get_active_bundle(db).version == second.version
    finally:
        db.close()


def test_rollback_creates_new_immutable_bundle_and_replays_idempotently():
    db = SessionLocal()
    try:
        first = create_draft(db, _entries(), change_note="first")
        publish_draft(db, first.version, expected_version=None)
        second = create_draft(db, _entries("-next"), change_note="second")
        publish_draft(db, second.version, expected_version=first.version)

        rolled_back = rollback_active(
            db,
            target_version=first.version,
            expected_version=second.version,
        )
        replayed = rollback_active(
            db,
            target_version=first.version,
            expected_version=second.version,
        )
        bundle = get_active_bundle(db)

        assert rolled_back.version > second.version
        assert replayed.id == rolled_back.id
        assert bundle.version == rolled_back.version
        assert bundle.entries == get_active_bundle(db).entries
        assert bundle.entries["no_result.intent"]["primaryModel"] == "qwen3.6-plus"
        persisted = {
            entry.node_key: entry.primary_model
            for entry in db.get(type(first), first.id).entries
        }
        assert persisted["no_result.intent"] == "qwen3.6-plus"
    finally:
        db.close()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda entries: entries.pop("no_result.intent"),
        lambda entries: entries.update({"unknown.node": entries["no_result.intent"]}),
        lambda entries: entries["no_result.intent"].update({"channel": "legacy_agent"}),
        lambda entries: entries["no_result.intent"].update(
            {"fallbackModel": entries["no_result.intent"]["primaryModel"]}
        ),
    ],
)
def test_create_draft_fails_closed_for_invalid_registry_payload(mutator):
    entries = _entries()
    mutator(entries)
    db = SessionLocal()
    try:
        with pytest.raises(NodeModelConfigValidationError):
            create_draft(db, entries)
    finally:
        db.close()


def test_store_rejects_invalid_control_arguments_and_missing_records():
    db = SessionLocal()
    try:
        with pytest.raises(NodeModelConfigValidationError):
            create_draft(db, _entries(), change_note=123)  # type: ignore[arg-type]
        with pytest.raises(NodeModelConfigValidationError):
            create_draft(db, _entries(), change_note="x" * 241)
        with pytest.raises(NodeModelConfigValidationError):
            publish_draft(db, True, expected_version=None)
        with pytest.raises(NodeModelConfigNotFound):
            publish_draft(db, 999, expected_version=None)
        with pytest.raises(NodeModelConfigValidationError):
            rollback_active(db, target_version=True, expected_version=None)
        with pytest.raises(NodeModelConfigNotFound):
            rollback_active(db, target_version=1, expected_version=None)
    finally:
        db.close()


def test_published_version_cannot_be_published_again_after_it_is_retired():
    db = SessionLocal()
    try:
        first = create_draft(db, _entries())
        publish_draft(db, first.version, expected_version=None)
        second = create_draft(db, _entries("-next"))
        publish_draft(db, second.version, expected_version=first.version)

        with pytest.raises(NodeModelConfigConflict):
            publish_draft(db, first.version, expected_version=second.version)
        with pytest.raises(NodeModelConfigNotFound):
            rollback_active(db, target_version=999, expected_version=second.version)
    finally:
        db.close()
