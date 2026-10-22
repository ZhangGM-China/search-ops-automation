// 发版会删除旧哈希的前端 chunk；停留在旧版 HTML 的标签页懒加载路由时会 404，
// 自动刷新一次即可拿到新 HTML 引用的新资源。冷却窗口防止资源持续异常时反复刷新。
const CHUNK_RELOAD_COOLDOWN_KEY = 'search_ops_chunk_reload_at';
const CHUNK_RELOAD_COOLDOWN_MS = 10_000;

export const shouldReloadForChunkError = (now: number = Date.now()): boolean => {
  const lastReloadAt = Number(sessionStorage.getItem(CHUNK_RELOAD_COOLDOWN_KEY));
  if (Number.isFinite(lastReloadAt) && now - lastReloadAt < CHUNK_RELOAD_COOLDOWN_MS) {
    return false;
  }
  sessionStorage.setItem(CHUNK_RELOAD_COOLDOWN_KEY, String(now));
  return true;
};

export const installChunkReloadGuard = (reload: () => void = () => window.location.reload()): (() => void) => {
  const listener = (event: Event) => {
    event.preventDefault();
    if (shouldReloadForChunkError()) reload();
  };
  window.addEventListener('vite:preloadError', listener);
  return () => window.removeEventListener('vite:preloadError', listener);
};
