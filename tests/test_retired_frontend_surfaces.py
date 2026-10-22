from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class RetiredFrontendSurfacesTest(unittest.TestCase):
    def test_legacy_pages_and_components_are_removed(self):
        retired_paths = [
            FRONTEND / "pages" / "OpsDashboard",
            FRONTEND / "pages" / "ManualAttention",
            FRONTEND / "pages" / "AbnormalQueries",
            FRONTEND / "pages" / "TaskBoard",
            FRONTEND / "components" / "QueryStatusTag.tsx",
            FRONTEND / "pages" / "Config" / "components" / "GlobalConfigSection.tsx",
            FRONTEND / "pages" / "Config" / "components" / "experiment" / "ExperimentCurrentView.tsx",
            FRONTEND / "pages" / "Config" / "components" / "experiment" / "ExperimentCurrentView.test.tsx",
            FRONTEND / "pages" / "Config" / "components" / "experiment" / "ExperimentGovernanceCoverage.tsx",
            FRONTEND / "pages" / "Config" / "components" / "experiment" / "ExperimentQualityTrend.tsx",
            FRONTEND / "components" / "QueryDetail" / "ExpertRankingPanel.tsx",
            FRONTEND / "components" / "QueryDetail" / "ExpertRankingPanel.test.tsx",
            FRONTEND / "components" / "QueryDetail" / "ExpertRankingPanel.contract.test.tsx",
            FRONTEND / "components" / "QueryDetail" / "expertRanking.ts",
            FRONTEND / "components" / "QueryDetail" / "expertRanking.test.ts",
        ]
        self.assertEqual([], [str(path) for path in retired_paths if path.exists()])

    def test_legacy_dashboard_clients_and_types_are_removed(self):
        api_source = (FRONTEND / "shared" / "api" / "index.ts").read_text()
        types_source = (FRONTEND / "shared" / "types" / "index.ts").read_text()
        app_source = (FRONTEND / "App.tsx").read_text()
        mobile_source = (FRONTEND / "app" / "layouts" / "MobileLayout.tsx").read_text()

        for symbol in (
            "getTaskBoard",
            "getTaskDetail",
            "getOpsDashboard",
            "getManualAttention",
            "getAbnormalQueries",
            "getTopQueryExperimentOverview",
            "getTopQueryExperimentTrend",
            "getTopQueryExperimentQueries",
            "getTopQueryExpertRanking",
            "generateTopQueryExpertRanking",
            "retryTopQueryExpertRanking",
            "saveTopQueryExpertRankingPlan",
            "getQueryGovernanceDetail",
            "getQueryGovernanceLiveResult",
            "getQueryGovernanceEvents",
        ):
            self.assertNotIn(symbol, api_source)

        for symbol in (
            "ManualAttentionQuery",
            "OpsDashboardData",
            "OpsTaskBoardData",
            "OpsTaskType",
            "OpsTaskRelatedProduct",
            "ExpertRankingState",
            "ExpertRankingRun",
            "ExpertRankingPlan",
            "ExpertRankingTemplate",
            "QueryGovernanceQueryDetailData",
            "QueryGovernanceLiveResult",
            "QueryGovernanceEventsData",
        ):
            self.assertNotIn(symbol, types_source)

        self.assertNotIn("OpsExpertRanking", types_source)
        for route in ("/manual-attention", "/task-board"):
            self.assertNotIn(route, app_source)
            self.assertNotIn(route, mobile_source)

    def test_unused_frontend_exports_are_removed_and_active_chart_dependency_is_retained(self):
        api_source = (FRONTEND / "shared" / "api" / "index.ts").read_text()
        columns_source = (
            FRONTEND / "features" / "no-result" / "pages" / "NoResultGovernance" / "columns.tsx"
        ).read_text()
        package_source = (ROOT / "frontend" / "package.json").read_text()
        vite_source = (ROOT / "frontend" / "vite.config.ts").read_text()

        self.assertNotIn("healthApi", api_source)
        self.assertNotIn("createNoResultGovernanceColumns", columns_source)
        self.assertIn('"@ant-design/charts"', package_source)
        self.assertIn("@ant-design/charts", vite_source)
        chart_sources = "\n".join(path.read_text() for path in FRONTEND.rglob("*.tsx"))
        self.assertIn("from '@ant-design/charts'", chart_sources)

    def test_legacy_experiment_frontend_fixture_is_removed(self):
        frontend_source = "\n".join(
            path.read_text()
            for root in (FRONTEND, ROOT / "frontend" / "e2e")
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".ts", ".tsx"}
        )

        for legacy_marker in (
            "/manual-attention",
            "/task-board",
            "top-query-ab-test-v1",
            "旧实验保留历史结果",
            "该实验不按新版 7 日基线重算。",
        ):
            self.assertNotIn(legacy_marker, frontend_source)


if __name__ == "__main__":
    unittest.main()
