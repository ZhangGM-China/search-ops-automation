"""Side-effect-free governance-before (S0) collection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .ports import SearchGateway, StateStore
from .models import QuerySpec
from .snapshot import S0SnapshotError, normalize_s0_products
from .product_adapter import ProductAdapterError, ProductAdapterRegistry


class S0CollectionError(RuntimeError):
    """A Mixed failure whose request outcome was persisted separately."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class S0Collector:
    """Collect and persist the immutable S0 snapshot for one Query.

    This boundary intentionally knows only the Mixed search gateway and the
    S0 store.  Diagnostics, model calls, inventory checks, KG operations and
    governance actions belong to later nodes and cannot be reached here.
    """

    def __init__(
        self,
        *,
        search: SearchGateway,
        store: StateStore,
        adapters: ProductAdapterRegistry | None = None,
    ) -> None:
        self.search = search
        self.store = store
        self.adapters = adapters or ProductAdapterRegistry()

    def collect(self, spec: QuerySpec) -> Mapping[str, Any]:
        existing = self.store.get_s0(spec.brand, spec.query)
        if existing is not None:
            return existing

        try:
            mixed_with_total = getattr(self.search, "mixed_with_total", None)
            if callable(mixed_with_total):
                response = mixed_with_total(spec.brand, spec.query)
                if not isinstance(response, Mapping) or response.get("success") is not True:
                    raise RuntimeError(str(response.get("error_reason") or "Mixed 请求失败") if isinstance(response, Mapping) else "Mixed 返回结果无效")
                raw_products_value = response.get("products")
                if not isinstance(raw_products_value, Sequence) or isinstance(raw_products_value, (str, bytes)):
                    raise RuntimeError("Mixed 商品结果无效")
                raw_products = list(raw_products_value)
                raw_total = response.get("total_count")
                if raw_total is not None and (
                    not isinstance(raw_total, int)
                    or isinstance(raw_total, bool)
                    or raw_total < len(raw_products)
                ):
                    raise RuntimeError("Mixed 结果数量无效")
                result_count = raw_total if raw_total is not None else len(raw_products)
                result_count_known = raw_total is not None
                result_complete = bool(response.get("result_complete")) if raw_total is not None else True
            else:
                raw_products = list(self.search.mixed(spec.brand, spec.query))
                result_count = len(raw_products)
                result_count_known = True
                result_complete = True
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            self.store.save_s0_failure(
                spec.brand,
                spec.query,
                "MIXED_FAILED",
                message,
            )
            raise S0CollectionError("MIXED_FAILED", message) from exc

        try:
            adapted_products = []
            for index, raw in enumerate(raw_products, start=1):
                try:
                    adapted_products.append(
                        self.adapters.adapt(spec.brand, raw, rank=index)
                    )
                except ProductAdapterError as exc:
                    if exc.error_code == "PRODUCT_ID_MISSING":
                        raise ProductAdapterError(
                            exc.error_code,
                            f"Mixed result at rank {index} has no product identifier",
                        ) from exc
                    raise
            products = normalize_s0_products(
                [product.to_s0() for product in adapted_products]
            )
        except ProductAdapterError as exc:
            error_code = (
                "S0_PRODUCT_ID_MISSING"
                if exc.error_code == "PRODUCT_ID_MISSING"
                else exc.error_code
            )
            self.store.save_s0_failure(
                spec.brand, spec.query, error_code, exc.message
            )
            raise S0CollectionError(error_code, exc.message) from exc
        except S0SnapshotError as exc:
            self.store.save_s0_failure(
                spec.brand,
                spec.query,
                exc.error_code,
                exc.message,
            )
            raise S0CollectionError(exc.error_code, exc.message) from exc

        candidate = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "products": products,
            "result_count": result_count,
            "result_count_known": result_count_known,
            "result_complete": result_complete,
            "result_count_source": "mixed_total" if result_count_known and not result_complete else "product_list",
            "request_status": "success",
        }
        from .recall_drop import product_id
        identities = {product_id(row) for row in products}
        identities.discard("")
        identity_complete = result_count_known and len(identities) == result_count
        if result_count_known and not identity_complete and callable(mixed_with_total):
            try:
                page_size = 100
                paged_ids = set()
                for page in range(1, min((result_count + page_size - 1) // page_size, 100) + 1):
                    page_result = mixed_with_total(spec.brand, spec.query, page_size=page_size, page_num=page)
                    if page_result.get("success") is not True or page_result.get("total_count") != result_count:
                        break
                    page_rows = page_result.get("products")
                    if not isinstance(page_rows, list) or not page_rows:
                        break
                    paged_ids.update(product_id(row) for row in page_rows if isinstance(row, Mapping))
                paged_ids.discard("")
                if len(paged_ids) == result_count:
                    identities, identity_complete = paged_ids, True
            except Exception:
                identity_complete = False
        candidate["recall_product_ids"] = sorted(identities)
        candidate["recall_identity_complete"] = identity_complete
        return self.store.save_s0_if_absent(
            spec.brand, spec.query, candidate
        )
