import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:5174';
const apiBase = process.env.VITE_API_BASE || '(proxied by frontend)';
const offlineTest = process.env.AIOPS_OFFLINE_TEST === '1';

console.log(`[playwright] cwd=${process.cwd()} baseURL=${baseURL} apiBase=${apiBase}`);

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL,
    trace: 'on',
    video: 'on-first-retry',
    proxy: offlineTest
      ? { server: 'http://127.0.0.1:9', bypass: 'localhost,127.0.0.1' }
      : undefined,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
