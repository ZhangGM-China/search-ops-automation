import axios from 'axios';
import type {
  ActionLog,
  InventoryTask,
  UserInfo,
  PaginatedResponse,
  AppConfig,
  AssistantContextData,
  AssistantWorkspaceData,
  AssistantWorkspaceFilePreview,
  AssistantTerminalSession,
  AssistantRuntimeInspector,
  AssistantChatResponse,
  AssistantSessionListResponse,
  AssistantSessionRecord,
  AssistantPageContext,
  NoResultGovernanceItem,
  QueryDemotionExecutionPreview,
  QueryDemotionRollbackResponse,
  TopQueryBrandQueriesData,
  TopQueryBrandMetricTrendData,
  TopQueryBrandSummaryData,
  TopQueryBrandTrendData,
  TopQueryCurrentResultCountData,
  TopQueryDetailData,
  TopQueryEventsData,
  TopQueryExperimentDetailData,
  TopQueryExperimentReportData,
  TopQueryExperimentSamplePoolData,
  TopQueryBrandExperimentListData,
  TopQueryProductDetailData,
  TopQueryQueryTrendData,
  TopQueryTask,
  NodeModelConfigDraftPayload,
  NodeModelConfigDraftResponse,
  NodeModelConfigPublishResponse,
  NodeModelConfigResponse,
  NodeModelConfigRollbackPayload,
  NodeModelConfigRollbackResponse,
  QueryGovernanceOverviewData,
  QueryGovernanceBrandDetailData,
  RelevanceGuidance,
  ManualRelevanceCheckResponse,
  PageConfigPayload,
  PageConfigUpdatePayload,
} from '@/shared/types';

// 修改基础 URL 以涵盖所有 API
const API_BASE_URL = import.meta.env.VITE_API_BASE || '/api';
const AUTH_RETURN_TO_KEY = 'search_ops_auth_return_to';
export const AUTH_EXPIRED_EVENT = 'search-ops:auth-expired';
type QueryParams = Record<string, unknown>;
type TopQueryDateParams = { date?: string };
type TopQueryMetricTrendParams = { startDate?: string; endDate?: string };
type TopQueryBrandQueryParams = TopQueryDateParams & { page?: number; pageSize?: number; filter?: string };
type RequestOptions = { signal?: AbortSignal; includeTrend?: boolean };
type TopQueryTaskParams = { brand?: string; status?: string };
type ExperimentListParams = {
  status?: string;
  startDate?: string;
  endDate?: string;
  page?: number;
  pageSize?: number;
};
type ExperimentSamplePoolParams = { today?: string; limit?: number };
type ExperimentDetailParams = { asOfDate?: string };
type InventoryTrackingStatus = 'monitoring' | 'manually_stopped';

export type BatchBlacklistItemResult = {
  sku: string;
  status: string;
  requestId?: string;
  message?: string;
};

export type BatchBlacklistResponse = {
  code: number;
  msg: string;
  succeeded: number;
  failed: number;
  items: BatchBlacklistItemResult[];
};

const getRequestToken = (error: { config?: { headers?: unknown } }) => {
  const headers = error.config?.headers;
  if (!headers || typeof headers !== 'object') return null;

  const headersWithAuth = headers as {
    get?: (name: string) => unknown;
    Authorization?: unknown;
    authorization?: unknown;
  };
  const authorization = typeof headersWithAuth.get === 'function'
    ? headersWithAuth.get('Authorization')
    : headersWithAuth.Authorization ?? headersWithAuth.authorization;

  return typeof authorization === 'string' && authorization.startsWith('Bearer ')
    ? authorization.slice('Bearer '.length)
    : null;
};

