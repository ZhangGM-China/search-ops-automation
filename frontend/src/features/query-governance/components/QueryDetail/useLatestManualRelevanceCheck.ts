import React from 'react';
import { message } from 'antd';

import { dashboardApi } from '@/shared/api';
import type { ManualRelevanceCheckResponse } from '@/shared/types';
import { getApiErrorMessage } from '@/shared/lib/error';

const activeStatuses = new Set(['queued', 'running']);

/**
 * 同一 Query 的验证状态在详情面板多处展示，读取与轮询集中在这里，
 * 避免每个展示点各自发一次 latest 请求。
 */
export const useLatestManualRelevanceCheck = (brand?: string, query?: string) => {
  const key = brand && query ? `${brand}|${query}` : '';
  const [loaded, setLoaded] = React.useState<{ key: string; check: ManualRelevanceCheckResponse | null } | null>(null);
  // 切换 Query 时旧结果立即失效，避免把上一个 Query 的验证状态显示到新 Query 上。
  const check = loaded && loaded.key === key ? loaded.check : null;
  const setCheck = React.useCallback(
    (next: ManualRelevanceCheckResponse | null) => setLoaded({ key, check: next }),
    [key],
  );

  React.useEffect(() => {
    if (!key || !brand || !query) return undefined;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void dashboardApi.getLatestManualRelevanceCheck(brand, query, { signal: controller.signal })
        .then((response) => {
          if (!controller.signal.aborted) setLoaded({ key, check: response.data ?? null });
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted || (error as { name?: string })?.name === 'CanceledError') return;
          setLoaded({ key, check: null });
          message.error(getApiErrorMessage(error, '获取最近一次验证结果失败'));
        });
    }, 0);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [key, brand, query]);

  React.useEffect(() => {
    if (!check || !activeStatuses.has(check.status)) return undefined;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const response = await dashboardApi.getManualRelevanceCheck(check.checkId);
        if (cancelled) return;
        setCheck(response.data);
        if (activeStatuses.has(response.data.status)) timer = setTimeout(() => void poll(), 1500);
      } catch (error: unknown) {
        if (!cancelled) message.error(getApiErrorMessage(error, '读取验证状态失败'));
      }
    };
    timer = setTimeout(() => void poll(), 1000);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [check, setCheck]);

  return { check, setCheck };
};
