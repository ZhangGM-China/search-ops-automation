import datetime
import unittest
from unittest.mock import MagicMock

from src.assistant.tools import AssistantToolService
from src.persistence import (
    Base,
    BrandDailyMetric,
    DailyAbnormalQuery,
    SessionLocal,
    reset_engine_for_testing,
)


class TestAssistantToolService(unittest.TestCase):
    def setUp(self):
        self.engine = reset_engine_for_testing()
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.operational_service = MagicMock()
        self.tool_service = AssistantToolService(operational_service=self.operational_service)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_brand_metrics_tool_requires_allowed_brand_scope(self):
        self.db.add_all([
            BrandDailyMetric(
                brand="atlas",
                data_date=datetime.date(2026, 7, 3),
                brand_uv=45074,
                brand_pv=90000,
                ctr=0.12,
                cvr=0.03,
                top_query_count=150,
            ),
            BrandDailyMetric(
                brand="harbor",
                data_date=datetime.date(2026, 7, 3),
                brand_uv=99999,
                brand_pv=123456,
                ctr=0.99,
                cvr=0.88,
                top_query_count=999,
            ),
        ])
        self.db.commit()

        result = self.tool_service.maybe_handle_message(
            "查看 atlas 品牌指标",
            allowed_brands=["atlas"],
            context={"date": "2026-07-03"},
            selected_brand=None,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["toolCalls"][0]["name"], "read_brand_metrics")
        self.assertIn("45074", result["message"])
        self.assertNotIn("99999", result["message"])

    def test_abnormal_query_log_tool_summarizes_recent_logs(self):
        self.db.add_all([
            DailyAbnormalQuery(
                brand="atlas",
                query_text="香水 女士",
                anomaly_type="low_ctr",
                reason_analysis="首图不吸引点击",
                action_suggestions="建议复核首图和高价商品",
                data_date=datetime.date(2026, 7, 3),
                created_at=datetime.datetime.now(),
                updated_at=datetime.datetime.now(),
            ),
            DailyAbnormalQuery(
                brand="harbor",
                query_text="裤子",
                anomaly_type="low_cvr",
                reason_analysis="不应暴露",
                data_date=datetime.date(2026, 7, 3),
                created_at=datetime.datetime.now(),
                updated_at=datetime.datetime.now(),
            ),
        ])
        self.db.commit()

        result = self.tool_service.maybe_handle_message(
            "查看异常日志",
            allowed_brands=["atlas"],
            context={"date": "2026-07-03"},
            selected_brand="atlas",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["toolCalls"][0]["name"], "read_abnormal_query_logs")
        self.assertIn("香水 女士", result["message"])
        self.assertNotIn("裤子", result["message"])

    def test_inventory_summary_prefers_selected_query_from_page_context(self):
        result = self.tool_service.maybe_handle_message(
            "关于这个Top词的断码情况怎么样？",
            allowed_brands=["atlas"],
            context={
                "date": "2026-07-03",
                "pageContext": {
                    "selectedQuery": "四折精选 女士",
                },
                "inventoryMonitoring": [
                    {"brand": "atlas", "query": "毛衣 女士", "productId": "SKU-OTHER", "status": "monitoring", "lastCoverageRate": 40},
                    {"brand": "atlas", "query": "四折精选 女士", "productId": "SKU-1", "status": "monitoring", "lastCoverageRate": 25},
                    {"brand": "atlas", "query": "四折精选 女士", "productId": "SKU-2", "status": "monitoring", "lastCoverageRate": 25},
                ],
            },
            selected_brand="atlas",
        )

        self.assertIsNotNone(result)
        self.assertIn("四折精选 女士", result["message"])
        self.assertNotIn("毛衣 女士", result["message"])


if __name__ == "__main__":
    unittest.main()
