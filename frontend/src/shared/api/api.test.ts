import { beforeEach, describe, expect, it, vi } from 'vitest';

const axiosState = vi.hoisted(() => {
  const requestUse = vi.fn();
  const responseUse = vi.fn();
  const api = {
    interceptors: {
      request: { use: requestUse },
      response: { use: responseUse },
    },
    get: vi.fn().mockResolvedValue({ data: {} }),
    post: vi.fn().mockResolvedValue({ data: {} }),
    put: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  };
  return {
    api,
    createConfig: undefined as
      | { paramsSerializer: { serialize: (params: Record<string, unknown>) => string } }
      | undefined,
    requestUse,
    responseUse,
  };
});

vi.mock('axios', () => ({
  default: {
    create: vi.fn((config) => {
      axiosState.createConfig = config;
      return axiosState.api;
    }),
  },
}));

const { adminApi, authApi, dashboardApi } = await import('./index');

type TransportMethod = 'get' | 'post' | 'put' | 'patch' | 'delete';
const transportMethods: TransportMethod[] = ['get', 'post', 'put', 'patch', 'delete'];
type WrapperCase = {
  name: string;
  method: TransportMethod;
  invoke: () => unknown;
  expected: unknown[];
};

const signal = new AbortController().signal;
const wrapperCases: WrapperCase[] = [
  { name: 'submits manual relevance verification', method: 'post', invoke: () => dashboardApi.submitManualRelevanceCheck('atlas', { query: 'dress', products: ['sku-1'] }, 'manual-key'), expected: ['/query-governance/relevance-checks', { brand: 'atlas', queries: [{ query: 'dress', products: ['sku-1'] }] }, { headers: { 'Idempotency-Key': 'manual-key' } }] },
  { name: 'loads manual relevance verification', method: 'get', invoke: () => dashboardApi.getManualRelevanceCheck('relchk-1'), expected: ['/query-governance/relevance-checks/relchk-1', {}] },
  { name: 'loads latest manual relevance verification', method: 'get', invoke: () => dashboardApi.getLatestManualRelevanceCheck('atlas', 'dress'), expected: ['/query-governance/relevance-checks/latest', { params: { brand: 'atlas', query: 'dress' }, signal: undefined }] },
  { name: 'logs in', method: 'post', invoke: () => authApi.login({ username: 'ops', password: 'secret' }), expected: ['/auth/login', { username: 'ops', password: 'secret' }] },
  { name: 'loads current user', method: 'get', invoke: () => authApi.getMe(), expected: ['/auth/me'] },
  { name: 'logs out through the server', method: 'post', invoke: () => authApi.logout(), expected: ['/auth/logout'] },
  { name: 'changes password', method: 'post', invoke: () => authApi.changePassword({ oldPassword: 'old', newPassword: 'new' }), expected: ['/auth/change-password', { oldPassword: 'old', newPassword: 'new' }] },
  { name: 'loads users', method: 'get', invoke: () => adminApi.getUsers(), expected: ['/admin/users'] },
  { name: 'creates user', method: 'post', invoke: () => adminApi.createUser({ username: 'new-user' }), expected: ['/admin/users', { username: 'new-user' }] },
  { name: 'updates user', method: 'put', invoke: () => adminApi.updateUser(7, { isActive: false }), expected: ['/admin/users/7', { isActive: false }] },
  { name: 'deletes user', method: 'delete', invoke: () => adminApi.deleteUser(7), expected: ['/admin/users/7'] },
  { name: 'resets password', method: 'post', invoke: () => adminApi.resetPassword(7), expected: ['/admin/users/7/reset-password'] },
  { name: 'loads app config', method: 'get', invoke: () => adminApi.getConfig(), expected: ['/admin/config'] },
  { name: 'loads experiment brand status with cancellation', method: 'get', invoke: () => dashboardApi.getTopQueryExperimentBrandStatus('atlas', { page: 1, pageSize: 20 }, signal), expected: ['/dashboard/top-query/experiments/brands/atlas', { params: { page: 1, pageSize: 20 }, signal }] },
  { name: 'loads experiment sample pool', method: 'get', invoke: () => dashboardApi.getTopQueryExperimentSamplePool('atlas', { limit: 20 }, signal), expected: ['/dashboard/top-query/experiments/brands/atlas/sample-pool', { params: { limit: 20 }, signal }] },
  { name: 'creates experiment draft', method: 'post', invoke: () => dashboardApi.createTopQueryExperimentDraft('atlas', { experimentName: 'ranking test', treatmentQueries: ['dress'], controlQueries: ['coat'] }), expected: ['/dashboard/top-query/experiments/brands/atlas/draft', { experimentName: 'ranking test', treatmentQueries: ['dress'], controlQueries: ['coat'] }] },
  { name: 'schedules experiment', method: 'post', invoke: () => dashboardApi.scheduleTopQueryExperiment(21), expected: ['/dashboard/top-query/experiments/21/schedule'] },
  { name: 'ends experiment', method: 'post', invoke: () => dashboardApi.endTopQueryExperiment(21), expected: ['/dashboard/top-query/experiments/21/end'] },
  { name: 'cancels experiment', method: 'post', invoke: () => dashboardApi.cancelTopQueryExperiment(21), expected: ['/dashboard/top-query/experiments/21/cancel'] },
  { name: 'loads experiment detail', method: 'get', invoke: () => dashboardApi.getTopQueryExperimentDetail(21, { asOfDate: '2026-07-19' }, signal), expected: ['/dashboard/top-query/experiments/21', { params: { asOfDate: '2026-07-19' }, signal }] },
  { name: 'loads experiment report', method: 'get', invoke: () => dashboardApi.getTopQueryExperimentReport(21, signal), expected: ['/dashboard/top-query/experiments/21/report', { signal }] },
  { name: 'exports live experiment snapshot through GET', method: 'get', invoke: () => dashboardApi.exportTopQueryExperiment(21, 'csv', signal), expected: ['/dashboard/top-query/experiments/21/export', { params: { format: 'csv' }, responseType: 'blob', signal }] },
  { name: 'exports frozen experiment snapshot through POST', method: 'post', invoke: () => dashboardApi.exportTopQueryExperiment(21, 'zip', signal, 'snapshot-1'), expected: ['/dashboard/top-query/experiments/21/export', { format: 'zip', snapshotToken: 'snapshot-1' }, { responseType: 'blob', signal }] },
  { name: 'approves top-query action', method: 'post', invoke: () => dashboardApi.approveTopQueryAction(31, { reason: 'verified' }), expected: ['/dashboard/top-query/actions/31/approve', { reason: 'verified' }] },
  { name: 'executes top-query action', method: 'post', invoke: () => dashboardApi.executeTopQueryAction(31, { dryRun: false, expectedConfigVersion: 'v2' }), expected: ['/dashboard/top-query/actions/31/execute', { dryRun: false, expectedConfigVersion: 'v2' }] },
  { name: 'previews task actions', method: 'get', invoke: () => dashboardApi.previewTopQueryTaskActions(11), expected: ['/dashboard/top-query/tasks/11/execute-action-preview'] },
  { name: 'executes selected task actions', method: 'post', invoke: () => dashboardApi.executeTopQueryTaskActions(11, { dryRun: true, actionIds: [31] }), expected: ['/dashboard/top-query/tasks/11/execute-action', { dryRun: true, actionIds: [31] }] },
  { name: 'executes a KG governance plan', method: 'post', invoke: () => dashboardApi.executeQueryGovernanceKGPlan(41), expected: ['/query-governance/kg-plans/41/execute'] },
  { name: 'rolls back top-query action', method: 'post', invoke: () => dashboardApi.rollbackTopQueryAction(31), expected: ['/dashboard/top-query/actions/31/rollback'] },
  { name: 'rejects top-query action', method: 'post', invoke: () => dashboardApi.rejectTopQueryAction(31, { reason: 'unsafe' }), expected: ['/dashboard/top-query/actions/31/reject', { reason: 'unsafe' }] },
  { name: 'marks task processed', method: 'post', invoke: () => dashboardApi.markTopQueryTaskProcessed(11), expected: ['/dashboard/top-query/tasks/11/mark-processed'] },
  { name: 'loads assistant context', method: 'get', invoke: () => dashboardApi.getAssistantContext({ brand: 'atlas' }), expected: ['/dashboard/assistant-context', { params: { brand: 'atlas' } }] },
  { name: 'loads assistant sessions', method: 'get', invoke: () => dashboardApi.getAssistantSessions(), expected: ['/assistant/sessions'] },
  { name: 'loads assistant workspace', method: 'get', invoke: () => dashboardApi.getAssistantWorkspace('src'), expected: ['/assistant/workspace', { params: { path: 'src' } }] },
  { name: 'previews assistant workspace file', method: 'get', invoke: () => dashboardApi.previewAssistantWorkspaceFile('README.md'), expected: ['/assistant/workspace/file', { params: { path: 'README.md' } }] },
  { name: 'uploads assistant workspace file', method: 'post', invoke: () => dashboardApi.uploadAssistantWorkspaceFile('notes.txt', new File(['hello'], 'notes.txt', { type: 'text/plain' })), expected: ['/assistant/workspace/file', expect.any(FormData), { params: { path: 'notes.txt' } }] },
  { name: 'updates assistant session', method: 'patch', invoke: () => dashboardApi.updateAssistantSession(51, { pinned: true }), expected: ['/assistant/sessions/51', { pinned: true }] },
  { name: 'deletes assistant session', method: 'delete', invoke: () => dashboardApi.deleteAssistantSession(51), expected: ['/assistant/sessions/51'] },
  { name: 'confirms assistant action without a request body', method: 'post', invoke: () => dashboardApi.confirmAssistantAction(61), expected: ['/assistant/actions/61/confirm'] },
  { name: 'cancels assistant action without a request body', method: 'post', invoke: () => dashboardApi.cancelAssistantAction(61), expected: ['/assistant/actions/61/cancel'] },
  { name: 'sends assistant message with abort signal', method: 'post', invoke: () => dashboardApi.sendAssistantMessage({ message: 'status' }, { signal }), expected: ['/assistant/chat', { message: 'status' }, { signal }] },
  { name: 'loads no-result governance', method: 'get', invoke: () => dashboardApi.getNoResultGovernance({ brand: 'atlas' }), expected: ['/dashboard/no-result-governance', { params: { brand: 'atlas' } }] },
  { name: 'rolls back no-result batch', method: 'post', invoke: () => dashboardApi.rollbackNoResultBatch(71), expected: ['/dashboard/no-result-governance/batches/71/rollback'] },
  { name: 'approves no-result action with reason payload', method: 'post', invoke: () => dashboardApi.approveNoResultAction(72, 'verified'), expected: ['/dashboard/no-result-governance/actions/72/approve', { reason: 'verified' }] },
  { name: 'starts no-result execution', method: 'post', invoke: () => dashboardApi.startNoResultQueryExecution(73, 'approved'), expected: ['/dashboard/no-result-governance/queries/73/execution/start', { reason: 'approved' }] },
  { name: 'cancels blacklist', method: 'post', invoke: () => dashboardApi.cancelBlacklist({ brand: 'atlas', query: 'dress', sku: 'sku-1' }), expected: ['/dashboard/actions/cancel-blacklist', { brand: 'atlas', query: 'dress', sku: 'sku-1' }] },
  { name: 'blacklists product', method: 'post', invoke: () => dashboardApi.blacklistProduct({ brand: 'atlas', query: 'dress', sku: 'sku-1', reason: 'irrelevant' }), expected: ['/dashboard/actions/blacklist', { brand: 'atlas', query: 'dress', sku: 'sku-1', reason: 'irrelevant' }] },
  { name: 'loads relevance guidance', method: 'get', invoke: () => dashboardApi.getRelevanceGuidance('atlas', 'dress'), expected: ['/dashboard/top-query/brands/atlas/relevance-guidance', { params: { query: 'dress' } }] },
  { name: 'saves relevance guidance', method: 'put', invoke: () => dashboardApi.saveRelevanceGuidance('atlas', { query: 'dress', rules: 'exclude kids' }), expected: ['/dashboard/top-query/brands/atlas/relevance-guidance', { query: 'dress', rules: 'exclude kids' }] },
  { name: 'appends a relevance correction', method: 'post', invoke: () => dashboardApi.appendRelevanceGuidanceRule('atlas', { query: 'dress', rule: 'include jackets' }), expected: ['/dashboard/top-query/brands/atlas/relevance-guidance/append', { query: 'dress', rule: 'include jackets' }] },
  { name: 'previews relevance guidance organization', method: 'post', invoke: () => dashboardApi.previewRelevanceGuidanceOrganization('atlas', 'dress'), expected: ['/dashboard/top-query/brands/atlas/relevance-guidance/organize', { query: 'dress' }] },
  { name: 'toggles stock task status', method: 'post', invoke: () => dashboardApi.toggleStockTaskStatus(81, 'manually_stopped'), expected: ['/dashboard/stock-tasks/81/toggle-status', { status: 'manually_stopped' }] },
];

