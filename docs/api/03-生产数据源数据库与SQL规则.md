# 生产数据源数据库与 SQL 规则

## 1. 核心业务指标

- CTR = `点击用户数 / 搜索用户数`
- CVR = `购买用户数 / 搜索用户数`
- Top Query PV 覆盖率 = `被监测 Top Query 的 PV 之和 / 品牌昨日搜索 PV`

基准日期：

- 使用当前日期的前一天作为待分析数据日
- 日期必须在 Python 层计算为明确日期字符串后传入 SQL，不直接依赖数据库 `CURRENT_DATE`

新版 Top Query 治理入口：

- 每日 00:30 抽取各启用品牌昨日 Top Query。
- 抽取结果不再只包含 CTR / CVR 下跌 Query，而是覆盖品牌配置的 Top N。
- 每个 Query 至少需要返回：`query`、`rank`、`query_search_uv`、`query_search_pv`、`query_search_click_uv`、`query_search_pay_uv`、`ctr`、`cvr`。
- 同时需要按品牌汇总昨日 `uv`、`pv`、`ctr`、`cvr`，用于品牌总览和 Top Query PV 覆盖率计算。
- 旧异常判定标准保留为 PlannerAgent 的输入信号，不再作为抽取入口。

旧异常判定标准：

- Atlas / Meadow / Summit：对比过去 7 天均值，CTR 绝对下降超过 10 个百分点，或 CVR 绝对下降超过 3 个百分点
- HARBOR：对比过去 7 天均值，CTR 绝对下降超过 5 个百分点，或 CVR 绝对下降超过 2 个百分点
- Riviera：对比过去 3 天均值，CTR 或 CVR 相对下降超过 10%

## 2. 数据库连接信息

| 适用品牌 | Host | Port | User | Database | Database |
| --- | --- | --- | --- | --- | --- |
| atlas, meadow, summit, HARBOR | `db.example.test` | 3306 | `atlas_returning_readonly` | `search-ops-admin` | <from-controlled-secret-provider> |
| Riviera | `db.example.test` | 26462 | `memor` | `data` | <from-controlled-secret-provider> |

## 3. 各品牌 TOP N Query 查询 SQL

### Atlas

