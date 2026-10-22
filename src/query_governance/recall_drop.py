"""Batch evidence gate for yesterday's disappeared recall products."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def product_id(row: Mapping[str, Any]) -> str:
    return str(row.get('product_id') or row.get('productId') or row.get('spu') or row.get('spuId') or row.get('skuId') or '')


def snapshot_ids(snapshot: Mapping[str, Any]) -> set[str] | None:
    ids = snapshot.get('recall_product_ids')
    if snapshot.get('recall_identity_complete') is True and isinstance(ids, list):
        result = {str(v) for v in ids if v}
        return result if len(result) == snapshot.get('result_count') else None
    rows = snapshot.get('products')
    if snapshot.get('result_complete') is not True or not isinstance(rows, list):
        return None
    result = {product_id(row) for row in rows if isinstance(row, Mapping)}
    result.discard('')
    return result if len(result) == snapshot.get('result_count') else None


class RecallDropAssessor:
    def __init__(self, operational, product_details=None):
        self.operational = operational
        self.product_details = product_details

    def assess(self, spec, current, baseline) -> dict[str, Any]:
        previous = baseline.previous_snapshot or {}
        before, after = snapshot_ids(previous), snapshot_ids(current)
        if before is None or after is None:
            return {'status': 'manual_review', 'reason': '召回商品身份快照不完整，不能把首页排名变化当作商品消失'}
        missing = sorted(before - after)
        if not missing:
            return {'status': 'manual_review', 'reason': '数量下降但没有可核对的消失商品'}
        try:
            states = self.operational.get_product_status_map(spec.brand, missing)
            blacklisted = self.operational.get_query_blacklisted_product_ids(spec.brand, spec.query)
        except Exception:
            states, blacklisted = None, None
        if not isinstance(states, Mapping) or blacklisted is None:
            return {'status': 'manual_review', 'reason': '批量商品状态或Query黑名单查询失败', 'missing_count': len(missing)}
        normal, unexplained, unknown = [], [], []
        for identity in missing:
            entry = states.get(identity)
            raw = entry.get('raw') if isinstance(entry, Mapping) else None
            status = raw.get('status') if isinstance(raw, Mapping) else None
            # The documented list contract is status=0/1. Missing rows and zero stock are not offline evidence.
            if identity in blacklisted or status in (0, '0', False):
                normal.append(identity)
            elif status in (1, '1', True):
                unexplained.append(identity)
            else:
                unknown.append(identity)
        samples = unexplained[:10]
        details = {}
        if samples:
            try:
                details = self.product_details.fetch(spec.brand, samples) if self.product_details else {}
            except Exception:
                details = {}
        evidence = {
            'status': 'anomaly' if unexplained else 'manual_review' if unknown else 'normal',
            'missing_count': len(missing), 'normal_count': len(normal), 'unknown_count': len(unknown),
            'unexplained_count': len(unexplained),
            'normal_product_ids': normal, 'unknown_product_ids': unknown,
            'unexplained_products': [{'product_id': identity, 'product_details': details.get(identity) or states[identity].get('raw'),
                                      'historical_graph_available': False} for identity in samples],
        }
        if unknown:
            evidence['reason'] = '部分商品状态未知，不能推断其正常消失'
        return evidence
