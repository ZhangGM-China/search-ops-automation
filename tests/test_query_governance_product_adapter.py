from dataclasses import dataclass, field

from src.query_governance.collection import S0Collector
from src.query_governance.models import QuerySpec
from src.query_governance.product_adapter import ProductAdapterRegistry


def test_generic_adapter_preserves_distinct_identifiers_and_builds_minimal_s0():
    raw = {
        "productId": "PRODUCT-1",
        "skuId": "SKU-1",
        "spuId": "SPU-1",
        "productName": "女士亚麻连衣裙",
        "mainImage": "https://cdn.example.com/dress.jpg",
        "originalPrice": "399.00",
        "discountPrice": 299,
        "tagName": "不应进入S0",
        "description": "轻盈透气",
        "gender": "women",
        "series": "summer",
        "category": "dress",
        "style": "casual",
        "color": "white",
        "size": "M",
        "material": "linen",
        "design": "midi",
        "stock": 12,
        "ext": {"bucket": "productIdsByCode"},
        "isNew": True,
    }

    product = ProductAdapterRegistry().adapt("atlas", raw, rank=3)

    assert product.to_s0() == {
        "product_id": "PRODUCT-1",
        "sku_id": "SKU-1",
        "spu_id": "SPU-1",
        "rank": 3,
        "product_name": "女士亚麻连衣裙",
        "image_url": "https://cdn.example.com/dress.jpg",
        "original_price": 399.0,
        "discount_price": 299.0,
        "bucket": "productIdsByCode",
    }
    assert "tagName" not in product.to_s0()


def test_generic_adapter_omits_duplicate_ids_and_defaults_discount_price():
    product = ProductAdapterRegistry().adapt(
        "harbor",
        {
            "product_id": "SAME-1",
            "sku_id": "SAME-1",
            "spu_id": "SAME-1",
            "name": "基础款T恤",
            "imageUrl": "//cdn.example.com/tshirt.jpg",
            "price": 99,
        },
        rank=1,
    )

    assert product.to_s0() == {
        "product_id": "SAME-1",
        "rank": 1,
        "product_name": "基础款T恤",
        "image_url": "//cdn.example.com/tshirt.jpg",
        "original_price": 99.0,
        "discount_price": 99.0,
    }


def test_generic_adapter_uses_title_as_product_name():
    product = ProductAdapterRegistry().adapt(
        "atlas",
        {"productId": "P-1", "title": "标题商品"},
        rank=1,
    )

    assert product.product_name == "标题商品"
    assert product.to_relevance_input()["productName"] == "标题商品"


def test_generic_adapter_builds_behavior_and_relevance_contracts_from_same_product():
    product = ProductAdapterRegistry().adapt(
        "meadow",
        {
            "productId": "P-1",
            "productName": "防晒外套",
            "mainImage": "https://cdn.example.com/jacket.jpg",
            "price": 199,
            "description": "轻薄防晒",
            "gender": "unisex",
            "series": "airism",
            "category": "jacket",
            "style": "sport",
            "color": "blue",
            "size": "L",
            "material": "nylon",
            "design": "hooded",
            "stock": 8,
            "isNew": False,
        },
        rank=6,
    )

    behavior = product.to_behavior_evidence(click_rank=2, purchase_rank=1)
    relevance = product.to_relevance_input()

    assert behavior["product_id"] == relevance["productId"] == "P-1"
    assert behavior["product_name"] == relevance["productName"] == "防晒外套"
    assert behavior["image_url"] == relevance["imageUrl"]
    assert behavior["price"] == relevance["price"] == 199.0
    assert behavior["discount_price"] == relevance["discountPrice"] == 199.0
    assert behavior["current_rank"] == 6
    assert behavior["click_rank"] == 2
    assert behavior["purchase_rank"] == 1
    assert relevance == {
        "productId": "P-1",
        "productName": "防晒外套",
        "imageUrl": "https://cdn.example.com/jacket.jpg",
        "description": "轻薄防晒",
        "gender": "unisex",
        "series": "airism",
        "category": "jacket",
        "style": "sport",
        "color": "blue",
        "size": "L",
        "material": "nylon",
        "design": "hooded",
        "price": 199.0,
        "discountPrice": 199.0,
        "stock": 8,
        "isNew": False,
    }