```sql
#---------------- Atlas筛选昨日ctr/cvr低于前7天平均的值------------------
#-------------------- CTR（绝对差值下降10个百分点）--------------------

-- 1. 先找出 t-1 日的 top150 词（带UV和CTR）
with top150 as (
  select
    `query`,
    sum(query_search_uv) as search_uv,
    sum(query_search_click_uv) * 1.0 / nullif(sum(query_search_uv), 0) as ctr
  from dwd_query_behavior_daily
  where `date` = CURRENT_DATE - interval 1 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
    AND `query` NOT LIKE '%特惠%'
  group by `query`
  order by sum(query_search_pv) desc
  limit 150
),

-- 2. 计算过去7天（t-2 到 t-8）每个词的平均CTR
past7 as (
  select
    `query`,
    -- 改计算方法，用区间内所有总数相除，而不是先算率再算平均
    -- avg(coalesce(query_search_click_uv * 1.0 / nullif(query_search_uv, 0), 0)) as ctr_avg
    sum(query_search_click_uv) * 1.0 / nullif(sum(query_search_uv), 0) as ctr_avg
  from dwd_query_behavior_daily
  where `date` between CURRENT_DATE - interval 8 day and CURRENT_DATE - interval 2 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
  group by `query`
  having count(distinct `date`) >= 5  -- 至少5天有数据
)

-- 3. 关联并筛选（绝对差值下降超过10个百分点）
select
  row_number() over (order by search_uv desc) as rn,
  t.`query`,
  t.search_uv,
  -- round(t.ctr * 100, 2) as ctr_today_pct,              -- 昨日CTR(%)
  -- round(p.ctr_avg * 100, 2) as ctr_avg_past7_pct,      -- 过去7天平均CTR(%)
  round((t.ctr - p.ctr_avg) * 100, 2) as ctr_gap_pct   -- 绝对差值(百分点)
from top150 t
join past7 p on t.`query` = p.`query`
where (t.ctr - p.ctr_avg) * 100 < -10  -- 绝对下降超10个百分点
order by search_uv desc;


#-------------------- CVR（绝对差值下降3个百分点）--------------------

-- 1. 先找出 t-1 日的 top150 词（带UV和CVR）
with top150 as (
  select
    `query`,
    sum(query_search_uv) as search_uv,
    sum(query_search_pay_uv) * 1.0 / nullif(sum(query_search_uv), 0) as cvr
  from dwd_query_behavior_daily
  where `date` = CURRENT_DATE - interval 1 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
  group by `query`
  order by sum(query_search_pv) desc
  limit 150
),

-- 2. 计算过去7天（t-2 到 t-8）每个词的平均CVR
past7 as (
  select
    `query`,
    -- 改计算方法，用区间内所有总数相除，而不是先算率再算平均
    -- avg(coalesce(query_search_pay_uv * 1.0 / nullif(query_search_uv, 0), 0)) as cvr_avg
    sum(query_search_pay_uv) * 1.0 / nullif(sum(query_search_uv), 0) as cvr_avg
  from dwd_query_behavior_daily
  where `date` between CURRENT_DATE - interval 8 day and CURRENT_DATE - interval 2 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
  group by `query`
  having count(distinct `date`) >= 5  -- 至少5天有数据
)

-- 3. 关联并筛选（绝对差值下降超过3个百分点）
select
  row_number() over (order by search_uv desc) as rn,
  t.`query`,
  t.search_uv,
  -- round(t.cvr * 100, 2) as cvr_today_pct,              -- 昨日CVR(%)
  -- round(p.cvr_avg * 100, 2) as cvr_avg_past7_pct,      -- 过去7天平均CVR(%)
  round((t.cvr - p.cvr_avg) * 100, 2) as cvr_gap_pct   -- 绝对差值(百分点)
from top150 t
join past7 p on t.`query` = p.`query`
where (t.cvr - p.cvr_avg) * 100 < -3  -- 绝对下降超3个百分点
order by search_uv desc;
```

