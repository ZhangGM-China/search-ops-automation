import datetime
import io
import json
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import xlsxwriter


Column = Tuple[str, str, str, float]


class ExperimentXlsxRenderer:
    SHEET_NAMES = (
        "效果结论",
        "每日组效果",
        "Query综合效果",
        "Query每日变化",
        "治理动作明细",
        "原始日数据",
        "样本基线",
        "口径说明",
    )
    CHANGE_RATE_FIELDS = frozenset({
        "ctrChange",
        "cvrChange",
        "ctrNetChange",
        "cvrNetChange",
        "groupContribution.ctr",
        "groupContribution.cvr",
        "netCtrContribution",
        "netCvrContribution",
    })
    STATUS_FIELDS = frozenset({
        "qualityStatus",
        "dataQualityStatus",
        "identityStatus",
        "governanceStatus",
        "observedStatus",
        "contaminationStatus",
        "clickNumeratorStatus",
        "purchaseNumeratorStatus",
        "status",
    })
    ERROR_FIELDS = frozenset({
        "qualityError",
        "dataQualityError",
        "identityError",
        "clickNumeratorError",
        "purchaseNumeratorError",
        "errorMessage",
    })
    WARNING_FIELDS = frozenset({
        "contaminationReason",
        "missingDetailReason",
    })
    SUCCESS_STATUSES = frozenset({
        "complete",
        "completed",
        "executed",
        "verified",
        "clean",
        "observed",
    })
    ERROR_STATUSES = frozenset({
        "failed",
        "error",
        "verification_failed",
        "contaminated",
        "not_observed",
    })
    WARNING_STATUSES = frozenset({
        "pending",
        "scheduled",
        "unresolved",
        "manual_required",
        "waiting_human_approval",
        "verification_inconclusive",
        "execution_unknown",
        "dry_run_executed",
        "approved",
        "executing",
        "verifying",
        "rollback_pending",
    })

    def render(self, report: Dict[str, Any]) -> bytes:
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {
            "in_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        })
        try:
            formats = self._build_formats(workbook)
            self._write_conclusion(workbook, formats, report)
            self._write_daily_groups(workbook, formats, report)
            self._write_queries(workbook, formats, report)
            self._write_query_daily(workbook, formats, report)
            self._write_actions(workbook, formats, report)
            self._write_raw_daily(workbook, formats, report)
            self._write_baseline(workbook, formats, report)
            self._write_methodology(workbook, formats, report)
        finally:
            workbook.close()
        return output.getvalue()

    @staticmethod
    def _build_formats(workbook: Any) -> Dict[str, Any]:
        base = {
            "font_name": "Microsoft YaHei",
            "font_color": "#1F2937",
            "font_size": 10,
            "valign": "vcenter",
        }
        formats = {
            "title": workbook.add_format({
                **base,
                "bold": True,
                "font_size": 18,
                "font_color": "#FFFFFF",
                "bg_color": "#17324D",
                "align": "left",
                "valign": "vcenter",
            }),
            "section": workbook.add_format({
                **base,
                "bold": True,
                "font_size": 11,
                "bg_color": "#F4F6F8",
                "bottom": 1,
                "bottom_color": "#D5DCE3",
            }),
            "header": workbook.add_format({
                **base,
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#2878A8",
                "align": "center",
                "text_wrap": True,
                "border": 1,
                "border_color": "#D5DCE3",
            }),
            "label": workbook.add_format({
                **base,
                "bold": True,
                "bg_color": "#E7F4FA",
                "border": 1,
                "border_color": "#D5DCE3",
            }),
            "value": workbook.add_format({
                **base,
                "border": 1,
                "border_color": "#E5E7EB",
            }),
            "note": workbook.add_format({
                **base,
                "bg_color": "#E7F4FA",
                "text_wrap": True,
                "border": 1,
                "border_color": "#B8DCEB",
            }),
            "warning": workbook.add_format({
                **base,
                "bold": True,
                "font_color": "#8A5A00",
                "bg_color": "#FFF5DB",
                "text_wrap": True,
                "border": 1,
                "border_color": "#E6D49C",
            }),
            "positive": workbook.add_format({
                **base,
                "bold": True,
                "font_color": "#1B5E20",
                "num_format": "0.00%",
                "align": "right",
                "border": 1,
                "border_color": "#E5E7EB",
            }),
            "negative": workbook.add_format({
                **base,
                "bold": True,
                "font_color": "#B42318",
                "num_format": "0.00%",
                "align": "right",
                "border": 1,
                "border_color": "#E5E7EB",
            }),
            "positive_stripe": workbook.add_format({
                **base,
                "bold": True,
                "font_color": "#1B5E20",
                "bg_color": "#F4FAFD",
                "num_format": "0.00%",
                "align": "right",
                "border": 1,
                "border_color": "#E5E7EB",
            }),
            "negative_stripe": workbook.add_format({
                **base,
                "bold": True,
                "font_color": "#B42318",
                "bg_color": "#F4FAFD",
                "num_format": "0.00%",
                "align": "right",
                "border": 1,
                "border_color": "#E5E7EB",
            }),
            "success": workbook.add_format({
                **base,
                "bold": True,
                "font_color": "#1B5E20",
                "bg_color": "#E8F5E9",
                "align": "center",
                "border": 1,
                "border_color": "#B7D7B9",
            }),
            "error": workbook.add_format({
                **base,
                "bold": True,
                "font_color": "#B42318",
                "bg_color": "#FDECEC",
                "text_wrap": True,
                "border": 1,
                "border_color": "#E8B4AF",
            }),
        }
        type_options = {
            "text": {},
            "int": {"num_format": "#,##0", "align": "right"},
            "number": {"num_format": "#,##0.00", "align": "right"},
            "rate": {"num_format": "0.00%", "align": "right"},
            "date": {"num_format": "yyyy-mm-dd", "align": "center"},
            "datetime": {"num_format": "yyyy-mm-dd hh:mm:ss", "align": "center"},
            "duration": {"num_format": "0.00", "align": "right"},
            "bool": {"align": "center"},
        }
        for kind, options in type_options.items():
            formats[kind] = workbook.add_format({
                **base,
                **options,
                "border": 1,
                "border_color": "#E5E7EB",
            })
            formats[f"{kind}_stripe"] = workbook.add_format({
                **base,
                **options,
                "bg_color": "#F4FAFD",
                "border": 1,
                "border_color": "#E5E7EB",
            })
        return formats

    def _write_conclusion(self, workbook: Any, formats: Dict[str, Any], report: Dict[str, Any]) -> None:
        sheet = workbook.add_worksheet(self.SHEET_NAMES[0])
        sheet.hide_gridlines(2)
        sheet.set_zoom(90)
        sheet.set_column("A:A", 21)
        sheet.set_column("B:B", 18)
        sheet.set_column("C:C", 21)
        sheet.set_column("D:D", 18)
        sheet.set_column("E:E", 18)
        sheet.set_column("F:O", 15)
        sheet.set_row(0, 28)
        sheet.set_row(1, 28)
        sheet.merge_range("A1:O2", "A/B Test 效果分析报告", formats["title"])

        experiment = report.get("experiment") or {}
        self._section(sheet, 3, 14, "实验信息", formats)
        metadata = [
            ("A5", "实验 ID", "B5", experiment.get("experimentId"), "int"),
            ("D5", "品牌", "E5", experiment.get("brand"), "text"),
            ("G5", "状态", "H5", experiment.get("status"), "text"),
            ("J5", "实验名称", "K5", experiment.get("experimentName"), "text"),
            ("A6", "计划开始", "B6", experiment.get("plannedStartDate"), "date"),
            ("D6", "计划结束", "E6", experiment.get("plannedEndDate"), "date"),
            ("G6", "数据开始", "H6", report.get("dataStartDate"), "date"),
            ("J6", "数据结束", "K6", report.get("dataEndDate"), "date"),
            ("A7", "基线开始", "B7", experiment.get("baselineStartDate"), "date"),
            ("D7", "基线结束", "E7", experiment.get("baselineEndDate"), "date"),
            ("G7", "计算版本", "H7", report.get("calculationVersion"), "text"),
            ("J7", "时区", "K7", report.get("timezone"), "text"),
            ("A8", "基线锁定时间", "B8", experiment.get("baselineLockedAt"), "datetime"),
            ("D8", "报告生成时间", "E8", report.get("generatedAt"), "datetime"),
            ("G8", "数据天数", "H8", report.get("dataDays"), "int"),
        ]
        for label_cell, label, value_cell, value, kind in metadata:
            sheet.write_string(label_cell, label, formats["label"])
            self._write_typed(sheet, value_cell, value, kind, formats[kind])

        self._section(sheet, 9, 14, "质量门禁", formats)
        quality = report.get("quality") or {}
        baseline_quality = report.get("baselineQuality") or {}
        quality_items = [
            ("A11", "实验质量", "B11", quality.get("status"), "status"),
            ("D11", "异常原因", "E11", quality.get("error"), "error"),
            ("G11", "完整天数", "H11", quality.get("completeDays"), None),
            ("J11", "失败天数", "K11", quality.get("failedDays"), None),
            ("A12", "基线质量", "B12", baseline_quality.get("status"), "status"),
            ("D12", "基线异常", "E12", baseline_quality.get("error"), "error"),
            ("G12", "基线期望天数", "H12", baseline_quality.get("expectedDays"), None),
            ("J12", "基线已锁定", "K12", baseline_quality.get("locked"), None),
        ]
        for label_cell, label, value_cell, value, semantic in quality_items:
            sheet.write_string(label_cell, label, formats["label"])
            kind = "bool" if isinstance(value, bool) else "int" if isinstance(value, int) else "text"
            cell_format = (
                self._status_format(value, formats)
                if semantic == "status"
                else formats["error"]
                if semantic == "error" and value
                else formats[kind]
            )
            self._write_typed(sheet, value_cell, value, kind, cell_format)

        self._section(sheet, 12, 14, "分组加权结果（分母为 Search UV）", formats)
        headers = [
            "组别", "基线 Search UV", "周期 Search UV", "基线 Click UV", "周期 Click UV",
            "基线 Purchase UV", "周期 Purchase UV", "基线 CTR", "周期 CTR", "CTR 变化",
            "基线 CVR", "周期 CVR", "CVR 变化",
        ]
        for col, label in enumerate(headers):
            sheet.write_string(13, col, label, formats["header"])
        for row, group_name in enumerate(("treatment", "control"), start=14):
            group = (report.get("groups") or {}).get(group_name) or {}
            values = [
                ("实验组" if group_name == "treatment" else "对照组", "text"),
                (group.get("baselineSearchUv"), "int"),
                (group.get("currentSearchUv"), "int"),
                (group.get("baselineClickUv"), "number"),
                (group.get("currentClickUv"), "number"),
                (group.get("baselinePurchaseUv"), "number"),
                (group.get("currentPurchaseUv"), "number"),
                (group.get("baselineCtr"), "rate"),
                (group.get("currentCtr"), "rate"),
                (group.get("ctrChange"), "rate"),
                (group.get("baselineCvr"), "rate"),
                (group.get("currentCvr"), "rate"),
                (group.get("cvrChange"), "rate"),
            ]
            for col, (value, kind) in enumerate(values):
                cell_format = (
                    self._signed_rate_format(value, False, formats)
                    if col in {9, 12}
                    else formats[kind]
                )
                self._write_typed(sheet, row, col, value, kind, cell_format)

        self._section(sheet, 18, 14, "正式效果结论（Difference-in-Differences）", formats)
        sheet.write_string("A20", "CTR 净变化", formats["label"])
        sheet.write_string("C20", "CVR 净变化", formats["label"])
        sheet.write_string("E20", "CTR 正向天数", formats["label"])
        sheet.write_string("G20", "CVR 正向天数", formats["label"])
        sheet.write_string("A21", "预估增量 Click UV", formats["label"])
        sheet.write_string("C21", "预估增量 Purchase UV", formats["label"])
        sheet.write_string("E21", "结论", formats["label"])
        effect = report.get("effect") or {}
        ctr_effect = effect.get("ctrNetChange")
        cvr_effect = effect.get("cvrNetChange")
        self._write_typed(
            sheet,
            "B20",
            ctr_effect,
            "rate",
            self._signed_rate_format(ctr_effect, False, formats),
        )
        self._write_typed(
            sheet,
            "D20",
            cvr_effect,
            "rate",
            self._signed_rate_format(cvr_effect, False, formats),
        )
        self._write_typed(
            sheet,
            "F20",
            effect.get("ctrPositiveDays"),
            "int",
            formats["int"],
        )
        self._write_typed(
            sheet,
            "H20",
            effect.get("cvrPositiveDays"),
            "int",
            formats["int"],
        )
        self._write_typed(
            sheet,
            "B21",
            effect.get("estimatedIncrementalClickUv"),
            "number",
            formats["number"],
        )
        self._write_typed(
            sheet,
            "D21",
            effect.get("estimatedIncrementalPurchaseUv"),
            "number",
            formats["number"],
        )
        conclusion = (
            "实验质量异常，正式效果不输出"
            if quality.get("status") != "complete" or not report.get("effect")
            else "质量门禁已通过，净变化仅作为智能运营效果信号。"
        )
        sheet.merge_range("F21:O22", conclusion, formats[
            "warning" if quality.get("status") != "complete" else "note"
        ])

        self._section(sheet, 22, 14, "运营执行概览", formats)
        operations = report.get("operations") or {}
        operation_items = [
            ("治理覆盖率", operations.get("governanceCoverage"), "rate"),
            ("动作成功率", operations.get("actionSuccessRate"), "rate"),
            ("计划动作", operations.get("plannedActions"), "int"),
            ("已执行", operations.get("executedActions"), "int"),
            ("失败", operations.get("failedActions"), "int"),
            ("未确定", operations.get("inconclusiveActions"), "int"),
            ("未收口", operations.get("unresolvedActions"), "int"),
            ("Token", operations.get("tokenUsage"), "int"),
            ("耗时（秒）", operations.get("durationSeconds"), "duration"),
        ]
        for index, (label, value, kind) in enumerate(operation_items):
            pair = index % 5
            row = 23 + index // 5
            label_col = pair * 3
            sheet.write_string(row, label_col, label, formats["label"])
            self._write_typed(sheet, row, label_col + 1, value, kind, formats[kind])

        boundary = (
            "结论边界：周期 CTR/CVR 按 ΣClick UV / ΣSearch UV 与 ΣPurchase UV / ΣSearch UV 计算；"
            "只有 observed 且 clean 的证据进入正式效果。DiD 为效果信号，不等于严格因果证明。"
        )
        sheet.merge_range("A27:O29", boundary, formats["note"])
        self._finish_sheet(sheet, 28, 14, landscape=True, filter_row=None)

    def _write_daily_groups(self, workbook: Any, formats: Dict[str, Any], report: Dict[str, Any]) -> None:
        columns: Sequence[Column] = (
            ("dataDate", "数据日期", "date", 12),
            ("groupType", "组别", "text", 11),
            ("sampleCount", "样本数", "int", 11),
            ("observedRows", "观测行数", "int", 12),
            ("baselineSearchUv", "基线 Search UV", "int", 15),
            ("baselineSearchPv", "基线 Search PV", "int", 15),
            ("baselineClickUv", "基线 Click UV", "number", 15),
            ("baselinePurchaseUv", "基线 Purchase UV", "number", 17),
            ("baselineCtr", "基线 CTR", "rate", 12),
            ("baselineCvr", "基线 CVR", "rate", 12),
            ("currentSearchUv", "周期 Search UV", "int", 15),
            ("currentSearchPv", "周期 Search PV", "int", 15),
            ("currentClickUv", "周期 Click UV", "number", 15),
            ("currentPurchaseUv", "周期 Purchase UV", "number", 17),
            ("currentCtr", "周期 CTR", "rate", 12),
            ("currentCvr", "周期 CVR", "rate", 12),
            ("ctrChange", "CTR 变化", "rate", 12),
            ("cvrChange", "CVR 变化", "rate", 12),
            ("ctrNetChange", "CTR 净变化", "rate", 13),
            ("cvrNetChange", "CVR 净变化", "rate", 13),
            ("qualityStatus", "质量状态", "text", 13),
            ("qualityError", "质量异常", "text", 24),
            ("governanceCoverage", "治理覆盖率", "rate", 14),
            ("actionSuccessRate", "动作成功率", "rate", 14),
            ("plannedActions", "计划动作", "int", 11),
            ("executedActions", "已执行动作", "int", 12),
            ("failedActions", "失败动作", "int", 11),
            ("inconclusiveActions", "未确定动作", "int", 12),
            ("unresolvedActions", "未收口动作", "int", 12),
            ("tokenUsage", "Token", "int", 12),
            ("durationSeconds", "耗时（秒）", "duration", 13),
        )
        rows = []
        for day in report.get("trend") or []:
            for group_name in ("treatment", "control"):
                rows.append({
                    "dataDate": day.get("dataDate"),
                    "groupType": group_name,
                    **((day.get("groups") or {}).get(group_name) or {}),
                    **(day.get("effect") or {}),
                    "qualityStatus": (day.get("quality") or {}).get("status"),
                    "qualityError": (day.get("quality") or {}).get("error"),
                    **(day.get("operations") or {}),
                })
        self._write_table_sheet(workbook, formats, self.SHEET_NAMES[1], "每日组效果", columns, rows)

    def _write_queries(self, workbook: Any, formats: Dict[str, Any], report: Dict[str, Any]) -> None:
        columns: Sequence[Column] = (
            ("groupType", "组别", "text", 11),
            ("queryText", "Query", "text", 24),
            ("sampleRank", "样本排名", "int", 11),
            ("baselineObservedDays", "基线天数", "int", 11),
            ("baselineSearchUv", "基线 Search UV", "int", 15),
            ("baselineSearchPv", "基线 Search PV", "int", 15),
            ("baselineClickUv", "基线 Click UV", "number", 15),
            ("baselinePurchaseUv", "基线 Purchase UV", "number", 17),
            ("baselineCtr", "基线 CTR", "rate", 12),
            ("baselineCvr", "基线 CVR", "rate", 12),
            ("latestDataDate", "最新数据日", "date", 13),
            ("observedStatus", "观测状态", "text", 13),
            ("contaminationStatus", "污染状态", "text", 13),
            ("contaminationReason", "污染原因", "text", 20),
            ("observedDays", "观测天数", "int", 11),
            ("unobservedDays", "未观测天数", "int", 12),
            ("contaminatedDays", "污染天数", "int", 11),
            ("dataQualityStatus", "数据质量", "text", 13),
            ("currentSearchUv", "周期 Search UV", "int", 15),
            ("currentSearchPv", "周期 Search PV", "int", 15),
            ("currentClickUv", "周期 Click UV", "number", 15),
            ("currentPurchaseUv", "周期 Purchase UV", "number", 17),
            ("currentCtr", "周期 CTR", "rate", 12),
            ("currentCvr", "周期 CVR", "rate", 12),
            ("ctrChange", "CTR 变化", "rate", 12),
            ("cvrChange", "CVR 变化", "rate", 12),
            ("groupContribution.ctr", "组内 CTR 贡献", "rate", 14),
            ("groupContribution.cvr", "组内 CVR 贡献", "rate", 14),
            ("netCtrContribution", "净 CTR 贡献", "rate", 13),
            ("netCvrContribution", "净 CVR 贡献", "rate", 13),
            ("effectDirection", "效果方向", "text", 13),
            ("numeratorSource", "分子来源", "text", 13),
            ("governanceTaskId", "治理任务 ID", "int", 14),
            ("governanceStatus", "治理状态", "text", 14),
            ("actionCount", "动作数", "int", 10),
            ("tokenUsage", "Token", "int", 12),
            ("durationSeconds", "耗时（秒）", "duration", 13),
        )
        self._write_table_sheet(workbook, formats, self.SHEET_NAMES[2], "Query 综合效果", columns, report.get("queries") or [])

    def _write_query_daily(self, workbook: Any, formats: Dict[str, Any], report: Dict[str, Any]) -> None:
        columns = self._query_daily_columns(include_governance=True)
        self._write_table_sheet(workbook, formats, self.SHEET_NAMES[3], "Query 每日变化", columns, report.get("queryDailyMetrics") or [])

    def _write_actions(self, workbook: Any, formats: Dict[str, Any], report: Dict[str, Any]) -> None:
        columns: Sequence[Column] = (
            ("recordType", "记录类型", "text", 12),
            ("detailAvailable", "明细可用", "bool", 11),
            ("missingDetailReason", "明细缺失原因", "text", 28),
            ("dataDate", "数据日期", "date", 12),
            ("groupType", "组别", "text", 11),
            ("governanceStatus", "治理状态", "text", 14),
            ("id", "动作 ID", "int", 11),
            ("taskId", "任务 ID", "int", 11),
            ("brand", "品牌", "text", 12),
            ("queryText", "Query", "text", 24),
            ("checkKey", "检查项", "text", 15),
            ("actionType", "动作类型", "text", 18),
            ("executionMode", "执行模式", "text", 12),
            ("status", "动作状态", "text", 16),
            ("reason", "原因", "text", 36),
            ("errorMessage", "错误信息", "text", 32),
            ("createdAt", "创建时间", "datetime", 20),
            ("planned", "计划", "int", 10),
            ("executed", "已执行", "int", 10),
            ("failed", "失败", "int", 10),
            ("inconclusive", "未确定", "int", 10),
            ("rejected", "已拒绝", "int", 10),
            ("cancelled", "已取消", "int", 10),
            ("unresolved", "未收口", "int", 10),
        )
        self._write_table_sheet(
            workbook,
            formats,
            self.SHEET_NAMES[4],
            "治理动作明细",
            columns,
            report.get("actions") or [],
            no_data_message="无治理动作",
        )

    def _write_raw_daily(self, workbook: Any, formats: Dict[str, Any], report: Dict[str, Any]) -> None:
        columns: Sequence[Column] = (
            ("dataDate", "数据日期", "date", 12),
            ("groupType", "组别", "text", 11),
            ("queryText", "Query", "text", 24),
            ("observedStatus", "观测状态", "text", 13),
            ("contaminationStatus", "污染状态", "text", 13),
            ("contaminationReason", "污染原因", "text", 20),
            ("searchUv", "Search UV", "int", 13),
            ("searchPv", "Search PV", "int", 13),
            ("clickUv", "Click UV", "number", 13),
            ("purchaseUv", "Purchase UV", "number", 14),
            ("ctr", "CTR", "rate", 11),
            ("cvr", "CVR", "rate", 11),
            ("numeratorSource", "分子来源", "text", 13),
            ("dataQualityStatus", "数据质量", "text", 13),
            ("dataQualityError", "质量异常", "text", 24),
            ("includedInEffect", "进入正式效果", "bool", 15),
            ("governanceTaskId", "治理任务 ID", "int", 14),
            ("governanceStatus", "治理状态", "text", 14),
            ("actionCount", "动作数", "int", 10),
            ("tokenUsage", "Token", "int", 12),
            ("durationSeconds", "耗时（秒）", "duration", 13),
        )
        self._write_table_sheet(workbook, formats, self.SHEET_NAMES[5], "原始日数据", columns, report.get("queryDailyMetrics") or [])

    def _write_baseline(self, workbook: Any, formats: Dict[str, Any], report: Dict[str, Any]) -> None:
        columns: Sequence[Column] = (
            ("groupType", "组别", "text", 11),
            ("queryText", "Query", "text", 24),
            ("sampleRank", "样本排名", "int", 11),
            ("sampleSearchUv", "样本日 Search UV", "int", 16),
            ("sampleSearchPv", "样本日 Search PV", "int", 16),
            ("sampleCtr", "样本日 CTR", "rate", 13),
            ("sampleCvr", "样本日 CVR", "rate", 13),
            ("sampleAvgCtr", "样本均值 CTR", "rate", 14),
            ("sampleAvgCvr", "样本均值 CVR", "rate", 14),
            ("sampleQueryPvShare", "Query PV 占比", "rate", 14),
            ("baselineWindowType", "基线类型", "text", 20),
            ("baselineStartDate", "基线开始", "date", 13),
            ("baselineEndDate", "基线结束", "date", 13),
            ("baselineObservedDays", "基线天数", "int", 11),
            ("baselineSearchUv", "基线 Search UV", "int", 15),
            ("baselineSearchPv", "基线 Search PV", "int", 15),
            ("baselineClickUv", "基线 Click UV", "number", 15),
            ("baselinePurchaseUv", "基线 Purchase UV", "number", 17),
            ("baselineCtr", "基线 CTR", "rate", 12),
            ("baselineCvr", "基线 CVR", "rate", 12),
            ("qualityStatus", "质量状态", "text", 13),
            ("qualityError", "质量异常", "text", 24),
        )
        self._write_table_sheet(workbook, formats, self.SHEET_NAMES[6], "样本基线证据", columns, report.get("baselineEvidence") or [])

    def _write_methodology(self, workbook: Any, formats: Dict[str, Any], report: Dict[str, Any]) -> None:
        rows = [
            {"item": "CTR 分母", "description": "CTR = ΣClick UV / ΣSearch UV，不使用 Search PV 作为分母。"},
            {"item": "CVR 分母", "description": "CVR = ΣPurchase UV / ΣSearch UV，不使用 Search PV 作为分母。"},
            {"item": "周期加权", "description": "周期指标先汇总真实分子和 Search UV，再做比值；不对每日比率做算术平均。"},
            {"item": "7 日基线", "description": "v2 基线窗口为计划开始日前 7 个自然日，按同一真实分子/分母口径加权。"},
            {"item": "正式证据", "description": "只有 observed 且 clean，且身份、分子和源数据质量完整的行进入正式效果。"},
            {"item": "DiD", "description": "净变化 = 实验组（周期-基线）- 对照组（周期-基线）。"},
            {"item": "质量门禁", "description": "任一缺日、缺 Query、污染、零 Search UV、治理未收口或基线异常时，正式效果留空。"},
            {"item": "v1 / v2", "description": "v1 是历史单日样本基线兼容口径；v2 是锁定的 7 日真实分子加权基线。"},
            {"item": "结论边界", "description": "报告用于观察智能运营效果信号，不等于严格随机化因果证明。"},
            {"item": "计算版本", "description": report.get("calculationVersion")},
            {"item": "生成时间", "description": report.get("generatedAt")},
            {"item": "时区", "description": report.get("timezone")},
        ]
        columns: Sequence[Column] = (
            ("item", "口径项", "text", 22),
            ("description", "说明", "text", 95),
        )
        self._write_table_sheet(workbook, formats, self.SHEET_NAMES[7], "口径说明", columns, rows, landscape=False)

    @staticmethod
    def _query_daily_columns(include_governance: bool) -> Sequence[Column]:
        columns: List[Column] = [
            ("dataDate", "数据日期", "date", 12),
            ("groupType", "组别", "text", 11),
            ("queryText", "Query", "text", 24),
            ("canonicalQueryKey", "规范化身份", "text", 22),
            ("lockedQueryText", "锁定 Query", "text", 22),
            ("lockedGroupType", "锁定组别", "text", 12),
            ("identityStatus", "身份状态", "text", 13),
            ("identityError", "身份异常", "text", 24),
            ("observedStatus", "观测状态", "text", 13),
            ("contaminationStatus", "污染状态", "text", 13),
            ("contaminationReason", "污染原因", "text", 20),
            ("searchUv", "Search UV", "int", 13),
            ("searchPv", "Search PV", "int", 13),
            ("clickUv", "Click UV", "number", 13),
            ("purchaseUv", "Purchase UV", "number", 14),
            ("ctr", "CTR", "rate", 11),
            ("cvr", "CVR", "rate", 11),
            ("numeratorSource", "分子来源", "text", 13),
            ("clickNumeratorSource", "Click 分子来源", "text", 16),
            ("purchaseNumeratorSource", "Purchase 分子来源", "text", 18),
            ("clickNumeratorStatus", "Click 分子状态", "text", 16),
            ("purchaseNumeratorStatus", "Purchase 分子状态", "text", 18),
            ("clickNumeratorError", "Click 分子异常", "text", 22),
            ("purchaseNumeratorError", "Purchase 分子异常", "text", 24),
            ("dataQualityStatus", "数据质量", "text", 13),
            ("dataQualityError", "质量异常", "text", 24),
            ("includedInEffect", "进入正式效果", "bool", 15),
        ]
        if include_governance:
            columns.extend([
                ("governanceTaskId", "治理任务 ID", "int", 14),
                ("governanceStatus", "治理状态", "text", 14),
                ("actionCount", "动作数", "int", 10),
                ("autoActionCount", "自动动作", "int", 11),
                ("manualActionCount", "人工动作", "int", 11),
                ("tokenUsage", "Token", "int", 12),
                ("durationSeconds", "耗时（秒）", "duration", 13),
            ])
        return columns

    def _write_table_sheet(
        self,
        workbook: Any,
        formats: Dict[str, Any],
        sheet_name: str,
        title: str,
        columns: Sequence[Column],
        rows: Iterable[Dict[str, Any]],
        *,
        no_data_message: str = "暂无数据",
        landscape: bool = True,
    ) -> None:
        sheet = workbook.add_worksheet(sheet_name)
        sheet.hide_gridlines(2)
        sheet.set_zoom(85)
        last_col = len(columns) - 1
        sheet.set_row(0, 28)
        sheet.set_row(1, 28)
        sheet.merge_range(0, 0, 1, last_col, title, formats["title"])
        sheet.merge_range(
            2,
            0,
            2,
            last_col,
            "数据来自同一份实验报告快照；工作簿只做排列与格式化，不重算正式指标。",
            formats["note"],
        )
        sheet.set_row(3, 32)
        for col, (_, label, _, width) in enumerate(columns):
            sheet.set_column(col, col, width)
            sheet.write_string(3, col, label, formats["header"])
        data_rows = list(rows)
        if not data_rows:
            sheet.merge_range(4, 0, 4, last_col, no_data_message, formats["note"])
            last_row = 4
        else:
            for row_index, item in enumerate(data_rows, start=4):
                striped = row_index % 2 == 1
                for col, (key, _, kind, _) in enumerate(columns):
                    value = self._nested_value(item, key)
                    cell_format = self._semantic_format(
                        key,
                        value,
                        kind,
                        striped,
                        formats,
                    )
                    self._write_typed(sheet, row_index, col, value, kind, cell_format)
            last_row = 3 + len(data_rows)
        self._finish_sheet(sheet, last_row, last_col, landscape=landscape, filter_row=3)

    @staticmethod
    def _nested_value(item: Dict[str, Any], key: str) -> Any:
        value: Any = item
        for part in key.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value

    @classmethod
    def _semantic_format(
        cls,
        key: str,
        value: Any,
        kind: str,
        striped: bool,
        formats: Dict[str, Any],
    ) -> Any:
        if key in cls.CHANGE_RATE_FIELDS:
            return cls._signed_rate_format(value, striped, formats)
        if key in cls.STATUS_FIELDS:
            return cls._status_format(value, formats)
        if value not in {None, ""} and key in cls.ERROR_FIELDS:
            return formats["error"]
        if value not in {None, ""} and key in cls.WARNING_FIELDS:
            return formats["warning"]
        format_key = f"{kind}_stripe" if striped else kind
        return formats[format_key]

    @staticmethod
    def _signed_rate_format(value: Any, striped: bool, formats: Dict[str, Any]) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > 0:
                return formats["positive_stripe" if striped else "positive"]
            if value < 0:
                return formats["negative_stripe" if striped else "negative"]
        return formats["rate_stripe" if striped else "rate"]

    @classmethod
    def _status_format(cls, value: Any, formats: Dict[str, Any]) -> Any:
        normalized = str(value or "").strip().lower()
        if normalized in cls.SUCCESS_STATUSES:
            return formats["success"]
        if normalized in cls.ERROR_STATUSES:
            return formats["error"]
        if normalized in cls.WARNING_STATUSES:
            return formats["warning"]
        return formats["text"]

    @staticmethod
    def _safe_text(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return "" if value is None else str(value)

    def _write_typed(self, sheet: Any, *args: Any) -> None:
        if len(args) == 4:
            cell, value, kind, cell_format = args
            row = cell
            col = None
        else:
            row, col, value, kind, cell_format = args
        target = (row,) if col is None else (row, col)
        if value is None:
            sheet.write_blank(*target, None, cell_format)
            return
        if kind == "text":
            sheet.write_string(*target, self._safe_text(value), cell_format)
            return
        if kind == "bool":
            if isinstance(value, bool):
                sheet.write_boolean(*target, value, cell_format)
            else:
                sheet.write_string(*target, self._safe_text(value), cell_format)
            return
        if kind in {"date", "datetime"}:
            parsed = self._parse_datetime(value, date_only=kind == "date")
            if parsed is not None:
                sheet.write_datetime(*target, parsed, cell_format)
            else:
                sheet.write_string(*target, self._safe_text(value), cell_format)
            return
        if kind in {"int", "number", "rate", "duration"} and isinstance(value, (int, float)) and not isinstance(value, bool):
            sheet.write_number(*target, float(value), cell_format)
            return
        sheet.write_string(*target, self._safe_text(value), cell_format)

    @staticmethod
    def _parse_datetime(value: Any, *, date_only: bool) -> datetime.datetime | None:
        if isinstance(value, datetime.datetime):
            parsed = value
        elif isinstance(value, datetime.date):
            parsed = datetime.datetime.combine(value, datetime.time())
        elif isinstance(value, str):
            try:
                parsed = datetime.datetime.fromisoformat(value)
            except ValueError:
                try:
                    parsed = datetime.datetime.combine(datetime.date.fromisoformat(value), datetime.time())
                except ValueError:
                    return None
        else:
            return None
        if date_only:
            parsed = datetime.datetime.combine(parsed.date(), datetime.time())
        return parsed.replace(tzinfo=None)

    @staticmethod
    def _section(sheet: Any, row: int, last_col: int, title: str, formats: Dict[str, Any]) -> None:
        sheet.merge_range(row, 0, row, last_col, title, formats["section"])

    @staticmethod
    def _finish_sheet(
        sheet: Any,
        last_row: int,
        last_col: int,
        *,
        landscape: bool,
        filter_row: int | None,
    ) -> None:
        if filter_row is not None:
            sheet.freeze_panes(filter_row + 1, 0)
            sheet.autofilter(filter_row, 0, last_row, last_col)
            sheet.repeat_rows(filter_row, filter_row)
        if landscape:
            sheet.set_landscape()
        else:
            sheet.set_portrait()
        sheet.set_paper(9)
        sheet.fit_to_pages(1, 0)
        sheet.set_margins(0.25, 0.25, 0.5, 0.5)
        sheet.set_header("&C&10A/B Test 效果分析")
        sheet.set_footer("&L生成报告&R第 &P 页，共 &N 页")
        sheet.print_area(0, 0, last_row, last_col)