def test_summit_adapter_preserves_all_model_codes_and_variant_meaning():
    raw = {
        "dsmCode": "DSM123",
        "brandDisplayName": "WEDZE",
        "productNatureText": "滑雪夹克",
        "family": "滑雪服",
        "designedFor": "成人滑雪",
        "gender": "unisex",
        "material": "polyester",
        "models": [
            {
                "modelCode": "MC001",
                "webLabel": "蓝色滑雪夹克",
                "colorLabel": "蓝色",
                "price": {"activePrice": 399},
                "media": {"images": ["https://cdn.example.com/blue.jpg"]},
                "stock": 4,
            },
            {
                "modelCode": "MC002",
                "webLabel": "红色滑雪夹克",
                "colorLabel": "红色",
                "price": {"activePrice": 429},
                "media": {"images": ["https://cdn.example.com/red.jpg"]},
                "stock": 2,
            },
        ],
    }

    product = ProductAdapterRegistry().adapt("summit", raw, rank=4)

    assert product.product_id == "MC001;MC002"
    assert [variant.product_id for variant in product.variants] == ["MC001", "MC002"]
    assert product.to_s0() == {
        "product_id": "MC001;MC002",
        "rank": 4,
        "product_name": "蓝色滑雪夹克",
        "image_url": "https://cdn.example.com/blue.jpg",
        "original_price": 399.0,
        "discount_price": 399.0,
    }

    behavior = product.to_behavior_evidence(click_rank=3, purchase_rank=None)
    relevance = product.to_relevance_input()
    assert [item["product_id"] for item in behavior["variants"]] == ["MC001", "MC002"]
    assert behavior["variants"][0]["image_url"] == "https://cdn.example.com/blue.jpg"
    assert behavior["variants"][0]["price"] == 399.0
    assert behavior["variants"][0]["attributes"]["gender"] == "unisex"
    assert behavior["variants"][0]["attributes"]["material"] == "polyester"
    assert [item["productId"] for item in relevance["variants"]] == ["MC001", "MC002"]
    assert relevance["variants"][1]["productName"] == "红色滑雪夹克"
    assert relevance["variants"][1]["color"] == "红色"
    assert relevance["category"] == "滑雪夹克"
    assert relevance["description"] == "成人滑雪"


def test_distinct_sku_and_spu_ids_remain_traceable_in_later_node_contracts():
    product = ProductAdapterRegistry().adapt(
        "riviera",
        {
            "productId": "PRODUCT-1",
            "skuId": "SKU-1",
            "spuId": "SPU-1",
            "productName": "Polo衫",
            "price": 799,
        },
        rank=2,
    )

    behavior = product.to_behavior_evidence()
    relevance = product.to_relevance_input()

    assert behavior["sku_id"] == relevance["skuId"] == "SKU-1"
    assert behavior["spu_id"] == relevance["spuId"] == "SPU-1"


def test_s0_collection_records_adapter_error_instead_of_misreporting_mixed_failure():
    store = _Store()
    collector = S0Collector(search=_Search([{"productName": "缺少ID"}]), store=store)

    try:
        collector.collect(QuerySpec("q-1", "atlas", "连衣裙"))
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "S0_PRODUCT_ID_MISSING"
    else:
        raise AssertionError("missing product ID must fail S0 collection")

    assert store.failures == [
        (
            "atlas",
            "连衣裙",
            "S0_PRODUCT_ID_MISSING",
            "Mixed result at rank 1 has no product identifier",
        )
    ]


def test_summit_adapter_accepts_existing_enriched_search_shape_without_mutating_it():
    raw = {
        "brand": "summit",
        "productId": "MC001;MC002",
        "skuId": "MC001;MC002",
        "spuId": "MC001;MC002",
        "productName": "第一款式",
        "mainImage": "https://cdn.example.com/first.jpg",
        "price": 100,
        "summit_models_details": [
            {"modelCode": "MC001", "webLabel": "第一款式", "colorLabel": "黑色"},
            {"modelCode": "MC002", "webLabel": "第二款式", "colorLabel": "白色"},
        ],
    }
    before = {
        **raw,
        "summit_models_details": [dict(item) for item in raw["summit_models_details"]],
    }

    product = ProductAdapterRegistry().adapt("SUMMIT", raw, rank=1)

    assert product.product_id == "MC001;MC002"
    assert [item.product_id for item in product.variants] == ["MC001", "MC002"]
    assert raw == before


@dataclass
class _Search:
    products: list[dict]

    def mixed(self, brand: str, query: str) -> list[dict]:
        return self.products


@dataclass
class _Store:
    snapshots: dict[tuple[str, str], dict] = field(default_factory=dict)
    failures: list[tuple[str, str, str, str]] = field(default_factory=list)

    def get_s0(self, brand: str, query: str):
        return self.snapshots.get((brand, query))

    def save_s0_if_absent(self, brand: str, query: str, snapshot: dict):
        return self.snapshots.setdefault((brand, query), snapshot)

    def save_s0_failure(self, brand, query, error_code, error_message):
        self.failures.append((brand, query, error_code, error_message))

    def save_step(self, *args):
        raise AssertionError("S0 collection must not save governance steps")


def test_s0_collector_uses_brand_adapter_and_persists_only_minimal_fields():
    collector = S0Collector(
        search=_Search(
            [
                {
                    "productId": "P-1",
                    "skuId": "SKU-1",
                    "productName": "测试商品",
                    "mainImage": "https://cdn.example.com/p.jpg",
                    "price": 88,
                    "tagName": "不得持久化",
                    "description": "不得持久化",
                }
            ]
        ),
        store=_Store(),
    )

    snapshot = collector.collect(QuerySpec("q-1", "atlas", "连衣裙"))

    assert snapshot["products"] == [
        {
            "product_id": "P-1",
            "sku_id": "SKU-1",
            "rank": 1,
            "product_name": "测试商品",
            "image_url": "https://cdn.example.com/p.jpg",
            "original_price": 88.0,
            "discount_price": 88.0,
        }
    ]
    assert snapshot["result_count"] == 1
