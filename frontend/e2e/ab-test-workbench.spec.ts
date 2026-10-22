import { test, expect, type BrowserContext, type ConsoleMessage, type Download, type Page, type Route } from '@playwright/test';

type ExperimentStatus = 'draft' | 'scheduled' | 'running' | 'ended' | 'cancelled';

const samplePool = Array.from({ length: 45 }, (_, index) => ({
  queryText: `Query ${String(index + 1).padStart(2, '0')}`,
  governanceKey: `query-${index + 1}`,
  queryRank: index + 1,
  searchUv: 1000 - index,
  searchPv: 2000 - index,
  ctr: 0.2 + index / 10000,
  cvr: 0.03 + index / 100000,
  avgCtr: 0.19,
  avgCvr: 0.025,
  queryPvShare: 0.02,
}));

const samples = samplePool.slice(0, 40).map((item, index) => ({
  queryText: item.queryText,
  groupType: index < 20 ? 'treatment' : 'control',
  sampleRank: item.queryRank,
  sampleSearchUv: item.searchUv,
  sampleSearchPv: item.searchPv,
  sampleCtr: item.ctr,
  sampleCvr: item.cvr,
  sampleAvgCtr: item.avgCtr,
  sampleAvgCvr: item.avgCvr,
  sampleQueryPvShare: item.queryPvShare,
}));

const detail = (id: number, status: ExperimentStatus) => ({
  id,
  brand: 'atlas',
  experimentName: `ATLAS A/B Test #${id}`,
  status,
  sampleDataDate: '2026-07-07',
  plannedStartDate: '2026-07-09',
  plannedEndDate: '2026-07-15',
  startedAt: status === 'running' || status === 'ended' ? '2026-07-09T00:00:00' : null,
  endedAt: status === 'ended' ? '2026-07-15T23:59:59' : null,
  createdBy: 'operator',
  samples,
  items: samples.map((sample, index) => ({
    ...sample,
    observedStatus: 'observed',
    contaminationStatus: 'clean',
    latestSearchPv: 2100 - index,
    latestSearchUv: 1050 - index,
    latestCtr: 0.22,
    latestCvr: 0.035,
    governanceTaskId: index < 20 ? 100 + index : null,
    governanceStatus: index < 20 ? 'completed' : null,
  })),
});

const dayQuality = (overrides: Record<string, unknown> = {}) => ({
  expectedSamples: 40,
  observedSamples: 40,
  treatmentSamples: 20,
  controlSamples: 20,
  treatmentTaskCount: 20,
  controlTaskCount: 0,
  controlActionCount: 0,
  contaminationCount: 0,
  failedTaskCount: 0,
  unresolvedTaskCount: 0,
  failedActionCount: 0,
  unresolvedActionCount: 0,
  ...overrides,
});

