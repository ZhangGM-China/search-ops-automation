import { test, expect } from '@playwright/test';

test('strategy groups keep their products, tag attributes and actions scoped', async ({ page }) => {
  const failures: string[] = [];
  page.on('pageerror', (error) => failures.push(error.message));
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    if (route.request().url().includes('kg-tag-details')) {
      await route.fulfill({ json: { name: '材质', viewTag: '材质', isRoot: '1', dimensions: ['材质'], weight: 8, isEnglish: 0, segmentingAgain: '1', isCompose: '0', nameAnd: [], nameNot: [] } });
    } else if (route.request().url().includes('relevance-checks/latest')) {
      await route.fulfill({ status: 404, json: { detail: 'Not found' } });
    } else {
      await route.fulfill({ json: { status: 'verified' } });
    }
  });
  await page.goto('/e2e/fixtures/governance-strategy.html');
  const groups = page.getByTestId('governance-cause-group');
  await expect(groups).toHaveCount(2);
  await expect(groups.nth(0)).toContainText('策略 1');
  await expect(groups.nth(0)).toContainText('错误商品（1）');
  await expect(groups.nth(0)).toContainText('羊毛外套');
  await expect(groups.nth(0)).not.toContainText('针织上衣');
  await expect(groups.nth(1)).toContainText('个例，需拉黑');
  await expect(groups.nth(1).getByRole('button', { name: '一键执行' })).toHaveCount(0);
  await groups.nth(0).getByRole('button', { name: '查看标签 材质' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('标签名称')).toBeVisible();
  await expect(dialog.locator('.ant-descriptions-item-label')).toHaveCount(10);
  await expect(dialog).toContainText('英文分词');
  await dialog.getByRole('tab', { name: '羊毛' }).click();
  await expect(dialog.getByRole('tab', { name: '羊毛' })).toHaveAttribute('aria-selected', 'true');
  await dialog.getByRole('button', { name: 'Close' }).click();
  const execute = page.waitForRequest('**/api/query-governance/actions/22/execute');
  await groups.nth(0).getByRole('button', { name: '一键执行' }).click();
  expect(JSON.parse((await execute).postData()!)).toEqual({ groupId: 'kg-1' });
  await groups.nth(1).getByRole('checkbox', { name: '选择 针织上衣' }).check();
  await expect(groups.nth(0).getByRole('button', { name: '批量取消拉黑' })).toBeDisabled();
  const batch = page.waitForRequest('**/api/e2e/batch');
  await groups.nth(1).getByRole('button', { name: '批量取消拉黑' }).click();
  expect(JSON.parse((await batch).postData()!)).toEqual({ groupId: 'individual-2', products: ['P2'], action: 'cancel' });
  expect(failures).toEqual([]);
  await page.screenshot({ path: test.info().outputPath('strategy-groups.png'), fullPage: true });
});
