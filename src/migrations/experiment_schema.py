import tempfile
from pathlib import Path
from typing import Any, Dict, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine, URL, make_url

from src.config import get_env


ZHA36_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, str]]] = {
    "top_query_experiment": (
        ("baseline_start_date", "DATE"),
        ("baseline_end_date", "DATE"),
        ("baseline_locked_at", "DATETIME"),
        ("calculation_version", "VARCHAR(64)"),
    ),
    "top_query_experiment_sample": (
        ("baseline_observed_days", "INTEGER"),
        ("baseline_search_uv", "INTEGER"),
        ("baseline_search_pv", "INTEGER"),
        ("baseline_click_uv", "FLOAT"),
        ("baseline_purchase_uv", "FLOAT"),
        ("baseline_ctr", "FLOAT"),
        ("baseline_cvr", "FLOAT"),
    ),
}

LEGACY_EXPERIMENT_STATUSES = ("ended", "scheduled", "running")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET_ERROR = (
    "ZHA-36 migration requires an isolated SQLite database; "
    "target must be isolated test SQLite storage"
)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def validate_zha36_test_migration_target(
    target: Engine | URL | str,
) -> URL:
    url = target.url if isinstance(target, Engine) else make_url(str(target))
    if url.get_backend_name() != "sqlite":
        raise RuntimeError(TARGET_ERROR)

    database = url.database
    if database in {None, "", ":memory:"}:
        return url

    is_uri_memory = (
        str(url.query.get("uri") or "").lower() == "true"
        and str(url.query.get("mode") or "").lower() == "memory"
        and database.startswith("file:")
    )
    if is_uri_memory:
        return url

    database_path = Path(database).expanduser()
    if not database_path.is_absolute():
        database_path = Path.cwd() / database_path
    resolved_path = database_path.resolve()
    allowed_roots = (
        Path(tempfile.gettempdir()).resolve(),
        (PROJECT_ROOT / ".tmp").resolve(),
    )
    if not any(_is_within(resolved_path, root) for root in allowed_roots):
        raise RuntimeError(TARGET_ERROR)
    return url


def _legacy_experiment_counts(engine: Engine) -> Dict[str, int]:
    counts = {status: 0 for status in LEGACY_EXPERIMENT_STATUSES}
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT status, COUNT(*)
            FROM top_query_experiment
            WHERE calculation_version IS NULL
              AND status IN ('ended', 'scheduled', 'running')
            GROUP BY status
        """)).fetchall()
    for status, count in rows:
        counts[str(status)] = int(count)
    return counts


def ensure_zha36_ab_test_schema(engine: Engine) -> Dict[str, Any]:
    if get_env() != "test":
        raise RuntimeError("ZHA-36 A/B test migration requires ENV=test")

    validate_zha36_test_migration_target(engine)

    from src.persistence import TopQueryExperiment, TopQueryExperimentSample
    from .release_schema import ensure_release_schema_upgrades

    TopQueryExperiment.__table__.create(bind=engine, checkfirst=True)
    TopQueryExperimentSample.__table__.create(
        bind=engine,
        checkfirst=True,
    )
    added_columns = ensure_release_schema_upgrades(
        engine,
        ZHA36_SCHEMA_COLUMNS,
    )
    return {
        "addedColumns": added_columns,
        "legacyCounts": _legacy_experiment_counts(engine),
    }
