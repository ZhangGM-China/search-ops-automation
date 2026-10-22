import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { useBrandAssistantSession } from './useBrandAssistantSession'
import { dashboardApi } from '@/shared/api'

vi.mock('@/shared/api', () => ({ dashboardApi: {
  getAssistantContext: vi.fn(), streamAssistantMessage: vi.fn(), sendAssistantMessage: vi.fn(),
  prepareAssistantAction: vi.fn(), confirmAssistantAction: vi.fn(), cancelAssistantAction: vi.fn(),
} }))

describe('useBrandAssistantSession', () => {
  beforeEach(() => vi.clearAllMocks())
  const options = { open: true, pageKey: 'brand_detail' as const, brand: 'Atlas', date: '2026-09-08' }

  it('loads context and selects preferred binding', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: {
      runtime: { modelName: 'test-model' }, availableBindings: [{ agentBindingId: 2, brand: 'Atlas', modelName: 'atlas-model' }],
      summary: { taskCount: 1, pendingCount: 1, actionCount: 0, monitoringCount: 0 }, messages: [], suggestedPrompts: ['帮助'],
    } } as never)
    const { result } = renderHook(() => useBrandAssistantSession(options))
    await waitFor(() => expect(result.current.suggestedPrompts).toContain('帮助'))
  })

  it('falls back to non-stream response and exposes proposed actions', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: { availableBindings: [], messages: [], suggestedPrompts: [] } } as never)
    vi.mocked(dashboardApi.streamAssistantMessage).mockRejectedValue(new Error('stream failed'))
    vi.mocked(dashboardApi.sendAssistantMessage).mockResolvedValue({ data: { message: '答复', proposedActions: [{ actionType: 'x', target: 'y' }] } } as never)
    const { result } = renderHook(() => useBrandAssistantSession(options))
    await waitFor(() => expect(result.current.loading).toBe(false))
    await act(async () => { await result.current.handleSend('  查询状态  ') })
    expect(result.current.messages.at(-1)?.content).toBe('答复')
    expect(result.current.proposedActions).toHaveLength(1)
  })

  it('ignores empty sends and clears failed fallback messages', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: { availableBindings: [], messages: [], suggestedPrompts: [] } } as never)
    vi.mocked(dashboardApi.streamAssistantMessage).mockRejectedValue(new Error('bad'))
    vi.mocked(dashboardApi.sendAssistantMessage).mockRejectedValue(new Error('bad2'))
    const { result } = renderHook(() => useBrandAssistantSession(options))
    await waitFor(() => expect(result.current.loading).toBe(false))
    await act(async () => { await result.current.handleSend(' ') })
    expect(dashboardApi.streamAssistantMessage).not.toHaveBeenCalled()
    await act(async () => { await result.current.handleSend('失败') })
    expect(result.current.messages).toHaveLength(0)
  })
})