const report = (
  id: number,
  status: ExperimentStatus,
  qualityOverrides: Record<string, unknown> = {},
) => {
  const calculationVersion = 'top-query-ab-test-v2';
  const latestDayQuality = dayQuality(qualityOverrides);
  const complete = Object.values({
    observed: latestDayQuality.observedSamples === 40,
    tasks: latestDayQuality.treatmentTaskCount === 20,
    controlTasks: latestDayQuality.controlTaskCount === 0,
    controlActions: latestDayQuality.controlActionCount === 0,
    pollution: latestDayQuality.contaminationCount === 0,
  }).every(Boolean);
  const quality = {
    status: complete ? 'complete' : 'failed',
    error: complete ? null : latestDayQuality.contaminationCount ? 'control_group_contamination' : 'experiment_quality_gate_failed',
    completeDays: complete ? 1 : 0,
    failedDays: complete ? 0 : 1,
    latestDataDate: '2026-07-08',
    latestDayQuality,
    failedDates: complete ? [] : ['2026-07-08'],
  };
  const groups = {
    treatment: { sampleCount: 20, observedRows: 20, baselineSearchUv: 21000, baselineSearchPv: 41000, baselineClickUv: 4200, baselinePurchaseUv: 630, baselineCtr: 0.2, baselineCvr: 0.03, currentSearchUv: 22000, currentSearchPv: 42000, currentClickUv: 5060, currentPurchaseUv: 836, currentCtr: 0.23, currentCvr: 0.038, ctrChange: 0.03, cvrChange: 0.008 },
    control: { sampleCount: 20, observedRows: 20, baselineSearchUv: 20000, baselineSearchPv: 40000, baselineClickUv: 4000, baselinePurchaseUv: 600, baselineCtr: 0.2, baselineCvr: 0.03, currentSearchUv: 21000, currentSearchPv: 41000, currentClickUv: 4410, currentPurchaseUv: 693, currentCtr: 0.21, currentCvr: 0.033, ctrChange: 0.01, cvrChange: 0.003 },
  };
  const effect = complete ? {
    ctrNetChange: 0.02,
    cvrNetChange: 0.005,
    ctrPositiveDays: 2,
    cvrPositiveDays: 1,
    estimatedIncrementalClickUv: 440,
    estimatedIncrementalPurchaseUv: 110,
  } : null;
  const operations = { governanceCoverage: 1, actionSuccessRate: 1, plannedActions: 12, executedActions: 12, failedActions: 0, inconclusiveActions: 0, unresolvedActions: 0, rejectedActions: 0, cancelledActions: 0, tokenUsage: 3200, durationSeconds: 88 };
  const metadata = {
    experimentId: id,
    brand: 'atlas',
    experimentName: `ATLAS A/B Test #${id}`,
    status,
    sampleDataDate: '2026-07-07',
    plannedStartDate: '2026-07-09',
    plannedEndDate: '2026-07-15',
    baselineStartDate: '2026-07-02',
    baselineEndDate: '2026-07-08',
    baselineLockedAt: '2026-07-09T00:00:00+08:00',
    calculationVersion,
  };
  const queryItems = samples.map((sample, index) => ({
    queryText: sample.queryText,
    groupType: sample.groupType,
    sampleRank: sample.sampleRank,
    baselineObservedDays: 7,
    baselineSearchUv: sample.sampleSearchUv,
    baselineSearchPv: sample.sampleSearchPv,
    baselineClickUv: sample.sampleSearchUv * sample.sampleCtr,
    baselinePurchaseUv: sample.sampleSearchUv * sample.sampleCvr,
    baselineCtr: sample.sampleCtr,
    baselineCvr: sample.sampleCvr,
    latestDataDate: '2026-07-08',
    observedStatus: 'observed',
    contaminationStatus: 'clean',
    contaminationReason: null,
    observedDays: 1,
    unobservedDays: 0,
    contaminatedDays: 0,
    dataQualityStatus: index === 39 ? 'failed' : 'complete',
    currentSearchUv: sample.sampleSearchUv + 10,
    currentSearchPv: sample.sampleSearchPv + 20,
    currentClickUv: (sample.sampleSearchUv + 10) * (sample.sampleCtr + 0.01),
    currentPurchaseUv: (sample.sampleSearchUv + 10) * (sample.sampleCvr + 0.002),
    currentCtr: sample.sampleCtr + 0.01,
    currentCvr: sample.sampleCvr + 0.002,
    ctrChange: 0.01,
    cvrChange: 0.002,
    groupContribution: { ctr: 0.0005, cvr: 0.0001 },
    netCtrContribution: sample.groupType === 'treatment' ? 0.0005 : -0.0005,
    netCvrContribution: sample.groupType === 'treatment' ? 0.0001 : -0.0001,
    effectDirection: index === 1 ? 'mixed' : index === 39 ? 'undetermined' : sample.groupType === 'treatment' ? 'positive' : 'negative',
    governanceTaskId: index < 20 ? 100 + index : null,
    governanceStatus: index < 20 ? 'completed' : null,
    actionCount: index < 20 ? 1 : 0,
    tokenUsage: index < 20 ? 100 : 0,
    durationSeconds: index < 20 ? 2 : 0,
  }));
  const queryDailyMetrics = queryItems.map((item) => ({
    dataDate: '2026-07-08',
    queryText: item.queryText,
    groupType: item.groupType,
    canonicalQueryKey: item.queryText.toLowerCase(),
    lockedQueryText: item.queryText,
    lockedGroupType: item.groupType,
    identityStatus: 'complete',
    observedStatus: 'observed',
    contaminationStatus: 'clean',
    searchUv: item.currentSearchUv,
    searchPv: item.currentSearchPv,
    clickUv: item.currentClickUv,
    purchaseUv: item.currentPurchaseUv,
    ctr: item.currentCtr,
    cvr: item.currentCvr,
    numeratorSource: 'raw',
    clickNumeratorSource: 'raw',
    purchaseNumeratorSource: 'raw',
    clickNumeratorStatus: 'complete',
    purchaseNumeratorStatus: 'complete',
    dataQualityStatus: 'complete',
    includedInEffect: true,
    governanceTaskId: item.governanceTaskId,
    governanceStatus: item.governanceStatus,
    actionCount: item.actionCount,
    autoActionCount: item.actionCount,
    manualActionCount: 0,
    tokenUsage: item.tokenUsage,
    durationSeconds: item.durationSeconds,
  }));
  const overview = { ...metadata, quality, groups, effect, operations };
  return {
    reportSnapshotToken: `snapshot-${id}-${status}`,
    experiment: metadata,
    calculationVersion,
    generatedAt: '2026-07-09T10:00:00+08:00',
    timezone: 'Asia/Shanghai',
    reportDate: '2026-07-09',
    baselineQuality: {
      status: 'complete',
      error: null,
      calculationVersion,
      sampleCount: 40,
      treatmentSamples: 20,
      controlSamples: 20,
      expectedDays: 7,
      windowValid: true,
      locked: true,
    },
    quality,
    groups,
    effect,
    operations,
    trend: [
      { dataDate: '2026-07-08', quality: { status: 'complete' }, groups, effect: { ctrNetChange: 0.01, cvrNetChange: 0.002 }, operations },
      { dataDate: '2026-07-09', quality: { status: 'failed', error: 'experiment_quality_gate_failed' }, groups, effect: null, operations },
      { dataDate: '2026-07-10', quality: { status: 'complete' }, groups, effect: { ctrNetChange: 0.015, cvrNetChange: null }, operations },
    ],
    queries: queryItems,
    queryDailyMetrics,
    baselineEvidence: [],
    actions: [{ recordType: 'action', detailAvailable: true, dataDate: '2026-07-08', groupType: 'treatment', governanceStatus: 'completed', id: 501, taskId: 100, brand: 'atlas', queryText: 'Query 01', checkKey: 'relevance', actionType: 'query_demote', executionMode: 'auto', status: 'verified', reason: '离线 E2E fixture', errorMessage: null, createdAt: '2026-07-08T08:00:00+08:00' }],
    dataStartDate: '2026-07-08',
    dataEndDate: '2026-07-10',
    dataDays: 3,
    overview,
  };
};

