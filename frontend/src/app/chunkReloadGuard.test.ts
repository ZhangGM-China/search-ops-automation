import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { installChunkReloadGuard, shouldReloadForChunkError } from './chunkReloadGuard';

describe('chunk reload guard', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('allows one reload per cooldown window', () => {
    expect(shouldReloadForChunkError(1_000_000)).toBe(true);
    expect(shouldReloadForChunkError(1_005_000)).toBe(false);
    expect(shouldReloadForChunkError(1_010_000)).toBe(true);
  });

  it('prevents vite preload errors and reloads once within the cooldown window', () => {
    const reload = vi.fn();
    const uninstall = installChunkReloadGuard(reload);
    try {
      const first = new Event('vite:preloadError', { cancelable: true });
      window.dispatchEvent(first);
      expect(first.defaultPrevented).toBe(true);
      expect(reload).toHaveBeenCalledTimes(1);

      const second = new Event('vite:preloadError', { cancelable: true });
      window.dispatchEvent(second);
      expect(reload).toHaveBeenCalledTimes(1);
    } finally {
      uninstall();
    }
  });

  afterEach(() => {
    sessionStorage.clear();
  });
});
