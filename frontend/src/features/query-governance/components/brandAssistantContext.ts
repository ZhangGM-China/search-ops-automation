import type {
  AssistantAvailableBinding,
  AssistantPageContext,
  AssistantRuntimeInfo,
  TopQueryTask,
} from '@/shared/types'

export type AssistantContextSummary = {
  taskCount: number
  pendingCount: number
  actionCount: number
  monitoringCount: number
}

type ContextMetaItem = {
  label: string
  value: number | string
}

export const DEFAULT_RUNTIME: AssistantRuntimeInfo = {
  provider: 'PI Agent',
  modelLabel: 'PI Agent',
}

const modelPreferenceScore = (binding: AssistantAvailableBinding) => {
  const modelName = String(binding.modelName || binding.modelDisplayName || '').toLowerCase()
  if (modelName.includes('qwen3.7-max')) return 100
  if (modelName.includes('glm-5.2')) return 95
  if (modelName.includes('qwen3.6-plus')) return 70
  return 80
}

export const dedupePrompts = (items: string[]) => {
  const seen = new Set<string>()
  return items.filter((item) => {
    const normalized = item.trim()
    if (!normalized || seen.has(normalized)) return false
    seen.add(normalized)
    return true
  })
}

export const pickPreferredBinding = (bindings: AssistantAvailableBinding[], brand?: string) => {
  const candidates = brand ? bindings.filter((item) => item.brand === brand) : []
  const pool = candidates.length ? candidates : bindings
  return [...pool].sort((left, right) => modelPreferenceScore(right) - modelPreferenceScore(left))[0] || null
}

export const buildPageContext = (
  pageKey: 'brand_detail' | 'brand_overview',
  brand?: string,
  date?: string,
  task?: TopQueryTask | null,
): AssistantPageContext => {
  return {
    pageKey,
    brand,
    selectedDate: date,
    selectedQuery: task?.queryText,
    selectedTaskId: task?.id,
    displayStatus: task?.displayStatus,
    resultCount: task?.resultCount ?? null,
    ctr: task?.ctr ?? null,
    cvr: task?.cvr ?? null,
    searchPv: task?.searchPv ?? null,
    stepStatuses: task?.stepStatuses?.map((step) => ({
      checkKey: step.checkKey,
      displayStatus: step.displayStatus,
      summary: step.summary ?? null,
    })),
    querySource: 'top_query',
  }
}

export const buildQueryPrompts = (task?: TopQueryTask | null) => {
  if (!task?.queryText) {
    return ['当前品牌总览有哪些需要优先处理的问题？', '请总结最近的治理结论']
  }
  return [
    '这个Top词最近的治理情况怎么样？',
    '这个Top词最近有哪些治理记录？',
    '这个Top词还有哪些没有闭环的问题？',
  ]
}

export const buildAssistantSessionId = (
  pageKey: 'brand_detail' | 'brand_overview',
  brand?: string,
  task?: TopQueryTask | null,
  date?: string,
) => `${pageKey}-${brand || 'all'}-${task?.id || 'overview'}-${date || 'today'}`

export const buildPromptLeadText = (task?: TopQueryTask | null) => (
  task?.queryText
    ? `关于「${task.queryText}」这个Top词的处理情况，有什么我能帮您的？`
    : '关于当前品牌总览，有什么我能帮您的？'
)

export const buildContextMeta = (
  summary: AssistantContextSummary,
): ContextMetaItem[] => {
  return [
    { label: '待处理', value: summary.pendingCount },
    { label: '治理动作', value: summary.actionCount },
    { label: '断码监测', value: summary.monitoringCount },
  ]
}