### Meadow
```sql
#---------------- Meadow筛选昨日ctr/cvr低于前7天平均的值------------------
#-------------------- CTR（绝对差值下降10个百分点，筛选30+词）--------------------

-- 1. 先找出 t-1 日的 top200 词（带UV和CTR）
with top200 as (
  select
    `query`,
    sum(query_search_uv) as search_uv,
    sum(query_search_click_uv) * 1.0 / nullif(sum(query_search_uv), 0) as ctr
  from dwd_query_behavior_daily
  where `date` = CURRENT_DATE - interval 1 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
        -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
    AND `query` NOT LIKE '%特惠%'
  group by `query`
  order by sum(query_search_pv) desc
  limit 200
),

-- 2. 计算过去7天（t-2 到 t-8）每个词的加权平均CTR
past7 as (
  select
    `query`,
    -- 改计算方法，用区间内所有总数相除，而不是先算率再算平均
    sum(query_search_click_uv) * 1.0 / nullif(sum(query_search_uv), 0) as ctr_avg
  from dwd_query_behavior_daily
  where `date` between CURRENT_DATE - interval 8 day and CURRENT_DATE - interval 2 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
    AND `query` NOT LIKE '%特惠%'
  group by `query`
  having count(distinct `date`) >= 5  -- 至少5天有数据
)

-- 3. 关联并筛选（绝对差值下降超过10个百分点）
select
  row_number() over (order by search_uv desc) as rn,
  t.`query`,
  t.search_uv,
  -- round(t.ctr * 100, 2) as ctr_today_pct,              -- 昨日CTR(%)
  -- round(p.ctr_avg * 100, 2) as ctr_avg_past7_pct,      -- 过去7天平均CTR(%)
  round((t.ctr - p.ctr_avg) * 100, 2) as ctr_gap_pct   -- 绝对差值(百分点)
from top200 t
join past7 p on t.`query` = p.`query`
where (t.ctr - p.ctr_avg) * 100 < -10  -- 绝对下降超10个百分点
order by search_uv desc;


#-------------------- CVR（绝对差值下降3个百分点，筛选20+词）--------------------

-- 1. 先找出 t-1 日的 top200 词（带UV和CVR）
with top200 as (
  select
    `query`,
    sum(query_search_uv) as search_uv,
    sum(query_search_pay_uv) * 1.0 / nullif(sum(query_search_uv), 0) as cvr
  from dwd_query_behavior_daily
  where `date` = CURRENT_DATE - interval 1 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
    AND `query` NOT LIKE '%特惠%'
  group by `query`
  order by sum(query_search_pv) desc
  limit 200
),

-- 2. 计算过去7天（t-2 到 t-8）每个词的加权平均CVR
past7 as (
  select
    `query`,
    -- 改计算方法，用区间内所有总数相除，而不是先算率再算平均
    sum(query_search_pay_uv) * 1.0 / nullif(sum(query_search_uv), 0) as cvr_avg
  from dwd_query_behavior_daily
  where `date` between CURRENT_DATE - interval 8 day and CURRENT_DATE - interval 2 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
    AND `query` NOT LIKE '%特惠%'
  group by `query`
  having count(distinct `date`) >= 5  -- 至少5天有数据
)

-- 3. 关联并筛选（绝对差值下降超过3个百分点）
select
  row_number() over (order by search_uv desc) as rn,
  t.`query`,
  t.search_uv,
  -- round(t.cvr * 100, 2) as cvr_today_pct,              -- 昨日CVR(%)
  -- round(p.cvr_avg * 100, 2) as cvr_avg_past7_pct,      -- 过去7天平均CVR(%)
  round((t.cvr - p.cvr_avg) * 100, 2) as cvr_gap_pct   -- 绝对差值(百分点)
from top200 t
join past7 p on t.`query` = p.`query`
where (t.cvr - p.cvr_avg) * 100 < -3  -- 绝对下降超3个百分点
order by search_uv desc;
```

