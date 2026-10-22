"""Ports used by the deterministic workflow.

Concrete database, search, model, and action integrations are supplied by
later tasks.  Keeping them as protocols prevents the new workflow from
depending on the legacy Agent Loop or on a particular persistence adapter.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .models import ActionRecoveryResult, NodeResult, QuerySpec


class SearchGateway(Protocol):
    def mixed(self, brand: str, query: str) -> Sequence[Mapping[str, Any]]:
        """Return the current Mixed search results for one Query."""


class WorldFactsGateway(Protocol):
    def collect(
        self,
        value: date,
        weather_cities: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Return normalized engineering facts for one data date."""


class StateStore(Protocol):
    def get_s0(self, brand: str, query: str) -> Mapping[str, Any] | None:
        """Read the immutable governance-before snapshot, if it exists."""

    def save_s0_if_absent(
        self,
        brand: str,
        query: str,
        snapshot: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Create S0 once and return the authoritative stored value."""

    def save_s0_failure(
        self,
        brand: str,
        query: str,
        error_code: str,
        error_message: str,
    ) -> None:
        """Persist a failed S0 request without creating a successful snapshot."""

    def save_step(
        self,
        brand: str,
        query: str,
        node: str,
        result: NodeResult,
    ) -> None:
        """Persist a completed node result immediately."""

    def save_model_config_version(self, batch_id: str, version: int) -> None:
        """Pin the immutable model configuration version for one batch."""


class ExperimentMetricsObserver(Protocol):
    """Persist one treatment/control observation after a Query run."""

    def record(
        self,
        spec: QuerySpec,
        run: Any,
        *,
        data_date: date | None,
        batch_id: str | None,
    ) -> None:
        ...

    def record_observation(
        self,
        spec: QuerySpec,
        products: Sequence[Mapping[str, Any]] | None,
        *,
        data_date: date | None,
        batch_id: str | None,
        error: str | None = None,
    ) -> None:
        ...


class CheckpointStore(Protocol):
    """Batch recovery operations backed by the new Query tables."""

    def start_batch(
        self,
        batch_id: str,
        specs: Iterable[QuerySpec],
        *,
        data_date: date | None = None,
    ) -> Any:
        ...

    def begin_node(
        self,
        batch_id: str,
        spec: QuerySpec,
        node: str,
        expected_version: int,
    ) -> Any:
        ...

    def save_action(
        self,
        batch_id: str,
        spec: QuerySpec,
        node: str,
        action: Any,
        expected_version: int,
    ) -> Any:
        ...

    def record_node(
        self,
        batch_id: str,
        spec: QuerySpec,
        node: str,
        result: NodeResult,
        expected_version: int,
    ) -> Any:
        ...

    def finish_batch(self, batch_id: str, expected_version: int) -> Any:
        ...

    def yield_batch(self, batch_id: str, expected_version: int) -> Any:
        """Persist a voluntary yield without changing the original scope."""
        ...

    def get_node(
        self, batch_id: str, spec: QuerySpec, node: str
    ) -> NodeResult | None:
        ...

    def get_inflight_action(
        self, batch_id: str, spec: QuerySpec, node: str
    ) -> Any:
        ...

    def all_nodes_terminal(self, batch_id: str) -> bool:
        ...

    def s0_store(self, batch_id: str) -> StateStore:
        """Return an S0 store permanently scoped to one batch."""


class NodeRunner(Protocol):
    def run(self, spec: QuerySpec, context: Mapping[str, Any]) -> NodeResult:
        """Run one deterministic node for one Query."""


class ActionGateway(Protocol):
    def execute_and_verify(
        self,
        spec: QuerySpec,
        action: Any,
        context: Mapping[str, Any],
    ) -> NodeResult:
        """Execute a governed action and verify its authoritative effect."""

    def recover_interrupted(
        self,
        spec: QuerySpec,
        node: str,
        action: Any,
        context: Mapping[str, Any],
    ) -> ActionRecoveryResult:
        """Use request identity and authoritative facts before any retry."""
