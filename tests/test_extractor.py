import unittest
from unittest.mock import patch, MagicMock
from src.integrations.query_extractor import QueryExtractor
from src.governance.query_identity import is_non_intent_query


def test_product_code_like_queries_are_filtered_at_extraction_boundary():
    assert is_non_intent_query("5644315081 女士")
    assert is_non_intent_query("3205/810/800 女士")
    assert not is_non_intent_query("001")
    assert is_non_intent_query("12345 女士")
    assert not is_non_intent_query("123 女士")
    assert not is_non_intent_query("Z1975 女士")
    assert not is_non_intent_query("100%棉")
import datetime

class TestQueryExtractor(unittest.TestCase):
    
    @patch('src.integrations.query_extractor.create_engine')
    @patch('src.integrations.query_extractor.os.getenv')
    def test_init_rds(self, mock_getenv, mock_create_engine):
        mock_getenv.side_effect = lambda k, default=None: {
            "RD_RDS_USER": "u",
            "RD_RDS_PASS": "p",
            "RD_RDS_HOST": "h",
            "RD_RDS_PORT": "3306",
            "RD_RDS_DB": "d"
        }.get(k, default)
        
        extractor = QueryExtractor(brand_type="rds")
        self.assertIn("mysql+pymysql://u:p@h:3306/d", extractor.uri)
        mock_create_engine.assert_called_once_with(
            extractor.uri,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": 10,
                "read_timeout": 60,
                "write_timeout": 60,
            },
        )

    @patch('src.integrations.query_extractor.create_engine')
    @patch('src.integrations.query_extractor.os.getenv')
    def test_init_rl(self, mock_getenv, mock_create_engine):
        mock_getenv.side_effect = lambda k, default=None: {
            "RD_RIVIERA_USER": "u_rl",
            "RD_RIVIERA_PASS": "p_rl",
            "RD_RIVIERA_HOST": "h_rl",
            "RD_RIVIERA_PORT": "3306",
            "RD_RIVIERA_DB": "d_rl"
        }.get(k, default)
        
        extractor = QueryExtractor(brand_type="rl")
        self.assertIn("mysql+pymysql://u_rl:p_rl@h_rl:3306/d_rl", extractor.uri)

    @patch('src.integrations.query_extractor.create_engine')
    @patch('src.integrations.query_extractor.os.getenv')
    def test_init_summit(self, mock_getenv, mock_create_engine):
        mock_getenv.side_effect = lambda k, default=None: {
            "RD_SUMMIT_USER": "u_dec",
            "RD_SUMMIT_PASS": "p_dec",
            "RD_SUMMIT_HOST": "h_dec",
            "RD_SUMMIT_PORT": "3306",
            "RD_SUMMIT_DB": "d_dec"
        }.get(k, default)

        extractor = QueryExtractor(brand_type="summit")
        self.assertIn("mysql+pymysql://u_dec:p_dec@h_dec:3306/d_dec", extractor.uri)

    @patch('src.integrations.query_extractor.create_engine')
    @patch('src.integrations.query_extractor.os.getenv')
    def test_init_reports_missing_data_source_env(self, mock_getenv, mock_create_engine):
        mock_getenv.side_effect = lambda k, default=None: {
            "RD_RDS_USER": "u",
            "RD_RDS_PASS": "p",
            "RD_RDS_HOST": "h",
            "RD_RDS_DB": "d",
        }.get(k, default)

        with self.assertRaisesRegex(ValueError, "RD_RDS_PORT"):
            QueryExtractor(brand_type="rds")
        mock_create_engine.assert_not_called()

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_abnormal_queries_atlas(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        
        # Mock result set
        mock_row = MagicMock()
        mock_row._mapping = {
            "query": "test",
            "search_uv": 100,
            "ctr": 0.05,
            "cvr": 0.01,
            "ctr_avg": 0.06,
            "cvr_avg": 0.02,
            "type": "low_ctr",
            "ctr_gap": -1.0,
            "cvr_gap": -1.0
        }
        mock_conn.execute.return_value = [mock_row]
        
        extractor = QueryExtractor()
        res = extractor.get_abnormal_queries("atlas")
        
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["query"], "test")
        self.assertEqual(res[0]["search_uv"], 100.0)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_abnormal_queries_harbor(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = []
        
        extractor = QueryExtractor()
        extractor.get_abnormal_queries("harbor")
        
        # Verify SQL contains HARBOR specific columns (now aligned with standard)
        sql_arg = mock_conn.execute.call_args[0][0].text
        self.assertIn("query_search_uv", sql_arg)
        self.assertIn("query_search_click_uv", sql_arg)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_abnormal_queries_failure_returns_empty(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("db error")
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn

        extractor = QueryExtractor()
        self.assertEqual(extractor.get_abnormal_queries("atlas"), [])

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_top_queries_riviera_uses_relative_gaps(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = []

        extractor = QueryExtractor()
        extractor.get_top_queries("riviera")

        sql_arg = mock_conn.execute.call_args[0][0].text
        self.assertIn("search_term", sql_arg)
        self.assertIn("/ NULLIF(p.avg_ctr, 0) * 100", sql_arg)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_top_queries_failure_returns_empty(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("db error")
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn

        extractor = QueryExtractor()
        self.assertEqual(extractor.get_top_queries("atlas"), [])

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_query_performance(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        
        mock_conn.execute.return_value.fetchone.return_value = [200, 100, 5, 3, 1]
        
        extractor = QueryExtractor()
        res = extractor.get_query_performance("atlas", "test", datetime.date(2026, 5, 20))
        
        self.assertEqual(res["uv"], 100)
        self.assertEqual(res["pv"], 200)
        self.assertEqual(res["click_uv"], 5)
        self.assertEqual(res["add_to_cart_uv"], 3)
        self.assertEqual(res["purchase_uv"], 1)
        self.assertEqual(res["ctr"], 0.05)
        self.assertEqual(res["atc"], 0.03)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_query_performance_empty_returns_missing_payload(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchone.return_value = None

        extractor = QueryExtractor()
        res = extractor.get_query_performance("riviera", "test", datetime.date(2026, 5, 20))

        self.assertEqual(res, {"date": "2026-05-20", "status": "missing"})
        sql_arg = mock_conn.execute.call_args[0][0].text
        self.assertIn("search_term", sql_arg)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_query_performance_harbor_uses_harbor_columns(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchone.return_value = [2, 1, 1, 0, 0]

        extractor = QueryExtractor()
        extractor.get_query_performance("harbor", "test", datetime.date(2026, 5, 20))

        sql_arg = mock_conn.execute.call_args[0][0].text
        self.assertIn("query_search_uv", sql_arg)
        self.assertIn("query_search_click_uv", sql_arg)
        self.assertIn("query_search_atc_uv", sql_arg)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_query_performance_failure_returns_failed_payload(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("db error")
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn

        extractor = QueryExtractor()
        res = extractor.get_query_performance("atlas", "test", datetime.date(2026, 5, 20))

        self.assertEqual(res, {"date": "2026-05-20", "status": "failed"})

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_queries_performance_batch(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        
        mock_row = ["test", 200, 100, 5, 3, 1]
        mock_conn.execute.return_value = [mock_row]
        
        extractor = QueryExtractor()
        res = extractor.get_queries_performance_batch("atlas", ["test"], datetime.date(2026, 5, 20))
        
        self.assertIn("test", res)
        self.assertEqual(res["test"]["uv"], 100)
        self.assertEqual(res["test"]["pv"], 200)
        self.assertEqual(res["test"]["add_to_cart_uv"], 3)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_queries_performance_batch_empty_short_circuit(self, mock_create_engine):
        extractor = QueryExtractor()
        self.assertEqual(extractor.get_queries_performance_batch("atlas", [], datetime.date(2026, 5, 20)), {})

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_queries_performance_batch_failure_returns_empty(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("db error")
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn

        extractor = QueryExtractor()
        self.assertEqual(
            extractor.get_queries_performance_batch("harbor", ["test"], datetime.date(2026, 5, 20)),
            {}
        )

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_no_result_queries_harbor_uses_python_date_parameter(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_row = MagicMock()
        mock_row._mapping = {"search_text": "粉色大衣", "search_pv": 12}
        mock_conn.execute.return_value = [mock_row]

        target_date = datetime.date(2026, 6, 22)
        extractor = QueryExtractor()
        rows = extractor.get_no_result_queries("harbor", target_date=target_date, limit=20)

        sql_arg = mock_conn.execute.call_args[0][0].text
        params = mock_conn.execute.call_args[0][1]
        self.assertEqual(rows[0]["search_text"], "粉色大衣")
        self.assertEqual(params["target_date"], "2026-06-22")
        self.assertNotIn("2026-06-21", sql_arg)
        self.assertIn("recall_count = 0", sql_arg)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_no_result_queries_skips_brands_without_sql(self, mock_create_engine):
        extractor = QueryExtractor()

        self.assertEqual(extractor.get_no_result_queries("atlas", datetime.date(2026, 6, 22)), [])
        self.assertEqual(extractor.get_no_result_queries("meadow", datetime.date(2026, 6, 22)), [])

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_query_daily_metrics_strict_merges_query_variants_and_preserves_display_query(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = [
            {
                "query": "ZW  女士",
                "query_rank": 1,
                "search_uv": 60,
                "search_pv": 100,
                "ctr": 0.5,
                "cvr": 0.1,
                "avg_ctr": 0.4,
                "avg_cvr": 0.08,
                "query_pv_share": 0.5,
            },
            {
                "query": "ｚｗ 女士",
                "query_rank": 2,
                "search_uv": 40,
                "search_pv": 80,
                "ctr": 0.25,
                "cvr": 0.05,
                "avg_ctr": 0.3,
                "avg_cvr": 0.06,
                "query_pv_share": 0.4,
            },
        ]

        extractor = QueryExtractor()
        result = extractor.get_query_daily_metrics_strict(
            "atlas",
            datetime.date(2026, 7, 9),
            ["zw 女士"],
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["requestedKeys"], ["zw 女士"])
        self.assertEqual(result["matchedKeys"], ["zw 女士"])
        self.assertEqual(result["missingKeys"], [])
        self.assertEqual(result["duplicateKeys"], ["zw 女士"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["query"], "zw 女士")
        self.assertEqual(result["items"][0]["search_uv"], 100.0)
        self.assertEqual(result["items"][0]["search_pv"], 180.0)
        self.assertAlmostEqual(result["items"][0]["ctr"], 0.4)
        self.assertEqual(mock_conn.execute.call_count, 1)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_query_daily_metrics_legacy_path_keeps_requested_query_sql_filter(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = []
        extractor = QueryExtractor()

        result = extractor.get_query_daily_metrics(
            "atlas",
            datetime.date(2026, 7, 9),
            ["zw 女士"],
        )

        self.assertEqual(result, [])
        sql = mock_conn.execute.call_args.args[0].text
        params = mock_conn.execute.call_args.args[1]
        self.assertIn("`query` IN (:query_0)", sql)
        self.assertEqual(params["query_0"], "zw 女士")
        self.assertEqual(mock_conn.execute.call_count, 1)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_query_daily_metrics_strict_uses_one_unfiltered_snapshot_to_merge_all_canonical_variants(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        ordinary_variant = {
            "query": "zw 女士",
            "query_rank": 1,
            "search_uv": 70,
            "search_pv": 140,
            "ctr": 0.5,
            "cvr": 0.1,
            "avg_ctr": 0.4,
            "avg_cvr": 0.08,
            "query_pv_share": 0.7,
        }
        nfkc_variant = {
                "query": "\tＺＷ\n　女士 ",
                "query_rank": 2,
                "search_uv": 30,
                "search_pv": 60,
                "ctr": 0.25,
                "cvr": 0.05,
                "avg_ctr": 0.35,
                "avg_cvr": 0.04,
                "query_pv_share": 0.3,
        }

        def execute_by_sql(sql, _params):
            if " IN (" in sql.text:
                return [ordinary_variant]
            return [ordinary_variant, nfkc_variant]

        mock_conn.execute.side_effect = execute_by_sql
        extractor = QueryExtractor()

        result = extractor.get_query_daily_metrics_strict(
            "atlas",
            datetime.date(2026, 7, 9),
            ["zw 女士"],
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["matchedKeys"], ["zw 女士"])
        self.assertEqual(result["missingKeys"], [])
        self.assertEqual(result["duplicateKeys"], ["zw 女士"])
        self.assertEqual(result["items"][0]["query"], "zw 女士")
        self.assertEqual(result["items"][0]["search_uv"], 100.0)
        self.assertEqual(result["items"][0]["search_pv"], 200.0)
        self.assertAlmostEqual(result["items"][0]["ctr"], 0.425)
        self.assertAlmostEqual(result["items"][0]["cvr"], 0.085)
        self.assertEqual(mock_conn.execute.call_count, 1)
        strict_sql = mock_conn.execute.call_args.args[0].text
        self.assertNotIn("query_0", strict_sql)
        self.assertNotIn(" IN (", strict_sql)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_query_daily_metrics_strict_merges_current_and_history_numerators_by_canonical_key(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = [
            {
                "query": "zw 女士",
                "query_rank": 1,
                "search_uv": 70,
                "search_pv": 140,
                "current_click_uv": 35,
                "current_purchase_uv": 7,
                "ctr": 0.5,
                "cvr": 0.1,
                "history_search_uv": 100,
                "history_click_uv": 30,
                "history_purchase_uv": 5,
                "history_dates": "2026-07-01,2026-07-02,2026-07-03",
                "avg_ctr": 0.3,
                "avg_cvr": 0.05,
                "query_pv_share": 0.7,
            },
            {
                "query": "ＺＷ 女士",
                "query_rank": 2,
                "search_uv": 30,
                "search_pv": 60,
                "current_click_uv": 6,
                "current_purchase_uv": 1.5,
                "ctr": 0.2,
                "cvr": 0.05,
                "history_search_uv": 50,
                "history_click_uv": 30,
                "history_purchase_uv": 10,
                "history_dates": "2026-07-04,2026-07-05",
                "avg_ctr": 0.6,
                "avg_cvr": 0.2,
                "query_pv_share": 0.3,
            },
        ]
        extractor = QueryExtractor()

        result = extractor.get_query_daily_metrics_strict(
            "atlas",
            datetime.date(2026, 7, 9),
            ["zw 女士"],
        )

        item = result["items"][0]
        self.assertAlmostEqual(item["ctr"], 0.41)
        self.assertAlmostEqual(item["cvr"], 0.085)
        self.assertAlmostEqual(item["avg_ctr"], 0.4)
        self.assertAlmostEqual(item["avg_cvr"], 0.1)
        self.assertNotAlmostEqual(item["avg_ctr"], 0.39)
        sql = mock_conn.execute.call_args.args[0].text
        self.assertIn("GROUP_CONCAT(DISTINCT", sql)
        self.assertNotIn("HAVING COUNT(DISTINCT", sql)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_query_daily_metrics_strict_recomputes_rank_and_share_after_brandwide_canonical_merge(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = [
            {"query": "zw 女士", "query_rank": 1, "search_uv": 70, "search_pv": 140, "ctr": 0.5, "cvr": 0.1, "query_pv_share": 0.7},
            {"query": "连衣裙", "query_rank": 2, "search_uv": 200, "search_pv": 500, "ctr": 0.3, "cvr": 0.05, "query_pv_share": 0.2},
            {"query": "ＺＷ 女士", "query_rank": 3, "search_uv": 30, "search_pv": 60, "ctr": 0.25, "cvr": 0.05, "query_pv_share": 0.3},
        ]
        extractor = QueryExtractor()

        result = extractor.get_query_daily_metrics_strict(
            "atlas",
            datetime.date(2026, 7, 9),
            ["zw 女士"],
        )

        item = result["items"][0]
        self.assertEqual(item["query_rank"], 2)
        self.assertAlmostEqual(item["query_pv_share"], 200 / 700)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_query_daily_metrics_strict_links_history_only_variant_by_python_canonical_key(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        current = {"row_type": "current", "query": "zw 女士", "search_uv": 100, "search_pv": 200, "current_click_uv": 40, "current_purchase_uv": 8}
        history = {
            "row_type": "history", "query": "ＺＷ  女士", "history_search_uv": 200,
            "history_click_uv": 60, "history_purchase_uv": 20,
            "history_dates": "2026-07-01,2026-07-02,2026-07-03,2026-07-04,2026-07-05",
        }

        def execute_by_sql(sql, _params):
            if "UNION ALL" in sql.text:
                return [current, history]
            return [current]

        mock_conn.execute.side_effect = execute_by_sql
        result = QueryExtractor().get_query_daily_metrics_strict(
            "atlas", datetime.date(2026, 7, 9), ["zw 女士"]
        )

        self.assertAlmostEqual(result["items"][0]["avg_ctr"], 0.3)
        self.assertAlmostEqual(result["items"][0]["avg_cvr"], 0.1)
        self.assertIn("UNION ALL", mock_conn.execute.call_args.args[0].text)

    @patch('src.integrations.query_extractor.create_engine')
    def test_get_query_daily_metrics_strict_distinguishes_partial_from_failed(self, mock_create_engine):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = [{
            "query": "连衣裙",
            "query_rank": 1,
            "search_uv": 10,
            "search_pv": 20,
            "ctr": 0.5,
            "cvr": 0.1,
            "avg_ctr": 0.4,
            "avg_cvr": 0.08,
            "query_pv_share": 1,
        }]
        extractor = QueryExtractor()

        partial = extractor.get_query_daily_metrics_strict(
            "atlas",
            datetime.date(2026, 7, 9),
            ["连衣裙", "短裤 女士"],
        )

        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["missingKeys"], ["短裤 女士"])
        self.assertIsNone(partial["error"])

        mock_conn.execute.side_effect = RuntimeError("db error")
        failed = extractor.get_query_daily_metrics_strict(
            "atlas",
            datetime.date(2026, 7, 9),
            ["连衣裙"],
        )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["matchedKeys"], [])
        self.assertEqual(failed["missingKeys"], ["连衣裙"])
        self.assertIn("db error", failed["error"])

    @staticmethod
    def _baseline_rows(query="小黑裙"):
        rows = []
        for day in range(1, 8):
            rows.append({
                "data_date": f"2026-07-{day:02d}",
                "query": query,
                "search_uv": 100 if day == 1 else 200 if day == 2 else 0,
                "search_pv": 150 if day == 1 else 300 if day == 2 else 0,
                "click_uv": 2 if day == 1 else 6 if day == 2 else 0,
                "purchase_uv": 1 if day == 1 else 2 if day == 2 else 0,
            })
        return rows

    @patch('src.integrations.query_extractor.create_engine')
    def test_query_baseline_window_uses_exact_seven_days_and_weighted_numerators(
        self,
        mock_create_engine,
    ):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = self._baseline_rows()

        result = QueryExtractor().get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), ["小黑裙"]
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["baselineStartDate"], "2026-07-01")
        self.assertEqual(result["baselineEndDate"], "2026-07-07")
        self.assertEqual(result["expectedDays"], 7)
        item = result["items"][0]
        self.assertEqual(item["observedDays"], 7)
        self.assertEqual(item["searchUv"], 300)
        self.assertEqual(item["searchPv"], 450)
        self.assertIs(type(item["searchUv"]), int)
        self.assertIs(type(item["searchPv"]), int)
        self.assertEqual(item["clickUv"], 8.0)
        self.assertEqual(item["purchaseUv"], 3.0)
        self.assertAlmostEqual(item["ctr"], 8 / 300)
        self.assertAlmostEqual(item["cvr"], 3 / 300)
        self.assertNotAlmostEqual(item["ctr"], (0.02 + 0.03) / 2)
        sql = mock_conn.execute.call_args.args[0].text
        params = mock_conn.execute.call_args.args[1]
        self.assertIn("SUM(query_search_click_uv) AS click_uv", sql)
        self.assertIn("SUM(query_search_pay_uv) AS purchase_uv", sql)
        self.assertNotIn(" IN (", sql)
        self.assertEqual(params, {
            "baseline_start_date": "2026-07-01",
            "baseline_end_date": "2026-07-07",
        })
        self.assertEqual(mock_conn.execute.call_count, 1)

    @patch('src.integrations.query_extractor.create_engine')
    def test_query_baseline_window_requires_every_natural_day(
        self,
        mock_create_engine,
    ):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = self._baseline_rows()[:-1]

        result = QueryExtractor().get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), ["小黑裙"]
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["items"][0]["observedDays"], 6)
        self.assertEqual(result["error"], "baseline_window_incomplete")

    @patch('src.integrations.query_extractor.create_engine')
    def test_query_baseline_window_keeps_requested_missing_queries_visible(
        self,
        mock_create_engine,
    ):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = self._baseline_rows("小黑裙")

        result = QueryExtractor().get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), ["小黑裙", "zw 女士"]
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["requestedKeys"], ["小黑裙", "zw 女士"])
        self.assertEqual(result["matchedKeys"], ["小黑裙"])
        self.assertEqual(result["missingKeys"], ["zw 女士"])
        self.assertEqual(result["error"], "baseline_window_incomplete")

    @patch('src.integrations.query_extractor.create_engine')
    def test_query_baseline_window_merges_variants_without_duplicate_days(
        self,
        mock_create_engine,
    ):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        rows = self._baseline_rows("ZW  女士")
        rows.append({
            "data_date": "2026-07-01",
            "query": "\tｚｗ\n　女士 ",
            "search_uv": 50,
            "search_pv": 80,
            "click_uv": 5,
            "purchase_uv": 1,
        })
        mock_conn.execute.return_value = rows

        result = QueryExtractor().get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), [" ＺＷ   女士 "]
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["requestedKeys"], ["zw 女士"])
        item = result["items"][0]
        self.assertEqual(item["query"], "ZW 女士")
        self.assertEqual(item["observedDays"], 7)
        self.assertEqual(item["searchUv"], 350.0)
        self.assertEqual(item["clickUv"], 13.0)
        self.assertAlmostEqual(item["ctr"], 13 / 350)

    @patch('src.integrations.query_extractor.create_engine')
    def test_query_baseline_window_rejects_zero_and_invalid_metrics(
        self,
        mock_create_engine,
    ):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        extractor = QueryExtractor()

        zero_rows = self._baseline_rows()
        for row in zero_rows:
            row.update(search_uv=0, click_uv=0, purchase_uv=0)
        mock_conn.execute.return_value = zero_rows
        zero = extractor.get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), ["小黑裙"]
        )
        self.assertEqual(zero["status"], "partial")
        self.assertEqual(zero["error"], "baseline_zero_search_uv")

        invalid_rows = self._baseline_rows()
        invalid_rows[0]["click_uv"] = float("nan")
        mock_conn.execute.return_value = invalid_rows
        invalid = extractor.get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), ["小黑裙"]
        )
        self.assertEqual(invalid["status"], "partial")
        self.assertEqual(invalid["error"], "baseline_invalid_metric")

        negative_rows = self._baseline_rows()
        negative_rows[0]["search_pv"] = -1
        mock_conn.execute.return_value = negative_rows
        negative = extractor.get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), ["小黑裙"]
        )
        self.assertEqual(negative["status"], "partial")
        self.assertEqual(negative["error"], "baseline_invalid_metric")

        fractional_rows = self._baseline_rows()
        fractional_rows[0]["search_uv"] = 100.5
        mock_conn.execute.return_value = fractional_rows
        fractional_uv = extractor.get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), ["小黑裙"]
        )
        self.assertEqual(fractional_uv["status"], "partial")
        self.assertEqual(fractional_uv["error"], "baseline_invalid_metric")

        fractional_rows = self._baseline_rows()
        fractional_rows[0]["search_pv"] = 150.25
        mock_conn.execute.return_value = fractional_rows
        fractional_pv = extractor.get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), ["小黑裙"]
        )
        self.assertEqual(fractional_pv["status"], "partial")
        self.assertEqual(fractional_pv["error"], "baseline_invalid_metric")

    @patch('src.integrations.query_extractor.create_engine')
    @patch('src.integrations.query_extractor.logging.error')
    def test_query_baseline_window_returns_failed_for_sql_error(
        self,
        mock_logging_error,
        mock_create_engine,
    ):
        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        sensitive_error = "mysql://user:secret@private-host/db read timeout"
        mock_conn.execute.side_effect = RuntimeError(sensitive_error)

        result = QueryExtractor().get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), ["小黑裙"]
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["matchedKeys"], [])
        self.assertEqual(result["missingKeys"], ["小黑裙"])
        self.assertEqual(result["error"], "baseline_source_query_failed")
        logged = " ".join(str(value) for value in mock_logging_error.call_args.args)
        self.assertIn("baseline_source_query_failed", logged)
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("secret", logged)
        self.assertNotIn("private-host", logged)
        self.assertNotIn("read timeout", logged)
        self.assertNotIn("atlas", logged)

    @patch('src.integrations.query_extractor.create_engine')
    def test_query_baseline_window_rejects_empty_and_folds_duplicates(
        self,
        mock_create_engine,
    ):
        extractor = QueryExtractor()
        empty = extractor.get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), [" ", "\t"]
        )
        self.assertEqual(empty["status"], "failed")
        self.assertEqual(empty["error"], "baseline_requested_queries_empty")
        mock_create_engine.return_value.connect.assert_not_called()

        mock_conn = MagicMock()
        mock_create_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = self._baseline_rows("ZW 女士")
        folded = extractor.get_query_baseline_window_strict(
            "atlas", datetime.date(2026, 7, 8), ["ZW  女士", "ｚｗ 女士"]
        )
        self.assertEqual(folded["status"], "complete")
        self.assertEqual(folded["requestedKeys"], ["zw 女士"])
        self.assertEqual(folded["items"][0]["query"], "ZW 女士")


if __name__ == '__main__':
    unittest.main()
