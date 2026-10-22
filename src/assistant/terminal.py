"""Account-scoped local terminal sessions for the PI Agent workbench."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from src.assistant.workspace import resolve_workspace_path, configured_workspace_roots


class TerminalSecurityError(ValueError):
    pass


class TerminalManager:
    def __init__(self, *, roots: list[Path] | None = None):
        self.roots = roots or configured_workspace_roots()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def resolve_cwd(self, cwd: str = ".") -> Path:
        try:
            return resolve_workspace_path(cwd, roots=self.roots)
        except ValueError as exc:
            raise TerminalSecurityError(str(exc)) from exc

    def create_session(self, cwd: str = ".", *, owner_id: int | None = None) -> dict[str, Any]:
        path = self.resolve_cwd(cwd)
        if not path.is_dir():
            raise FileNotFoundError("terminal working directory not found")
        session_id = f"terminal-{uuid.uuid4().hex}"
        record = {"terminalSessionId": session_id, "ownerId": owner_id, "cwd": str(path), "status": "idle", "output": "", "exitCode": None, "process": None, "done": threading.Event()}
        with self._lock:
            self._sessions[session_id] = record
        return self._public(record)

    def execute(self, command: str, *, cwd: str = ".", terminal_session_id: str | None = None, owner_id: int | None = None) -> dict[str, Any]:
        if terminal_session_id:
            session = self._session(terminal_session_id, owner_id=owner_id)
        else:
            created = self.create_session(cwd, owner_id=owner_id)
            session = self._session(created["terminalSessionId"], owner_id=owner_id)
        path = self.resolve_cwd(cwd) if terminal_session_id is None else Path(session["cwd"])
        if not str(command or "").strip():
            raise ValueError("terminal command is required")
        process = subprocess.Popen(command, cwd=path, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        with self._lock:
            session.update({"status": "running", "process": process, "output": "", "exitCode": None, "done": threading.Event()})
        threading.Thread(target=self._collect_output, args=(session, process), daemon=True).start()
        session["done"].wait(0.05)
        return self._public(session)

    def _collect_output(self, session: dict[str, Any], process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        chunks: list[str] = []
        for chunk in iter(lambda: process.stdout.read(1), ""):
            chunks.append(chunk)
            with self._lock:
                session["output"] = "".join(chunks)
                if session["status"] == "cancelled":
                    break
        process.stdout.close()
        return_code = process.wait()
        with self._lock:
            if session["status"] != "cancelled":
                session.update({"status": "completed" if return_code == 0 else "failed", "exitCode": return_code})
            session["process"] = None
            session["done"].set()

    def stop_session(self, terminal_session_id: str, *, owner_id: int | None = None) -> dict[str, Any]:
        session = self._session(terminal_session_id, owner_id=owner_id)
        process = session.get("process")
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        with self._lock:
            session.update({"status": "cancelled"})
        return self._public(session)

    def get_session(self, terminal_session_id: str, *, owner_id: int | None = None) -> dict[str, Any]:
        return self._public(self._session(terminal_session_id, owner_id=owner_id))

    def _session(self, terminal_session_id: str, *, owner_id: int | None = None) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(str(terminal_session_id))
        if not session or (owner_id is not None and session.get("ownerId") != owner_id):
            raise KeyError("terminal session not found")
        return session

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key not in {"process", "ownerId", "done"}}
