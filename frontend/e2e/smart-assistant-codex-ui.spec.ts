import { expect, test } from '@playwright/test';

test('renders the simplified Codex-style agent conversation and can send a message', async ({ page }, testInfo) => {
  const consoleErrors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });

  await page.goto('/login');
  await page.fill('input[placeholder="用户名"]', 'admin');
  await page.fill('input[placeholder="密码"]', 'test-admin-password');
  await page.getByRole('button').filter({ hasText: /^登\s*录$/ }).click();
  await expect(page).toHaveURL(/\/ops-dashboard$/, { timeout: 15_000 });

  await page.goto('/smart-assistant');
  await expect(page.getByRole('region', { name: '智能运营助理页面' })).toBeVisible();
  await expect(page.getByText('项目工作区')).toHaveCount(0);
  await expect(page.getByText('运行时检查器')).toHaveCount(0);
  await expect(page.getByText('账号权限工作区')).toHaveCount(0);
  await expect(page.locator('.ops-smart-assistant-list-item__preview')).toHaveCount(0);
  await expect(page.locator('.ops-smart-assistant-list-item__meta')).toHaveCount(0);
  await expect(page.locator('.ops-smart-assistant-list-item__time')).toHaveCount(0);
  await expect(page.getByLabel('当前模型')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '切换模型' })).toHaveCount(0);

  await page.getByText('新对话', { exact: true }).click();
  const prompt = '请只回复：界面连接测试成功';
  await page.getByLabel('输入智能运营助理消息').fill(prompt);
  const streamRequest = page.waitForRequest((request) => request.url().includes('/api/assistant/chat/stream'));
  await page.getByRole('button', { name: '发送消息' }).click();
  expect((await streamRequest).postDataJSON()).not.toHaveProperty('agentBindingId');
  await expect(page.getByRole('article', { name: '你的消息' }).last()).toContainText(prompt);
  await expect(page.getByRole('article', { name: '助理消息' }).last()).toContainText(/界面连接测试成功/, { timeout: 45_000 });

  expect(consoleErrors).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('smart-assistant-codex-ui.png'), fullPage: true });
});
