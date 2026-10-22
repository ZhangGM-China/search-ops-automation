import { test, expect } from '@playwright/test';

test.describe('operations log pages', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[placeholder="用户名"]', 'admin');
    await page.fill('input[placeholder="密码"]', 'test-admin-password');
    // antd Button might have space between characters like "登 录"
    await page.getByRole('button').filter({ hasText: /^登\s*录$/ }).click();
    await expect(page).toHaveURL(/\/ops-dashboard$/, { timeout: 15000 });
  });

  test.afterEach(async ({ page }, testInfo) => {
    if (testInfo.status !== testInfo.expectedStatus) {
      await page.screenshot({
        path: testInfo.outputPath(`failure-${testInfo.title.replace(/\s+/g, '-')}.png`),
      });
    }
  });

  test('should navigate to blacklist logs and verify entries', async ({ page }) => {
    await page.goto('/blacklist');
    await expect(page.getByRole('menuitem', { name: /运营日志/ })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('menuitem', { name: /相关性检测/ })).toHaveCount(0);
    await expect(page.getByRole('tab', { name: '相关性检测' })).toBeVisible({ timeout: 15000 });
    
    const responsePromise = page.waitForResponse(resp =>
      resp.url().includes('/api/dashboard/blacklist-logs') && resp.url().includes('date=2026-05-20') && resp.status() === 200
    );
    const dateInput = page.locator('.ant-picker-input input').first();
    await dateInput.click();
    await dateInput.fill('2026-05-20');
    await page.keyboard.press('Enter');
    await responsePromise;

    await expect(page.getByRole('tab', { name: '相关性检测' })).toBeVisible();
    await expect(page.locator('.ant-empty-description')).toHaveText('暂无数据', { timeout: 15000 });
  });
});
