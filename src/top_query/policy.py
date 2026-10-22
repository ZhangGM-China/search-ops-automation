from typing import Mapping, Optional

from .constants import (
    CHECK_ITEM_KEYS,
    DEFAULT_TOP_QUERY_CHECK_MODES,
    DEFAULT_TOP_QUERY_TOOL_MODES,
    EXECUTION_MODE_AUTO,
    EXECUTION_MODE_DISABLED,
    EXECUTION_MODE_HUMAN_APPROVAL,
    EXECUTION_MODES,
    TOOL_KEYS,
)


class ActionPolicy:
    def __init__(
        self,
        check_modes: Optional[Mapping[str, str]] = None,
        tool_modes: Optional[Mapping[str, str]] = None,
    ):
        self.check_modes = self._merge_modes(DEFAULT_TOP_QUERY_CHECK_MODES, check_modes, "check item")
        self.tool_modes = self._merge_modes(DEFAULT_TOP_QUERY_TOOL_MODES, tool_modes, "tool")

    def resolve(self, check_item_key: str, tool_key: str) -> str:
        if check_item_key not in CHECK_ITEM_KEYS:
            raise ValueError(f"Unknown check item: {check_item_key}")
        if tool_key not in TOOL_KEYS:
            raise ValueError(f"Unknown tool: {tool_key}")

        return self.resolve_modes(self.check_modes[check_item_key], self.tool_modes[tool_key])

    @staticmethod
    def resolve_modes(check_mode: str, tool_mode: str) -> str:
        ActionPolicy._validate_mode(check_mode)
        ActionPolicy._validate_mode(tool_mode)

        if EXECUTION_MODE_DISABLED in {check_mode, tool_mode}:
            return EXECUTION_MODE_DISABLED
        if EXECUTION_MODE_HUMAN_APPROVAL in {check_mode, tool_mode}:
            return EXECUTION_MODE_HUMAN_APPROVAL
        return EXECUTION_MODE_AUTO

    @staticmethod
    def _merge_modes(defaults: Mapping[str, str], overrides: Optional[Mapping[str, str]], label: str) -> dict:
        modes = dict(defaults)
        if not overrides:
            return modes

        for key, mode in overrides.items():
            if key not in defaults:
                raise ValueError(f"Unknown {label}: {key}")
            ActionPolicy._validate_mode(mode)
            modes[key] = mode
        return modes

    @staticmethod
    def _validate_mode(mode: str) -> None:
        if mode not in EXECUTION_MODES:
            raise ValueError(f"Invalid execution mode: {mode}")
