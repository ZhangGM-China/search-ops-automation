import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { message } from 'antd';
import NoResultGovernance from './index';
import { dashboardApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

vi.mock('@/shared/api', () => ({
  dashboardApi: {
    getNoResultGovernance: vi.fn(),
    rollbackNoResultBatch: vi.fn(),
    rollbackNoResultQuery: vi.fn(),
    rollbackNoResultBrandDate: vi.fn(),
	    approveNoResultAction: vi.fn(),
	    rejectNoResultAction: vi.fn(),
	    executeNoResultAction: vi.fn(),
    startNoResultQueryExecution: vi.fn(),
    pauseNoResultQueryExecution: vi.fn(),
    resumeNoResultQueryExecution: vi.fn(),
	  },
	}));

describe('NoResultGovernance Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(message, 'success').mockImplementation(() => undefined as never);
    vi.spyOn(message, 'info').mockImplementation(() => undefined as never);
    vi.spyOn(message, 'error').mockImplementation(() => undefined as never);
    vi.spyOn(message, 'warning').mockImplementation(() => undefined as never);
    vi.mocked(dashboardApi.getNoResultGovernance).mockResolvedValue(
      axiosResponse({
        total: 1,
        items: [
          {
            id: 1,
            batchId: 7,
            dataDate: '2026-06-22',
            brand: 'summit',
            query: '露营天幕',
            status: 'retagged_pending_review',
            aiDecision: 'process',
            aiReason: '购物意图明确',
            finalStrategy: '新增天幕标签并建立露营从属关系',
            beforeResultCount: 0,
            afterResultCount: 8,
            searchPv: 42,
            searchUv: null,
            executionStatus: 'idle',
            agentTimeline: [
              {
                id: 101,
                stage: 'intent_filter',
                eventType: 'reasoning',
                toolName: null,
                summary: '具备购物搜索意图',
                payload: { decision: 'process' },
                createdAt: '2026-06-22 09:01:00',
              },
              {
                id: 102,
                stage: 'diagnosis_strategy',
                eventType: 'final_strategy',
                toolName: null,
                summary: '缺少从属关系',
                payload: { attributionType: 'subordinate_missing' },
                createdAt: '2026-06-22 09:04:00',
              },
            ],
            createdAt: '2026-06-22 09:00:00',
            processedAt: '2026-06-22 09:05:00',
            actions: [
              {
                id: 11,
                actionType: 'add_relationship',
                targetInterface: 'tagRelationship/addRelationship',
                status: 'success',
                rollbackStatus: 'pending',
                riskLevel: 'medium',
                agentReason: '建立从属关系提升露营召回',
                requestPayload: { tagName: '天幕' },
                responsePayload: { code: 1 },
                beforeSnapshot: {},
                rollbackPayload: { id: 'rel-1' },
                createdAt: '2026-06-22 09:05:00',
              },
	              {
	                id: 12,
                actionType: 'delete_relationship',
                targetInterface: 'tagRelationship/deleteRelationship',
                status: 'manual_review',
                rollbackStatus: 'not_required',
                riskLevel: 'high',
                agentReason: '原关系影响露营天幕召回，需要人工确认后删除',
                requestPayload: {
                  id: 'rel-risky',
                  riskExplanation: '删除关系可能影响已有 Query 召回',
                  rollbackSuggestion: '使用删除前快照重建原关系',
                },
                responsePayload: {},
                beforeSnapshot: { id: 'rel-risky', tagName: '天幕', responseTagName: '配件' },
                rollbackPayload: {},
                errorMessage: '高风险动作等待人工复核',
	                createdAt: '2026-06-22 09:06:00',
	              },
	              {
	                id: 13,
	                actionType: 'add_tag',
	                targetInterface: 'tag/addTag',
	                status: 'pending_execute',
	                rollbackStatus: 'not_required',
	                riskLevel: 'low',
	                agentReason: 'dry-run 阶段生成新标签，等待人工执行',
	                requestPayload: { name: '天幕' },
	                responsePayload: { message: 'dry_run_pending_execute' },
	                beforeSnapshot: {},
	                rollbackPayload: {},
	                errorMessage: 'dry_run: waiting for manual execution',
	                createdAt: '2026-06-22 09:07:00',
	              },
	            ],
	          },
        ],
      })
    );
    vi.mocked(dashboardApi.rollbackNoResultBatch).mockResolvedValue(
      axiosResponse({ batchId: 7, rolledBackActions: 1, status: 'rolled_back' })
    );
    vi.mocked(dashboardApi.rollbackNoResultQuery).mockResolvedValue(
      axiosResponse({ queryId: 1, rolledBackActions: 1, status: 'rolled_back' })
    );
    vi.mocked(dashboardApi.rollbackNoResultBrandDate).mockResolvedValue(
      axiosResponse({ brand: 'summit', date: '2026-06-22', batchCount: 1, rolledBackActions: 1, status: 'rolled_back' })
    );
    vi.mocked(dashboardApi.approveNoResultAction).mockResolvedValue(
      axiosResponse({ actionId: 12, status: 'success' })
    );
	    vi.mocked(dashboardApi.rejectNoResultAction).mockResolvedValue(
	      axiosResponse({ actionId: 12, status: 'rejected' })
	    );
	    vi.mocked(dashboardApi.executeNoResultAction).mockResolvedValue(
	      axiosResponse({ actionId: 13, status: 'success' })
	    );
    vi.mocked(dashboardApi.startNoResultQueryExecution).mockResolvedValue(
      axiosResponse({
        queryId: 1,
        status: 'running',
        executedActions: 1,
        actions: [{ id: 13, status: 'success' }],
      })
    );
    vi.mocked(dashboardApi.pauseNoResultQueryExecution).mockResolvedValue(
      axiosResponse({
        queryId: 1,
        status: 'paused',
        executedActions: 1,
        actions: [{ id: 13, status: 'success' }],
      })
    );
    vi.mocked(dashboardApi.resumeNoResultQueryExecution).mockResolvedValue(
      axiosResponse({
        queryId: 1,
        status: 'completed',
        executedActions: 0,
        actions: [{ id: 13, status: 'success' }],
      })
    );
	  });

  it('displays no-result governance records and AI graph actions', async () => {
    render(<NoResultGovernance />);

    expect(await screen.findByText('露营天幕')).toBeInTheDocument();
    expect(screen.getByText('已重打标签待复查')).toBeInTheDocument();
    expect(screen.getByText('0 -> 8')).toBeInTheDocument();
    expect(screen.getByText('新增天幕标签并建立露营从属关系')).toBeInTheDocument();
    expect(screen.getByText('tagRelationship/addRelationship')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /回滚品牌日期/i })).toBeInTheDocument();
  });

  it('shows audit details for every graph action before manual approval or rollback', async () => {
    render(<NoResultGovernance />);

    await screen.findByText('露营天幕');
    fireEvent.click(screen.getByRole('button', { name: /详\s*情/i }));

    expect(await screen.findByText('动作详情')).toBeInTheDocument();
    expect(screen.getAllByText('Agent 推理过程').length).toBeGreaterThan(0);
    expect(screen.getAllByText('具备购物搜索意图').length).toBeGreaterThan(0);
    expect(screen.getAllByText('缺少从属关系').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/"attributionType": "subordinate_missing"/).length).toBeGreaterThan(0);
	    expect(screen.getAllByText('待人工复核').length).toBeGreaterThan(0);
    expect(screen.getAllByText('高风险动作等待人工复核').length).toBeGreaterThan(0);
    expect(screen.getAllByText('原关系影响露营天幕召回，需要人工确认后删除').length).toBeGreaterThan(0);
    expect(screen.getAllByText('删除关系可能影响已有 Query 召回').length).toBeGreaterThan(0);
    expect(screen.getAllByText('使用删除前快照重建原关系').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/"id": "rel-risky"/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/"responseTagName": "配件"/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/"code": 1/).length).toBeGreaterThan(0);
    expect(screen.getByText(/"id": "rel-1"/)).toBeInTheDocument();
  });

  it('rolls back one query from the table', async () => {
    render(<NoResultGovernance />);

    await screen.findByText('露营天幕');
    fireEvent.click(screen.getByRole('button', { name: /回滚Query/i }));
    fireEvent.click(screen.getByText('确 定'));

    await waitFor(() => {
      expect(dashboardApi.rollbackNoResultQuery).toHaveBeenCalledWith(1);
    });
  });

  it('rolls back a governance batch from the table', async () => {
    render(<NoResultGovernance />);

    await screen.findByText('露营天幕');
    fireEvent.click(screen.getByRole('button', { name: /回滚批次/i }));
    fireEvent.click(screen.getByText('确 定'));

    await waitFor(() => {
      expect(dashboardApi.rollbackNoResultBatch).toHaveBeenCalledWith(7);
    });
  });

  it('approves or rejects a manual-review graph action from the detail modal', async () => {
    render(<NoResultGovernance />);

    await screen.findByText('露营天幕');
    fireEvent.click(screen.getByRole('button', { name: /详\s*情/i }));
    fireEvent.click(await screen.findByRole('button', { name: /批准执行/i }));

    await waitFor(() => {
      expect(dashboardApi.approveNoResultAction).toHaveBeenCalledWith(12, '运营人工批准');
    });

    fireEvent.click(screen.getByRole('button', { name: /拒绝动作/i }));

	    await waitFor(() => {
	      expect(dashboardApi.rejectNoResultAction).toHaveBeenCalledWith(12, '运营人工拒绝');
    });
  });

  it('runs query-level one-click execution from table and detail modal', async () => {
    render(<NoResultGovernance />);

    await screen.findByText('露营天幕');
	    expect(screen.getAllByText('待执行').length).toBeGreaterThan(0);
	    fireEvent.click(screen.getByRole('button', { name: '一键执行' }));

    await waitFor(() => {
      expect(dashboardApi.startNoResultQueryExecution).toHaveBeenCalledWith(1, '无结果词一键执行');
    });
    expect(await screen.findByRole('button', { name: '暂停任务' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /详\s*情/i }));
    fireEvent.click(await screen.findByRole('button', { name: '暂停任务' }));
    expect(await screen.findByRole('button', { name: '继续任务' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '继续任务' }));
    const completedButtons = await screen.findAllByRole('button', { name: '已完成' });
    expect(completedButtons[0]).toBeDisabled();
    expect(dashboardApi.pauseNoResultQueryExecution).toHaveBeenCalledWith(1, '暂停无结果词任务');
    expect(dashboardApi.resumeNoResultQueryExecution).toHaveBeenCalledWith(1, '继续无结果词任务');
  }, 10000);


});