const historyItems = [
  { id: 7, brand: 'atlas', experimentName: 'ATLAS A/B Test #7', status: 'running', sampleDataDate: '2026-07-07', plannedStartDate: '2026-07-09', plannedEndDate: '2026-07-15', treatmentCount: 20, controlCount: 20 },
  { id: 6, brand: 'atlas', experimentName: 'ATLAS A/B Test #6', status: 'ended', sampleDataDate: '2026-06-30', plannedStartDate: '2026-07-02', plannedEndDate: '2026-07-08', treatmentCount: 20, controlCount: 20 },
];

const user = (canManage = true) => ({
  id: 1,
  username: canManage ? 'brand-admin' : 'operator',
  isAdmin: false,
  role: canManage ? 'brand_admin' : 'brand_member',
  pagePermissions: ['/config'],
  brandPermissions: ['atlas'],
  actionPermissions: canManage ? ['manage_brand_config'] : [],
});

async function seedSession(context: BrowserContext, canManage = true) {
  await context.addInitScript(({ currentUser }) => {
    localStorage.setItem('token', 'e2e-mock-token');
    localStorage.setItem('user', JSON.stringify(currentUser));
  }, { currentUser: user(canManage) });
}

async function installApiMocks(
  context: BrowserContext,
  options: { status?: ExperimentStatus | null; quality?: Record<string, unknown>; selectedId?: number; canManage?: boolean } = {},
) {
  let currentStatus = options.status === undefined ? 'running' : options.status;
  let selectedId = options.selectedId ?? 7;
  let draftSamples = samples;
  const downloads: string[] = [];
  const exportRequests: Array<{ method: string; body: unknown }> = [];
  const reportRequests: number[] = [];
  const blockedExternalRequests: string[] = [];
  const blockedBusinessRequests: string[] = [];
  const unexpectedApiRequests: string[] = [];
  const pageErrors: Error[] = [];
  const consoleErrors: ConsoleMessage[] = [];

  const attachRuntimeAudit = (page: Page) => {
    page.on('pageerror', (error) => pageErrors.push(error));
    page.on('console', (message) => {
      if (message.type() === 'error' || message.type() === 'warning') consoleErrors.push(message);
    });
  };
  context.pages().forEach(attachRuntimeAudit);
  context.on('page', attachRuntimeAudit);

  await context.route('**/*', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) {
      blockedExternalRequests.push(request.url());
      if (url.hostname === 'example.test' || url.hostname.endsWith('.example.test')) {
        blockedBusinessRequests.push(request.url());
      }
      await route.abort('blockedbyclient');
      return;
    }
    if (!url.pathname.startsWith('/api/')) {
      await route.continue();
      return;
    }

    const apiPath = url.pathname.replace(/^\/api/, '');
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (apiPath === '/auth/me') return json(user(options.canManage ?? true));
    if (apiPath === '/admin/page-config') {
      return json({
        schemaVersion: 1,
        version: 0,
        values: {
          'brand_overview.show_operational_data': true,
          'brand_detail.show_query_metrics': true,
        },
        definitions: [],
      });
    }
    if (apiPath === '/dashboard/top-query/experiments/brands/atlas/sample-pool') {
      return json({ brand: 'atlas', sampleDataDate: '2026-07-07', items: samplePool });
    }
    if (apiPath === '/dashboard/top-query/experiments/brands/atlas/draft' && request.method() === 'POST') {
      const body = request.postDataJSON() as { treatmentQueries: string[]; controlQueries: string[] };
      draftSamples = [...body.treatmentQueries.map((queryText) => ({ ...samples[0], queryText, groupType: 'treatment' })), ...body.controlQueries.map((queryText) => ({ ...samples[20], queryText, groupType: 'control' }))];
      currentStatus = 'draft';
      selectedId = 9;
      return json({ ...detail(9, 'draft'), samples: draftSamples });
    }
    const lifecycle = apiPath.match(/^\/dashboard\/top-query\/experiments\/(\d+)\/(schedule|end|cancel)$/);
    if (lifecycle && request.method() === 'POST') {
      selectedId = Number(lifecycle[1]);
      currentStatus = lifecycle[2] === 'schedule' ? 'scheduled' : lifecycle[2] === 'end' ? 'ended' : 'cancelled';
      return json({ ...detail(selectedId, currentStatus), samples: draftSamples });
    }
    if (apiPath === '/dashboard/top-query/experiments/brands/atlas') {
      const current = currentStatus ? { ...historyItems[0], id: selectedId, status: currentStatus } : null;
      return json({ brand: 'atlas', current, items: current ? [current, ...historyItems.filter((item) => item.id !== selectedId)] : historyItems, total: 2, page: 1, pageSize: 20 });
    }
    const exportMatch = apiPath.match(/^\/dashboard\/top-query\/experiments\/(\d+)\/export$/);
    if (exportMatch) {
      const requestBody = request.method() === 'POST' ? request.postDataJSON() as { format?: string; snapshotToken?: string } : null;
      const format = requestBody?.format || url.searchParams.get('format') || '';
      exportRequests.push({ method: request.method(), body: requestBody });
      downloads.push(format);
      const contentType = format === 'xlsx'
        ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        : format === 'zip' ? 'application/zip' : 'text/csv; charset=utf-8';
      const body = format === 'csv'
        ? Buffer.from('\ufeffexperimentId,status\n7,ended\n', 'utf8')
        : Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x6f, 0x66, 0x66, 0x6c, 0x69, 0x6e, 0x65]);
      return route.fulfill({
        status: 200,
        contentType,
        headers: { 'Content-Disposition': `attachment; filename="ab-test-${exportMatch[1]}.${format}"` },
        body,
      });
    }
    const reportMatch = apiPath.match(/^\/dashboard\/top-query\/experiments\/(\d+)\/report$/);
    if (reportMatch) {
      const id = Number(reportMatch[1]);
      const status = id === selectedId && currentStatus ? currentStatus : 'ended';
      reportRequests.push(id);
      return json(report(id, status, options.quality));
    }
    const detailMatch = apiPath.match(/^\/dashboard\/top-query\/experiments\/(\d+)$/);
    if (detailMatch) {
      const id = Number(detailMatch[1]);
      const status = id === selectedId && currentStatus ? currentStatus : 'ended';
      return json({ ...detail(id, status), samples: id === selectedId ? draftSamples : samples });
    }
    unexpectedApiRequests.push(`${request.method()} ${apiPath}`);
    return json({ detail: `Unexpected mocked API route: ${request.method()} ${apiPath}` }, 404);
  });

  return { downloads, exportRequests, reportRequests, blockedExternalRequests, blockedBusinessRequests, unexpectedApiRequests, pageErrors, consoleErrors };
}

