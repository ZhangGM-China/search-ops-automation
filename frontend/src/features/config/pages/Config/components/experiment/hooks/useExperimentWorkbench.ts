import React from 'react'
import dayjs, { type Dayjs } from 'dayjs'
import { message } from 'antd'

import { dashboardApi } from '@/shared/api'
import useMobile from '@/shared/hooks/useMobile'
import type {
  TopQueryBrandExperimentListData,
  TopQueryExperimentDetailData,
  TopQueryExperimentReportData,
  TopQueryExperimentSamplePoolData,
} from '@/shared/types'

import { downloadBlob, downloadFileName } from '../experimentDownload'

export interface ExperimentWorkbenchProps {
  brand: string
  canManage: boolean
  embedded?: boolean
  experimentId?: number
  onExperimentChange?: (experimentId?: number, mode?: 'push' | 'replace') => void
  onDirtyChange?: (dirty: boolean) => void
  confirmNavigation?: () => boolean
  view?: 'config' | 'effect'
  onViewChange?: (view: 'config' | 'effect') => void
}

type ApiErrorLike = { response?: { status?: number; data?: { detail?: string } } }

type HistoryFilters = {
  status?: string
  startDate?: string
  endDate?: string
  page: number
  pageSize: number
}

type ExperimentMutation = {
  brand: string
  navigationEpoch: number
  navigationExperimentId?: number
  sequence: number
}

const apiErrorDetail = (error: unknown, fallback: string) => (
  (error as ApiErrorLike)?.response?.data?.detail || fallback
)

const isPermissionError = (error: unknown) => (error as ApiErrorLike)?.response?.status === 403

