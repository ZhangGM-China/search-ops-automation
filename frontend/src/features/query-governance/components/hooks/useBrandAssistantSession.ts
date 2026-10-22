import React from 'react'
import dayjs from 'dayjs'
import { message as antdMessage } from 'antd'

import { dashboardApi } from '@/shared/api'
import { getApiErrorMessage } from '@/shared/lib/error'
import type {
  AssistantAvailableBinding,
  AssistantContextMessage,
  AssistantProposedAction,
  AssistantRuntimeInfo,
  TopQueryDetailData,
  TopQueryTask,
} from '@/shared/types'

import {
  buildAssistantSessionId,
  buildContextMeta,
  buildPageContext,
  buildPromptLeadText,
  buildQueryPrompts,
  dedupePrompts,
  DEFAULT_RUNTIME,
  pickPreferredBinding,
  type AssistantContextSummary,
} from '../brandAssistantContext'

type BrandAssistantSessionOptions = {
  brand?: string
  date?: string
  detail?: TopQueryDetailData | null
  open: boolean
  pageKey: 'brand_detail' | 'brand_overview'
  task?: TopQueryTask | null
}

type ActiveAssistantRequest = {
  controller: AbortController
  sessionEpoch: number
}

const emptyContextSummary: AssistantContextSummary = {
  taskCount: 0,
  pendingCount: 0,
  actionCount: 0,
  monitoringCount: 0,
}

