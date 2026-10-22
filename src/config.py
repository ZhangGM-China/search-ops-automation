import errno
import json
import os
import ssl
import threading
from copy import deepcopy
from io import StringIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from ruamel.yaml import YAML
from .top_query.constants import DEFAULT_TOP_QUERY_CHECK_MODES, DEFAULT_TOP_QUERY_TOOL_MODES

load_dotenv(override=False)

# 获取项目根目录 (config.yaml 所在位置)
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
CONFIG_EXAMPLE_PATH = BASE_DIR / "config.example.yaml"
_CONFIG_LOCK = threading.RLock()
_KUBERNETES_CONFIG_PERSISTENCE = "kubernetes_configmap"
_FILE_CONFIG_PERSISTENCE = "file"
_CONFIG_PERSISTENCE_MODES = {
    _FILE_CONFIG_PERSISTENCE,
    _KUBERNETES_CONFIG_PERSISTENCE,
}


class ConfigPersistenceError(RuntimeError):
    """Raised when the selected runtime configuration store is unavailable."""


def _build_yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _config_persistence_mode() -> str:
    mode = str(os.getenv("AIOPS_CONFIG_PERSISTENCE") or _FILE_CONFIG_PERSISTENCE).strip().lower()
    if mode not in _CONFIG_PERSISTENCE_MODES:
        raise ConfigPersistenceError("runtime config persistence mode is invalid")
    return mode


def _kubernetes_configmap_name() -> str:
    name = str(os.getenv("AIOPS_CONFIGMAP_NAME") or "aiops-runtime-config").strip()
    if not name or any(character.isspace() for character in name) or "/" in name:
        raise ConfigPersistenceError("runtime config ConfigMap name is invalid")
    return name


def _kubernetes_configmap_namespace() -> str:
    namespace = str(os.getenv("AIOPS_CONFIGMAP_NAMESPACE") or "aiops-demo").strip()
    if not namespace or any(character.isspace() for character in namespace) or "/" in namespace:
        raise ConfigPersistenceError("runtime config ConfigMap namespace is invalid")
    return namespace


def _kubernetes_serviceaccount_token() -> str:
    path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigPersistenceError("runtime config Kubernetes credentials are unavailable") from exc
    if not value:
        raise ConfigPersistenceError("runtime config Kubernetes credentials are unavailable")
    return value


def _kubernetes_serviceaccount_ca() -> str:
    path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    try:
        node = path.stat()
        if not path.is_file() or node.st_size <= 0 or node.st_size > 1024 * 1024:
            raise OSError("invalid Kubernetes service-account CA")
    except OSError as exc:
        raise ConfigPersistenceError(
            "runtime config Kubernetes CA is unavailable"
        ) from exc
    return str(path)