### Summit
```sql
#---------------- Summit筛选昨日ctr/cvr低于前7天平均的值------------------
#-------------------- CTR（绝对差值下降10个百分点，筛选30+词）--------------------

-- 1. 先找出 t-1 日的 top200 词（带UV和CTR）
with top200 as (
  select
    `query`,
    sum(query_search_uv) as search_uv,
    sum(query_search_click_uv) * 1.0 / nullif(sum(query_search_uv), 0) as ctr
  from dwd_summit_query_behavior_daily
  where `date` = CURRENT_DATE - interval 1 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
        -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
    AND `query` NOT LIKE '%特惠%'
  group by `query`
  order by sum(query_search_pv) desc
  limit 200
),

-- 2. 计算过去7天（t-2 到 t-8）每个词的平均CTR
past7 as (
  select
    `query`,
    avg(coalesce(query_search_click_uv * 1.0 / nullif(query_search_uv, 0), 0)) as ctr_avg
  from dwd_summit_query_behavior_daily
  where `date` between CURRENT_DATE - interval 8 day and CURRENT_DATE - interval 2 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
  group by `query`
  having count(distinct `date`) >= 5  -- 至少5天有数据
)

-- 3. 关联并筛选（绝对差值下降超过10个百分点）
select
  row_number() over (order by search_uv desc) as rn,
  t.`query`,
  t.search_uv,
  -- round(t.ctr * 100, 2) as ctr_today_pct,              -- 昨日CTR(%)
  -- round(p.ctr_avg * 100, 2) as ctr_avg_past7_pct,      -- 过去7天平均CTR(%)
  round((t.ctr - p.ctr_avg) * 100, 2) as ctr_gap_pct   -- 绝对差值(百分点)
from top200 t
join past7 p on t.`query` = p.`query`
where (t.ctr - p.ctr_avg) * 100 < -10  -- 绝对下降超10个百分点
order by search_uv desc;

#-------------------- CVR（绝对差值下降3个百分点，筛选20+词）--------------------

-- 1. 先找出 t-1 日的 top200 词（带UV和CVR）
with top200 as (
  select
    `query`,
    sum(query_search_uv) as search_uv,
    sum(query_search_pay_uv) * 1.0 / nullif(sum(query_search_uv), 0) as cvr
  from dwd_summit_query_behavior_daily
  where `date` = CURRENT_DATE - interval 1 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
  group by `query`
  order by sum(query_search_pv) desc
  limit 200
),

-- 2. 计算过去7天（t-2 到 t-8）每个词的平均CVR
past7 as (
  select
    `query`,
    avg(coalesce(query_search_pay_uv * 1.0 / nullif(query_search_uv, 0), 0)) as cvr_avg
  from dwd_summit_query_behavior_daily
  where `date` between CURRENT_DATE - interval 8 day and CURRENT_DATE - interval 2 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
  group by `query`
  having count(distinct `date`) >= 5  -- 至少5天有数据
)

-- 3. 关联并筛选（绝对差值下降超过3个百分点）
select
  row_number() over (order by search_uv desc) as rn,
  t.`query`,
  t.search_uv,
  -- round(t.cvr * 100, 2) as cvr_today_pct,              -- 昨日CVR(%)
  -- round(p.cvr_avg * 100, 2) as cvr_avg_past7_pct,      -- 过去7天平均CVR(%)
  round((t.cvr - p.cvr_avg) * 100, 2) as cvr_gap_pct   -- 绝对差值(百分点)
from top200 t
join past7 p on t.`query` = p.`query`
where (t.cvr - p.cvr_avg) * 100 < -3  -- 绝对下降超3个百分点
order by search_uv desc;
```