export const useBrandAssistantSession = ({
  brand,
  date,
  open,
  pageKey,
  task,
}: BrandAssistantSessionOptions) => {
  const [loading, setLoading] = React.useState(false)
  const [sending, setSending] = React.useState(false)
  const [draft, setDraft] = React.useState('')
  const [messages, setMessages] = React.useState<AssistantContextMessage[]>([])
  const [runtime, setRuntime] = React.useState<AssistantRuntimeInfo>(DEFAULT_RUNTIME)
  const [availableBindings, setAvailableBindings] = React.useState<AssistantAvailableBinding[]>([])
  const [selectedBindingId, setSelectedBindingId] = React.useState<number | null>(null)
  const [suggestedPrompts, setSuggestedPrompts] = React.useState<string[]>([])
  const [recentMessages, setRecentMessages] = React.useState<AssistantContextMessage[]>([])
  const [contextSummary, setContextSummary] = React.useState<AssistantContextSummary>(emptyContextSummary)
  const [proposedActions, setProposedActions] = React.useState<AssistantProposedAction[]>([])
  const activeRequestRef = React.useRef<ActiveAssistantRequest | null>(null)
  const sessionEpochRef = React.useRef(0)
  const contextRequestSequence = React.useRef(0)
  const sessionId = React.useMemo(
    () => buildAssistantSessionId(pageKey, brand, task, date),
    [brand, date, pageKey, task],
  )
  const pageContext = React.useMemo(
    () => buildPageContext(pageKey, brand, date, task),
    [brand, date, pageKey, task],
  )
  const activeBinding = React.useMemo(
    () => availableBindings.find((item) => item.agentBindingId === selectedBindingId) || null,
    [availableBindings, selectedBindingId],
  )
  const runtimeDisplayName = runtime.modelName || '模型未配置'
  const activeModelDisplayName = activeBinding?.modelName || runtimeDisplayName
  const promptLeadText = React.useMemo(() => buildPromptLeadText(task), [task])
  const contextMeta = React.useMemo(
    () => buildContextMeta(contextSummary),
    [contextSummary],
  )

  React.useLayoutEffect(() => {
    const sessionEpoch = ++sessionEpochRef.current
    const timer = window.setTimeout(() => {
      if (sessionEpoch !== sessionEpochRef.current) return
      setMessages([])
      setDraft('')
      setProposedActions([])
      setSending(false)
    }, 0)
    return () => {
      window.clearTimeout(timer)
      if (activeRequestRef.current?.sessionEpoch === sessionEpoch) {
        activeRequestRef.current.controller.abort()
        activeRequestRef.current = null
      }
    }
  }, [sessionId])

  const refreshContext = React.useCallback(async () => {
    const requestEpoch = sessionEpochRef.current
    const sequence = ++contextRequestSequence.current
    setLoading(true)
    try {
      const response = await dashboardApi.getAssistantContext({
        ...(date ? { date } : {}),
        ...(brand ? { brand } : {}),
      })
      if (requestEpoch !== sessionEpochRef.current || sequence !== contextRequestSequence.current) return
      setRuntime(response.data.runtime || DEFAULT_RUNTIME)
      const nextBindings = response.data.availableBindings || []
      setAvailableBindings(nextBindings)
      setContextSummary(response.data.summary || emptyContextSummary)
      setRecentMessages(response.data.messages || [])
      const preferredBinding = pickPreferredBinding(nextBindings, brand)
      setSelectedBindingId(preferredBinding?.agentBindingId ?? null)
      setSuggestedPrompts(dedupePrompts([
        ...buildQueryPrompts(task),
        ...(response.data.suggestedPrompts || []),
      ]).slice(0, 6))
    } catch (error: unknown) {
      if (requestEpoch !== sessionEpochRef.current || sequence !== contextRequestSequence.current) return
      void antdMessage.error(`智能助理上下文加载失败: ${getApiErrorMessage(error, '未知错误')}`)
    } finally {
      if (requestEpoch === sessionEpochRef.current && sequence === contextRequestSequence.current) {
        setLoading(false)
      }
    }
  }, [brand, date, task])

  React.useEffect(() => {
    if (!open) return
    const timer = window.setTimeout(() => {
      void refreshContext()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [open, refreshContext])

  const updateAssistantMessage = React.useCallback((assistantMessageId: string, updater: (message: AssistantContextMessage) => AssistantContextMessage) => {
    setMessages((current) => current.map((item) => (item.id === assistantMessageId ? updater(item) : item)))
  }, [])

  const isCurrentRequest = React.useCallback((request: ActiveAssistantRequest) => (
    activeRequestRef.current?.controller === request.controller
    && activeRequestRef.current.sessionEpoch === request.sessionEpoch
    && sessionEpochRef.current === request.sessionEpoch
    && !request.controller.signal.aborted
  ), [])

  const handleSend = React.useCallback(async (presetText?: string) => {
    const text = (presetText ?? draft).trim()
    if (!text || sending) return
    const operatorMessage: AssistantContextMessage = {
      id: `operator-${Date.now()}`,
      role: 'operator',
      title: '运营人员',
      content: text,
      createdAt: dayjs().format('YYYY-MM-DD HH:mm'),
    }
    const assistantMessageId = `assistant-${Date.now()}`
    const assistantMessage: AssistantContextMessage = {
      id: assistantMessageId,
      role: 'assistant',
      title: activeModelDisplayName,
      content: '',
      createdAt: dayjs().format('YYYY-MM-DD HH:mm'),
    }
    const request: ActiveAssistantRequest = {
      controller: new AbortController(),
      sessionEpoch: sessionEpochRef.current,
    }
    activeRequestRef.current?.controller.abort()
    activeRequestRef.current = request
    setMessages((current) => [...current, operatorMessage, assistantMessage])
    setDraft('')
    setSending(true)
    setProposedActions([])

    const payload = {
      message: text,
      sessionId,
      date,
      brand,
      agentBindingId: selectedBindingId,
      pageContext,
    }

    try {
      await dashboardApi.streamAssistantMessage(payload, {
        onDelta: (content) => {
          if (!isCurrentRequest(request)) return
          updateAssistantMessage(assistantMessageId, (message) => ({
            ...message,
            title: activeModelDisplayName,
            content: `${message.content}${content}`,
          }))
        },
        onDone: (donePayload) => {
          if (!isCurrentRequest(request)) return
          if (donePayload.runtime) setRuntime(donePayload.runtime)
          setProposedActions(donePayload.proposedActions || [])
        },
        onError: (errorMessage) => {
          throw new Error(errorMessage)
        },
      }, { signal: request.controller.signal })
    } catch {
      if (!isCurrentRequest(request)) return
      try {
        const response = await dashboardApi.sendAssistantMessage(payload, { signal: request.controller.signal })
        if (!isCurrentRequest(request)) return
        if (response.data.runtime) setRuntime(response.data.runtime)
        updateAssistantMessage(assistantMessageId, (message) => ({
          ...message,
          title: response.data.runtime?.modelName || activeModelDisplayName,
          content: response.data.message,
        }))
        setProposedActions(response.data.proposedActions || [])
      } catch (fallbackError: unknown) {
        if (!isCurrentRequest(request)) return
        void antdMessage.error(`智能助理发送失败: ${getApiErrorMessage(fallbackError, '未知错误')}`)
        setMessages((current) => current.filter((item) => item.id !== operatorMessage.id && item.id !== assistantMessageId))
      }
    } finally {
      if (isCurrentRequest(request)) {
        activeRequestRef.current = null
        setSending(false)
      }
    }
  }, [activeModelDisplayName, brand, date, draft, isCurrentRequest, pageContext, selectedBindingId, sending, sessionId, updateAssistantMessage])

  const handleConfirmAction = React.useCallback(async (action: AssistantProposedAction) => {
    const actionSessionEpoch = sessionEpochRef.current
    try {
      const prepared = await dashboardApi.prepareAssistantAction({ sessionId, action })
      const confirmed = await dashboardApi.confirmAssistantAction(Number(prepared.data.id))
      if (actionSessionEpoch !== sessionEpochRef.current) return
      setProposedActions((current) => current.map((item) => (
        item.actionType === action.actionType && item.target === action.target
          ? { ...item, ...confirmed.data }
          : item
      )))
    } catch (error: unknown) {
      if (actionSessionEpoch !== sessionEpochRef.current) return
      void antdMessage.error(`确认执行失败: ${getApiErrorMessage(error, '未知错误')}`)
    }
  }, [sessionId])

  const handleCancelAction = React.useCallback(async (action: AssistantProposedAction) => {
    if (!action.id) return
    const actionSessionEpoch = sessionEpochRef.current
    try {
      const cancelled = await dashboardApi.cancelAssistantAction(action.id)
      if (actionSessionEpoch !== sessionEpochRef.current) return
      setProposedActions((current) => current.map((item) => (
        item.id === action.id ? { ...item, ...cancelled.data } : item
      )))
    } catch (error: unknown) {
      if (actionSessionEpoch !== sessionEpochRef.current) return
      void antdMessage.error(`取消动作失败: ${getApiErrorMessage(error, '未知错误')}`)
    }
  }, [])

  return {
    contextMeta,
    displayedRecentMessages: recentMessages,
    draft,
    handleCancelAction,
    handleConfirmAction,
    handleSend,
    loading,
    messages,
    promptLeadText,
    proposedActions,
    sending,
    setDraft,
    suggestedPrompts,
  }
}

export type BrandAssistantSessionModel = ReturnType<typeof useBrandAssistantSession>
