from pathlib import Path
import time

import pytest

from src.assistant.terminal import TerminalManager, TerminalSecurityError


def test_terminal_rejects_workdir_outside_authorized_root(tmp_path: Path):
    manager = TerminalManager(roots=[tmp_path])
    with pytest.raises(TerminalSecurityError):
        manager.resolve_cwd("../outside")


def test_terminal_executes_command_and_captures_exit_code(tmp_path: Path):
    manager = TerminalManager(roots=[tmp_path])
    result = manager.execute("printf 'hello'", cwd=".")
    assert result["output"] == "hello"
    assert result["exitCode"] == 0
    failed = manager.execute("printf 'bad'; exit 3", cwd=".")
    assert failed["output"] == "bad"
    assert failed["exitCode"] == 3


def test_terminal_stop_marks_session_cancelled(tmp_path: Path):
    manager = TerminalManager(roots=[tmp_path])
    session = manager.create_session(".")
    assert manager.stop_session(session["terminalSessionId"])["status"] == "cancelled"
    assert manager.get_session(session["terminalSessionId"])["status"] == "cancelled"


def test_terminal_session_is_scoped_to_owner(tmp_path: Path):
    manager = TerminalManager(roots=[tmp_path])
    session = manager.create_session(".", owner_id=1)
    with pytest.raises(KeyError):
        manager.get_session(session["terminalSessionId"], owner_id=2)


def test_long_running_command_can_be_stopped_and_reports_partial_output(tmp_path: Path):
    manager = TerminalManager(roots=[tmp_path])
    session = manager.create_session(".", owner_id=7)
    started = manager.execute(
        "printf 'before'; sleep 1; printf 'after'",
        terminal_session_id=session["terminalSessionId"],
        owner_id=7,
    )
    assert started["status"] == "running"
    time.sleep(0.1)
    stopped = manager.stop_session(session["terminalSessionId"], owner_id=7)
    assert stopped["status"] == "cancelled"
    deadline = time.time() + 2
    while time.time() < deadline:
        current = manager.get_session(session["terminalSessionId"], owner_id=7)
        if current["status"] == "cancelled":
            break
        time.sleep(0.02)
    assert "before" in manager.get_session(session["terminalSessionId"], owner_id=7)["output"]


def test_terminal_returns_incremental_state_for_long_command(tmp_path: Path):
    manager = TerminalManager(roots=[tmp_path])
    session = manager.create_session(".")
    result = manager.execute(
        "printf 'ready'; sleep 1",
        terminal_session_id=session["terminalSessionId"],
    )
    time.sleep(0.1)
    assert result["status"] == "running"
    deadline = time.time() + 2
    while time.time() < deadline:
        result = manager.get_session(session["terminalSessionId"])
        if result["status"] == "completed":
            break
        time.sleep(0.02)
    assert result["status"] == "completed"
    assert result["output"] == "ready"