type OfflineAudit = Awaited<ReturnType<typeof installApiMocks>>;

function assertOfflineAudit(network: OfflineAudit) {
  expect(network.blockedBusinessRequests).toEqual([]);
  expect(network.unexpectedApiRequests).toEqual([]);
  expect(network.blockedExternalRequests).toEqual([]);
  expect(network.pageErrors).toEqual([]);
  expect(network.consoleErrors.map((message) => message.text())).toEqual([]);
}

async function readDownloadPrefix(download: Download, length = 4): Promise<Buffer> {
  const stream = await download.createReadStream();
  if (!stream) throw new Error(`Download stream unavailable for ${download.suggestedFilename()}`);
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of stream) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    chunks.push(buffer);
    bytes += buffer.length;
    if (bytes >= length) break;
  }
  return Buffer.concat(chunks).subarray(0, length);
}

async function openExperimentTab(page: Page, search = '?tab=ab-test&brand=atlas') {
  await page.goto(`/config${search}`);
  await expect(page.getByRole('tab', { name: 'A/B Test' })).toHaveAttribute('aria-selected', 'true');
}

async function selectQueries(page: Page, label: '实验组' | '对照组', queryNames: string[]) {
  const select = page.getByLabel(`${label} Query`);
  for (const queryName of queryNames) {
    await select.click();
    await select.fill(queryName);
    const dropdown = page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden)');
    await expect(dropdown).toHaveCount(1);
    await expect(dropdown).toBeVisible();
    await dropdown.locator('.ant-select-item-option').filter({ hasText: queryName }).click();
  }
}

