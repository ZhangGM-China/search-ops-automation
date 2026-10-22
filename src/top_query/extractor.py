import datetime
from typing import Any, Dict, List, Optional

from src.integrations.query_extractor import QueryExtractor


class TopQueryDataExtractor:
    """Top Query 新链路的数据抽取入口。"""

    def __init__(self, brand_type: Optional[str] = None):
        self._fixed_brand_type = brand_type
        self._extractors: Dict[str, QueryExtractor] = {}
        self.extractor = None
        if brand_type is not None:
            self.extractor = QueryExtractor(brand_type=brand_type)
            self._extractors[brand_type] = self.extractor

    def _get_extractor_for_brand(self, brand: str) -> QueryExtractor:
        if self._fixed_brand_type is not None:
            return self._extractors[self._fixed_brand_type]
        brand_type = {
            "riviera": "rl",
            "summit": "summit",
        }.get(brand, "rds")
        if brand_type not in self._extractors:
            self._extractors[brand_type] = QueryExtractor(brand_type=brand_type)
        return self._extractors[brand_type]

    def get_top_queries(
        self,
        brand: str,
        target_date: datetime.date,
        limit: int,
    ) -> List[Dict[str, Any]]:
        extractor = self._get_extractor_for_brand(brand)
        return extractor.get_top_query_rows(
            brand,
            target_date=target_date,
            limit=limit,
        )

    def get_brand_metric(self, brand: str, target_date: datetime.date) -> Dict[str, Any]:
        extractor = self._get_extractor_for_brand(brand)
        return extractor.get_brand_daily_metric(brand, target_date)

    def get_query_daily_metrics(
        self,
        brand: str,
        target_date: datetime.date,
        queries: List[str],
    ) -> List[Dict[str, Any]]:
        extractor = self._get_extractor_for_brand(brand)
        return extractor.get_query_daily_metrics(brand, target_date, queries)

    def get_query_daily_metrics_strict(
        self,
        brand: str,
        target_date: datetime.date,
        queries: List[str],
    ) -> Dict[str, Any]:
        extractor = self._get_extractor_for_brand(brand)
        return extractor.get_query_daily_metrics_strict(
            brand,
            target_date,
            queries,
        )

    def get_query_daily_snapshot_strict(
        self,
        brand: str,
        target_date: datetime.date,
    ) -> Dict[str, Any]:
        extractor = self._get_extractor_for_brand(brand)
        return extractor.get_query_daily_snapshot_strict(brand, target_date)

    def get_query_baseline_window_strict(
        self,
        brand: str,
        planned_start_date: datetime.date,
        requested_queries: List[str],
    ) -> Dict[str, Any]:
        extractor = self._get_extractor_for_brand(brand)
        return extractor.get_query_baseline_window_strict(
            brand,
            planned_start_date,
            requested_queries,
        )
