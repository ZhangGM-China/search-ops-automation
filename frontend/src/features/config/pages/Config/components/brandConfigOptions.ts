export interface ConfigFieldOption {
  key: string;
  label: string;
  tip: string;
  min?: number;
  max?: number;
  step?: number;
}

export const executionModeOptions = [
  { value: 'dry_run', label: '确认后执行' },
  { value: 'real_write', label: '自动执行' },
];

export const topQueryModeOptions = [
  { value: 'disabled', label: '禁止执行' },
  { value: 'human_approval', label: '确认后执行' },
  { value: 'auto', label: '自动执行' },
];

export const topQueryChecks = [
  { key: 'recall', label: '召回数量检测', tip: '检查 Query 是否无召回、召回偏少或召回数量骤降。' },
  { key: 'relevance', label: '相关性检测', tip: '检查召回商品与 Query 的图文相关性。' },
  { key: 'inventory', label: '断码检测', tip: '检查召回商品是否因库存不足导致可售商品变少。' },
];

export const topQueryTools = [
  { key: 'product_blacklist', label: '商品拉黑', tip: '将确认不应出现在该 Query 结果中的商品加入黑名单。' },
  { key: 'query_demote', label: '商品降桶', tip: '降低指定商品在该 Query 结果中的排序优先级，不会删除商品。' },
  { key: 'tag_bucket_config', label: '标签权重调整', tip: '调整标签对应的排序桶或权重，影响后续召回与排序。' },
  { key: 'kg_tag', label: '知识图谱调整', tip: '统一管理知识图谱标签及标签关系的新增、修改和删除。' },
  { key: 'query_reorder', label: 'Query 精确排序', tip: '通过新版 Query 排序执行链路调整商品顺序，动作会经过校验、审计和回读确认。' },
  { key: 'whitelist_tag', label: '白名单标签补召回', tip: 'AI 召回率检测发现漏召回商品时，为商品补打 whiteListTag 标签并触发重打标签。' },
  { key: 'query_whitelist', label: 'Query 白名单补召回', tip: 'AI 召回率检测发现漏召回商品时，将商品加入该 Query 的白名单，保证后续可被召回。' },
];

export const recallRateRuntimeFields: ConfigFieldOption[] = [
  { key: 'query_governance_recall_rate_product_batch_size', label: 'LLM 商品目录批量大小', tip: 'AI 召回率检测时每次提交给大模型的商品目录数量，默认 200；目录会按该数量分批扫描。', min: 1, max: 1000, step: 1 },
  { key: 'query_governance_recall_rate_score_threshold', label: 'LLM 召回分数阈值', tip: '大模型给出的商品召回分数低于该值时不计入召回结果，范围 0-100，默认 0。', min: 0, max: 100, step: 1 },
  { key: 'query_governance_recall_rate_jev_threshold', label: 'JEV 标签判定阈值', tip: 'JEV 判定知识图谱分词标签是否适合打到商品上的置信度阈值，范围 0-1，默认 0.7。', min: 0, max: 1, step: 0.05 },
] as const;

export const kgAgentRuntimeFields: ConfigFieldOption[] = [
  { key: 'query_governance_kg_agent_timeout_seconds', label: '知识图谱 Agent 超时（秒）', tip: '知识图谱 Agent 单次请求最多等待的时间。默认 1200 秒；例如设置为 600 表示最多等待 10 分钟。', min: 1, max: 3600, step: 1 },
];

export const querySelectionFields: ConfigFieldOption[] = [
  { key: 'query_governance_query_concurrency', label: '候选 Query 并发处理数', tip: '取词节点同时处理候选 Query 的数量，只影响候选计算，不代表重排序或相关性节点的并发数。例：设置为 10 时，最多同时处理 10 个候选 Query。', min: 1, max: 64, step: 1 },
  { key: 'query_governance_candidate_high_pv_limit', label: '高访问量 Query 数量', tip: '从候选 Query 中按目标日 PV 选出前 N 个，作为“高访问量”治理规则的输入。', min: 0, max: 5000, step: 1 },
  { key: 'query_governance_candidate_alert_ctr_gap_pp', label: 'CTR 下降告警百分点', tip: '当前 CTR 比历史基线低达到该百分点时，标记为 CTR 下降候选。例：设置 5，代表下降 5 个百分点触发。', min: 0, max: 100, step: 0.1 },
  { key: 'query_governance_candidate_alert_cvr_gap_pp', label: 'CVR 下降告警百分点', tip: '当前 CVR 比历史基线低达到该百分点时，标记为 CVR 下降候选。', min: 0, max: 100, step: 0.1 },
  { key: 'query_governance_candidate_history_days', label: '历史对比窗口（天）', tip: '用最近多少天的行为数据计算历史基线，用于 CTR、CVR 和访问量变化判断。', min: 1, max: 365, step: 1 },
  { key: 'query_governance_candidate_new_query_pv_threshold', label: '新 Query 最低访问量', tip: '新出现的 Query 当日 PV 达到该值，才会进入新词候选，避免低流量噪声进入治理。', min: 0, max: 1000000, step: 1 },
] as const;