export const useExperimentWorkbench = ({
  brand,
  canManage,
  experimentId,
  onExperimentChange,
  onDirtyChange,
  confirmNavigation,
  view = 'config',
  onViewChange,
}: ExperimentWorkbenchProps) => {
  const [messageApi, messageContextHolder] = message.useMessage()
  const isMobile = useMobile()
  const [loading, setLoading] = React.useState(true)
  const [submitting, setSubmitting] = React.useState(false)
  const [exporting, setExporting] = React.useState(false)
  const [permissionDenied, setPermissionDenied] = React.useState(false)
  const [requestError, setRequestError] = React.useState('')
  const [inlineError, setInlineError] = React.useState('')
  const [currentSummary, setCurrentSummary] = React.useState<TopQueryBrandExperimentListData['current']>(null)
  const [historyListing, setHistoryListing] = React.useState<Pick<TopQueryBrandExperimentListData, 'items' | 'total' | 'page' | 'pageSize'>>({
    items: [],
    total: 0,
    page: 1,
    pageSize: 20,
  })
  const [samplePool, setSamplePool] = React.useState<TopQueryExperimentSamplePoolData | null>(null)
  const [sampleError, setSampleError] = React.useState('')
  const [detail, setDetail] = React.useState<TopQueryExperimentDetailData | null>(null)
  const [report, setReport] = React.useState<TopQueryExperimentReportData | null>(null)
  const [treatmentQueries, setTreatmentQueries] = React.useState<string[]>([])
  const [controlQueries, setControlQueries] = React.useState<string[]>([])
  const [plannedEndDate, setPlannedEndDate] = React.useState<Dayjs>(() => dayjs().add(7, 'day').startOf('day'))
  const [dirty, setDirty] = React.useState(false)
  const [historyFilters, setHistoryFilters] = React.useState<HistoryFilters>({ page: 1, pageSize: 20 })
  const [historyReadyBrand, setHistoryReadyBrand] = React.useState<string>()
  const activeBrandRef = React.useRef<string | undefined>(undefined)
  const historyBrandRef = React.useRef<string | undefined>(undefined)
  const initialWorkspaceBrandRef = React.useRef<string | undefined>(undefined)
  const loadedCurrentKeyRef = React.useRef<string | undefined>(undefined)
  const loadedHistoryKeyRef = React.useRef<string | undefined>(undefined)
  const currentRequestSequence = React.useRef(0)
  const historyRequestSequence = React.useRef(0)
  const currentAbortController = React.useRef<AbortController | null>(null)
  const historyAbortController = React.useRef<AbortController | null>(null)
  const mountedRef = React.useRef(false)
  const renderedBrandRef = React.useRef(brand)
  const renderedExperimentIdRef = React.useRef(experimentId)
  const navigationEpochRef = React.useRef(0)
  const mutationSequence = React.useRef(0)
  const mutationInFlightRef = React.useRef(false)
  const exportRequestSequence = React.useRef(0)
  const exportAbortController = React.useRef<AbortController | null>(null)
  const exportInFlightRef = React.useRef(false)

  React.useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      navigationEpochRef.current += 1
      mutationSequence.current += 1
      mutationInFlightRef.current = false
      exportRequestSequence.current += 1
      exportAbortController.current?.abort()
      exportInFlightRef.current = false
    }
  }, [])

  React.useLayoutEffect(() => {
    let active = true
    renderedBrandRef.current = brand
    renderedExperimentIdRef.current = experimentId
    navigationEpochRef.current += 1
    mutationSequence.current += 1
    mutationInFlightRef.current = false
    exportRequestSequence.current += 1
    exportAbortController.current?.abort()
    exportInFlightRef.current = false
    queueMicrotask(() => {
      if (active && mountedRef.current && renderedBrandRef.current === brand) {
        setSubmitting(false)
        setExporting(false)
      }
    })
    return () => { active = false }
  }, [brand, experimentId])

  const applyDetail = React.useCallback((nextDetail: TopQueryExperimentDetailData | null) => {
    setDetail(nextDetail)
    setTreatmentQueries(nextDetail?.samples.filter((item) => item.groupType === 'treatment').map((item) => item.queryText) ?? [])
    setControlQueries(nextDetail?.samples.filter((item) => item.groupType === 'control').map((item) => item.queryText) ?? [])
    const plannedStart = nextDetail?.plannedStartDate ? dayjs(nextDetail.plannedStartDate) : dayjs().add(1, 'day').startOf('day')
    setPlannedEndDate(nextDetail?.plannedEndDate ? dayjs(nextDetail.plannedEndDate) : plannedStart.add(6, 'day'))
  }, [])

  const loadAnalytics = React.useCallback(async (id: number, signal: AbortSignal) => (
    (await dashboardApi.getTopQueryExperimentReport(id, signal)).data
  ), [])

  const historyKey = React.useCallback((targetBrand: string, filters: HistoryFilters) => (
    JSON.stringify([targetBrand, filters.status ?? '', filters.startDate ?? '', filters.endDate ?? '', filters.page, filters.pageSize])
  ), [])

  const loadWorkspaceReport = React.useCallback(async (targetBrand: string, selectedId?: number, seedInitialHistory = false) => {
    currentAbortController.current?.abort()
    const controller = new AbortController()
    currentAbortController.current = controller
    const sequence = ++currentRequestSequence.current
    const brandChanged = activeBrandRef.current !== targetBrand
    activeBrandRef.current = targetBrand
    setLoading(true)
    setPermissionDenied(false)
    setRequestError('')
    setInlineError('')
    setSampleError('')
    setSamplePool(null)
    if (brandChanged) {
      setCurrentSummary(null)
      applyDetail(null)
      setReport(null)
    }
    try {
      const initialFilters = { page: 1, pageSize: 20 }
      const listingResponse = await dashboardApi.getTopQueryExperimentBrandStatus(targetBrand, initialFilters, controller.signal)
      if (controller.signal.aborted || sequence !== currentRequestSequence.current) return false
      setCurrentSummary(listingResponse.data.current ?? null)
      if (seedInitialHistory) {
        setHistoryListing({
          items: listingResponse.data.items,
          total: listingResponse.data.total ?? listingResponse.data.items.length,
          page: listingResponse.data.page ?? initialFilters.page,
          pageSize: listingResponse.data.pageSize ?? initialFilters.pageSize,
        })
        initialWorkspaceBrandRef.current = targetBrand
        historyBrandRef.current = targetBrand
        loadedHistoryKeyRef.current = historyKey(targetBrand, initialFilters)
        setHistoryReadyBrand(targetBrand)
      }
      const targetId = selectedId ?? listingResponse.data.current?.id
      if (!targetId) {
        applyDetail(null)
        setReport(null)
        try {
          const poolResponse = await dashboardApi.getTopQueryExperimentSamplePool(targetBrand, { limit: 100 }, controller.signal)
          if (!controller.signal.aborted && sequence === currentRequestSequence.current) setSamplePool(poolResponse.data)
        } catch (error: unknown) {
          if (sequence === currentRequestSequence.current && !controller.signal.aborted) {
            setSamplePool(null)
            setSampleError(apiErrorDetail(error, '样本池加载失败'))
          }
        }
        loadedCurrentKeyRef.current = `${targetBrand}:new`
        return true
      }
      const detailResponse = await dashboardApi.getTopQueryExperimentDetail(targetId, {}, controller.signal)
      if (controller.signal.aborted || sequence !== currentRequestSequence.current) return false
      if (detailResponse.data.brand !== targetBrand) {
        onExperimentChange?.(undefined, 'replace')
        setRequestError('实验与当前品牌不匹配')
        return false
      }
      if (detailResponse.data.status === 'draft') {
        try {
          const poolResponse = await dashboardApi.getTopQueryExperimentSamplePool(targetBrand, { limit: 100 }, controller.signal)
          if (!controller.signal.aborted && sequence === currentRequestSequence.current) setSamplePool(poolResponse.data)
        } catch (error: unknown) {
          if (sequence === currentRequestSequence.current && !controller.signal.aborted) {
            setSamplePool(null)
            setSampleError(apiErrorDetail(error, '样本池加载失败'))
          }
        }
      } else if (['running', 'ended'].includes(detailResponse.data.status)) {
        const analytics = await loadAnalytics(targetId, controller.signal)
        if (controller.signal.aborted || sequence !== currentRequestSequence.current) return false
        setReport(analytics)
      } else {
        setReport(null)
      }
      if (controller.signal.aborted || sequence !== currentRequestSequence.current) return false
      applyDetail(detailResponse.data)
      loadedCurrentKeyRef.current = `${targetBrand}:${targetId}`
      if (selectedId === undefined) onExperimentChange?.(targetId, 'replace')
      return true
    } catch (error: unknown) {
      if (controller.signal.aborted || sequence !== currentRequestSequence.current) return false
      if (isPermissionError(error)) setPermissionDenied(true)
      else setRequestError(apiErrorDetail(error, '实验数据加载失败'))
      return false
    } finally {
      if (!controller.signal.aborted && sequence === currentRequestSequence.current) setLoading(false)
    }
  }, [applyDetail, historyKey, loadAnalytics, onExperimentChange])

  const loadCurrentReport = React.useCallback((targetBrand: string, selectedId?: number) => (
    loadWorkspaceReport(targetBrand, selectedId)
  ), [loadWorkspaceReport])

  const loadInitialWorkspace = React.useCallback((targetBrand: string, selectedId?: number) => (
    loadWorkspaceReport(targetBrand, selectedId, true)
  ), [loadWorkspaceReport])

  const loadHistory = React.useCallback(async (targetBrand: string, filters: HistoryFilters) => {
    historyAbortController.current?.abort()
    const controller = new AbortController()
    historyAbortController.current = controller
    const sequence = ++historyRequestSequence.current
    const brandChanged = historyBrandRef.current !== targetBrand
    historyBrandRef.current = targetBrand
    if (brandChanged) {
      setHistoryListing({ items: [], total: 0, page: filters.page, pageSize: filters.pageSize })
      loadedHistoryKeyRef.current = undefined
    }
    try {
      const response = await dashboardApi.getTopQueryExperimentHistory(targetBrand, filters, controller.signal)
      if (controller.signal.aborted || sequence !== historyRequestSequence.current) return
      setHistoryListing({
        items: response.data.items,
        total: response.data.total ?? response.data.items.length,
        page: response.data.page ?? filters.page,
        pageSize: response.data.pageSize ?? filters.pageSize,
      })
      loadedHistoryKeyRef.current = historyKey(targetBrand, filters)
    } catch (error: unknown) {
      if (controller.signal.aborted || sequence !== historyRequestSequence.current) return
      setInlineError(apiErrorDetail(error, '历史实验加载失败'))
    }
  }, [historyKey])

  React.useEffect(() => {
    const requestedKey = `${brand}:${experimentId ?? 'auto'}`
    if (experimentId !== undefined && loadedCurrentKeyRef.current === requestedKey) return
    const brandChanged = initialWorkspaceBrandRef.current !== brand
    if (brandChanged) setHistoryReadyBrand(undefined)
    const timer = window.setTimeout(() => {
      void (brandChanged ? loadInitialWorkspace(brand, experimentId) : loadCurrentReport(brand, experimentId))
    }, 0)
    return () => {
      window.clearTimeout(timer)
      currentAbortController.current?.abort()
    }
  }, [brand, experimentId, loadCurrentReport, loadInitialWorkspace])

  React.useEffect(() => {
    if (historyReadyBrand !== brand) return
    const key = historyKey(brand, historyFilters)
    if (loadedHistoryKeyRef.current === key) return
    const timer = window.setTimeout(() => void loadHistory(brand, historyFilters), 0)
    return () => {
      window.clearTimeout(timer)
      historyAbortController.current?.abort()
    }
  }, [brand, historyFilters, historyKey, historyReadyBrand, loadHistory])

  React.useEffect(() => {
    let active = true
    queueMicrotask(() => {
      if (!active) return
      setDirty(false)
      onDirtyChange?.(false)
    })
    return () => { active = false }
  }, [brand, experimentId, onDirtyChange])

  React.useEffect(() => {
    if (!detail) return
    const desiredView = ['running', 'ended'].includes(detail.status) ? 'effect' : 'config'
    if (view !== desiredView) onViewChange?.(desiredView)
  }, [detail, onViewChange, view])

  const overlap = React.useMemo(() => {
    const keyByQuery = new Map((samplePool?.items ?? []).map((item) => [item.queryText, item.governanceKey]))
    const treatmentKeys = new Set(treatmentQueries.map((query) => keyByQuery.get(query)).filter(Boolean))
    return controlQueries.filter((query) => treatmentKeys.has(keyByQuery.get(query)))
  }, [controlQueries, samplePool?.items, treatmentQueries])

  const invalidateMutationForNavigation = React.useCallback(() => {
    navigationEpochRef.current += 1
    mutationSequence.current += 1
    mutationInFlightRef.current = false
    setSubmitting(false)
  }, [])

  const beginMutation = React.useCallback((): ExperimentMutation | null => {
    if (mutationInFlightRef.current) return null
    mutationInFlightRef.current = true
    const sequence = ++mutationSequence.current
    setSubmitting(true)
    setInlineError('')
    return {
      sequence,
      brand: renderedBrandRef.current,
      navigationEpoch: navigationEpochRef.current,
      navigationExperimentId: renderedExperimentIdRef.current,
    }
  }, [])

  const mutationIsCurrent = React.useCallback((mutation: ExperimentMutation) => (
    mountedRef.current
    && mutationSequence.current === mutation.sequence
    && navigationEpochRef.current === mutation.navigationEpoch
    && renderedBrandRef.current === mutation.brand
    && renderedExperimentIdRef.current === mutation.navigationExperimentId
  ), [])

  const setDraftDirty = React.useCallback(() => {
    setDirty(true)
    onDirtyChange?.(true)
  }, [onDirtyChange])

  const handleTreatmentQueriesChange = React.useCallback((value: string[]) => {
    setTreatmentQueries(value)
    setDraftDirty()
  }, [setDraftDirty])

  const handleControlQueriesChange = React.useCallback((value: string[]) => {
    setControlQueries(value)
    setDraftDirty()
  }, [setDraftDirty])

  const handlePlannedEndDateChange = React.useCallback((value: Dayjs | null) => {
    if (!value) return
    setPlannedEndDate(value)
    setDraftDirty()
  }, [setDraftDirty])

  const handleSaveDraft = React.useCallback(async () => {
    const mutation = beginMutation()
    if (!mutation) return
    const selectedTreatment = [...treatmentQueries]
    const selectedControl = [...controlQueries]
    const selectedEndDate = plannedEndDate.format('YYYY-MM-DD')
    try {
      const response = await dashboardApi.createTopQueryExperimentDraft(mutation.brand, {
        experimentName: `${mutation.brand.toUpperCase()} A/B Test`,
        treatmentQueries: selectedTreatment,
        controlQueries: selectedControl,
        plannedEndDate: selectedEndDate,
      })
      if (!mutationIsCurrent(mutation)) return
      applyDetail(response.data)
      setDirty(false)
      onDirtyChange?.(false)
      onExperimentChange?.(response.data.id, 'replace')
      messageApi.success('实验草稿已保存')
    } catch (error: unknown) {
      if (!mutationIsCurrent(mutation)) return
      setInlineError(apiErrorDetail(error, '保存实验草稿失败'))
    } finally {
      if (mutationIsCurrent(mutation)) {
        mutationInFlightRef.current = false
        setSubmitting(false)
      }
    }
  }, [applyDetail, beginMutation, messageApi, mutationIsCurrent, onDirtyChange, onExperimentChange, plannedEndDate, treatmentQueries, controlQueries])

  const updateLifecycle = React.useCallback(async (action: 'schedule' | 'end' | 'cancel') => {
    if (!detail) return
    const mutation = beginMutation()
    if (!mutation) return
    const targetExperimentId = detail.id
    const filters = { ...historyFilters }
    try {
      const response = action === 'schedule'
        ? await dashboardApi.scheduleTopQueryExperiment(targetExperimentId)
        : action === 'cancel'
          ? await dashboardApi.cancelTopQueryExperiment(targetExperimentId)
          : await dashboardApi.endTopQueryExperiment(targetExperimentId)
      if (!mutationIsCurrent(mutation)) return
      applyDetail(response.data)
      messageApi.success(action === 'schedule' ? '实验已排期' : action === 'cancel' ? '实验已取消' : '实验已结束')
      await Promise.all([
        loadCurrentReport(mutation.brand, targetExperimentId),
        loadHistory(mutation.brand, filters),
      ])
    } catch (error: unknown) {
      if (!mutationIsCurrent(mutation)) return
      setInlineError(apiErrorDetail(error, action === 'schedule' ? '排期失败' : action === 'cancel' ? '取消实验失败' : '结束实验失败'))
    } finally {
      if (mutationIsCurrent(mutation)) {
        mutationInFlightRef.current = false
        setSubmitting(false)
      }
    }
  }, [applyDetail, beginMutation, detail, historyFilters, loadCurrentReport, loadHistory, messageApi, mutationIsCurrent])

  const reportSnapshotToken = report?.reportSnapshotToken

  const handleExport = React.useCallback(async (format: 'csv' | 'zip' | 'xlsx') => {
    if (!detail || exportInFlightRef.current) return
    exportInFlightRef.current = true
    exportAbortController.current?.abort()
    const controller = new AbortController()
    exportAbortController.current = controller
    const sequence = ++exportRequestSequence.current
    const navigationEpoch = navigationEpochRef.current
    const targetExperimentId = detail.id
    setExporting(true)
    try {
      const response = await dashboardApi.exportTopQueryExperiment(targetExperimentId, format, controller.signal, reportSnapshotToken)
      if (controller.signal.aborted || !mountedRef.current || sequence !== exportRequestSequence.current || navigationEpoch !== navigationEpochRef.current) return
      const fallback = `ab-test-${targetExperimentId}.${format}`
      downloadBlob(response.data, downloadFileName(response.headers['content-disposition'], fallback))
    } catch (error: unknown) {
      if (controller.signal.aborted || sequence !== exportRequestSequence.current) return
      setInlineError(apiErrorDetail(error, '导出失败'))
    } finally {
      if (sequence === exportRequestSequence.current) {
        exportInFlightRef.current = false
        setExporting(false)
      }
    }
  }, [detail, reportSnapshotToken])

  const handleRefresh = React.useCallback(async () => {
    const wasDirty = dirty
    if (wasDirty && !(confirmNavigation?.() ?? window.confirm('实验草稿存在未保存的修改，刷新后将丢失。是否继续？'))) return
    const loaded = await loadCurrentReport(brand, experimentId)
    if (!loaded || !wasDirty) return
    setDirty(false)
    onDirtyChange?.(false)
  }, [brand, confirmNavigation, dirty, experimentId, loadCurrentReport, onDirtyChange])

  const navigateExperiment = React.useCallback((nextId?: number) => {
    if (dirty && nextId !== detail?.id && !(confirmNavigation?.() ?? window.confirm('实验草稿存在未保存的修改，离开后将丢失。是否继续？'))) return
    invalidateMutationForNavigation()
    setDirty(false)
    onDirtyChange?.(false)
    onExperimentChange?.(nextId)
  }, [confirmNavigation, detail?.id, dirty, invalidateMutationForNavigation, onDirtyChange, onExperimentChange])

  const isConfiguration = !detail || ['draft', 'scheduled'].includes(detail.status)
  const currentExperimentId = currentSummary?.id
  const plannedStart = detail?.plannedStartDate ? dayjs(detail.plannedStartDate) : dayjs().add(1, 'day').startOf('day')
  const staleSamplePool = Boolean(
    detail?.status === 'draft'
    && detail.sampleDataDate
    && samplePool?.sampleDataDate
    && detail.sampleDataDate !== samplePool.sampleDataDate,
  )

  return {
    canManage,
    currentExperimentId,
    currentSummary,
    detail,
    dirty,
    exporting,
    handleControlQueriesChange,
    handleExport,
    handlePlannedEndDateChange,
    handleRefresh,
    handleSaveDraft,
    handleTreatmentQueriesChange,
    historyFilters,
    historyListing,
    inlineError,
    isConfiguration,
    isMobile,
    loading,
    messageContextHolder,
    navigateExperiment,
    overlap,
    permissionDenied,
    plannedEndDate,
    plannedStart,
    report,
    requestError,
    sampleError,
    samplePool,
    setHistoryFilters,
    setInlineError,
    staleSamplePool,
    submitting,
    treatmentQueries,
    controlQueries,
    updateLifecycle,
    retryCurrentReport: () => void loadCurrentReport(brand, experimentId),
  }
}

export type ExperimentWorkbenchModel = ReturnType<typeof useExperimentWorkbench>
