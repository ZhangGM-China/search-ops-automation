import csv
import io
import json
import zipfile
from typing import Any, Dict, Iterable, List, Tuple

from .experiment_analytics import ExperimentAnalyticsService
from .experiment_xlsx import ExperimentXlsxRenderer


class ExperimentExportService:
    def __init__(self, analytics: ExperimentAnalyticsService | None = None):
        self.analytics = analytics or ExperimentAnalyticsService()

    def export(self, experiment_id: int, export_format: str) -> Tuple[bytes, str, str]:
        normalized = self._normalize_format(export_format)
        data = self.analytics.get_export_data(experiment_id)
        return self.export_report(data, normalized, experiment_id)

    def export_report(
        self,
        report: Dict[str, Any],
        export_format: str,
        experiment_id: int,
    ) -> Tuple[bytes, str, str]:
        normalized = self._normalize_format(export_format)
        data = report
        if normalized == "xlsx":
            return (
                ExperimentXlsxRenderer().render(data),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                f"ab-test-{experiment_id}.xlsx",
            )
        if normalized == "csv":
            return (
                self._combined_csv(data),
                "text/csv; charset=utf-8",
                f"ab-test-{experiment_id}.csv",
            )
        return (
            self._zip(data),
            "application/zip",
            f"ab-test-{experiment_id}.zip",
        )

    @staticmethod
    def _normalize_format(export_format: str) -> str:
        normalized = str(export_format or "").lower()
        if normalized not in {"csv", "zip", "xlsx"}:
            raise ValueError("format must be csv, zip or xlsx")
        return normalized

    def _combined_csv(self, data: Dict[str, Any]) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["section", "field", "value"])
        for field, value in self._flatten(data["experiment"]):
            writer.writerow(["experiment", field, self._safe_cell(value)])
        for field, value in self._flatten(data["overview"].get("quality") or {}):
            writer.writerow(["quality", field, self._safe_cell(value)])
        for field, value in self._flatten(data["overview"].get("effect") or {}):
            writer.writerow(["effect", field, self._safe_cell(value)])
        self._write_table(writer, "daily_trend", data["trend"])
        self._write_table(writer, "queries", data["queries"])
        self._write_table(writer, "query_daily_metrics", data["queryDailyMetrics"])
        self._write_table(writer, "governance_actions", data["actions"])
        return ("\ufeff" + output.getvalue()).encode("utf-8")

    def _zip(self, data: Dict[str, Any]) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("overview.csv", self._overview_csv(data))
            archive.writestr("daily_trend.csv", self._table_csv(data["trend"]))
            archive.writestr("query_daily_metrics.csv", self._table_csv(data["queryDailyMetrics"]))
            archive.writestr("governance_actions.csv", self._table_csv(data["actions"]))
            archive.writestr("README.txt", self._readme(data).encode("utf-8-sig"))
        return output.getvalue()

    def _overview_csv(self, data: Dict[str, Any]) -> bytes:
        rows = []
        for section in ("experiment", "quality", "groups", "effect", "operations"):
            value = data["experiment"] if section == "experiment" else data["overview"].get(section)
            for field, item in self._flatten(value or {}):
                rows.append({"section": section, "field": field, "value": item})
        return self._table_csv(rows)

    def _table_csv(self, rows: List[Dict[str, Any]]) -> bytes:
        output = io.StringIO(newline="")
        fieldnames = self._fieldnames(rows)
        if not fieldnames:
            fieldnames = ["no_data"]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: self._safe_cell(value) for key, value in row.items()})
        return ("\ufeff" + output.getvalue()).encode("utf-8")

    def _write_table(self, writer: Any, section: str, rows: List[Dict[str, Any]]) -> None:
        for index, row in enumerate(rows, start=1):
            for field, value in self._flatten(row):
                writer.writerow([section, f"{index}.{field}", self._safe_cell(value)])

    @classmethod
    def _flatten(cls, value: Any, prefix: str = "") -> Iterable[Tuple[str, Any]]:
        if isinstance(value, dict):
            for key, item in value.items():
                field = f"{prefix}.{key}" if prefix else str(key)
                yield from cls._flatten(item, field)
            return
        if isinstance(value, list):
            yield prefix, json.dumps(value, ensure_ascii=False)
            return
        yield prefix, value

    @staticmethod
    def _fieldnames(rows: List[Dict[str, Any]]) -> List[str]:
        fields = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        return fields

    @staticmethod
    def _safe_cell(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        if value is None:
            return ""
        text = str(value)
        if text.startswith(("=", "+", "-", "@")):
            return "'" + text
        return text

    @staticmethod
    def _readme(data: Dict[str, Any]) -> str:
        quality = data["overview"].get("quality") or {}
        return "\n".join([
            "A/B Test export",
            f"Experiment ID: {data['experiment'].get('experimentId')}",
            f"Brand: {data['experiment'].get('brand')}",
            f"Status: {data['experiment'].get('status')}",
            f"Quality: {quality.get('status')}",
            f"Quality error: {quality.get('error') or ''}",
            "CTR/CVR are Search UV weighted. Effect is difference-in-differences against the fixed baseline.",
            "Invalid or contaminated evidence never produces a normal effect conclusion.",
        ])
