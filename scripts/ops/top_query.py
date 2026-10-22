#!/usr/bin/env python3
"""Unified, compatibility-preserving Top Query operations CLI."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Callable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_HANDLER_MODULES = {
    "report": "scripts.ops.report_top_query_batch_anomalies",
    "remediate": "scripts.ops.remediate_top_query_false_zero",
}


def _load_handler(action: str) -> Callable[[list[str] | None], int]:
    module = importlib.import_module(_HANDLER_MODULES[action])
    handler = getattr(module, "main", None)
    if not callable(handler):
        raise RuntimeError(f"Top Query handler is unavailable: {action}")
    return handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Top Query operations. Run '<command> --help' for subcommand options."
    )
    parser.add_argument("command", choices=sorted(_HANDLER_MODULES))
    return parser


def main(argv: list[str] | None = None) -> int:
    command_argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if not command_argv:
        parser.error("the following arguments are required: command")
    if command_argv[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    if command_argv[0] not in _HANDLER_MODULES:
        parser.error(f"invalid choice: {command_argv[0]!r}")

    os.environ.setdefault("ENV", "test")
    result = _load_handler(command_argv[0])(command_argv[1:])
    return 0 if result is None else int(result)


if __name__ == "__main__":
    raise SystemExit(main())
