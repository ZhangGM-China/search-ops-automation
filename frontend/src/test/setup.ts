import '@testing-library/jest-dom'
import { act, cleanup } from '@testing-library/react'
import { setImmediate } from 'node:timers'
import { afterEach, vi } from 'vitest'

afterEach(async () => {
  await act(async () => {
    cleanup()
    // React's scheduler uses setImmediate in Node. Drain that phase before
    // Vitest disposes JSDOM, otherwise deferred commits can access a dead window.
    await new Promise<void>((resolve) => setImmediate(resolve))
  })
})

const storageValues = new Map<string, string>();
const testLocalStorage: Storage = {
  get length() {
    return storageValues.size;
  },
  clear() {
    storageValues.clear();
  },
  getItem(key: string) {
    return storageValues.has(key) ? storageValues.get(key) ?? null : null;
  },
  key(index: number) {
    return Array.from(storageValues.keys())[index] ?? null;
  },
  removeItem(key: string) {
    storageValues.delete(key);
  },
  setItem(key: string, value: string) {
    storageValues.set(key, String(value));
  },
};

Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: testLocalStorage,
});
Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: testLocalStorage,
});

Object.defineProperty(window, 'prompt', {
  configurable: true,
  writable: true,
  value: vi.fn(() => ''),
});

const originalGetComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = ((element: Element, pseudoElement?: string | null) => {
  const style = originalGetComputedStyle(element);
  if (element.classList.contains('adm-px-tester')) {
    return new Proxy(style, {
      get(target, property, receiver) {
        if (property === 'position') {
          return 'fixed';
        }
        if (property === 'getPropertyValue') {
          return (name: string) => (name === 'position' ? 'fixed' : target.getPropertyValue(name));
        }
        return Reflect.get(target, property, receiver);
      },
    });
  }
  if (!pseudoElement) {
    return style;
  }
  return style;
}) as typeof window.getComputedStyle;

await import('antd-mobile/es/global');

// Mock matchMedia for antd
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {}, // Deprecated
    removeListener: () => {}, // Deprecated
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

// Mock ResizeObserver for antd
class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

window.ResizeObserver = ResizeObserver;