### HARBOR
```sql
#---------------- HARBOR筛选昨日ctr/cvr低于前7天平均的值------------------
#-------------------- CTR（绝对差值下降5个百分点）--------------------

-- 1. 先找出 t-1 日的 top100 词（带UV和CTR）
with top100 as (
  select
    `query`,
    sum(query_search_uv) as search_uv,
    sum(query_search_click_uv) * 1.0 / nullif(sum(query_search_uv), 0) as ctr
  from dwd_harbor_query_behavior_daily
  where `date` = CURRENT_DATE - interval 1 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
        -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
    AND `query` NOT LIKE '%特惠%'
  group by `query`
  order by sum(query_search_pv) desc
  limit 100
),

-- 2. 计算过去7天（t-2 到 t-8）每个词的加权平均CTR
past7 as (
  select
    `query`,
    -- 改计算方法，用区间内所有总数相除，而不是先算率再算平均
    sum(query_search_click_uv) * 1.0 / nullif(sum(query_search_uv), 0) as ctr_avg
  from dwd_harbor_query_behavior_daily
  where `date` between CURRENT_DATE - interval 8 day and CURRENT_DATE - interval 2 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
    AND `query` NOT LIKE '%特惠%'
  group by `query`
  having count(distinct `date`) >= 5  -- 至少5天有数据
)

-- 3. 关联并筛选（绝对差值下降超过5个百分点）
select
  row_number() over (order by search_uv desc) as rn,
  t.`query`,
  t.search_uv,
  -- round(t.ctr * 100, 2) as ctr_today_pct,              -- 昨日CTR(%)
  -- round(p.ctr_avg * 100, 2) as ctr_avg_past7_pct,      -- 过去7天平均CTR(%)
  round((t.ctr - p.ctr_avg) * 100, 2) as ctr_gap_pct   -- 绝对差值(百分点)
from top100 t
join past7 p on t.`query` = p.`query`
where (t.ctr - p.ctr_avg) * 100 < -5  -- 绝对下降超过5个百分点
order by search_uv desc;


#-------------------- CVR（绝对差值下降2个百分点）--------------------

-- 1. 先找出 t-1 日的 top100 词（带UV和CVR）
with top100 as (
  select
    `query`,
    sum(query_search_uv) as search_uv,
    sum(query_search_pay_uv) * 1.0 / nullif(sum(query_search_uv), 0) as cvr
  from dwd_harbor_query_behavior_daily
  where `date` = CURRENT_DATE - interval 1 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
    AND `query` NOT LIKE '%特惠%'
  group by `query`
  order by sum(query_search_pv) desc
  limit 100
),

-- 2. 计算过去7天（t-2 到 t-8）每个词的加权平均CVR
past7 as (
  select
    `query`,
    -- 改计算方法，用区间内所有总数相除，而不是先算率再算平均
    sum(query_search_pay_uv) * 1.0 / nullif(sum(query_search_uv), 0) as cvr_avg
  from dwd_harbor_query_behavior_daily
  where `date` between CURRENT_DATE - interval 8 day and CURRENT_DATE - interval 2 day
    and length(regexp_replace(`query`, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and `query` not like '%折扣%'
    AND `query` NOT LIKE '%特价%'
    AND `query` NOT LIKE '%打折%'
    AND `query` NOT LIKE '%特惠%'
  group by `query`
  having count(distinct `date`) >= 5  -- 至少5天有数据
)

-- 3. 关联并筛选（绝对差值下降超过2个百分点）
select
  row_number() over (order by search_uv desc) as rn,
  t.`query`,
  t.search_uv,
  -- round(t.cvr * 100, 2) as cvr_today_pct,              -- 昨日CVR(%)
  -- round(p.cvr_avg * 100, 2) as cvr_avg_past7_pct,      -- 过去7天平均CVR(%)
  round((t.cvr - p.cvr_avg) * 100, 2) as cvr_gap_pct   -- 绝对差值(百分点)
from top100 t
join past7 p on t.`query` = p.`query`
where (t.cvr - p.cvr_avg) * 100 < -2  -- 绝对下降超过2个百分点
order by search_uv desc;
```

