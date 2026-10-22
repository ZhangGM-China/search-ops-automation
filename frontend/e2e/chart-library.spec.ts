import { test, expect } from '@playwright/test';

test.describe('Ant Design Charts migration', () => {
  test('renders the brand trend with the library chart and native G2 slider', async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    await page.route('**/api/admin/page-config', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      await route.fulfill({
        response,
        json: {
          ...payload,
          values: {
            ...payload.values,
            'brand_overview.drawer.show_chart': true,
          },
        },
      });
    });
    await page.route('**/api/dashboard/top-query/brands/atlas/trend*', async (route) => {
      const trend = Array.from({ length: 30 }, (_, index) => ({
        date: `2026-08-${String(index + 1).padStart(2, '0')}`,
        uv: 100 + index,
        pv: 200 + index * 2,
        clickUv: 30 + index,
        addToCartUv: 12 + index,
        purchaseUv: 5 + index,
        ctr: 0.2 + index / 1000,
        atc: 0.08 + index / 2000,
        cvr: 0.04 + index / 3000,
      }));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ brand: 'atlas', dataDate: '2026-09-08', trend }),
      });
    });
    await page.goto('/login');
    await page.fill('input[placeholder="用户名"]', 'admin');
    await page.fill('input[placeholder="密码"]', 'test-admin-password');
    await page.getByRole('button').filter({ hasText: /^登\s*录$/ }).click();
    await expect(page).toHaveURL(/\/ops-dashboard$/, { timeout: 15000 });

    await page.goto('/ops-dashboard?date=2026-09-08');
    const dataButton = page.getByRole('button', { name: '运营数据' }).first();
    await expect(dataButton).toBeVisible({ timeout: 15000 });
    await dataButton.click();

    const chart = page.locator('.ops-brand-metric-drawer .ops-trend-chart__canvas').first();
    await expect(chart).toBeVisible({ timeout: 15000 });
    const canvas = chart.locator('canvas').first();
    await expect(canvas).toBeVisible();
    const box = await canvas.boundingBox();
    expect(box).not.toBeNull();
    const before = await canvas.evaluate((element) => (element as HTMLCanvasElement).toDataURL());

    // G2 renders its native slider into the same canvas. Drag the left handle
    // to prove the chart responds without replacing it with custom navigation.
    const { x, y, width, height } = box!;
    await page.mouse.move(x + width * 0.16, y + height - 42);
    await page.mouse.down();
    await page.mouse.move(x + width * 0.34, y + height - 42, { steps: 8 });
    await page.mouse.up();
    await page.waitForTimeout(1200);

    const after = await canvas.evaluate((element) => (element as HTMLCanvasElement).toDataURL());
    expect(after).not.toBe(before);
    await expect(canvas).toBeVisible();
    expect(consoleErrors).toEqual([]);
  });
});