describe('API transport contract', () => {
  beforeEach(() => {
    localStorage.clear();
    for (const method of transportMethods) {
      axiosState.api[method].mockClear();
    }
  });

  it('serializes scalar and repeated query values while omitting empty values', () => {
    const serialize = axiosState.createConfig?.paramsSerializer.serialize;

    expect(serialize?.({
      brand: 'ATLAS',
      page: 2,
      active: false,
      empty: '',
      nothing: null,
      missing: undefined,
      statuses: ['pending', '', null, 'completed'],
    })).toBe('brand=ATLAS&page=2&active=false&statuses=pending&statuses=completed');
  });

  it('adds the current bearer token without changing unauthenticated requests', () => {
    const requestInterceptor = axiosState.requestUse.mock.calls[0][0] as (
      config: { headers: Record<string, string> },
    ) => { headers: Record<string, string> };

    expect(requestInterceptor({ headers: {} }).headers).toEqual({});
    localStorage.setItem('token', 'current-token');
    expect(requestInterceptor({ headers: {} }).headers).toEqual({ Authorization: 'Bearer current-token' });
  });

  it.each(wrapperCases)('$name', async ({ method, invoke, expected }) => {
    await invoke();

    expect(axiosState.api[method]).toHaveBeenCalledTimes(1);
    expect(axiosState.api[method]).toHaveBeenCalledWith(...expected);
    expect(transportMethods.reduce(
      (callCount, transportMethod) => callCount + axiosState.api[transportMethod].mock.calls.length,
      0,
    )).toBe(1);
  });
});