def _kubernetes_configmap_request(*, method: str, payload=None):
    """Read or patch the dedicated ConfigMap through in-cluster Kubernetes auth."""
    if method not in {"GET", "PATCH"}:
        raise ConfigPersistenceError("runtime config Kubernetes method is invalid")
    api_host = str(os.getenv("KUBERNETES_SERVICE_HOST") or "").strip()
    api_port = str(os.getenv("KUBERNETES_SERVICE_PORT_HTTPS") or "443").strip()
    if not api_host or not api_port.isdigit():
        raise ConfigPersistenceError("runtime config Kubernetes API is unavailable")
    url = (
        f"https://{api_host}:{api_port}/api/v1/namespaces/"
        f"{_kubernetes_configmap_namespace()}/configmaps/{_kubernetes_configmap_name()}"
    )
    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {_kubernetes_serviceaccount_token()}",
    }
    if method == "PATCH":
        try:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ConfigPersistenceError("runtime config Kubernetes payload is invalid") from exc
        headers["Content-Type"] = "application/merge-patch+json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        context = ssl.create_default_context(cafile=_kubernetes_serviceaccount_ca())
        with urlopen(request, timeout=5, context=context) as response:
            raw = response.read(1024 * 1024 + 1)
    except (HTTPError, URLError, OSError) as exc:
        raise ConfigPersistenceError("runtime config Kubernetes request failed") from exc
    if not raw or len(raw) > 1024 * 1024:
        raise ConfigPersistenceError("runtime config Kubernetes response is invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigPersistenceError("runtime config Kubernetes response is invalid") from exc
    if not isinstance(value, dict):
        raise ConfigPersistenceError("runtime config Kubernetes response is invalid")
    return value


def _load_runtime_config_from_kubernetes() -> dict:
    response = _kubernetes_configmap_request(method="GET")
    data = response.get("data")
    raw_config = data.get("config.yaml") if isinstance(data, dict) else None
    if not isinstance(raw_config, str) or not raw_config.strip():
        raise ConfigPersistenceError("runtime config ConfigMap data is invalid")
    try:
        loaded = _build_yaml().load(StringIO(raw_config))
    except Exception as exc:
        raise ConfigPersistenceError("runtime config YAML is invalid") from exc
    if not isinstance(loaded, dict):
        raise ConfigPersistenceError("runtime config YAML must be a mapping")
    return loaded


def load_config():
    """从 yaml 文件加载配置 (保留注释)"""
    if _config_persistence_mode() == _KUBERNETES_CONFIG_PERSISTENCE:
        return _load_runtime_config_from_kubernetes()
    config_path = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件未找到: {CONFIG_PATH} 或 {CONFIG_EXAMPLE_PATH}")

    with open(config_path, 'r', encoding='utf-8') as f:
        return _build_yaml().load(f)

# 加载原始配置数据
_raw_config = load_config()

# 为了保持向下兼容，提取出原有全局变量名。GLOBAL_BUSINESS_CONFIG 只保留系统级调度项，
# 业务配置不得再从这里回退。
# 注意：这里需要确保返回的是字典以便修改内容
GLOBAL_SAFETY_CONFIG = dict(_raw_config.get("safety_config", {}))
GLOBAL_BUSINESS_CONFIG = dict(_raw_config.get("system_config", {}))
DEFAULT_BRAND_BUSINESS_CONFIG = {
    "enabled": False,
    "api_alias": None,
    "no_result_governance_enabled": False,
    "no_result_execution_mode": "dry_run",
    "no_result_extract_limit": 200,
    "no_result_search_pv_threshold": 5,
    "no_result_search_uv_threshold": 0,
    "no_result_excluded_keywords": ["积分", "兑换", "抽奖", "门店", "优惠券", "客服", "订单", "会员"],
    "no_result_auto_max_risk": "medium",
    "no_result_retag_cooldown_hours": 24,
    "no_result_review_wait_minutes": 30,
    "no_result_top_n": 16,
    "no_result_agent_max_rounds": 4,
    "no_result_ignore_ttl_days": 14,
    "top_query_governance_enabled": True,
    "query_governance_extract_limit": 150,
    "query_governance_query_concurrency": 10,
    "query_governance_query_min_metric_type": "uv",
    "query_governance_query_min_metric_value": 5,
    "query_governance_excluded_keywords": [],
    "query_governance_candidate_high_pv_limit": 100,
    "query_governance_candidate_alert_ctr_gap_pp": 5,
    "query_governance_candidate_alert_cvr_gap_pp": 2,
    "query_governance_candidate_history_days": 7,
    "query_governance_candidate_new_query_pv_threshold": 5,
    "query_governance_candidate_pv_growth_ratio_floor": 1.2,
    "query_governance_candidate_pv_growth_sqrt_multiplier": 2,
    "query_governance_candidate_pv_growth_abs_floor": 5,
    "query_governance_preference_top_purchase_product_limit": 8,
    "query_governance_preference_top_click_product_limit": 8,
    "query_governance_preference_generation_batch_query_limit": 10,
    "query_governance_preference_generation_concurrency": 4,
    "query_governance_preference_generation_timeout_seconds": 1200,
    "query_governance_kg_agent_timeout_seconds": 1200,
    "query_governance_kg_reference": """案例：Query“花衬衫”召回错误商品时，先查询“花衬衫”和“印花衬衫”是否已有标签。若“印花衬衫”是已有根标签，而“花衬衫”只是同一商品实体的用户说法，应新增“花衬衫”非根标签，并建立“花衬衫”→“印花衬衫”的同义关系；不要把它建立为从属关系，也不要重复创建“印花衬衫”根标签。所有名称、ID、维度和关系方向都必须以当前查询结果为准。""",
    "query_governance_kg_reference_updated_by": "",
    "query_governance_kg_reference_updated_at": "",
    "query_governance_reorder_enabled": True,
    "query_governance_vector_reorder_concurrency": 10,
    "query_governance_vector_reorder_retry_attempts": 3,
    "query_governance_vector_reorder_retry_backoff_seconds": 0.5,
    "query_governance_relevance_enabled": True,
    "query_governance_relevance_multimodal_concurrency": 5,
    "query_governance_relevance_batch_size": 240,
    "query_governance_relevance_model_timeout_seconds": 1200,
    "query_governance_relevance_image_download_timeout_seconds": 30,
    "query_governance_relevance_blacklist_ratio_threshold": 0.5,
    "query_governance_relevance_max_rounds": 3,
    "query_governance_relevance_buckets": [
        "productIdsByCode",
        "productIdsByProductNameTag",
        "productIdsByCodeSpellCorrections",
        "productIdsByTagsAll",
        "productIdsByWhiteList",
        "productIdsBySubTag",
    ],
    "query_governance_recall_rate_enabled": False,
    "query_governance_recall_rate_product_batch_size": 200,
    "query_governance_recall_rate_score_threshold": 0.0,
    "query_governance_recall_rate_jev_threshold": 0.7,
    "top_query_check_product_limit": 16,
    "top_query_low_result_threshold": 5,
    "top_query_result_drop_threshold": 0.3,
    "top_query_inventory_broken_rate_threshold": 25,
    "top_query_inventory_check_interval_minutes": 60,
    "top_query_inventory_max_cycles": 3,
    "top_query_token_limit": 50000,
    "top_query_active_duration_seconds": 1800,
    "top_query_round_action_limit": 10,
    "top_query_fingerprint_window": 5,
    "top_query_brand_day_token_limit": 500000,
    "top_query_brand_day_action_limit": 1000,
    "top_query_batch_token_limit": 250000,
    "top_query_batch_action_limit": 500,
    "top_query_llm_reserve_tokens": 50000,
    "top_query_max_consecutive_tool_failures": 3,
    "top_query_max_consecutive_no_improvement": 3,
    "top_query_workflow_lease_seconds": 300,
    "top_query_world_knowledge_config": {
        "weather_cities": [
            {"city": "上海", "city_code": 101020100},
            {"city": "杭州", "city_code": 101210101},
            {"city": "南京", "city_code": 101190101},
            {"city": "苏州", "city_code": 101190401},
        ],
        "social_trends": [],
        "tag_rules": [
            {"factor": "summer", "query_keywords": ["连衣裙", "T恤", "短裤"], "tags": ["无袖", "短袖", "轻薄"], "afterBucket": "top"},
            {"factor": "rain", "query_keywords": ["外套", "鞋", "雨衣"], "tags": ["防雨", "通勤", "轻便"], "afterBucket": "top"},
            {"factor": "social_trend", "query_keywords": [], "tags": [], "afterBucket": "top"},
        ],
        "default_filters": [],
        "default_after_bucket": "top",
    },
    "top_query_check_modes": dict(DEFAULT_TOP_QUERY_CHECK_MODES),
    "top_query_tool_modes": dict(DEFAULT_TOP_QUERY_TOOL_MODES),
}
BRAND_CONFIGS = dict(_raw_config.get("brand_configs", {}))
SEARCH_RULES = dict(_raw_config.get("search_rules", {}))

BRAND_OVERRIDABLE_BUSINESS_KEYS = {
    "enabled",
    "api_alias",
    "no_result_governance_enabled",
    "no_result_execution_mode",
    "no_result_extract_limit",
    "no_result_search_pv_threshold",
    "no_result_search_uv_threshold",
    "no_result_excluded_keywords",
    "no_result_auto_max_risk",
    "no_result_retag_cooldown_hours",
    "no_result_review_wait_minutes",
    "no_result_top_n",
    "no_result_agent_max_rounds",
    "no_result_ignore_ttl_days",
    "top_query_governance_enabled",
    "query_governance_extract_limit",
    "query_governance_query_concurrency",
    "query_governance_query_min_metric_type",
    "query_governance_query_min_metric_value",
    "query_governance_excluded_keywords",
    "query_governance_candidate_high_pv_limit",
    "query_governance_candidate_alert_ctr_gap_pp",
    "query_governance_candidate_alert_cvr_gap_pp",
    "query_governance_candidate_history_days",
    "query_governance_candidate_new_query_pv_threshold",
    "query_governance_candidate_pv_growth_ratio_floor",
    "query_governance_candidate_pv_growth_sqrt_multiplier",
    "query_governance_candidate_pv_growth_abs_floor",
    "query_governance_preference_top_purchase_product_limit",
    "query_governance_preference_top_click_product_limit",
    "query_governance_preference_generation_batch_query_limit",
    "query_governance_preference_generation_concurrency",
    "query_governance_preference_generation_timeout_seconds",
    "query_governance_kg_agent_timeout_seconds",
    "query_governance_kg_reference",
    "query_governance_kg_reference_updated_by",
    "query_governance_kg_reference_updated_at",
    "query_governance_reorder_enabled",
    "query_governance_vector_reorder_concurrency",
    "query_governance_vector_reorder_retry_attempts",
    "query_governance_vector_reorder_retry_backoff_seconds",
    "query_governance_relevance_enabled",
    "query_governance_relevance_multimodal_concurrency",
    "query_governance_relevance_batch_size",
    "query_governance_relevance_model_timeout_seconds",
    "query_governance_relevance_image_download_timeout_seconds",
    "query_governance_relevance_blacklist_ratio_threshold",
    "query_governance_relevance_max_rounds",
    "query_governance_relevance_buckets",
    "query_governance_recall_rate_enabled",
    "query_governance_recall_rate_product_batch_size",
    "query_governance_recall_rate_score_threshold",
    "query_governance_recall_rate_jev_threshold",
    "top_query_check_product_limit",
    "top_query_low_result_threshold",
    "top_query_result_drop_threshold",
    "top_query_inventory_broken_rate_threshold",
    "top_query_inventory_check_interval_minutes",
    "top_query_inventory_max_cycles",
    "top_query_token_limit",
    "top_query_active_duration_seconds",
    "top_query_round_action_limit",
    "top_query_fingerprint_window",
    "top_query_brand_day_token_limit",
    "top_query_brand_day_action_limit",
    "top_query_batch_token_limit",
    "top_query_batch_action_limit",
    "top_query_llm_reserve_tokens",
    "top_query_max_consecutive_tool_failures",
    "top_query_max_consecutive_no_improvement",
    "top_query_workflow_lease_seconds",
    "top_query_world_knowledge_config",
    "top_query_check_modes",
    "top_query_tool_modes",
}

def _apply_config(new_config) -> None:
    if not isinstance(new_config, dict):
        raise ConfigPersistenceError("runtime config YAML must be a mapping")
    _raw_config.clear()
    _raw_config.update(new_config)
    GLOBAL_SAFETY_CONFIG.clear()
    GLOBAL_SAFETY_CONFIG.update(dict(_raw_config.get("safety_config", {})))
    GLOBAL_BUSINESS_CONFIG.clear()
    GLOBAL_BUSINESS_CONFIG.update(dict(_raw_config.get("system_config", {})))
    BRAND_CONFIGS.clear()
    BRAND_CONFIGS.update(dict(_raw_config.get("brand_configs", {})))
    SEARCH_RULES.clear()
    SEARCH_RULES.update(dict(_raw_config.get("search_rules", {})))


def reload_config():
    """重新加载配置文件并更新全局字典内容"""
    with _CONFIG_LOCK:
        print("正在热加载配置文件 (ruamel.yaml)...")
        new_config = load_config()
        
        _apply_config(new_config)
        print("配置文件热加载成功。")

def save_config(config_data):
    """将配置数据写回 yaml 文件 (保留注释)"""
    if not isinstance(config_data, dict):
        raise ConfigPersistenceError("runtime config YAML must be a mapping")
    if _config_persistence_mode() == _KUBERNETES_CONFIG_PERSISTENCE:
        stream = StringIO()
        _build_yaml().dump(config_data, stream)
        _kubernetes_configmap_request(
            method="PATCH",
            payload={"data": {"config.yaml": stream.getvalue()}},
        )
        _apply_config(config_data)
        return
    temp_path = CONFIG_PATH.with_suffix(f"{CONFIG_PATH.suffix}.tmp")
    with _CONFIG_LOCK:
        with open(temp_path, 'w', encoding='utf-8') as f:
            _build_yaml().dump(config_data, f)
        try:
            os.replace(temp_path, CONFIG_PATH)
        except OSError as exc:
            if exc.errno not in {errno.EBUSY, errno.EXDEV}:
                raise
            # Docker bind mount 下原子替换可能报 EBUSY/EXDEV，退回到原路径覆盖写。
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                _build_yaml().dump(config_data, f)
            if temp_path.exists():
                temp_path.unlink()
        reload_config()

def get_env() -> str:
    """
    获取当前运行环境。
    生产环境必须通过环境变量 ENV=prod 显式启用；默认一律 test。
    """
    env = (os.getenv("ENV") or "test").strip().lower()
    if not env:
        env = "test"
    if env not in {"prod", "test"}:
        raise ValueError("ENV must be one of: prod, test")
    return env

def get_brand_config(brand: str) -> dict:
    """获取指定品牌的配置，不存在则返回默认(atlas)配置"""
    return BRAND_CONFIGS.get(brand, BRAND_CONFIGS.get("atlas", {}))

def get_brand_business_config(brand: str) -> dict:
    """获取品牌业务配置。业务配置只认品牌配置，缺失字段使用代码级兜底。"""
    merged = deepcopy(DEFAULT_BRAND_BUSINESS_CONFIG)
    brand_config = get_brand_config(brand)
    for key in BRAND_OVERRIDABLE_BUSINESS_KEYS:
        if key in brand_config and brand_config.get(key) is not None:
            brand_value = brand_config.get(key)
            if isinstance(merged.get(key), dict) and isinstance(brand_value, dict):
                merged[key] = {**merged[key], **deepcopy(brand_value)}
            else:
                merged[key] = deepcopy(brand_value)
    tool_modes = merged.get("top_query_tool_modes")
    if isinstance(tool_modes, dict):
        # ``exact_query_ranking`` belonged to the retired Planner surface;
        # the current mainline action is ``query_reorder``.
        tool_modes.pop("exact_query_ranking", None)
    world_knowledge_config = merged.get("top_query_world_knowledge_config")
    default_world_knowledge_config = DEFAULT_BRAND_BUSINESS_CONFIG.get("top_query_world_knowledge_config")
    if isinstance(world_knowledge_config, dict) and isinstance(default_world_knowledge_config, dict):
        if not world_knowledge_config.get("tag_rules") and default_world_knowledge_config.get("tag_rules"):
            world_knowledge_config["tag_rules"] = deepcopy(default_world_knowledge_config["tag_rules"])
    return merged

def supports_broken_rate_monitoring(brand: str) -> bool:
    """当前支持断码率抓取的品牌。"""
    return brand in {"atlas", "harbor", "riviera"}

def is_brand_enabled(brand: str) -> bool:
    """检查品牌是否启用了自动化运维"""
    config = BRAND_CONFIGS.get(brand)
    if not config:
        return False
    return config.get("enabled", False)
