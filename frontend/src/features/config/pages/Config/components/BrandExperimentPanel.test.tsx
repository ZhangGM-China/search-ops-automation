import React from 'react';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { message } from 'antd';
import BrandExperimentPanel from './BrandExperimentPanel';
import { dashboardApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';
import type { AxiosResponse } from 'axios';
import type { TopQueryBrandExperimentListData, TopQueryExperimentDetailData } from '@/shared/types';

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
};

vi.mock('@/shared/api', () => ({
  dashboardApi: {
    getTopQueryExperimentBrandStatus: vi.fn(),
    getTopQueryExperimentHistory: vi.fn(),
    getTopQueryExperimentSamplePool: vi.fn(),
    createTopQueryExperimentDraft: vi.fn(),
    getTopQueryExperimentDetail: vi.fn(),
    scheduleTopQueryExperiment: vi.fn(),
    endTopQueryExperiment: vi.fn(),
    cancelTopQueryExperiment: vi.fn(),
    getTopQueryExperimentReport: vi.fn(),
    exportTopQueryExperiment: vi.fn(),
  },
}));

describe('BrandExperimentPanel', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(message, 'success').mockImplementation(() => undefined as never);
    vi.spyOn(message, 'error').mockImplementation(() => undefined as never);

    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({
      brand: 'atlas',
      current: {
        id: 9,
        brand: 'atlas',
        status: 'running',
        treatmentCount: 20,
        controlCount: 20,
      },
      items: [],
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentHistory).mockResolvedValue(axiosResponse({ brand: 'atlas', current: null, items: [], total: 0, page: 1, pageSize: 20 }));
    vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mockResolvedValue(axiosResponse({
      brand: 'atlas',
      sampleDataDate: '2026-07-08',
      items: [
        { queryText: '连衣裙 女士', governanceKey: '连衣裙', queryRank: 1, searchPv: 100, searchUv: 80, ctr: 0.12, cvr: 0.03, avgCtr: 0.11, avgCvr: 0.028, queryPvShare: 0.2 },
        { queryText: '衬衫', governanceKey: '衬衫', queryRank: 2, searchPv: 90, searchUv: 70, ctr: 0.1, cvr: 0.02, avgCtr: 0.095, avgCvr: 0.019, queryPvShare: 0.18 },
      ],
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValue(axiosResponse({
      id: 9,
      brand: 'atlas',
      experimentName: 'ATLAS A/B Test',
      plannedEndDate: '2026-07-12',
      sampleDataDate: '2026-07-08',
      status: 'running',
      samples: [
        { queryText: '连衣裙', groupType: 'treatment', sampleRank: 1, sampleSearchUv: 80, sampleSearchPv: 100, sampleCtr: 0.12, sampleCvr: 0.03 },
        { queryText: '衬衫', groupType: 'control', sampleRank: 2, sampleSearchUv: 70, sampleSearchPv: 90, sampleCtr: 0.1, sampleCvr: 0.02 },
      ],
      groupSummary: {
        treatment: { observedCount: 10, cleanCount: 9, contaminatedCount: 1, avgCtr: 0.11, avgCvr: 0.03, totalSearchPv: 1000, totalSearchUv: 800 },
        control: { observedCount: 10, cleanCount: 10, contaminatedCount: 0, avgCtr: 0.09, avgCvr: 0.02, totalSearchPv: 900, totalSearchUv: 700 },
      },
      items: [
        {
          queryText: '连衣裙',
          groupType: 'treatment',
          sampleRank: 1,
          sampleSearchPv: 100,
          sampleSearchUv: 80,
          sampleCtr: 0.12,
          sampleCvr: 0.03,
          latestSearchPv: 120,
          latestSearchUv: 90,
          latestCtr: 0.13,
          latestCvr: 0.04,
          contaminationStatus: 'clean',
          observedStatus: 'observed',
          governanceStatus: 'running',
        },
      ],
    }));
    vi.mocked(dashboardApi.createTopQueryExperimentDraft).mockResolvedValue(axiosResponse({
      id: 10,
      brand: 'atlas',
      experimentName: 'New Draft',
      plannedEndDate: '2026-07-15',
      sampleDataDate: '2026-07-08',
      status: 'draft',
      samples: [
        { queryText: '连衣裙', groupType: 'treatment', sampleRank: 1, sampleSearchUv: 80, sampleSearchPv: 100, sampleCtr: 0.12, sampleCvr: 0.03 },
        { queryText: '衬衫', groupType: 'control', sampleRank: 2, sampleSearchUv: 70, sampleSearchPv: 90, sampleCtr: 0.1, sampleCvr: 0.02 },
      ],
      items: [],
    }));
    vi.mocked(dashboardApi.scheduleTopQueryExperiment).mockResolvedValue(axiosResponse({
      id: 10,
      brand: 'atlas',
      experimentName: 'New Draft',
      plannedEndDate: '2026-07-15',
      sampleDataDate: '2026-07-08',
      status: 'scheduled',
      samples: [],
      items: [],
    }));
    vi.mocked(dashboardApi.endTopQueryExperiment).mockResolvedValue(axiosResponse({
      id: 9,
      brand: 'atlas',
      experimentName: 'ATLAS A/B Test',
      plannedEndDate: '2026-07-12',
      sampleDataDate: '2026-07-08',
      status: 'ended',
      samples: [],
      items: [],
    }));
    vi.mocked(dashboardApi.cancelTopQueryExperiment).mockResolvedValue(axiosResponse({
      id: 10, brand: 'atlas', experimentName: 'ATLAS A/B Test', status: 'cancelled', samples: [], items: [],
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentReport).mockResolvedValue(axiosResponse({
      reportSnapshotToken: 'snapshot-9-v1',
      experiment: { experimentId: 9, brand: 'atlas', status: 'running', calculationVersion: 'top-query-ab-test-v2' },
      calculationVersion: 'top-query-ab-test-v2',
      generatedAt: '2026-07-12T08:00:00+08:00',
      timezone: 'Asia/Shanghai',
      baselineQuality: { status: 'complete', calculationVersion: 'top-query-ab-test-v2', sampleCount: 40, treatmentSamples: 20, controlSamples: 20, expectedDays: 7, windowValid: true, locked: true },
      quality: { status: 'complete', completeDays: 2, failedDays: 0, latestDayQuality: { expectedSamples: 40, observedSamples: 40, treatmentSamples: 20, controlSamples: 20, treatmentTaskCount: 20, controlTaskCount: 0, controlActionCount: 0, contaminationCount: 0, failedTaskCount: 0, unresolvedTaskCount: 0, failedActionCount: 0, unresolvedActionCount: 0 } },
      groups: {
        treatment: { sampleCount: 20, searchUv: 800, searchPv: 1000, baselineCtr: 0.1, baselineCvr: 0.02, currentCtr: 0.12, currentCvr: 0.025, ctrChange: 0.02, cvrChange: 0.005 },
        control: { sampleCount: 20, searchUv: 700, searchPv: 900, baselineCtr: 0.09, baselineCvr: 0.018, currentCtr: 0.095, currentCvr: 0.019, ctrChange: 0.005, cvrChange: 0.001 },
      },
      effect: { ctrNetChange: 0.015, cvrNetChange: 0.004, ctrPositiveDays: 2, cvrPositiveDays: 1, estimatedIncrementalClickUv: 12, estimatedIncrementalPurchaseUv: 3 },
      operations: { governanceCoverage: 1, actionSuccessRate: 0.95, plannedActions: 20, executedActions: 19, failedActions: 1, tokenUsage: 1200, durationSeconds: 300 },
      trend: [],
      queries: [],
      queryDailyMetrics: [],
      baselineEvidence: [],
      actions: [],
      dataStartDate: '2026-07-09',
      dataEndDate: '2026-07-10',
      dataDays: 2,
      overview: {
        experimentId: 9,
        brand: 'atlas',
        status: 'running',
        quality: { status: 'complete', completeDays: 2, failedDays: 0, latestDayQuality: { expectedSamples: 40, observedSamples: 40, treatmentSamples: 20, controlSamples: 20, treatmentTaskCount: 20, controlTaskCount: 0, controlActionCount: 0, contaminationCount: 0, failedTaskCount: 0, unresolvedTaskCount: 0, failedActionCount: 0, unresolvedActionCount: 0 } },
        groups: {
          treatment: { sampleCount: 20, searchUv: 800, searchPv: 1000, baselineCtr: 0.1, baselineCvr: 0.02, currentCtr: 0.12, currentCvr: 0.025, ctrChange: 0.02, cvrChange: 0.005 },
          control: { sampleCount: 20, searchUv: 700, searchPv: 900, baselineCtr: 0.09, baselineCvr: 0.018, currentCtr: 0.095, currentCvr: 0.019, ctrChange: 0.005, cvrChange: 0.001 },
        },
        effect: { ctrNetChange: 0.015, cvrNetChange: 0.004, ctrPositiveDays: 2, cvrPositiveDays: 1, estimatedIncrementalClickUv: 12, estimatedIncrementalPurchaseUv: 3 },
        operations: { governanceCoverage: 1, actionSuccessRate: 0.95, plannedActions: 20, executedActions: 19, failedActions: 1, tokenUsage: 1200, durationSeconds: 300 },
      },
    }));
  });

  it('loads one unified report and projects the existing analytics views', async () => {
    render(<BrandExperimentPanel brand="atlas" canManage embedded />);

    expect(await screen.findByText('实验质量正常')).toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentReport).toHaveBeenCalledTimes(1);
  });

  it('defaults running and ended experiments to the effect report view', async () => {
    const onViewChange = vi.fn();
    render(<BrandExperimentPanel brand="atlas" canManage embedded view="config" onViewChange={onViewChange} />);

    expect(await screen.findByRole('heading', { name: '1. 效果结论' })).toBeInTheDocument();
    expect(onViewChange).toHaveBeenCalledWith('effect');
  });

  it('defaults a real ended experiment to the effect report view', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({
      brand: 'atlas', current: null, items: [{ id: 88, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }],
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValueOnce(axiosResponse({
      id: 88, brand: 'atlas', experimentName: 'Ended Test', status: 'ended', samples: [],
    }));
    const onViewChange = vi.fn();
    render(<BrandExperimentPanel brand="atlas" canManage={false} experimentId={88} view="config" onViewChange={onViewChange} />);

    expect(await screen.findByRole('heading', { name: '1. 效果结论' })).toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentReport).toHaveBeenCalledWith(88, expect.any(AbortSignal));
    expect(onViewChange).toHaveBeenCalledWith('effect');
    expect(screen.queryByRole('button', { name: '结束实验' })).not.toBeInTheDocument();
  });

  it('keeps scheduled experiments in the configuration view without requesting a report', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({
      brand: 'atlas', current: { id: 10, brand: 'atlas', status: 'scheduled', treatmentCount: 20, controlCount: 20 }, items: [],
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValueOnce(axiosResponse({
      id: 10, brand: 'atlas', experimentName: 'Scheduled Test', status: 'scheduled', plannedStartDate: '2026-07-10', plannedEndDate: '2026-07-16', samples: [],
    }));
    const onViewChange = vi.fn();
    render(<BrandExperimentPanel brand="atlas" canManage view="effect" onViewChange={onViewChange} />);

    expect(await screen.findByText('计划周期')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '1. 效果结论' })).not.toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentReport).not.toHaveBeenCalled();
    expect(onViewChange).toHaveBeenCalledWith('config');
  });

  it('renders embedded running state with summaries and refresh', async () => {
    render(<BrandExperimentPanel brand="atlas" canManage embedded />);

    expect(await screen.findByText('ATLAS A/B Test')).toBeInTheDocument();
    expect(await screen.findByText('实验质量正常')).toBeInTheDocument();
    expect(screen.getByText('CTR 净效果')).toBeInTheDocument();
    expect(screen.getByText('治理覆盖率')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /\u5237新/ }));
    await waitFor(() => {
      expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(2);
      expect(dashboardApi.getTopQueryExperimentSamplePool).not.toHaveBeenCalled();
    });
  });

  it('renders success messages through the Ant Design context holder without a static API warning', async () => {
    vi.mocked(message.success).mockRestore();
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    render(<BrandExperimentPanel brand="atlas" canManage />);
    fireEvent.click(await screen.findByRole('button', { name: '结束实验' }));
    fireEvent.click(await screen.findByRole('button', { name: /确\s*认/ }));
    expect(await screen.findByText('实验已结束')).toBeInTheDocument();
    expect(consoleError.mock.calls.flat().join('\n')).not.toContain('[antd: message] Static function can not consume context');
    consoleError.mockRestore();
  });

  it('seeds current and history from one initial brand listing request', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({
      brand: 'atlas', current: null, items: [{ id: 88, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }], total: 1, page: 1, pageSize: 20,
    }));
    render(<BrandExperimentPanel brand="atlas" canManage />);

    expect(await screen.findByText('ATLAS A/B Test #88')).toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getTopQueryExperimentHistory).not.toHaveBeenCalled();
  });

  it('exports Excel as the primary action and safely releases the blob URL', async () => {
    const createObjectURL = vi.fn(() => 'blob:experiment');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click(this: HTMLAnchorElement) {
      expect(document.body.contains(this)).toBe(true);
    });
    vi.mocked(dashboardApi.exportTopQueryExperiment).mockResolvedValue(axiosResponse(new Blob(['xlsx'])));
    render(<BrandExperimentPanel brand="atlas" canManage />);
    await screen.findByText('实验质量正常');
    vi.useFakeTimers();

    try {
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: /导出 Excel/ })); });
      expect(dashboardApi.exportTopQueryExperiment).toHaveBeenCalledWith(9, 'xlsx', expect.any(AbortSignal), 'snapshot-9-v1');
      expect(anchorClick).toHaveBeenCalledTimes(1);
      expect(anchorClick.mock.instances[0]).toHaveAttribute('download', 'ab-test-9.xlsx');
      expect(document.querySelector('a[download="ab-test-9.xlsx"]')).not.toBeInTheDocument();
      expect(revokeObjectURL).not.toHaveBeenCalled();
      await vi.runAllTimersAsync();
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:experiment');
    } finally {
      vi.useRealTimers();
      anchorClick.mockRestore();
    }
  });

  it('keeps CSV and ZIP in the secondary export menu', async () => {
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    vi.mocked(dashboardApi.exportTopQueryExperiment).mockResolvedValue(axiosResponse(new Blob(['export'])));
    try {
      render(<BrandExperimentPanel brand="atlas" canManage />);
      await screen.findByRole('heading', { name: '1. 效果结论' });

      fireEvent.click(screen.getByRole('button', { name: '更多导出格式' }));
      fireEvent.click(await screen.findByText('导出 CSV'));
      await waitFor(() => expect(dashboardApi.exportTopQueryExperiment).toHaveBeenCalledWith(9, 'csv', expect.any(AbortSignal), 'snapshot-9-v1'));

      fireEvent.click(screen.getByRole('button', { name: '更多导出格式' }));
      fireEvent.click(await screen.findByText('导出 ZIP'));
      await waitFor(() => expect(dashboardApi.exportTopQueryExperiment).toHaveBeenCalledWith(9, 'zip', expect.any(AbortSignal), 'snapshot-9-v1'));
    } finally {
      anchorClick.mockRestore();
    }
  });

  it('prevents concurrent exports and aborts the active request on unmount', async () => {
    const pending = deferred<AxiosResponse<Blob>>();
    let exportSignal: AbortSignal | undefined;
    vi.mocked(dashboardApi.exportTopQueryExperiment).mockImplementation((_id, _format, signal) => {
      exportSignal = signal;
      return pending.promise;
    });
    const view = render(<BrandExperimentPanel brand="atlas" canManage />);
    await screen.findByRole('heading', { name: '1. 效果结论' });

    fireEvent.click(screen.getByRole('button', { name: /导出 Excel/ }));
    fireEvent.click(screen.getByRole('button', { name: /导出 Excel/ }));
    await waitFor(() => expect(dashboardApi.exportTopQueryExperiment).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole('button', { name: /导出 Excel/ })).toBeDisabled());
    view.unmount();
    expect(exportSignal?.aborted).toBe(true);
  });

  it('sanitizes path segments from the server-provided export filename', async () => {
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:experiment') });
    vi.mocked(dashboardApi.exportTopQueryExperiment).mockResolvedValue({
      ...axiosResponse(new Blob(['xlsx'])),
      headers: { 'content-disposition': 'attachment; filename="../../unsafe\\report.xlsx"' },
    });
    render(<BrandExperimentPanel brand="atlas" canManage />);
    await screen.findByRole('heading', { name: '1. 效果结论' });

    fireEvent.click(screen.getByRole('button', { name: /导出 Excel/ }));
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(anchorClick.mock.instances[0]).toHaveAttribute('download', 'report.xlsx');
    anchorClick.mockRestore();
  });

  it('shows query-only selected tags, secondary option metrics, and disables cross-group matches', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: null, items: [] }));
    render(<BrandExperimentPanel brand="atlas" canManage />);

    fireEvent.mouseDown((await screen.findAllByRole('combobox'))[0]);
    const treatmentOption = (await screen.findAllByText('连衣裙 女士')).find((node) => node.closest('.ant-select-item-option'))?.closest('.ant-select-item-option');
    fireEvent.click(treatmentOption!);

    expect(await screen.findByText('连衣裙 女士', { selector: '.experiment-query-tag' })).toBeInTheDocument();
    expect(document.querySelector('.experiment-query-option__metrics')).toHaveTextContent('PV 100');

    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]);
    const duplicateOption = (await screen.findAllByText('连衣裙 女士')).at(-1)?.closest('.ant-select-item-option');
    expect(duplicateOption).toHaveClass('ant-select-item-option-disabled');
  });

  it('shows backend overlap errors inline instead of only a toast', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: null, items: [] }));
    vi.mocked(dashboardApi.createTopQueryExperimentDraft).mockRejectedValueOnce({ response: { data: { detail: '实验组和对照组存在治理等价 Query' } } });
    render(<BrandExperimentPanel brand="atlas" canManage />);

    fireEvent.mouseDown((await screen.findAllByRole('combobox'))[0]);
    fireEvent.click((await screen.findAllByText('连衣裙 女士'))[0]);
    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]);
    fireEvent.click((await screen.findAllByText('衬衫'))[0]);
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));

    expect(await screen.findByText('实验组和对照组存在治理等价 Query')).toBeInTheDocument();
  });

  it('creates draft, schedules, and ends experiment when manageable', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: null, items: [] }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValueOnce(axiosResponse({
      id: 10,
      brand: 'atlas',
      experimentName: 'New Draft',
      plannedEndDate: '2026-07-15',
      sampleDataDate: '2026-07-08',
      status: 'draft',
      samples: [
        { queryText: '连衣裙', groupType: 'treatment', sampleRank: 1, sampleSearchUv: 80, sampleSearchPv: 100, sampleCtr: 0.12, sampleCvr: 0.03 },
        { queryText: '衬衫', groupType: 'control', sampleRank: 2, sampleSearchUv: 70, sampleSearchPv: 90, sampleCtr: 0.1, sampleCvr: 0.02 },
      ],
      items: [],
    }));

    render(<BrandExperimentPanel brand="atlas" canManage />);
    expect(await screen.findByText('ATLAS A/B Test')).toBeInTheDocument();
    expect(screen.queryByLabelText('实验名称')).not.toBeInTheDocument();
    fireEvent.mouseDown(screen.getAllByRole('combobox')[0]);
    fireEvent.click((await screen.findAllByText('连衣裙 女士')).at(-1)!);
    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]);
    fireEvent.click((await screen.findAllByText('衬衫')).at(-1)!);

    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
    await waitFor(() => {
      expect(dashboardApi.createTopQueryExperimentDraft).toHaveBeenCalledWith('atlas', {
        experimentName: 'ATLAS A/B Test',
        treatmentQueries: ['连衣裙 女士'],
        controlQueries: ['衬衫'],
        plannedEndDate: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
      });
    });
  });

  it('disables management actions when canManage is false', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: null, items: [] }));
    render(<BrandExperimentPanel brand="atlas" canManage={false} />);
    expect(await screen.findByText('ATLAS A/B Test')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存草稿' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '确认排期' })).toBeDisabled();
  });

  it('lets read-only users view and export the running report without management actions', async () => {
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    vi.mocked(dashboardApi.exportTopQueryExperiment).mockResolvedValue(axiosResponse(new Blob(['export'])));
    try {
      render(<BrandExperimentPanel brand="atlas" canManage={false} />);
      expect(await screen.findByRole('heading', { name: '1. 效果结论' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /导出 Excel/ })).toBeEnabled();
      expect(screen.queryByRole('button', { name: '结束实验' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: '取消实验' })).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: /导出 Excel/ }));
      await waitFor(() => expect(dashboardApi.exportTopQueryExperiment).toHaveBeenCalledWith(9, 'xlsx', expect.any(AbortSignal), 'snapshot-9-v1'));
      fireEvent.click(screen.getByRole('button', { name: '更多导出格式' }));
      fireEvent.click(await screen.findByText('导出 CSV'));
      await waitFor(() => expect(dashboardApi.exportTopQueryExperiment).toHaveBeenCalledWith(9, 'csv', expect.any(AbortSignal), 'snapshot-9-v1'));
      fireEvent.click(screen.getByRole('button', { name: '更多导出格式' }));
      fireEvent.click(await screen.findByText('导出 ZIP'));
      await waitFor(() => expect(dashboardApi.exportTopQueryExperiment).toHaveBeenCalledWith(9, 'zip', expect.any(AbortSignal), 'snapshot-9-v1'));
    } finally {
      anchorClick.mockRestore();
    }
  });

  it('confirms cancellation and shows new experiment when reload has no current experiment', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus)
      .mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: { id: 10, brand: 'atlas', status: 'scheduled', treatmentCount: 20, controlCount: 20 }, items: [] }))
      .mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: null, items: [{ id: 10, brand: 'atlas', status: 'cancelled', treatmentCount: 20, controlCount: 20 }] }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail)
      .mockResolvedValueOnce(axiosResponse({ id: 10, brand: 'atlas', experimentName: 'ATLAS A/B Test', status: 'scheduled', samples: [] }))
      .mockResolvedValueOnce(axiosResponse({ id: 10, brand: 'atlas', experimentName: 'ATLAS A/B Test', status: 'cancelled', samples: [] }));
    render(<BrandExperimentPanel brand="atlas" canManage />);

    fireEvent.click(await screen.findByRole('button', { name: '取消实验' }));
    fireEvent.click(await screen.findByRole('button', { name: /确\s*认/ }));

    await waitFor(() => expect(dashboardApi.cancelTopQueryExperiment).toHaveBeenCalledWith(10));
    expect(await screen.findByRole('button', { name: '新建实验' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '返回当前实验' })).not.toBeInTheDocument();
  });

  it('dismisses cancellation without calling the lifecycle API', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: { id: 10, brand: 'atlas', status: 'scheduled', treatmentCount: 20, controlCount: 20 }, items: [] }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValueOnce(axiosResponse({ id: 10, brand: 'atlas', experimentName: 'ATLAS A/B Test', status: 'scheduled', samples: [] }));
    render(<BrandExperimentPanel brand="atlas" canManage />);

    fireEvent.click(await screen.findByRole('button', { name: '取消实验' }));
    fireEvent.click(await screen.findByRole('button', { name: /暂\s*不/ }));

    expect(dashboardApi.cancelTopQueryExperiment).not.toHaveBeenCalled();
  });

  it('shows new experiment after ending when the refreshed listing has no current experiment', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus)
      .mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: { id: 9, brand: 'atlas', status: 'running', treatmentCount: 20, controlCount: 20 }, items: [] }))
      .mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: null, items: [{ id: 9, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }] }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail)
      .mockResolvedValueOnce(axiosResponse({ id: 9, brand: 'atlas', experimentName: 'ATLAS A/B Test', status: 'running', samples: [] }))
      .mockResolvedValueOnce(axiosResponse({ id: 9, brand: 'atlas', experimentName: 'ATLAS A/B Test', status: 'ended', samples: [] }));
    render(<BrandExperimentPanel brand="atlas" canManage />);

    fireEvent.click(await screen.findByRole('button', { name: '结束实验' }));
    fireEvent.click(await screen.findByRole('button', { name: /确\s*认/ }));

    await waitFor(() => expect(dashboardApi.endTopQueryExperiment).toHaveBeenCalledWith(9));
    expect(await screen.findByRole('button', { name: '新建实验' })).toBeInTheDocument();
  });

  it('does not let dirty draft values schedule stale persisted samples', async () => {
    const samples = Array.from({ length: 40 }, (_, index) => ({ queryText: `query-${index}`, groupType: index < 20 ? 'treatment' : 'control', sampleRank: index + 1, sampleSearchUv: 100, sampleSearchPv: 200, sampleCtr: 0.1, sampleCvr: 0.02 }));
    const poolItems = samples.map((sample, index) => ({ queryText: sample.queryText, governanceKey: sample.queryText, queryRank: index + 1, searchPv: 200, searchUv: 100, ctr: 0.1, cvr: 0.02, avgCtr: 0.1, avgCvr: 0.02, queryPvShare: 0.01 }));
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: { id: 10, brand: 'atlas', status: 'draft', treatmentCount: 20, controlCount: 20 }, items: [] }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValueOnce(axiosResponse({ id: 10, brand: 'atlas', experimentName: 'ATLAS A/B Test', status: 'draft', sampleDataDate: '2026-07-08', plannedStartDate: '2026-07-10', plannedEndDate: '2026-07-16', samples }));
    vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', sampleDataDate: '2026-07-08', items: poolItems }));
    render(<BrandExperimentPanel brand="atlas" canManage />);

    fireEvent.mouseDown(await screen.findByLabelText('实验组 Query'));
    fireEvent.click(screen.getByLabelText('移除 query-0'));

    expect(screen.getByRole('button', { name: '确认排期' })).toBeDisabled();
    expect(screen.getByText('请先保存草稿后再排期')).toBeInTheDocument();
  });

  it('isolates running experiment rendering from sample pool failures', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mockRejectedValueOnce(new Error('pool unavailable'));
    render(<BrandExperimentPanel brand="atlas" canManage />);
    expect(await screen.findByText('实验质量正常')).toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentSamplePool).not.toHaveBeenCalled();
  });

  it('blocks scheduling when the loaded draft sample date is stale', async () => {
    const samples = Array.from({ length: 40 }, (_, index) => ({ queryText: `query-${index}`, groupType: index < 20 ? 'treatment' : 'control', sampleRank: index + 1, sampleSearchUv: 100, sampleSearchPv: 200, sampleCtr: 0.1, sampleCvr: 0.02 }));
    const poolItems = samples.map((sample, index) => ({ queryText: sample.queryText, governanceKey: sample.queryText, queryRank: index + 1, searchPv: 200, searchUv: 100, ctr: 0.1, cvr: 0.02, avgCtr: 0.1, avgCvr: 0.02, queryPvShare: 0.01 }));
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: { id: 10, brand: 'atlas', status: 'draft', treatmentCount: 20, controlCount: 20 }, items: [] }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValueOnce(axiosResponse({ id: 10, brand: 'atlas', status: 'draft', sampleDataDate: '2026-07-07', samples }));
    vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', sampleDataDate: '2026-07-08', items: poolItems }));
    render(<BrandExperimentPanel brand="atlas" canManage />);
    expect(await screen.findByText('样本池已更新，请重新保存草稿')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认排期' })).toBeDisabled();
  });

  it('rejects a deep-linked experiment whose detail brand conflicts with the URL brand', async () => {
    const onExperimentChange = vi.fn();
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValueOnce(axiosResponse({ id: 99, brand: 'harbor', status: 'ended', samples: [] }));
    render(<BrandExperimentPanel brand="atlas" canManage experimentId={99} onExperimentChange={onExperimentChange} />);
    expect(await screen.findByText('实验与当前品牌不匹配')).toBeInTheDocument();
    expect(onExperimentChange).toHaveBeenCalledWith(undefined, 'replace');
    expect(dashboardApi.getTopQueryExperimentReport).not.toHaveBeenCalled();
  });

  it('ignores late responses from the previously selected brand', async () => {
    let resolveAtlas: (value: AxiosResponse<TopQueryBrandExperimentListData>) => void = () => undefined;
    const atlasPromise = new Promise<AxiosResponse<TopQueryBrandExperimentListData>>((resolve) => { resolveAtlas = resolve; });
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockImplementation((brand) => brand === 'atlas'
      ? atlasPromise
      : Promise.resolve(axiosResponse({ brand: 'harbor', current: null, items: [], total: 0, page: 1, pageSize: 20 })));
    vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mockResolvedValue(axiosResponse({ brand: 'harbor', sampleDataDate: '2026-07-08', items: [] }));
    const { rerender } = render(<BrandExperimentPanel brand="atlas" canManage />);
    rerender(<BrandExperimentPanel brand="harbor" canManage />);
    expect(await screen.findByText('HARBOR A/B Test')).toBeInTheDocument();
    resolveAtlas(axiosResponse({ brand: 'atlas', current: null, items: [], total: 0, page: 1, pageSize: 20 }));
    await waitFor(() => expect(screen.getByText('HARBOR A/B Test')).toBeInTheDocument());
  });

  it('enables scheduling only after dirty 20+20 samples are saved', async () => {
    const samples = Array.from({ length: 40 }, (_, index) => ({ queryText: `query-${index}`, groupType: index < 20 ? 'treatment' : 'control', sampleRank: index + 1, sampleSearchUv: 100, sampleSearchPv: 200, sampleCtr: 0.1, sampleCvr: 0.02 }));
    const poolItems = samples.map((sample, index) => ({ queryText: sample.queryText, governanceKey: sample.queryText, queryRank: index + 1, searchPv: 200, searchUv: 100, ctr: 0.1, cvr: 0.02, avgCtr: 0.1, avgCvr: 0.02, queryPvShare: 0.01 }));
    const draft = { id: 10, brand: 'atlas', status: 'draft', sampleDataDate: '2026-07-08', plannedStartDate: '2026-07-10', plannedEndDate: '2026-07-16', samples };
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({ brand: 'atlas', current: { id: 10, brand: 'atlas', status: 'draft', treatmentCount: 20, controlCount: 20 }, items: [], total: 1, page: 1, pageSize: 20 }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValue(axiosResponse(draft));
    vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mockResolvedValue(axiosResponse({ brand: 'atlas', sampleDataDate: '2026-07-08', items: poolItems }));
    vi.mocked(dashboardApi.createTopQueryExperimentDraft).mockResolvedValue(axiosResponse(draft));
    render(<BrandExperimentPanel brand="atlas" canManage />);
    fireEvent.click(await screen.findByLabelText('移除 query-0'));
    fireEvent.mouseDown(screen.getByLabelText('实验组 Query'));
    const option = (await screen.findAllByText('query-0')).find((node) => node.closest('.ant-select-item-option'))?.closest('.ant-select-item-option');
    fireEvent.click(option!);
    expect(screen.getByRole('button', { name: '确认排期' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
    await waitFor(() => expect(screen.getByRole('button', { name: /确认排期/ })).toBeEnabled());
    expect(screen.queryByText('请先保存草稿后再排期')).not.toBeInTheDocument();
  });

  it('protects dirty drafts when opening a history experiment', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: null, items: [{ id: 88, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }], total: 1, page: 1, pageSize: 20 }));
    vi.mocked(dashboardApi.getTopQueryExperimentHistory).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: null, items: [{ id: 88, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }], total: 1, page: 1, pageSize: 20 }));
    render(<BrandExperimentPanel brand="atlas" canManage />);
    fireEvent.mouseDown(await screen.findByLabelText('实验组 Query'));
    const option = (await screen.findAllByText('连衣裙 女士')).find((node) => node.closest('.ant-select-item-option'))?.closest('.ant-select-item-option');
    fireEvent.click(option!);
    fireEvent.click(screen.getByText('ATLAS A/B Test #88'));
    expect(confirmSpy).toHaveBeenCalled();
    expect(dashboardApi.getTopQueryExperimentDetail).not.toHaveBeenCalledWith(88, expect.anything(), expect.anything());
  });

  it('offers a new experiment action from ended history when no active experiment exists', async () => {
    const onExperimentChange = vi.fn();
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: null, items: [{ id: 88, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }], total: 1, page: 1, pageSize: 20 }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValueOnce(axiosResponse({ id: 88, brand: 'atlas', status: 'ended', samples: [] }));
    render(<BrandExperimentPanel brand="atlas" canManage experimentId={88} onExperimentChange={onExperimentChange} />);
    fireEvent.click(await screen.findByRole('button', { name: '新建实验' }));
    expect(onExperimentChange).toHaveBeenCalledWith(undefined);
  });

  it('clears the previous brand current experiment before loading an empty new brand', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockImplementation((brand) => Promise.resolve(axiosResponse(
      brand === 'atlas'
        ? { brand, current: { id: 9, brand, status: 'ended', treatmentCount: 20, controlCount: 20 }, items: [], total: 1, page: 1, pageSize: 20 }
        : { brand, current: null, items: [], total: 0, page: 1, pageSize: 20 },
    )));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValue(axiosResponse({ id: 9, brand: 'atlas', status: 'ended', samples: [] }));
    vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mockResolvedValue(axiosResponse({ brand: 'harbor', sampleDataDate: '2026-07-08', items: [] }));
    const { rerender } = render(<BrandExperimentPanel brand="atlas" canManage />);
    expect(await screen.findByRole('button', { name: '返回当前实验' })).toBeInTheDocument();

    rerender(<BrandExperimentPanel brand="harbor" canManage />);

    expect(await screen.findByText('HARBOR A/B Test')).toBeInTheDocument();
    expect(await screen.findByText('选择 20+20 固定 Query 建立新实验')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '返回当前实验' })).not.toBeInTheDocument();
  });

  it('keeps loading while an obsolete brand request finishes before the active request', async () => {
    let resolveAtlas: (value: AxiosResponse<TopQueryBrandExperimentListData>) => void = () => undefined;
    let resolveHm: (value: AxiosResponse<TopQueryBrandExperimentListData>) => void = () => undefined;
    const atlasPromise = new Promise<AxiosResponse<TopQueryBrandExperimentListData>>((resolve) => { resolveAtlas = resolve; });
    const hmPromise = new Promise<AxiosResponse<TopQueryBrandExperimentListData>>((resolve) => { resolveHm = resolve; });
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockImplementation((brand) => brand === 'atlas' ? atlasPromise : hmPromise);
    vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mockResolvedValue(axiosResponse({ brand: 'harbor', sampleDataDate: '2026-07-08', items: [] }));
    const { rerender } = render(<BrandExperimentPanel brand="atlas" canManage />);
    await waitFor(() => expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledWith('atlas', expect.anything(), expect.anything()));
    rerender(<BrandExperimentPanel brand="harbor" canManage />);
    await waitFor(() => expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledWith('harbor', expect.anything(), expect.anything()));

    resolveAtlas(axiosResponse({ brand: 'atlas', current: null, items: [], total: 0, page: 1, pageSize: 20 }));
    await Promise.resolve();
    expect(screen.getByText('正在加载实验数据')).toBeInTheDocument();

    resolveHm(axiosResponse({ brand: 'harbor', current: null, items: [], total: 0, page: 1, pageSize: 20 }));
    expect(await screen.findByText('HARBOR A/B Test')).toBeInTheDocument();
  });

  it('loads history filters without reloading the current report endpoints', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({
      brand: 'atlas', current: { id: 9, brand: 'atlas', status: 'running', treatmentCount: 20, controlCount: 20 }, items: [], total: 45, page: 1, pageSize: 20,
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentHistory).mockResolvedValue(axiosResponse({ brand: 'atlas', current: null, items: [], total: 45, page: 1, pageSize: 20 }));
    render(<BrandExperimentPanel brand="atlas" canManage />);
    await screen.findByText('实验质量正常');
    expect(dashboardApi.getTopQueryExperimentHistory).not.toHaveBeenCalled();

    fireEvent.mouseDown(screen.getByLabelText('实验状态'));
    const option = screen.getAllByText('已结束').find((node) => node.closest('.ant-select-item-option'));
    fireEvent.click(option!);

    await waitFor(() => expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByTitle('2'));
    await waitFor(() => expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenCalledTimes(2));
    expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenLastCalledWith('atlas', expect.objectContaining({ status: 'ended', page: 2 }), expect.anything());
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getTopQueryExperimentDetail).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getTopQueryExperimentReport).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getTopQueryExperimentSamplePool).not.toHaveBeenCalled();
  });

  it('keeps filtered history items when opening a detail and refreshing the current report', async () => {
    const onExperimentChange = vi.fn();
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({
      brand: 'atlas', current: { id: 9, brand: 'atlas', status: 'running', treatmentCount: 20, controlCount: 20 }, items: [{ id: 9, brand: 'atlas', status: 'running', treatmentCount: 20, controlCount: 20 }], total: 1, page: 1, pageSize: 20,
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentHistory).mockResolvedValue(axiosResponse({
      brand: 'atlas', current: null, items: [{ id: 88, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }], total: 1, page: 1, pageSize: 20,
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockImplementation((id) => Promise.resolve(axiosResponse({ id, brand: 'atlas', experimentName: `Experiment ${id}`, status: id === 88 ? 'ended' : 'running', samples: [] })));
    const view = render(<BrandExperimentPanel brand="atlas" canManage onExperimentChange={onExperimentChange} />);
    await screen.findByText('实验质量正常');
    expect(dashboardApi.getTopQueryExperimentHistory).not.toHaveBeenCalled();

    fireEvent.mouseDown(screen.getByLabelText('实验状态'));
    const endedOption = screen.getAllByText('已结束').find((node) => node.closest('.ant-select-item-option'));
    fireEvent.click(endedOption!);
    await waitFor(() => expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText('ATLAS A/B Test #88'));
    expect(onExperimentChange).toHaveBeenCalledWith(88);

    view.rerender(<BrandExperimentPanel brand="atlas" canManage experimentId={88} onExperimentChange={onExperimentChange} />);
    expect(await screen.findByText('Experiment 88')).toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenCalledTimes(1);
    expect(screen.getByText('ATLAS A/B Test #88')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /刷新/ }));
    await waitFor(() => expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(3));
    expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenCalledTimes(1);
    expect(screen.getByText('ATLAS A/B Test #88')).toBeInTheDocument();
    expect(screen.getByLabelText('实验状态').closest('.ant-select')).toHaveTextContent('已结束');
  });

  it('refreshes history after cancellation with the active filters', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus)
      .mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: { id: 10, brand: 'atlas', status: 'scheduled', treatmentCount: 20, controlCount: 20 }, items: [], total: 1, page: 1, pageSize: 20 }))
      .mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: null, items: [{ id: 10, brand: 'atlas', status: 'cancelled', treatmentCount: 20, controlCount: 20 }], total: 1, page: 1, pageSize: 20 }));
    vi.mocked(dashboardApi.getTopQueryExperimentHistory).mockResolvedValue(axiosResponse({ brand: 'atlas', current: null, items: [{ id: 10, brand: 'atlas', status: 'scheduled', treatmentCount: 20, controlCount: 20 }], total: 1, page: 1, pageSize: 20 }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail)
      .mockResolvedValueOnce(axiosResponse({ id: 10, brand: 'atlas', experimentName: 'ATLAS A/B Test', status: 'scheduled', samples: [] }))
      .mockResolvedValueOnce(axiosResponse({ id: 10, brand: 'atlas', experimentName: 'ATLAS A/B Test', status: 'cancelled', samples: [] }));
    render(<BrandExperimentPanel brand="atlas" canManage />);
    await screen.findByRole('button', { name: '取消实验' });
    expect(dashboardApi.getTopQueryExperimentHistory).not.toHaveBeenCalled();

    fireEvent.mouseDown(screen.getByLabelText('实验状态'));
    const scheduledOption = screen.getAllByText('已排期').find((node) => node.closest('.ant-select-item-option'));
    fireEvent.click(scheduledOption!);
    await waitFor(() => expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '取消实验' }));
    fireEvent.click(await screen.findByRole('button', { name: /确\s*认/ }));

    await waitFor(() => expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenCalledTimes(2));
    expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenLastCalledWith('atlas', expect.objectContaining({ status: 'scheduled', page: 1 }), expect.anything());
    expect(screen.getByLabelText('实验状态').closest('.ant-select')).toHaveTextContent('已排期');
    expect(screen.getByText('ATLAS A/B Test #10')).toBeInTheDocument();
  });

  it('keeps a dirty draft unchanged when refresh navigation is rejected', async () => {
    const confirmNavigation = vi.fn(() => false);
    const onDirtyChange = vi.fn();
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({ brand: 'atlas', current: null, items: [], total: 0, page: 1, pageSize: 20 }));
    render(<BrandExperimentPanel brand="atlas" canManage confirmNavigation={confirmNavigation} onDirtyChange={onDirtyChange} />);
    await screen.findByLabelText('实验组 Query');
    expect(dashboardApi.getTopQueryExperimentHistory).not.toHaveBeenCalled();

    fireEvent.mouseDown(screen.getByLabelText('实验组 Query'));
    const option = screen.getAllByText('连衣裙 女士').find((node) => node.closest('.ant-select-item-option'));
    fireEvent.click(option!);
    const plannedEndDate = screen.getByLabelText('计划结束日期').getAttribute('value');
    const requestCounts = {
      brand: vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mock.calls.length,
      history: vi.mocked(dashboardApi.getTopQueryExperimentHistory).mock.calls.length,
      detail: vi.mocked(dashboardApi.getTopQueryExperimentDetail).mock.calls.length,
      pool: vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mock.calls.length,
      report: vi.mocked(dashboardApi.getTopQueryExperimentReport).mock.calls.length,
    };

    fireEvent.click(screen.getByRole('button', { name: /刷新/ }));

    expect(confirmNavigation).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(requestCounts.brand);
    expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenCalledTimes(requestCounts.history);
    expect(dashboardApi.getTopQueryExperimentDetail).toHaveBeenCalledTimes(requestCounts.detail);
    expect(dashboardApi.getTopQueryExperimentSamplePool).toHaveBeenCalledTimes(requestCounts.pool);
    expect(dashboardApi.getTopQueryExperimentReport).toHaveBeenCalledTimes(requestCounts.report);
    expect(screen.getByText('连衣裙 女士', { selector: '.experiment-query-tag' })).toBeInTheDocument();
    expect(screen.getByLabelText('计划结束日期')).toHaveValue(plannedEndDate);
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
  });

  it('reloads server state and clears dirty after refresh navigation is confirmed', async () => {
    const confirmNavigation = vi.fn(() => true);
    const onDirtyChange = vi.fn();
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus)
      .mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: null, items: [], total: 0, page: 1, pageSize: 20 }))
      .mockResolvedValueOnce(axiosResponse({ brand: 'atlas', current: { id: 9, brand: 'atlas', status: 'running', treatmentCount: 20, controlCount: 20 }, items: [], total: 1, page: 1, pageSize: 20 }));
    render(<BrandExperimentPanel brand="atlas" canManage confirmNavigation={confirmNavigation} onDirtyChange={onDirtyChange} />);
    await screen.findByLabelText('实验组 Query');
    fireEvent.mouseDown(screen.getByLabelText('实验组 Query'));
    const option = screen.getAllByText('连衣裙 女士').find((node) => node.closest('.ant-select-item-option'));
    fireEvent.click(option!);

    fireEvent.click(screen.getByRole('button', { name: /刷新/ }));

    expect(confirmNavigation).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('实验质量正常')).toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentDetail).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getTopQueryExperimentReport).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getTopQueryExperimentHistory).not.toHaveBeenCalled();
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it('refreshes immediately without confirmation when the draft is clean', async () => {
    const confirmNavigation = vi.fn(() => false);
    render(<BrandExperimentPanel brand="atlas" canManage confirmNavigation={confirmNavigation} />);
    await screen.findByText('实验质量正常');
    expect(dashboardApi.getTopQueryExperimentHistory).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /刷新/ }));

    await waitFor(() => expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(2));
    expect(confirmNavigation).not.toHaveBeenCalled();
    expect(dashboardApi.getTopQueryExperimentDetail).toHaveBeenCalledTimes(2);
    expect(dashboardApi.getTopQueryExperimentHistory).not.toHaveBeenCalled();
  });

  it('ignores a deferred draft mutation after switching brands', async () => {
    const draftMutation = deferred<AxiosResponse<TopQueryExperimentDetailData>>();
    const onExperimentChange = vi.fn();
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockImplementation((brand) => Promise.resolve(axiosResponse({ brand, current: null, items: [], total: 0, page: 1, pageSize: 20 })));
    vi.mocked(dashboardApi.createTopQueryExperimentDraft).mockImplementation(() => draftMutation.promise as never);
    const view = render(<BrandExperimentPanel brand="atlas" canManage onExperimentChange={onExperimentChange} />);
    fireEvent.mouseDown(await screen.findByLabelText('实验组 Query'));
    fireEvent.click(screen.getAllByText('连衣裙 女士').find((node) => node.closest('.ant-select-item-option'))!);
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
    const savingButton = screen.getByRole('button', { name: /保存草稿/ });
    expect(savingButton).toBeDisabled();
    fireEvent.click(savingButton);
    expect(dashboardApi.createTopQueryExperimentDraft).toHaveBeenCalledTimes(1);

    view.rerender(<BrandExperimentPanel brand="harbor" canManage onExperimentChange={onExperimentChange} />);
    await screen.findByText('HARBOR A/B Test');
    await act(async () => {
      draftMutation.resolve(axiosResponse({ id: 99, brand: 'atlas', experimentName: 'Stale ATLAS Draft', status: 'draft', samples: [] }));
      await draftMutation.promise;
    });

    expect(onExperimentChange).not.toHaveBeenCalledWith(99);
    expect(screen.queryByText('Stale ATLAS Draft')).not.toBeInTheDocument();
  });

  it('does not update or reload after a deferred lifecycle mutation resolves post-unmount', async () => {
    const cancellation = deferred<AxiosResponse<TopQueryExperimentDetailData>>();
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({ brand: 'atlas', current: { id: 10, brand: 'atlas', status: 'scheduled', treatmentCount: 20, controlCount: 20 }, items: [], total: 1, page: 1, pageSize: 20 }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValue(axiosResponse({ id: 10, brand: 'atlas', experimentName: 'Scheduled', status: 'scheduled', samples: [] }));
    vi.mocked(dashboardApi.cancelTopQueryExperiment).mockImplementation(() => cancellation.promise as never);
    const view = render(<BrandExperimentPanel brand="atlas" canManage />);
    fireEvent.click(await screen.findByRole('button', { name: '取消实验' }));
    fireEvent.click(await screen.findByRole('button', { name: /确\s*认/ }));
    expect(dashboardApi.cancelTopQueryExperiment).toHaveBeenCalledTimes(1);
    const brandRequests = vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mock.calls.length;
    const historyRequests = vi.mocked(dashboardApi.getTopQueryExperimentHistory).mock.calls.length;
    view.unmount();

    await act(async () => {
      cancellation.resolve(axiosResponse({ id: 10, brand: 'atlas', experimentName: 'Scheduled', status: 'cancelled', samples: [] }));
      await cancellation.promise;
    });

    expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(brandRequests);
    expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenCalledTimes(historyRequests);
  });

  it('invalidates a deferred save when navigating to same-brand history', async () => {
    const draftMutation = deferred<AxiosResponse<TopQueryExperimentDetailData>>();
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({ brand: 'atlas', current: null, items: [{ id: 88, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }], total: 1, page: 1, pageSize: 20 }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockImplementation((id) => Promise.resolve(axiosResponse({ id, brand: 'atlas', experimentName: `History ${id}`, status: 'ended', samples: [] })));
    vi.mocked(dashboardApi.createTopQueryExperimentDraft).mockImplementation(() => draftMutation.promise);
    const Harness = () => {
      const [selectedExperimentId, setSelectedExperimentId] = React.useState<number>();
      return <BrandExperimentPanel brand="atlas" canManage experimentId={selectedExperimentId} onExperimentChange={setSelectedExperimentId} confirmNavigation={() => true} />;
    };
    render(<Harness />);
    fireEvent.mouseDown(await screen.findByLabelText('实验组 Query'));
    fireEvent.click(screen.getAllByText('连衣裙 女士').find((node) => node.closest('.ant-select-item-option'))!);
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
    fireEvent.click(screen.getByText('ATLAS A/B Test #88'));
    expect(await screen.findByText('History 88')).toBeInTheDocument();
    const brandRequests = vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mock.calls.length;
    const detailRequests = vi.mocked(dashboardApi.getTopQueryExperimentDetail).mock.calls.length;

    await act(async () => {
      draftMutation.resolve(axiosResponse({ id: 99, brand: 'atlas', experimentName: 'Stale Draft', status: 'draft', samples: [] }));
      await draftMutation.promise;
    });

    expect(screen.getByText('History 88')).toBeInTheDocument();
    expect(screen.queryByText('Stale Draft')).not.toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(brandRequests);
    expect(dashboardApi.getTopQueryExperimentDetail).toHaveBeenCalledTimes(detailRequests);
  });

  it('invalidates a deferred lifecycle mutation before returning to the current experiment', async () => {
    const ending = deferred<AxiosResponse<TopQueryExperimentDetailData>>();
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({ brand: 'atlas', current: { id: 9, brand: 'atlas', status: 'running', treatmentCount: 20, controlCount: 20 }, items: [{ id: 88, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }], total: 2, page: 1, pageSize: 20 }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockImplementation((id) => Promise.resolve(axiosResponse({ id, brand: 'atlas', experimentName: id === 88 ? 'History 88' : 'Current 9', status: id === 88 ? 'ended' : 'running', samples: [] })));
    vi.mocked(dashboardApi.endTopQueryExperiment).mockImplementation(() => ending.promise);
    const Harness = () => {
      const [selectedExperimentId, setSelectedExperimentId] = React.useState<number>();
      return <BrandExperimentPanel brand="atlas" canManage experimentId={selectedExperimentId} onExperimentChange={setSelectedExperimentId} confirmNavigation={() => true} />;
    };
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: '结束实验' }));
    fireEvent.click(await screen.findByRole('button', { name: /确\s*认/ }));
    fireEvent.click(screen.getByText('ATLAS A/B Test #88'));
    expect(await screen.findByText('History 88')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '返回当前实验' }));
    expect(await screen.findByText('Current 9')).toBeInTheDocument();
    const brandRequests = vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mock.calls.length;
    const historyRequests = vi.mocked(dashboardApi.getTopQueryExperimentHistory).mock.calls.length;

    await act(async () => {
      ending.resolve(axiosResponse({ id: 9, brand: 'atlas', experimentName: 'Stale Ended 9', status: 'ended', samples: [] }));
      await ending.promise;
    });

    expect(screen.getByText('Current 9')).toBeInTheDocument();
    expect(screen.queryByText('Stale Ended 9')).not.toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(brandRequests);
    expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenCalledTimes(historyRequests);
  });

  it('invalidates a deferred lifecycle mutation when the external experiment id changes', async () => {
    const ending = deferred<AxiosResponse<TopQueryExperimentDetailData>>();
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({ brand: 'atlas', current: { id: 9, brand: 'atlas', status: 'running', treatmentCount: 20, controlCount: 20 }, items: [], total: 1, page: 1, pageSize: 20 }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockImplementation((id) => Promise.resolve(axiosResponse({ id, brand: 'atlas', experimentName: id === 88 ? 'External 88' : 'Current 9', status: id === 88 ? 'ended' : 'running', samples: [] })));
    vi.mocked(dashboardApi.endTopQueryExperiment).mockImplementation(() => ending.promise);
    const view = render(<BrandExperimentPanel brand="atlas" canManage experimentId={9} />);
    fireEvent.click(await screen.findByRole('button', { name: '结束实验' }));
    fireEvent.click(await screen.findByRole('button', { name: /确\s*认/ }));
    view.rerender(<BrandExperimentPanel brand="atlas" canManage experimentId={88} />);
    expect(await screen.findByText('External 88')).toBeInTheDocument();
    const brandRequests = vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mock.calls.length;
    const historyRequests = vi.mocked(dashboardApi.getTopQueryExperimentHistory).mock.calls.length;

    await act(async () => {
      ending.resolve(axiosResponse({ id: 9, brand: 'atlas', experimentName: 'Stale Ended 9', status: 'ended', samples: [] }));
      await ending.promise;
    });

    expect(screen.getByText('External 88')).toBeInTheDocument();
    expect(screen.queryByText('Stale Ended 9')).not.toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(brandRequests);
    expect(dashboardApi.getTopQueryExperimentHistory).toHaveBeenCalledTimes(historyRequests);
  });

  it('clears a stale sample pool when refresh cannot load the current pool', async () => {
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({ brand: 'atlas', current: null, items: [], total: 0, page: 1, pageSize: 20 }));
    render(<BrandExperimentPanel brand="atlas" canManage />);
    await screen.findByText('ATLAS A/B Test');
    expect(screen.getByRole('button', { name: '保存草稿' })).toBeEnabled();

    vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mockRejectedValueOnce(new Error('pool unavailable'));
    fireEvent.click(screen.getByRole('button', { name: /刷新/ }));

    expect(await screen.findByText('样本池加载失败')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存草稿' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '确认排期' })).toBeDisabled();
  });

  it('handles a rejected unified report without an unhandled rejection', async () => {
    const unhandled = vi.fn((event: PromiseRejectionEvent) => event.preventDefault());
    window.addEventListener('unhandledrejection', unhandled);
    vi.mocked(dashboardApi.getTopQueryExperimentReport).mockRejectedValueOnce(new Error('report failed'));

    try {
      render(<BrandExperimentPanel brand="atlas" canManage />);
      expect(await screen.findAllByText('实验数据加载失败')).toHaveLength(2);
      await new Promise((resolve) => window.setTimeout(resolve, 0));
      expect(unhandled).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('unhandledrejection', unhandled);
    }
  });
});