async function selectReportFilter(page: Page, label: string, optionLabel: string) {
  await page.getByLabel(label).click();
  const dropdown = page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden)');
  await expect(dropdown).toHaveCount(1);
  await dropdown.locator('.ant-select-item-option').filter({ hasText: optionLabel }).click();
}

test.describe('A/B Test workbench (offline API contract)', () => {
  test('records unmatched API requests so legacy report endpoints cannot pass silently', async ({ context, page }) => {
    await seedSession(context, true);
    const network = await installApiMocks(context, { status: 'running' });
    await openExperimentTab(page);

    const status = await page.evaluate(async () => (
      await fetch('/api/dashboard/top-query/experiments/7/overview')
    ).status);

    expect(status).toBe(404);
    expect(network.unexpectedApiRequests).toEqual([
      'GET /dashboard/top-query/experiments/7/overview',
    ]);
    expect(() => assertOfflineAudit(network)).toThrow();
  });

  test('creates fixed 20+20 samples, disables cross-group query, and schedules the draft', async ({ context, page }) => {
    await seedSession(context, true);
    const network = await installApiMocks(context, { status: null });
    await openExperimentTab(page);

    await expect(page.getByRole('tab', { name: '品牌配置' })).toBeVisible();
    await selectQueries(page, '实验组', samplePool.slice(0, 20).map((item) => item.queryText));
    await page.getByLabel('对照组 Query').click();
    const dropdown = page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden)');
    await expect(dropdown).toHaveCount(1);
    await expect(dropdown.locator('.ant-select-item-option').filter({ hasText: 'Query 01' })).toHaveAttribute('aria-disabled', 'true');
    await page.keyboard.press('Escape');
    await selectQueries(page, '对照组', samplePool.slice(20, 40).map((item) => item.queryText));
    await expect(page.locator('.experiment-selector__label').filter({ hasText: '实验组' })).toContainText('20/20');
    await expect(page.locator('.experiment-selector__label').filter({ hasText: '对照组' })).toContainText('20/20');

    await page.getByRole('button', { name: '保存草稿' }).click();
    await expect(page).toHaveURL(/experimentId=9/);
    await expect(page.getByText('请先保存草稿后再排期')).toHaveCount(0);
    await page.getByRole('button', { name: '确认排期' }).click();
    await expect(page.getByText('实验已排期', { exact: true })).toBeVisible();
    assertOfflineAudit(network);
  });

  test('shows running quality gate, net effect, governance coverage, and query evidence', async ({ context, page }) => {
    await seedSession(context, true);
    const network = await installApiMocks(context, { status: 'running' });
    await openExperimentTab(page);

    await expect(page.getByText('实验质量正常')).toBeVisible();
    await expect(page.getByText('40/40')).toBeVisible();
    await expect(page.getByRole('heading', { name: '1. 效果结论' })).toBeVisible();
    await expect(page.getByText('CTR 净效果')).toBeVisible();
    await expect(page.getByText('CVR 净效果')).toBeVisible();
    await expect(page.getByText('截至数据日')).toBeVisible();
    await expect(page.getByText('CTR 正向效果天数')).toBeVisible();
    await expect(page.getByText('预估 Click UV 增量')).toBeVisible();
    const trendChart = page.getByRole('img', { name: '实验效果趋势图' });
    await expect(trendChart).toBeVisible();
    await expect(trendChart.getByLabel('图例')).toContainText('CTR 净变化');
    await expect(trendChart.getByLabel('图例')).toContainText('CVR 净变化');
    await expect(page.getByText('治理覆盖率')).toBeVisible();
    await expect(page.getByText('Query 01', { exact: true }).first()).toBeVisible();
    await selectReportFilter(page, '分组筛选', '对照组');
    await selectReportFilter(page, '效果方向筛选', '负向');
    await selectReportFilter(page, '质量状态筛选', '质量完整');
    await expect(page.getByText('Query 21', { exact: true })).toBeVisible();
    await expect(page.getByText('Query 40', { exact: true })).toHaveCount(0);
    await expect(page).toHaveURL(/view=effect/);
    expect(network.reportRequests).toEqual([7]);
    assertOfflineAudit(network);
  });

  test('suppresses effect conclusion when only 39 of 40 samples are observed', async ({ context, page }) => {
    await seedSession(context, true);
    const network = await installApiMocks(context, { status: 'running', quality: { observedSamples: 39 } });
    await openExperimentTab(page);

    await expect(page.locator('.ant-tag').filter({ hasText: /^实验质量异常$/ })).toBeVisible();
    await expect(page.getByText('39/40')).toBeVisible();
    await expect(page.getByText('CTR 净效果')).toHaveCount(0);
    assertOfflineAudit(network);
  });

  test('shows control contamination and suppresses effect conclusion', async ({ context, page }) => {
    await seedSession(context, true);
    const network = await installApiMocks(context, { status: 'running', quality: { controlTaskCount: 1, controlActionCount: 1, contaminationCount: 1 } });
    await openExperimentTab(page);

    await expect(page.locator('.ant-tag').filter({ hasText: /^实验质量异常$/ })).toBeVisible();
    await expect(page.getByText('污染 1')).toBeVisible();
    await expect(page.getByText('CTR 净效果')).toHaveCount(0);
    assertOfflineAudit(network);
  });

  test('opens an ended effect report and exports server-generated Excel, CSV, and ZIP', async ({ context, page }) => {
    await seedSession(context, true);
    const network = await installApiMocks(context, { status: 'ended', selectedId: 7 });
    await openExperimentTab(page, '?tab=ab-test&brand=atlas&experimentId=7');

    await expect(page.getByText('ATLAS A/B Test #7').first()).toBeVisible();
    await expect(page.getByText('已结束').first()).toBeVisible();
    await expect(page.getByRole('heading', { name: '1. 效果结论' })).toBeVisible();
    const excelDownloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: /导出 Excel/ }).click();
    const excelDownload = await excelDownloadPromise;
    expect(excelDownload.suggestedFilename()).toBe('ab-test-7.xlsx');
    expect(await readDownloadPrefix(excelDownload)).toEqual(Buffer.from([0x50, 0x4b, 0x03, 0x04]));

    const csvDownloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: '更多导出格式' }).click();
    await page.getByText('导出 CSV', { exact: true }).click();
    const csvDownload = await csvDownloadPromise;
    expect(csvDownload.suggestedFilename()).toBe('ab-test-7.csv');

    const zipDownloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: '更多导出格式' }).click();
    await page.getByText('导出 ZIP', { exact: true }).click();
    const zipDownload = await zipDownloadPromise;
    expect(zipDownload.suggestedFilename()).toBe('ab-test-7.zip');
    expect(await readDownloadPrefix(zipDownload)).toEqual(Buffer.from([0x50, 0x4b, 0x03, 0x04]));
    await expect.poll(() => network.downloads).toEqual(['xlsx', 'csv', 'zip']);
    expect(network.exportRequests).toEqual([
      { method: 'POST', body: { format: 'xlsx', snapshotToken: 'snapshot-7-ended' } },
      { method: 'POST', body: { format: 'csv', snapshotToken: 'snapshot-7-ended' } },
      { method: 'POST', body: { format: 'zip', snapshotToken: 'snapshot-7-ended' } },
    ]);
    expect(network.reportRequests).toContain(7);
    const historyItem = page.locator('.experiment-history__list button')
      .filter({ has: page.getByText('ATLAS A/B Test #6', { exact: true }) });
    await expect(historyItem).toHaveCount(1);
    await historyItem.click();
    await expect(page).toHaveURL(/experimentId=6/);
    assertOfflineAudit(network);
  });

  test('keeps operator access read-only while retaining report and export visibility', async ({ context, page }) => {
    await seedSession(context, false);
    const network = await installApiMocks(context, { status: 'running', canManage: false });
    await openExperimentTab(page);

    await expect(page.getByRole('tab', { name: '品牌配置' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: '结束实验' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /导出 Excel/ })).toBeVisible();
    await expect(page.getByRole('button', { name: '更多导出格式' })).toBeVisible();
    await expect(page.getByText('实验质量正常')).toBeVisible();
    assertOfflineAudit(network);
  });

  test('renders the running report in a narrow mobile viewport without horizontal overflow', async ({ context, page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await seedSession(context, false);
    const network = await installApiMocks(context, { status: 'running', canManage: false });
    await openExperimentTab(page);

    await expect(page.getByText('数据质量门禁')).toBeVisible();
    await expect(page.getByText('CTR 净效果')).toBeVisible();
    await expect(page.locator('.experiment-report .experiment-mobile-query-card')).not.toHaveCount(0);
    await selectReportFilter(page, '分组筛选', '实验组');
    await selectReportFilter(page, '效果方向筛选', '效果分化');
    await selectReportFilter(page, '质量状态筛选', '质量完整');
    const queryEffectSection = page.locator('.experiment-report__section').filter({
      has: page.getByRole('heading', { name: '3. Query 综合效果' }),
    });
    const queryEffectCards = queryEffectSection.locator('.experiment-mobile-query-card');
    await expect(queryEffectCards.filter({ hasText: 'Query 02' })).toHaveCount(1);
    await expect(queryEffectCards.filter({ hasText: 'Query 01' })).toHaveCount(0);
    await expect(page).toHaveURL(/view=effect/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
    assertOfflineAudit(network);
  });
});
