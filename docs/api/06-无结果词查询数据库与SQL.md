# 无结果词查询数据库

## 1. 生产数据源连接信息

| 适用品牌 | Host | Port | User | Database | pass |
| --- | --- | --- | --- | --- | --- |
| atlas, HARBOR, Riviera | `db.example.test` | 26462 | `memor` | `data` | <from-controlled-secret-provider> |
| Summit | `analytics.example.test` | 3306 | `operation` | `summit` | <from-controlled-secret-provider> |
| atlas | `db.example.test` | 3306 | `atlas_returning_readonly` | `search-ops-admin` | <from-controlled-secret-provider> |

## 2. SQL

### Atlas

```sql
SELECT
    CASE
        WHEN `query` LIKE '%女士' THEN '女士'
        WHEN `query` LIKE '%男士' THEN '男士'
        WHEN `query` LIKE '%家居' THEN '家居'
        WHEN `query` LIKE '%儿童' THEN '儿童'
        ELSE '无分类'
    END AS category,

    CASE
        WHEN `query` LIKE '%女士' THEN TRIM(REPLACE(`query`, '女士', ''))
        WHEN `query` LIKE '%男士' THEN TRIM(REPLACE(`query`, '男士', ''))
        WHEN `query` LIKE '%家居' THEN TRIM(REPLACE(`query`, '家居', ''))
        WHEN `query` LIKE '%儿童' THEN TRIM(REPLACE(`query`, '儿童', ''))
        ELSE TRIM(`query`)
    END AS core_word,

    SUM(`query_no_result_pv`) AS pv

FROM dwd_query_behavior_daily

WHERE
    query_handle_type in ('200')
    AND query_no_result_pv > 0
    AND `date` = '2026-06-19'

    -- 去掉整体是纯数字的query（6位及以上）
    AND TRIM(`query`) NOT REGEXP '^[0-9]{6,}$'

    -- 核心词过滤逻辑：如果是纯数字组合（只含数字、/、空格），且数字位数 >= 6，则过滤
    AND (
        -- 先判断：核心词是否只包含数字、/、空格
        TRIM(
            CASE
                WHEN `query` LIKE '%女士' THEN REPLACE(`query`, '女士', '')
                WHEN `query` LIKE '%男士' THEN REPLACE(`query`, '男士', '')
                WHEN `query` LIKE '%家居' THEN REPLACE(`query`, '家居', '')
                WHEN `query` LIKE '%儿童' THEN REPLACE(`query`, '儿童', '')
                ELSE `query`
            END
        ) NOT REGEXP '^[0-9/ ]+$'
        -- 或者：虽然是纯数字组合，但数字总位数 < 6（保留）
        OR (
            TRIM(
                CASE
                    WHEN `query` LIKE '%女士' THEN REPLACE(`query`, '女士', '')
                    WHEN `query` LIKE '%男士' THEN REPLACE(`query`, '男士', '')
                    WHEN `query` LIKE '%家居' THEN REPLACE(`query`, '家居', '')
                    WHEN `query` LIKE '%儿童' THEN REPLACE(`query`, '儿童', '')
                    ELSE `query`
                END
            ) REGEXP '^[0-9/ ]+$'
            AND LENGTH(REPLACE(REPLACE(REPLACE(
                TRIM(
                    CASE
                        WHEN `query` LIKE '%女士' THEN REPLACE(`query`, '女士', '')
                        WHEN `query` LIKE '%男士' THEN REPLACE(`query`, '男士', '')
                        WHEN `query` LIKE '%家居' THEN REPLACE(`query`, '家居', '')
                        WHEN `query` LIKE '%儿童' THEN REPLACE(`query`, '儿童', '')
                        ELSE `query`
                    END
                ), '/', ''), ' ', ''), '-', '')) < 6
        )
    )

GROUP BY core_word, category
ORDER BY pv DESC
```

### Meadow

```sql
暂无,先跳过
```

### Summit
```sql
-- 指定日期的无结果词
SELECT
  searchTerm AS keyword,
  COUNT(*) AS no_result_count
FROM behavior
WHERE event = 'SearchNoResultsProductExposure'
  AND searchTerm IS NOT NULL
  AND TRIM(searchTerm) != ''
  -- 排除5个及以上纯数字
  AND NOT regexp_like(TRIM(searchTerm), '^[0-9]{5,}$')
  -- 排除干扰词（如果有）
  AND searchTerm NOT LIKE '%二手%'
  and searchTerm not like '%防晒%'
  and searchTerm not like '%租%'
  and searchTerm not like '%钛%'
  and searchTerm not like '%盐丸%'
  and searchTerm not like '%门店%'
  and searchTerm not like '%回收%'
  and searchTerm not like '%优惠券%'
  and searchTerm not like '%穿线%'
  and searchTerm not like '%抽奖%'
  and searchTerm not like '%冬日温泉季%'
  -- 使用 Grafana 变量排除
  AND searchTerm NOT IN (${excludedKeywords:raw})
  -- 时间范围过滤
  AND __timestamp__ >= $__from
  AND __timestamp__ <= $__to
GROUP BY keyword
ORDER BY no_result_count DESC
LIMIT 20
```

### HARBOR
```sql
-- 指定日期的无结果词
SELECT
  search_text, COUNT(*)AS search_pv
FROM
  harbor_parsed_search_log
WHERE
  DATE(search_time) = '2026-06-21'
  and brand = 'harbor'
  AND recall_count = 0
  -- 去掉5个以上纯数字
  and  trim(search_text) NOT REGEXP '^[0-9]{6,}$'
GROUP BY search_text
ORDER BY search_pv DESC ;

```

### Riviera
```sql
-- 昨日的无结果词
SELECT
  search_text,
  COUNT(*)AS search_pv
FROM
  moe_parsed_search_log
WHERE
  DATE(search_time) = DATE_SUB(CURDATE(), INTERVAL 1 DAY) and
  brand = 'Riviera'
  AND recall_count = 0
  -- 去掉5个以上纯数字
  and  trim(search_text) NOT REGEXP '^[0-9]{6,}$'
  -- 排除测试词123
  and  trim(search_text) !='123'
GROUP BY search_text
ORDER BY search_pv DESC ;
```
