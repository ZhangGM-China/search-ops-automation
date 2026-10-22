"""Read-only inventory and locked retirement for obsolete Top Query experiments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

RETIRED_VERSION = "top-query-ab-test-" + "v1"
MANIFEST_VERSION = 1

_RETIRED_WHERE = (
    "calculation_version IS NULL OR calculation_version = :retired_version"
)
_VERSION_PARAM = {"retired_version": RETIRED_VERSION}


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _rows(connection: Connection, statement, params: Mapping[str, Any] | None = None):
    result = connection.execute(statement, dict(params or {}))
    try:
        return result.mappings().all()
    except AttributeError as exc:
        raise RuntimeError("legacy_experiment_inventory_result_invalid") from exc


def _manifest(connection: Connection, *, lock: bool = False) -> dict[str, Any]:
    lock_clause = (
        " FOR UPDATE"
        if lock and connection.dialect.name in {"mysql", "mariadb"}
        else ""
    )
    experiment_rows = _rows(
        connection,
        text(
            "SELECT id, calculation_version FROM top_query_experiment "
            f"WHERE {_RETIRED_WHERE} ORDER BY id{lock_clause}"
        ),
        _VERSION_PARAM,
    )
    experiment_ids = [int(row["id"]) for row in experiment_rows]
    samples_by_experiment = {experiment_id: [] for experiment_id in experiment_ids}
    metrics_by_experiment = {experiment_id: [] for experiment_id in experiment_ids}

    if experiment_ids:
        id_param = bindparam("experiment_ids", expanding=True)
        child_params = {"experiment_ids": experiment_ids}
        sample_rows = _rows(
            connection,
            text(
                "SELECT id, experiment_id FROM top_query_experiment_sample "
                "WHERE experiment_id IN :experiment_ids ORDER BY experiment_id, id"
                + lock_clause
            ).bindparams(id_param),
            child_params,
        )
        metric_rows = _rows(
            connection,
            text(
                "SELECT id, experiment_id FROM top_query_experiment_daily_metric "
                "WHERE experiment_id IN :experiment_ids ORDER BY experiment_id, id"
                + lock_clause
            ).bindparams(id_param),
            child_params,
        )
        for row in sample_rows:
            samples_by_experiment[int(row["experiment_id"])].append(int(row["id"]))
        for row in metric_rows:
            metrics_by_experiment[int(row["experiment_id"])].append(int(row["id"]))

    return {
        "manifestVersion": MANIFEST_VERSION,
        "experiments": [
            {
                "id": int(row["id"]),
                "calculationVersion": row["calculation_version"],
                "sampleIds": samples_by_experiment[int(row["id"])],
                "dailyMetricIds": metrics_by_experiment[int(row["id"])],
            }
            for row in experiment_rows
        ],
    }


def _counts(manifest: Mapping[str, Any]) -> dict[str, int]:
    experiments = list(manifest["experiments"])
    return {
        "experiments": len(experiments),
        "nullVersionExperiments": sum(
            item["calculationVersion"] is None for item in experiments
        ),
        "v1Experiments": sum(
            item["calculationVersion"] == RETIRED_VERSION for item in experiments
        ),
        "samples": sum(len(item["sampleIds"]) for item in experiments),
        "dailyMetrics": sum(len(item["dailyMetricIds"]) for item in experiments),
    }


def _inventory(connection: Connection, *, lock: bool = False) -> dict[str, Any]:
    manifest = _manifest(connection, lock=lock)
    return {
        "counts": _counts(manifest),
        "fingerprint": hashlib.sha256(_canonical_json(manifest)).hexdigest(),
        "manifest": manifest,
    }


def inventory_legacy_experiments(engine: Engine) -> dict[str, Any]:
    """Return a deterministic inventory without beginning a write transaction."""
    with engine.connect() as connection:
        return _inventory(connection)


def _delete_exact_ids(
    connection: Connection,
    *,
    table_name: str,
    ids: list[int],
) -> int:
    if not ids:
        return 0
    result = connection.execute(
        text(f"DELETE FROM {table_name} WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        ),
        {"ids": ids},
    )
    return int(result.rowcount)


def _version_distribution(connection: Connection) -> dict[str, int]:
    rows = _rows(
        connection,
        text(
            "SELECT calculation_version, COUNT(*) AS row_count "
            "FROM top_query_experiment GROUP BY calculation_version "
            "ORDER BY calculation_version"
        ),
    )
    return {
        (
            "null"
            if row["calculation_version"] is None
            else str(row["calculation_version"])
        ): int(row["row_count"])
        for row in rows
    }


def _orphan_counts(connection: Connection) -> dict[str, int]:
    return {
        "samples": int(
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM top_query_experiment_sample AS child "
                    "LEFT JOIN top_query_experiment AS parent "
                    "ON parent.id = child.experiment_id WHERE parent.id IS NULL"
                )
            ).scalar_one()
        ),
        "dailyMetrics": int(
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM top_query_experiment_daily_metric AS child "
                    "LEFT JOIN top_query_experiment AS parent "
                    "ON parent.id = child.experiment_id WHERE parent.id IS NULL"
                )
            ).scalar_one()
        ),
    }


def _retire_legacy_experiments_in_transaction(
    connection: Connection,
    *,
    expected_fingerprint: str,
) -> dict[str, Any]:
    """Delete an exact, locked manifest inside the caller-owned transaction."""
    inventory = _inventory(connection, lock=True)
    if inventory["fingerprint"] != expected_fingerprint:
        raise RuntimeError("inventory_fingerprint_mismatch")

    experiments = inventory["manifest"]["experiments"]
    experiment_ids = [item["id"] for item in experiments]
    sample_ids = [sample_id for item in experiments for sample_id in item["sampleIds"]]
    metric_ids = [
        metric_id for item in experiments for metric_id in item["dailyMetricIds"]
    ]
    actual = {
        "experiments": 0,
        "nullVersionExperiments": inventory["counts"]["nullVersionExperiments"],
        "v1Experiments": inventory["counts"]["v1Experiments"],
        "samples": _delete_exact_ids(
            connection, table_name="top_query_experiment_sample", ids=sample_ids
        ),
        "dailyMetrics": _delete_exact_ids(
            connection,
            table_name="top_query_experiment_daily_metric",
            ids=metric_ids,
        ),
    }
    actual["experiments"] = _delete_exact_ids(
        connection, table_name="top_query_experiment", ids=experiment_ids
    )
    if actual != inventory["counts"]:
        raise RuntimeError("legacy_experiment_retirement_rowcount_mismatch")

    remaining = _inventory(connection, lock=True)
    if any(remaining["counts"].values()):
        raise RuntimeError("legacy_experiment_retirement_incomplete")
    orphan_counts = _orphan_counts(connection)
    if any(orphan_counts.values()):
        raise RuntimeError("legacy_experiment_retirement_orphans_detected")
    return {
        **inventory,
        "deleted": actual,
        "remainingLegacyCounts": remaining["counts"],
        "remainingVersionDistribution": _version_distribution(connection),
        "orphanCounts": orphan_counts,
    }


def retire_legacy_experiments(
    engine: Engine,
    *,
    expected_fingerprint: str,
) -> dict[str, Any]:
    """Reject direct execution; only the migration-lock wrapper owns capability."""
    del engine, expected_fingerprint
    raise RuntimeError("migration_lock_capability_required")