const createStreamResponse = (chunks: string[], ok = true, status = 200) => {
  const encodedChunks = chunks.map((chunk) => new TextEncoder().encode(chunk));
  let index = 0;
  return {
    ok,
    status,
    body: ok ? {
      getReader: () => ({
        read: vi.fn(async () => (
          index < encodedChunks.length
            ? { done: false, value: encodedChunks[index++] }
            : { done: true, value: undefined }
        )),
      }),
    } : null,
  };
};

describe('API streaming contract', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it('parses assistant deltas and tool lifecycle events without an auth header', async () => {
    const fetchMock = vi.fn().mockResolvedValue(createStreamResponse([
      'data: {"type":"delta","content":"answer"}\n\n',
      'data: {"type":"phase","phase":"thinking","label":"正在思考"}\n\n',
      'data: {"type":"tool_call","id":"call-1","name":"query_data","arguments":"{}"}\n\n',
      'data: {"type":"tool_result","id":"call-1","toolName":"query_data","status":"failed","output":"permission denied","error":"denied","exitCode":1,"durationMs":42}\n\n',
      'data: {"type":"error","content":"tool failed"}\n\n',
      'data: {"type":"done","sessionId":"session-1"}\n\n',
    ]));
    vi.stubGlobal('fetch', fetchMock);
    const handlers = {
      onDelta: vi.fn(),
      onPhase: vi.fn(),
      onToolCall: vi.fn(),
      onToolResult: vi.fn(),
      onError: vi.fn(),
      onDone: vi.fn(),
    };

    await dashboardApi.streamAssistantMessage({ message: 'status' }, handlers);

    expect(fetchMock).toHaveBeenCalledWith('/api/assistant/chat/stream', expect.objectContaining({
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'status' }),
    }));
    expect(handlers.onDelta).toHaveBeenCalledWith('answer');
    expect(handlers.onPhase).toHaveBeenCalledWith({ phase: 'thinking', label: '正在思考' });
    expect(handlers.onToolCall).toHaveBeenCalledWith({ id: 'call-1', name: 'query_data', arguments: '{}' });
    expect(handlers.onToolResult).toHaveBeenCalledWith({
      toolCallId: 'call-1',
      toolName: 'query_data',
      summary: '',
      status: 'failed',
      output: 'permission denied',
      error: 'denied',
      exitCode: 1,
      durationMs: 42,
    });
    expect(handlers.onError).toHaveBeenCalledWith('tool failed');
    expect(handlers.onDone).toHaveBeenCalledWith(expect.objectContaining({ sessionId: 'session-1' }));
  });

  it('expires the current session when the assistant stream returns 401', async () => {
    localStorage.setItem('token', 'expired-token');
    localStorage.setItem('user', JSON.stringify({ username: 'operator' }));
    window.history.pushState({}, '', '/assistant?date=2026-07-19');
    const listener = vi.fn();
    window.addEventListener('search-ops:auth-expired', listener);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(createStreamResponse([], false, 401)));

    await expect(dashboardApi.streamAssistantMessage(
      { message: 'status' },
      { onDelta: vi.fn(), onDone: vi.fn() },
    )).rejects.toThrow('assistant stream failed: 401');

    expect(localStorage.getItem('token')).toBeNull();
    expect(localStorage.getItem('user')).toBeNull();
    expect(sessionStorage.getItem('search_ops_auth_return_to')).toBe('/assistant?date=2026-07-19');
    expect(sessionStorage.getItem('search_ops_auth_expired')).toBeNull();
    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener('search-ops:auth-expired', listener);
  });

  it('rejects unsafe assistant stream responses before reading the body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(createStreamResponse([], false, 503)));

    await expect(dashboardApi.streamAssistantMessage(
      { message: 'status' },
      { onDelta: vi.fn(), onDone: vi.fn() },
    )).rejects.toThrow('assistant stream failed: 503');
  });
});