export const queryBehaviorEvidenceFields: ConfigFieldOption[] = [
  { key: 'query_governance_preference_top_purchase_product_limit', label: '高购买商品数', tip: '为偏好生成提供购买行为最高的商品数量。例：8 表示最多取 8 个商品作为购买偏好证据。', min: 1, max: 5000, step: 1 },
  { key: 'query_governance_preference_top_click_product_limit', label: '高点击商品数', tip: '为偏好生成提供点击行为最高的商品数量。', min: 1, max: 5000, step: 1 },
  { key: 'query_governance_preference_generation_batch_query_limit', label: '偏好生成单批 Query 数', tip: '一次偏好生成任务最多提交的 Query 数量，用于控制批次大小。', min: 1, max: 500, step: 1 },
  { key: 'query_governance_preference_generation_concurrency', label: '偏好生成并发数', tip: '偏好生成 Agent 同时处理的 Query 数量，数值越大速度越快，但模型和网络压力也越高。', min: 1, max: 32, step: 1 },
  { key: 'query_governance_preference_generation_timeout_seconds', label: '偏好生成超时（秒）', tip: '偏好生成 Agent 单次请求最多等待的时间，默认 1200 秒。', min: 1, max: 3600, step: 1 },
] as const;

export const vectorReorderRuntimeFields: ConfigFieldOption[] = [
  { key: 'query_governance_vector_reorder_concurrency', label: '向量重排并发数', tip: '重排序节点同时请求向量服务的 Query 数量。', min: 1, max: 32, step: 1 },
  { key: 'query_governance_vector_reorder_retry_attempts', label: '向量重排最大尝试次数', tip: '向量接口出现超时、连接异常、429/5xx 或空结果时的最大请求次数，默认 3 次。', min: 1, max: 5, step: 1 },
  { key: 'query_governance_vector_reorder_retry_backoff_seconds', label: '向量重排重试退避（秒）', tip: '重试之间的基础等待时间，实际按指数退避增加，默认 0.5 秒。', min: 0, max: 5, step: 0.1 },
] as const;

export const relevanceRuntimeFields: ConfigFieldOption[] = [
  { key: 'query_governance_relevance_multimodal_concurrency', label: '相关性 AI 并发数', tip: '相关性 AI 同时检查商品图文与 Query 的任务数。数值越大吞吐越高，也会增加模型服务压力。', min: 1, max: 32, step: 1 },
  { key: 'query_governance_relevance_batch_size', label: '相关性单批商品数', tip: '一次相关性检测请求中最多携带的商品数量。', min: 1, max: 1000, step: 1 },
  { key: 'query_governance_relevance_image_download_timeout_seconds', label: '商品图片下载超时（秒）', tip: '检测前下载商品图片的最长等待时间，超时会按图片不可用处理。', min: 1, max: 300, step: 1 },
  { key: 'query_governance_relevance_model_timeout_seconds', label: '相关性 AI 超时（秒）', tip: '相关性 AI 单次请求最多等待的时间，默认 1200 秒。', min: 1, max: 3600, step: 1 },
  { key: 'query_governance_relevance_blacklist_ratio_threshold', label: '相关性拉黑比例阈值', tip: '单个 Query 的不相关商品占比达到该值时，才进入拉黑治理判断。', min: 0, max: 1, step: 0.05 },
  { key: 'query_governance_relevance_max_rounds', label: '相关性最多复检轮数', tip: '相关性治理失败或结果不稳定时最多重试的轮数。', min: 1, max: 10, step: 1 },
] as const;

