from unittest.mock import MagicMock, patch

from src.llm.config_store import capture_runtime_snapshot


def test_capture_runtime_snapshot_reads_active_published_overrides_and_closes_session():
    session = MagicMock()
    snapshot = object()

    with patch(
        "src.llm.config_store.capture_active_snapshot", return_value=snapshot
    ) as capture:
        result = capture_runtime_snapshot(
            node_keys=("no_result.intent",),
            session_factory=lambda: session,
        )

    assert result is snapshot
    capture.assert_called_once_with(
        session,
        node_keys=("no_result.intent",),
    )
    session.close.assert_called_once_with()