### Riviera
```sql
#---------------- Riviera 筛选当日ctr/cvr低于前三天平均的相对值10%------------------
#-------------------- CTR --------------------

-- 1. 先找出 t-1 日的 top100 词（带UV和CTR）
with top100 as (
  select
    search_term as `query`,
    sum(search_uv) as search_uv,
    sum(click_uv) * 1.0 / nullif(sum(search_uv), 0) as ctr
  from rl_keyword_daily
  where `date` = CURRENT_DATE - interval 1 day
    and length(regexp_replace(search_term, '[^0-9]', '')) < 5
        -- 去掉折扣词
    and search_term not like '%折扣%'
    AND search_term NOT LIKE '%特价%'
    AND search_term NOT LIKE '%打折%'
    AND search_term NOT LIKE '%特惠%'
  group by search_term
  order by sum(search_uv) desc
  limit 100
),

-- 2. 计算过去3天（t-2, t-3, t-4）每个词的加权平均CTR
past3 as (
  select
    search_term as `query`,
    -- 改计算方法，用区间内所有总数相除，而不是先算率再算平均
    sum(click_uv) * 1.0 / nullif(sum(search_uv), 0) as ctr_avg
  from rl_keyword_daily
  where `date` between CURRENT_DATE - interval 4 day and CURRENT_DATE - interval 2 day
    and length(regexp_replace(search_term, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and search_term not like '%折扣%'
    AND search_term NOT LIKE '%特价%'
    AND search_term NOT LIKE '%打折%'
    AND search_term NOT LIKE '%特惠%'
  group by search_term
  having count(distinct `date`) >= 2  -- 至少2天有数据
)

-- 3. 关联并筛选（相对下降超过10%）
select
  row_number() over (order by search_uv desc) as rn,
  t.`query`,
  t.search_uv,
  round((t.ctr - p.ctr_avg) / nullif(p.ctr_avg, 0) * 100, 2) as ctr_gap_relative_pct
from top100 t
join past3 p on t.`query` = p.`query`
where (t.ctr - p.ctr_avg) / nullif(p.ctr_avg, 0) * 100 < -10  -- 相对下降超10%
order by search_uv desc;


#-------------------- CVR --------------------

-- 1. 先找出 t-1 日的 top100 词（带UV和CVR）
with top100 as (
  select
    search_term as `query`,
    sum(search_uv) as search_uv,
    sum(purchase_uv) * 1.0 / nullif(sum(search_uv), 0) as cvr
  from rl_keyword_daily
  where `date` = CURRENT_DATE - interval 1 day
    and length(regexp_replace(search_term, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and search_term not like '%折扣%'
    AND search_term NOT LIKE '%特价%'
    AND search_term NOT LIKE '%打折%'
    AND search_term NOT LIKE '%特惠%'
  group by search_term
  order by sum(search_uv) desc
  limit 100
),

-- 2. 计算过去3天（t-2, t-3, t-4）每个词的加权平均CVR
past3 as (
  select
    search_term as `query`,
    -- 改计算方法，用区间内所有总数相除，而不是先算率再算平均
    sum(purchase_uv) * 1.0 / nullif(sum(search_uv), 0) as cvr_avg
  from rl_keyword_daily
  where `date` between CURRENT_DATE - interval 4 day and CURRENT_DATE - interval 2 day
    and length(regexp_replace(search_term, '[^0-9]', '')) < 5
    -- 去掉折扣词
    and search_term not like '%折扣%'
    AND search_term NOT LIKE '%特价%'
    AND search_term NOT LIKE '%打折%'
    AND search_term NOT LIKE '%特惠%'
  group by search_term
  having count(distinct `date`) >= 2  -- 至少2天有数据
)

-- 3. 关联并筛选（相对下降超过10%）
select
  row_number() over (order by search_uv desc) as rn,
  t.`query`,
  t.search_uv,
  round((t.cvr - p.cvr_avg) / nullif(p.cvr_avg, 0) * 100, 2) as cvr_gap_relative_pct
from top100 t
join past3 p on t.`query` = p.`query`
where (t.cvr - p.cvr_avg) / nullif(p.cvr_avg, 0) * 100 < -10  -- 相对下降超10%
order by search_uv desc;
```

## 4. 各品牌整体转化率数据查询 SQL
用以获取各品牌的整体转化率数据,支持获取的指标如下:
- 搜索UV
- 点击UV
- 加购UV
- 购买UV
- 购买金额
- CTR
- ATC
- CVR

### Atlas, Meadow, Summit, HARBOR
这些品牌共用一个sql,仅需要更换sql中的查询表名即可,各品牌对应的表名如下:
```
Meadow：dwd_meadow_user_behavior_daily
summit：dwd_summit_user_behavior_daily
harbor：dwd_harbor_user_behavior_daily
atlas：dwd_user_behavior_daily
```

