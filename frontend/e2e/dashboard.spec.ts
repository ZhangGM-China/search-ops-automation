import { test, expect } from '@playwright/test';

test.describe('Search Ops Automation Dashboard', () => {
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
    await expect(page.getByRole('tab', { name: '相关性' })).toBeVisible({ timeout: 15000 });
    
    const dateInput = page.locator('.ant-picker-input input').first();
    const responsePromise = page.waitForResponse(resp => 
      resp.url().includes('/api/dashboard/blacklist-logs') && resp.url().includes('date=2026-05-20') && resp.status() === 200
    );
    await dateInput.click();
    await dateInput.fill('2026-05-20');
    await page.keyboard.press('Enter');
    await responsePromise;

    await expect(page.getByRole('tab', { name: '相关性' })).toBeVisible();
    await expect(page.locator('.ant-empty-description')).toHaveText('暂无数据', { timeout: 15000 });
  });
});

 test.describe('Relevance strategy groups', () => {
  test('shows 50 wrong products once under their final KG strategy', async ({ page }) => {
    const products = Array.from({ length: 50 }, (_, i) => ({ productId: `P${i}`, productName: `分组商品${i}`, reason: '商品非羊绒', blacklistStatus: 'blacklisted' }));
    const task = { id: 43, taskId: 43, brand: 'atlas', dataDate: '2026-10-07', queryText: '羊绒衫 女士', queryRank: 1, searchUv: 10, searchPv: 20, ctr: 0.2, cvr: 0.03, avgCtr: 0.2, avgCvr: 0.03, status: 'completed', displayStatus: 'issue', resultCount: 50, stepStatuses: [{ checkKey: 'relevance', status: 'issue', displayStatus: 'issue' }] };
    const stats = { topQueryCount: 1, manualRequiredCount: 1, completedCount: 0, brandUv: 10, brandPv: 20, ctr: 0.2, cvr: 0.03 };
    const basePlan = { taskId: 43, objective: '', summary: '', reason: '', status: 'pending', statusLabel: '待执行', executionMode: 'human_approval', executable: false, cancelable: false, steps: [], evidence: [], metrics: {}, products };
    const detail = { task, actions: [], stepStatuses: task.stepStatuses, trend: [], events: [], liveRecall: { resultCount: 50, products: [] }, productDisplayLimit: 60, inventoryBrokenRateThreshold: 0.25, governancePlans: [
      { ...basePlan, id: 'kg-final', type: 'kg', actionType: 'relevance_plan', title: '相关性治理 Agent 调整方案', actionIds: [60], causeGroups: [{ groupId: 'material', rootCause: 'kg_issue', productIds: products.map(p => p.productId), reasons: ['材质标签关系混淆'], conclusion: '调整羊绒标签关系' }] },
      { ...basePlan, id: 'candidates', type: 'blacklist', actionType: 'product_blacklist', title: '错误商品', actionIds: [], causeGroups: [{ rootCause: 'uncertain', products }] },
    ] };
    await page.route('**/api/dashboard/top-query/brands/atlas/summary*', route => route.fulfill({ json: { brand: 'atlas', dataDate: '2026-10-07', stats } }));
    await page.route('**/api/dashboard/top-query/brands/atlas/queries*', route => route.fulfill({ json: { brand: 'atlas', dataDate: '2026-10-07', stats, queries: [task], total: 1 } }));
    await page.route('**/api/dashboard/top-query/queries/43/workspace*', route => route.fulfill({ json: detail }));
    await page.route('**/api/dashboard/top-query/queries/43/current-result-count*', route => route.fulfill({ json: { taskId: 43, resultCount: 50, products: [] } }));
    await page.route('**/api/dashboard/top-query/brands/atlas/relevance-guidance*', route => route.fulfill({ json: { brand: 'atlas', query: task.queryText, rules: '' } }));
    await page.goto('/login');
    await page.fill('input[placeholder="用户名"]', 'admin');
    await page.fill('input[placeholder="密码"]', 'test-admin-password');
    await page.getByRole('button').filter({ hasText: /^登\s*录$/ }).click();
    await expect(page).toHaveURL(/\/ops-dashboard$/, { timeout: 15000 });
    await page.goto('/ops-dashboard/atlas?date=2026-10-07');
    const plans = page.locator('.ops-governance-plans');
    await expect(plans.getByText('错误商品（50）', { exact: true })).toHaveCount(1);
    await expect(plans.getByText(/^策略 \d+$/)).toHaveText(['策略 1']);
    await expect(plans.getByText('需人工复核', { exact: true })).toBeVisible();
    // 策略正文不再重复渲染分组原因与根因结论（837d6bee），错误原因由商品卡片展示
    await expect(plans.getByText('材质标签关系混淆', { exact: true })).toHaveCount(0);
    await expect(plans.getByText('调整羊绒标签关系', { exact: true })).toHaveCount(0);
    await expect(plans.locator('.ops-governance-cause-group')).toHaveCount(1);
    await expect(plans.getByRole('button', { name: /^商品 分组商品\d+$/ })).toHaveCount(50);
    for (const product of products) await expect(plans.getByRole('button', { name: `商品 ${product.productName}`, exact: true })).toHaveCount(1);
    await expect(plans.getByText('错误原因：', { exact: false }).first()).toBeVisible();
  });
});