export const relevanceBucketOptions = [
  { value: 'top', label: 'L0-1 置顶桶' },
  { value: 'productIdsByAgent', label: 'L0-2 Agent 选品' },
  { value: 'productIdsByCode', label: 'L1-1 编码完全召回' },
  { value: 'productIdsByProductNameTag', label: 'L1-2 商品名召回' },
  { value: 'productIdsByCodeSpellCorrections', label: 'L2-1 编码纠错召回' },
  { value: 'productIdsByTagsAll', label: 'L2-2 商品标签召回' },
  { value: 'productIdsByWhiteList', label: 'L2-3 白名单/主分类机器召回' },
  { value: 'productIdsBySubTag', label: 'L2-4 二次分词召回' },
  { value: 'productIdsByHighLowGroup', label: 'L3-1 高低组召回' },
  { value: 'productIdsByMultiCategory', label: 'L3-2 多分类召回' },
  { value: 'productIdsByTagNameDowngrade', label: 'L3-3 标签名降权召回' },
  { value: 'productIdsByOpsLowWeight', label: 'L3-4 运营低权重桶' },
  { value: 'productIdsByLowWeight', label: 'L3-5 低权重桶' },
  { value: 'productIdsByFuzzy', label: 'L3-6 模糊召回' },
  { value: 'productIdsByProductNameFuzzy', label: 'L3-7 商品名模糊召回' },
  { value: 'productIdsByTagFuzzy', label: 'L3-8 标签模糊召回' },
  { value: 'productIdsByFuzzySpellCorrections', label: 'L3-9 模糊纠错召回' },
  { value: 'Dynamic', label: '动态召回' },
  { value: 'productIdsByNoStock', label: 'L4 无库存桶' },
] as const;

export const defaultRelevanceBuckets = [
  'productIdsByCode',
  'productIdsByProductNameTag',
  'productIdsByCodeSpellCorrections',
  'productIdsByTagsAll',
  'productIdsByWhiteList',
  'productIdsBySubTag',
];

export const basicFields: ConfigFieldOption[] = [
  { key: 'query_governance_extract_limit', label: '每日最多治理 Query 数', tip: '每天最多进入治理主流程的 Query 数量，按取词结果优先级截取。', min: 1, max: 5000, step: 1 },
  { key: 'top_query_check_product_limit', label: '每个 Query 最多检查商品数', tip: '每个 Query 最多检查前 N 个召回商品，控制单个 Query 的治理成本。', min: 1, max: 100, step: 1 },
];

export const recallFields: ConfigFieldOption[] = [
  { key: 'top_query_low_result_threshold', label: '少结果判定阈值', tip: '召回商品数小于等于该值时，判定为无结果或少结果。', min: 0, max: 100, step: 1 },
  { key: 'top_query_result_drop_threshold', label: '召回数量下降比例', tip: '本次召回数量相对历史基线下降达到该比例时触发治理。例：0.3 表示下降 30%。', min: 0, max: 1, step: 0.1 },
];

export const inventoryFields: ConfigFieldOption[] = [
  { key: 'top_query_inventory_check_interval_minutes', label: '断码检查间隔（分钟）', tip: '两次断码监测之间的时间间隔。', min: 1, max: 1440, step: 1 },
  { key: 'top_query_inventory_max_cycles', label: '断码补位最多轮数', tip: '断码治理尝试补充可售商品的最多轮数；每轮都会重新检查库存和召回结果。', min: 1, max: 100, step: 1 },
  { key: 'top_query_inventory_broken_rate_threshold', label: '断码率告警阈值（%）', tip: '可售库存不足商品占比达到该百分比时触发断码治理。', min: 1, max: 100, step: 1 },
];

export const noResultFields: ConfigFieldOption[] = [
  { key: 'no_result_extract_limit', label: '无结果词拉取数量', tip: '每天从数据源拉取并进入无结果词治理的 Query 数量上限。', min: 1, max: 500, step: 1 },
  { key: 'no_result_search_pv_threshold', label: '无结果词最低 PV', tip: '无结果词至少达到该 PV 才进入治理，过滤低流量噪声。', min: 0, max: 10000, step: 1 },
  { key: 'no_result_search_uv_threshold', label: '无结果词最低 UV', tip: '无结果词至少达到该 UV 才进入治理，默认按用户数过滤低流量 Query。', min: 0, max: 10000, step: 1 },
  { key: 'no_result_retag_cooldown_hours', label: '重新打标签间隔（小时）', tip: '同一品牌再次执行标签调整前需要等待的最短时间。', min: 1, max: 168, step: 1 },
  { key: 'no_result_review_wait_minutes', label: '二次复查等待（分钟）', tip: '标签调整后等待多久再复查召回结果。', min: 1, max: 240, step: 1 },
];