以Meadow为示例,SQL示例如下:
```sql
-- spark user表

-- Meadow：dwd_meadow_user_behavior_daily
-- summit：dwd_summit_user_behavior_daily
-- harbor：dwd_harbor_user_behavior_daily
-- atlas：dwd_user_behavior_daily

WITH SearchMetrics AS (
    -- 1. 搜索 (UV) - 昨天
    SELECT
        user_date,
        COUNT(DISTINCT user_uid) AS 搜索UV
    FROM
        dwd_meadow_user_behavior_daily
    WHERE user_search_pv > 0
        AND user_date >DATE_SUB(CURDATE(), INTERVAL 13 DAY)
    GROUP BY
        user_date
),
ClickMetrics AS (
    -- 2. 点击 (UV) - 包含 PDP View 和 Click
    SELECT
        user_date,
        COUNT(DISTINCT CASE WHEN user_search_pv_pdp_view > 0 OR user_search_pv_click > 0 THEN user_uid END) AS 点击UV
    FROM
        dwd_meadow_user_behavior_daily
    WHERE (user_search_pv_pdp_view > 0 OR user_search_pv_click > 0)
        AND user_date >DATE_SUB(CURDATE(), INTERVAL 13 DAY)
    GROUP BY
        user_date
),
ATCMetrics AS (
    -- 3. 加购 (UV)
    SELECT
        user_date,
        COUNT(DISTINCT CASE WHEN user_search_pv_atc > 0 THEN user_uid END) AS 加购UV
    FROM
        dwd_meadow_user_behavior_daily
    WHERE user_search_pv_atc > 0
        AND user_date >DATE_SUB(CURDATE(), INTERVAL 13 DAY)
    GROUP BY
        user_date
),
PayMetrics AS (
    -- 4. 购买 (UV, 金额)
    SELECT
        user_date,
        COUNT(DISTINCT CASE WHEN user_search_pv_pay > 0 THEN user_uid END) AS 购买UV,
        SUM(user_pay_amount) AS 购买金额
    FROM
        dwd_meadow_user_behavior_daily
    WHERE user_search_pv_pay > 0
        AND user_date >DATE_SUB(CURDATE(), INTERVAL 13 DAY)
    GROUP BY
        user_date
)
-- 最终结果展示和转化率计算
SELECT
    -- 基础指标
       T1.user_date AS `日期`,


    -- 各环节UV
    IFNULL(T1.`搜索UV`, 0) AS `搜索UV`,
    IFNULL(T2.`点击UV`, 0) AS `点击UV`,
    IFNULL(T3.`加购UV`, 0) AS `加购UV`,
    IFNULL(T4.`购买UV`, 0) AS `购买UV`,

    -- 购买金额
    IFNULL(T4.`购买金额`, 0) AS `购买金额`,

    -- 转化率（从搜索开始计算）
    IFNULL(T2.`点击UV`, 0) / NULLIF(IFNULL(T1.`搜索UV`, 0), 0) AS `CTR`,  -- 点击率
    IFNULL(T3.`加购UV`, 0) / NULLIF(IFNULL(T1.`搜索UV`, 0), 0) AS `ATC`,  -- 加购率
    IFNULL(T4.`购买UV`, 0) / NULLIF(IFNULL(T1.`搜索UV`, 0), 0) AS `CVR`   -- 购买转化率

FROM
    SearchMetrics T1
LEFT JOIN
    ClickMetrics T2 ON T1.user_date = T2.user_date
LEFT JOIN
    ATCMetrics T3 ON T1.user_date = T3.user_date
LEFT JOIN
    PayMetrics T4 ON T1.user_date = T4.user_date;
```
### Riviera
```sql
SELECT
  `date` ,
    SUM(search_uv) AS 搜索uv,
    SUM(click_uv) AS 点击uv,
    SUM(add_to_cart_uv) AS 加购uv,
    SUM(purchase_uv) AS 购买uv,
    ROUND(SUM(click_uv) * 1.0 / NULLIF(SUM(search_uv), 0), 4) AS 点击率,
    ROUND(SUM(add_to_cart_uv) * 1.0 / NULLIF(SUM(search_uv), 0), 4) AS 加购率,
    ROUND(SUM(purchase_uv) * 1.0 / NULLIF(SUM(search_uv), 0), 4) AS 购买率
FROM rl_daily
WHERE `date` BETWEEN '2026-05-01' AND '2026-05-13'
group by     `date`
```