const handleUnauthorizedResponse = (status: number | undefined, requestUrl: string, requestToken: string | null) => {
  if (status !== 401) return;

  const currentToken = localStorage.getItem('token');
  const isLoginRequest = /\/auth\/login(?:$|[?#])/.test(requestUrl);
  const isCurrentAuthenticatedRequest = Boolean(
    requestToken && currentToken && requestToken === currentToken && !isLoginRequest
  );

  // 首个 401 会同步清除 token，后续并发 401 因 token 不再匹配而天然去重；
  // 不得引入持久化去重标记，否则标记残留后所有后续会话失效都会被静默跳过。
  if (isCurrentAuthenticatedRequest) {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    if (window.location.pathname !== '/login') {
      sessionStorage.setItem(
        AUTH_RETURN_TO_KEY,
        `${window.location.pathname}${window.location.search}${window.location.hash}`
      );
    }
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  }
};

export type AssistantStreamDonePayload = {
  sessionId?: string;
  proposedActions?: AssistantChatResponse['proposedActions'];
  runtime?: AssistantChatResponse['runtime'];
  toolResults?: AssistantChatResponse['toolResults'];
};

export type AssistantStreamToolCall = {
  id?: string;
  name: string;
  arguments?: string;
};

export type AssistantStreamPhase = {
  phase: 'thinking' | 'tool_running' | 'waiting' | 'done' | 'failed' | 'cancelled';
  label?: string;
};

export type AssistantStreamHandlers = {
  onDelta: (content: string) => void;
  onDone: (payload: AssistantStreamDonePayload) => void;
  onPhase?: (phase: AssistantStreamPhase) => void;
  onToolCall?: (toolCall: AssistantStreamToolCall) => void;
  onToolResult?: (toolResult: NonNullable<AssistantChatResponse['toolResults']>[number]) => void;
  onError?: (message: string) => void;
};

type AssistantStreamEvent = {
  type?: string;
  content?: string;
  toolName?: string;
  summary?: string;
  phase?: AssistantStreamPhase['phase'];
  label?: string;
  output?: string;
  error?: string;
  status?: 'completed' | 'failed' | 'cancelled';
  exitCode?: number | null;
  durationMs?: number | null;
}
  & AssistantStreamDonePayload
  & AssistantStreamToolCall;

type AssistantMessagePayload = {
  message: string;
  sessionId?: string;
  date?: string;
  brand?: string;
  agentBindingId?: number | null;
  pageContext?: AssistantPageContext;
  retryOfMessageId?: string;
};

const api = axios.create({
  baseURL: API_BASE_URL,
  paramsSerializer: {
    serialize: (params) => {
      const searchParams = new URLSearchParams();
      for (const key in params) {
        const value = params[key];
        if (value === undefined || value === null || value === '') continue;

        if (Array.isArray(value)) {
          value.forEach((v) => {
            if (v !== undefined && v !== null && v !== '') {
              searchParams.append(key, String(v));
            }
          });
        } else {
          searchParams.append(key, String(value));
        }
      }
      return searchParams.toString();
    }
  }
});

// 请求拦截器：自动携带 Token
api.interceptors.request.use((config) => {
  const authValue = localStorage.getItem('token');
  if (authValue) {
    config.headers.Authorization = `Bearer ${authValue}`;
  }
  return config;
});

// 响应拦截器：处理 401 和错误日志
api.interceptors.response.use(
  (response) => response,
  (error) => {
    handleUnauthorizedResponse(
      error.response?.status,
      String(error.config?.url ?? ''),
      getRequestToken(error),
    );
    const cancelled = error?.code === 'ERR_CANCELED' || error?.name === 'CanceledError';
    if (!cancelled) {
      console.error('API Error:', {
        url: error.config?.url,
        method: error.config?.method,
        status: error.response?.status,
        data: error.response?.data,
        params: error.config?.params,
      });
    }
    return Promise.reject(error);
  }
);

export const authApi = {
  login: (payload: { username: string; password: string }) => api.post('/auth/login', payload),
  getMe: () => api.get<UserInfo>('/auth/me'),
  logout: () => api.post('/auth/logout'),
  changePassword: (payload: { oldPassword: string; newPassword: string }) => api.post('/auth/change-password', payload),
};

export const adminApi = {
  getUsers: () => api.get<UserInfo[]>('/admin/users'),
  createUser: (payload: Partial<UserInfo>) => api.post<UserInfo & { temporaryPassword?: string }>('/admin/users', payload),
  updateUser: (id: number, payload: Partial<UserInfo>) => api.put(`/admin/users/${id}`, payload),
  deleteUser: (id: number) => api.delete(`/admin/users/${id}`),
  resetPassword: (id: number) => api.post<{ temporaryPassword?: string }>(`/admin/users/${id}/reset-password`),
  getConfig: () => api.get('/admin/config'),
  updateConfig: (payload: AppConfig) => api.post('/admin/config', payload),
  getNodeModelConfig: () => api.get<NodeModelConfigResponse>('/admin/node-model-config'),
  createNodeModelConfigDraft: (payload: NodeModelConfigDraftPayload) => api.post<NodeModelConfigDraftResponse>('/admin/node-model-config/drafts', payload),
  publishNodeModelConfig: (version: number, payload: { expectedVersion: number | null; requestId?: string }) => api.post<NodeModelConfigPublishResponse>(`/admin/node-model-config/versions/${version}/publish`, payload),
  rollbackNodeModelConfig: (payload: NodeModelConfigRollbackPayload) => api.post<NodeModelConfigRollbackResponse>('/admin/node-model-config/rollback', payload),
  getPageConfig: () => api.get<PageConfigPayload>('/admin/page-config'),
  updatePageConfig: (payload: PageConfigUpdatePayload) => api.post<PageConfigPayload>('/admin/page-config', payload),
};

export const dashboardApi = {
  getKnowledgeGraphRelationshipDetails: (brand: string, id: string) =>
    api.get<Record<string, unknown>>(`/dashboard/top-query/brands/${encodeURIComponent(brand)}/kg-relationship-details`, { params: { id } }),
  getKnowledgeGraphTagDetails: (brand: string, name: string) =>
    api.get<{
      name: string;
      viewTag?: string | null;
      isRoot?: boolean | number | string | null;
      dimensions?: unknown;
      weight?: number | string | null;
      isEnglish?: boolean | number | string | null;
      segmentingAgain?: boolean | number | string | null;
      isCompose?: boolean | number | string | null;
      nameAnd?: unknown;
      nameNot?: unknown;
    }>(`/dashboard/top-query/brands/${encodeURIComponent(brand)}/kg-tag-details`, { params: { name } }),

  submitManualRelevanceCheck: (brand: string, payload: { query: string; products: string[] }, idempotencyKey: string) =>
    api.post<{ checkId: string; status: string }>(
      '/query-governance/relevance-checks',
      { brand, queries: [{ query: payload.query, products: payload.products }] },
      { headers: { 'Idempotency-Key': idempotencyKey } },
    ),

  getManualRelevanceCheck: (checkId: string, options: RequestOptions = {}) =>
    api.get<ManualRelevanceCheckResponse>(`/query-governance/relevance-checks/${encodeURIComponent(checkId)}`, { signal: options.signal }),

  getLatestManualRelevanceCheck: (brand: string, query: string, options: RequestOptions = {}) =>
    api.get<ManualRelevanceCheckResponse | null>('/query-governance/relevance-checks/latest', {
      params: { brand, query },
      signal: options.signal,
    }),

  getRelevanceGuidance: (brand: string, query: string) =>
    api.get<RelevanceGuidance>(`/dashboard/top-query/brands/${encodeURIComponent(brand)}/relevance-guidance`, { params: { query } }),

  listRelevanceGuidance: (brand: string) =>
    api.get<{ brand: string; items: RelevanceGuidance[] }>(`/dashboard/top-query/brands/${encodeURIComponent(brand)}/relevance-guidance`),

  saveRelevanceGuidance: (brand: string, payload: { query: string; rules: string }) =>
    api.put<RelevanceGuidance>(`/dashboard/top-query/brands/${encodeURIComponent(brand)}/relevance-guidance`, payload),

  appendRelevanceGuidanceRule: (brand: string, payload: { query: string; rule: string }) =>
    api.post<RelevanceGuidance>(`/dashboard/top-query/brands/${encodeURIComponent(brand)}/relevance-guidance/append`, payload),

  deactivateRelevanceGuidance: (brand: string, query: string) =>
    api.post<RelevanceGuidance>(`/dashboard/top-query/brands/${encodeURIComponent(brand)}/relevance-guidance/deactivate`, { query }),

  previewRelevanceGuidanceOrganization: (brand: string, query: string) =>
    api.post<RelevanceGuidance>(`/dashboard/top-query/brands/${encodeURIComponent(brand)}/relevance-guidance/organize`, { query }),

  confirmRelevanceGuidanceOrganization: (brand: string, payload: { query: string; rules: string }) =>
    api.post<RelevanceGuidance>(`/dashboard/top-query/brands/${encodeURIComponent(brand)}/relevance-guidance/confirm`, payload),

  getTopQueryOverview: (params: TopQueryDateParams = {}, options: RequestOptions = {}) =>
    api.get<QueryGovernanceOverviewData>('/dashboard/top-query/overview', { params, signal: options.signal }),

  getTopQueryBrandDetail: (brand: string, params: TopQueryDateParams = {}) =>
    api.get<QueryGovernanceBrandDetailData>(`/dashboard/top-query/brands/${brand}`, { params }),

  getTopQueryBrandSummary: (brand: string, params: TopQueryDateParams = {}, options: RequestOptions = {}) =>
    api.get<TopQueryBrandSummaryData>(`/dashboard/top-query/brands/${brand}/summary`, { params, signal: options.signal }),

  getTopQueryBrandQueries: (brand: string, params: TopQueryBrandQueryParams = {}, options: RequestOptions = {}) =>
    api.get<TopQueryBrandQueriesData>(`/dashboard/top-query/brands/${brand}/queries`, { params, signal: options.signal }),

  getTopQueryBrandTrend: (brand: string, params: TopQueryDateParams = {}, options: RequestOptions = {}) =>
    api.get<TopQueryBrandTrendData>(`/dashboard/top-query/brands/${brand}/trend`, { params, signal: options.signal }),

  getTopQueryBrandMetricTrend: (brand: string, params: TopQueryMetricTrendParams = {}, options: RequestOptions = {}) =>
    api.get<TopQueryBrandMetricTrendData>(`/dashboard/top-query/brands/${brand}/metric-trend`, { params, signal: options.signal }),

  getTopQueryTasks: (params: TopQueryTaskParams = {}) =>
    api.get<PaginatedResponse<TopQueryTask>>('/dashboard/top-query/tasks', { params }),

  getTopQueryDetail: (taskId: number, options: RequestOptions = {}) =>
    api.get<TopQueryDetailData>(`/dashboard/top-query/queries/${taskId}`, { signal: options.signal }),

  getTopQueryWorkspace: (taskId: number, options: RequestOptions = {}) =>
    api.get<TopQueryDetailData>(`/dashboard/top-query/queries/${taskId}/workspace`, {
      signal: options.signal,
      params: options.includeTrend === undefined ? undefined : { includeTrend: options.includeTrend },
    }),

  executeQueryGovernanceKGPlan: (planId: number) =>
    api.post<{ status: string; plan_id?: number }>(`/query-governance/kg-plans/${planId}/execute`),

  getTopQueryQueryTrend: (taskId: number, options: RequestOptions = {}) =>
    api.get<TopQueryQueryTrendData>(`/dashboard/top-query/queries/${taskId}/trend`, { signal: options.signal }),

  getTopQueryCurrentResultCount: (taskId: number, options: RequestOptions = {}) =>
    api.get<TopQueryCurrentResultCountData>(`/dashboard/top-query/queries/${taskId}/current-result-count`, { signal: options.signal }),

  getTopQueryProductDetail: (brand: string, productId: string, taskId?: number) =>
    api.get<TopQueryProductDetailData>(
      `/dashboard/top-query/brands/${brand}/products/${encodeURIComponent(productId)}`,
      { params: taskId ? { taskId } : undefined },
    ),

  getTopQueryEvents: (taskId: number, options: RequestOptions = {}) =>
    api.get<TopQueryEventsData>(`/dashboard/top-query/queries/${taskId}/events`, { signal: options.signal }),

  getTopQueryExperimentBrandStatus: (brand: string, params: ExperimentListParams = {}, signal?: AbortSignal) =>
    api.get<TopQueryBrandExperimentListData>(`/dashboard/top-query/experiments/brands/${brand}`, { params, signal }),

  getTopQueryExperimentHistory: (brand: string, params: ExperimentListParams = {}, signal?: AbortSignal) =>
    api.get<TopQueryBrandExperimentListData>(`/dashboard/top-query/experiments/brands/${brand}`, { params, signal }),

  getTopQueryExperimentSamplePool: (brand: string, params: ExperimentSamplePoolParams = {}, signal?: AbortSignal) =>
    api.get<TopQueryExperimentSamplePoolData>(`/dashboard/top-query/experiments/brands/${brand}/sample-pool`, { params, signal }),

  createTopQueryExperimentDraft: (
    brand: string,
    payload: {
      experimentName: string;
      treatmentQueries: string[];
      controlQueries: string[];
      today?: string;
      plannedEndDate?: string;
    },
  ) => api.post<TopQueryExperimentDetailData>(`/dashboard/top-query/experiments/brands/${brand}/draft`, payload),

  scheduleTopQueryExperiment: (experimentId: number) =>
    api.post<TopQueryExperimentDetailData>(`/dashboard/top-query/experiments/${experimentId}/schedule`),

  endTopQueryExperiment: (experimentId: number) =>
    api.post<TopQueryExperimentDetailData>(`/dashboard/top-query/experiments/${experimentId}/end`),

  cancelTopQueryExperiment: (experimentId: number) =>
    api.post<TopQueryExperimentDetailData>(`/dashboard/top-query/experiments/${experimentId}/cancel`),

  getTopQueryExperimentDetail: (experimentId: number, params: ExperimentDetailParams = {}, signal?: AbortSignal) =>
    api.get<TopQueryExperimentDetailData>(`/dashboard/top-query/experiments/${experimentId}`, { params, signal }),

  getTopQueryExperimentReport: (experimentId: number, signal?: AbortSignal) =>
    api.get<TopQueryExperimentReportData>(`/dashboard/top-query/experiments/${experimentId}/report`, { signal }),

  exportTopQueryExperiment: (experimentId: number, format: 'csv' | 'zip' | 'xlsx', signal?: AbortSignal, snapshotToken?: string) => (
    snapshotToken
      ? api.post<Blob>(`/dashboard/top-query/experiments/${experimentId}/export`, { format, snapshotToken }, { responseType: 'blob', signal })
      : api.get<Blob>(`/dashboard/top-query/experiments/${experimentId}/export`, { params: { format }, responseType: 'blob', signal })
  ),

  approveTopQueryAction: (actionId: number, payload: { reason: string }) =>
    api.post<{ action_id?: number; actionId?: number; status: string }>(`/dashboard/top-query/actions/${actionId}/approve`, payload),

  executeTopQueryAction: (actionId: number, payload: { dryRun: boolean; expectedConfigVersion?: string }) =>
    api.post<{ action_id?: number; actionId?: number; dry_run?: boolean; dryRun?: boolean }>(`/dashboard/top-query/actions/${actionId}/execute`, payload),

  previewTopQueryTaskActions: (taskId: number) =>
    api.get<QueryDemotionExecutionPreview>(
      `/dashboard/top-query/tasks/${taskId}/execute-action-preview`,
    ),

  executeTopQueryTaskActions: (
    taskId: number,
    payload: { dryRun: boolean; expectedConfigVersion?: string; actionIds?: number[] },
  ) =>
    api.post<{ taskId?: number; executedActions: Array<{ actionId: number; status: string }>; skippedActions: Array<{ actionId: number; status: string; error?: string }> }>(`/dashboard/top-query/tasks/${taskId}/execute-action`, payload),

  executeQueryGovernanceKGAction: (actionId: number, payload: { groupId?: string }) =>
    api.post<{ status: string; groupId?: string; actions?: unknown[]; errorCode?: string; errorMessage?: string }>(`/query-governance/actions/${actionId}/execute`, payload),

  rollbackTopQueryAction: (actionId: number) =>
    api.post<QueryDemotionRollbackResponse>(
      `/dashboard/top-query/actions/${actionId}/rollback`,
    ),

  rejectTopQueryAction: (actionId: number, payload: { reason: string }) =>
    api.post<{ action_id?: number; actionId?: number; status: string }>(`/dashboard/top-query/actions/${actionId}/reject`, payload),

  markTopQueryTaskProcessed: (taskId: number) =>
    api.post<{ task_id?: number; taskId?: number; status: string }>(`/dashboard/top-query/tasks/${taskId}/mark-processed`),

  getAssistantContext: (params: QueryParams = {}) =>
    api.get<AssistantContextData>('/dashboard/assistant-context', { params }),

  getAssistantRuntimeInspector: () =>
    api.get<AssistantRuntimeInspector>('/assistant/runtime-inspector'),

  getAssistantSessions: () =>
    api.get<AssistantSessionListResponse>('/assistant/sessions'),

  getAssistantWorkspace: (path = '.') =>
    api.get<AssistantWorkspaceData>('/assistant/workspace', { params: { path } }),

  previewAssistantWorkspaceFile: (path: string) =>
    api.get<AssistantWorkspaceFilePreview>('/assistant/workspace/file', { params: { path } }),

  uploadAssistantWorkspaceFile: (path: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return api.post<{ path: string; name: string; size: number }>('/assistant/workspace/file', form, { params: { path } });
  },

  createAssistantTerminalSession: (payload: { cwd: string }) =>
    api.post<AssistantTerminalSession>('/assistant/terminal/sessions', payload),

  executeAssistantTerminal: (id: string, payload: { command: string }) =>
    api.post<AssistantTerminalSession>(`/assistant/terminal/sessions/${id}/execute`, payload),

  getAssistantTerminalSession: (id: string) =>
    api.get<AssistantTerminalSession>(`/assistant/terminal/sessions/${id}`),

  stopAssistantTerminal: (id: string) =>
    api.post<AssistantTerminalSession>(`/assistant/terminal/sessions/${id}/stop`),

  branchAssistantSession: (id: number, messageId: string) =>
    api.post<AssistantSessionRecord>(`/assistant/sessions/${id}/branches`, { messageId }),
  updateAssistantSession: (id: number, payload: { title?: string; pinned?: boolean }) =>
    api.patch(`/assistant/sessions/${id}`, payload),

  deleteAssistantSession: (id: number) =>
    api.delete(`/assistant/sessions/${id}`),

  prepareAssistantAction: (payload: { sessionId?: string; action: AssistantChatResponse['proposedActions'][number] }) =>
    api.post<AssistantChatResponse['proposedActions'][number]>('/assistant/actions/prepare', payload),

  confirmAssistantAction: (id: number) =>
    api.post<AssistantChatResponse['proposedActions'][number]>(`/assistant/actions/${id}/confirm`),

  cancelAssistantAction: (id: number) =>
    api.post<AssistantChatResponse['proposedActions'][number]>(`/assistant/actions/${id}/cancel`),

  sendAssistantMessage: (payload: AssistantMessagePayload, options?: { signal?: AbortSignal }) =>
    api.post<AssistantChatResponse>('/assistant/chat', payload, { signal: options?.signal }),

  streamAssistantMessage: async (
    payload: AssistantMessagePayload,
    handlers: AssistantStreamHandlers,
    options?: { signal?: AbortSignal },
  ) => {
    const authValue = localStorage.getItem('token');
    const response = await fetch(`${API_BASE_URL}/assistant/chat/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(authValue ? { Authorization: `Bearer ${authValue}` } : {}),
      },
      body: JSON.stringify(payload),
      signal: options?.signal,
    });
    handleUnauthorizedResponse(response.status, '/assistant/chat/stream', authValue);
    if (!response.ok || !response.body) {
      throw new Error(`assistant stream failed: ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() || '';
      for (const part of parts) {
        const dataLine = part.split('\n').find((line) => line.startsWith('data:'));
        if (!dataLine) continue;
        const rawData = dataLine.slice(5).trim();
        if (!rawData) continue;
        const event = JSON.parse(rawData) as AssistantStreamEvent;
        if (event.type === 'delta' && event.content) handlers.onDelta(event.content);
        if (event.type === 'phase' && event.phase) {
          handlers.onPhase?.({ phase: event.phase, label: event.label });
        }
        if (event.type === 'tool_call' && event.name) {
          handlers.onToolCall?.({
            id: event.id,
            name: event.name,
            arguments: event.arguments,
          });
        }
        if (event.type === 'tool_result' && event.toolName) {
          handlers.onToolResult?.({
            toolCallId: event.id,
            toolName: event.toolName,
            summary: event.summary || '',
            status: event.status,
            output: event.output,
            error: event.error,
            exitCode: event.exitCode,
            durationMs: event.durationMs,
          });
        }
        if (event.type === 'error') handlers.onError?.(event.content || 'assistant stream error');
        if (event.type === 'done') handlers.onDone(event);
      }
    }
  },

  getBlacklistLogs: (params: QueryParams) =>
    api.get<PaginatedResponse<ActionLog>>('/dashboard/blacklist-logs', { params }),

  getDemotedLogs: (params: QueryParams) =>
    api.get<PaginatedResponse<ActionLog>>('/dashboard/demoted-logs', { params }),

  getStockTasks: (params: QueryParams) =>
    api.get<PaginatedResponse<InventoryTask>>('/dashboard/stock-tasks', { params }),

  getNoResultGovernance: (params: QueryParams) =>
    api.get<PaginatedResponse<NoResultGovernanceItem>>('/dashboard/no-result-governance', { params }),

  rollbackNoResultBatch: (batchId: number) =>
    api.post<{ batchId: number; rolledBackActions: number; status: string }>(`/dashboard/no-result-governance/batches/${batchId}/rollback`),

  rollbackNoResultQuery: (queryId: number) =>
    api.post<{ queryId: number; rolledBackActions: number; status: string }>(`/dashboard/no-result-governance/queries/${queryId}/rollback`),

  rollbackNoResultBrandDate: (brand: string, date: string) =>
    api.post<{ brand: string; date: string; batchCount: number; rolledBackActions: number; status: string }>(
      `/dashboard/no-result-governance/brands/${brand}/dates/${date}/rollback`
    ),

	  approveNoResultAction: (actionId: number, reason: string) =>
	    api.post<{ actionId: number; status: string }>(`/dashboard/no-result-governance/actions/${actionId}/approve`, { reason }),

	  rejectNoResultAction: (actionId: number, reason: string) =>
	    api.post<{ actionId: number; status: string }>(`/dashboard/no-result-governance/actions/${actionId}/reject`, { reason }),

	  executeNoResultAction: (actionId: number, reason: string) =>
	    api.post<{ actionId: number; status: string }>(`/dashboard/no-result-governance/actions/${actionId}/execute`, { reason }),

  startNoResultQueryExecution: (queryId: number, reason: string) =>
    api.post<{ queryId: number; status: string; executedActions: number; actions: Array<{ id: number; status: string; errorMessage?: string | null }> }>(
      `/dashboard/no-result-governance/queries/${queryId}/execution/start`,
      { reason }
    ),

  pauseNoResultQueryExecution: (queryId: number, reason: string) =>
    api.post<{ queryId: number; status: string; executedActions: number; actions: Array<{ id: number; status: string; errorMessage?: string | null }> }>(
      `/dashboard/no-result-governance/queries/${queryId}/execution/pause`,
      { reason }
    ),

  resumeNoResultQueryExecution: (queryId: number, reason: string) =>
    api.post<{ queryId: number; status: string; executedActions: number; actions: Array<{ id: number; status: string; errorMessage?: string | null }> }>(
      `/dashboard/no-result-governance/queries/${queryId}/execution/resume`,
      { reason }
    ),

  cancelBlacklist: (payload: { brand: string; query: string; sku: string; reason?: string; correctionRule?: string }) =>
    api.post('/dashboard/actions/cancel-blacklist', payload),

  blacklistProduct: (payload: { brand: string; query: string; sku: string; productName?: string; reason?: string }) =>
    api.post('/dashboard/actions/blacklist', payload),

  blacklistProductBatch: (items: Array<{ brand: string; query: string; sku: string; productName?: string; reason?: string }>) =>
    api.post<BatchBlacklistResponse>('/dashboard/actions/blacklist/batch', { items }),

  cancelBlacklistBatch: (items: Array<{ brand: string; query: string; sku: string; reason?: string }>) =>
    api.post<BatchBlacklistResponse>('/dashboard/actions/cancel-blacklist/batch', { items }),

  cancelDemotion: (payload: { brand: string; query: string; sku: string; taskId?: number }) =>
    api.post('/dashboard/actions/cancel-demotion', payload),

  toggleStockTaskStatus: (taskId: number, status: InventoryTrackingStatus) =>
    api.post(`/dashboard/stock-tasks/${taskId}/toggle-status`, { status }),
};
