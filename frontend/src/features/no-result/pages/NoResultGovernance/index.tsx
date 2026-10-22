import React, { useEffect, useMemo, useState } from 'react';
import { Button, Descriptions, Flex, Form, Popconfirm, Tag, message, Modal, Space, Typography } from 'antd';
import { Form as MobForm } from 'antd-mobile';
import dayjs from 'dayjs';
import { dashboardApi } from '@/shared/api';
import {
  NoResultAgentTimeline,
  NoResultActionDetail,
  NoResultGovernanceDesktopView,
  NoResultGovernanceMobileView,
} from './components';
import { hasExecutableActions } from './columns';
import {
  actionStatusColorMap,
  actionStatusLabelMap,
  statusColorMap,
  statusLabelMap,
} from './constants';
import { executionButtonText } from './columns';
import type { NoResultGovernanceFilterValues } from './constants';
import useMobile from '@/shared/hooks/useMobile';
import type { NoResultGovernanceItem, QueryFilters } from '@/shared/types';
import { getApiErrorMessage } from '@/shared/lib/error';
import { formatQueryDate } from '@/shared/lib/queryDate';
import { readOperationsLogFilters, writeOperationsLogFilters } from '../../../operations/components/operationsLogFilters';

const { Text } = Typography;

const NoResultGovernance: React.FC = () => {
  const savedFilters = readOperationsLogFilters();
  const savedDate = typeof savedFilters.date === 'string' ? savedFilters.date : '';
  const savedBrand = typeof savedFilters.brand === 'string' ? savedFilters.brand : undefined;
  const savedSearch = typeof savedFilters.search === 'string' ? savedFilters.search : undefined;
  const [form] = Form.useForm<NoResultGovernanceFilterValues>();
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<NoResultGovernanceItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [hasMore, setHasMore] = useState(true);
  const [filterVisible, setFilterVisible] = useState(false);
  const [pickerVisible, setPickerVisible] = useState(false);
  const [mobileFilters, setMobileFilters] = useState<NoResultGovernanceFilterValues>({ date: dayjs().toDate() });
  const [detailRecord, setDetailRecord] = useState<NoResultGovernanceItem | null>(null);
  const isMobile = useMobile();
  const [mobForm] = MobForm.useForm<NoResultGovernanceFilterValues>();
  const mountedRef = React.useRef(true);

  const normalizeMobileValues = (values: NoResultGovernanceFilterValues): NoResultGovernanceFilterValues => ({
    ...values,
    brand: Array.isArray(values.brand) ? values.brand[0] : values.brand,
    status: Array.isArray(values.status) ? values.status[0] : values.status,
  });

  const fetchData = React.useCallback(async (values: QueryFilters = {}, append = false) => {
    if (!mountedRef.current) return;
    setLoading(true);
    try {
      const params: QueryFilters = {
        ...values,
        date: formatQueryDate(values.date) || dayjs().format('YYYY-MM-DD'),
        page: values.page || page,
        limit: values.limit || pageSize,
      };
      Object.keys(params).forEach((key) => {
        if (params[key] === undefined || params[key] === null || params[key] === '') delete params[key];
      });
      const res = await dashboardApi.getNoResultGovernance(params);
      if (!mountedRef.current) return;
      const nextItems = res.data.items || [];
      const nextTotal = res.data.total || 0;
      setData((prev) => {
        const merged = append ? [...prev, ...nextItems] : nextItems;
        setHasMore(merged.length < nextTotal);
        return merged;
      });
      setTotal(nextTotal);
      if (isMobile) {
        setMobileFilters(values as NoResultGovernanceFilterValues);
      }
    } catch (error: unknown) {
      if (!mountedRef.current) return;
      message.error(`获取无结果词记录失败: ${getApiErrorMessage(error, '未知错误')}`);
    } finally {
      if (mountedRef.current) {
        setLoading(false);
      }
    }
  }, [isMobile, page, pageSize]);

  useEffect(() => {
    mountedRef.current = true;
    const initialDate = savedDate ? dayjs(savedDate) : dayjs();
    const initialValues: NoResultGovernanceFilterValues = {
      date: initialDate,
      brand: savedBrand,
      query_text: savedSearch,
    };
    form.setFieldsValue(initialValues);
    const timer = window.setTimeout(() => {
      void fetchData({ ...initialValues, page: 1, limit: pageSize });
    }, 0);
    return () => {
      window.clearTimeout(timer);
      mountedRef.current = false;
    };
  }, [fetchData, form, pageSize, savedDate, savedBrand, savedSearch]);

  const handleSearch = (values: QueryFilters) => {
    writeOperationsLogFilters({ date: values.date, brand: values.brand, search: values.query_text });
    setPage(1);
    fetchData({ ...values, page: 1, limit: pageSize });
  };

  const loadMore = async () => {
    if (loading) return;
    const nextPage = page + 1;
    setPage(nextPage);
    await fetchData({ ...mobileFilters, page: nextPage, limit: pageSize }, true);
  };

  const handleRollbackBatch = React.useCallback(async (batchId: number) => {
    try {
      await dashboardApi.rollbackNoResultBatch(batchId);
      message.success('已提交批次回滚');
      await fetchData({ ...form.getFieldsValue(), page, limit: pageSize });
    } catch (error: unknown) {
      message.error(`回滚失败: ${getApiErrorMessage(error, '未知错误')}`);
    }
  }, [fetchData, form, page, pageSize]);

  const handleRollbackQuery = React.useCallback(async (queryId: number) => {
    try {
      await dashboardApi.rollbackNoResultQuery(queryId);
      message.success('已提交 Query 回滚');
      await fetchData({ ...form.getFieldsValue(), page, limit: pageSize });
    } catch (error: unknown) {
      message.error(`回滚失败: ${getApiErrorMessage(error, '未知错误')}`);
    }
  }, [fetchData, form, page, pageSize]);

  const handleRollbackBrandDate = React.useCallback(async () => {
    const values = form.getFieldsValue();
    const brand = Array.isArray(values.brand) ? values.brand[0] : values.brand;
    const date = formatQueryDate(values.date);
    if (!brand || !date) {
      message.warning('请先选择品牌和日期');
      return;
    }
    try {
      await dashboardApi.rollbackNoResultBrandDate(brand, date);
      message.success('已提交品牌日期回滚');
      await fetchData({ ...values, page, limit: pageSize });
    } catch (error: unknown) {
      message.error(`回滚失败: ${getApiErrorMessage(error, '未知错误')}`);
    }
  }, [fetchData, form, page, pageSize]);

  const handleManualAction = React.useCallback(async (actionId: number, decision: 'approve' | 'reject') => {
    try {
      if (decision === 'approve') {
        await dashboardApi.approveNoResultAction(actionId, '运营人工批准');
        message.success('已批准执行图谱动作');
      } else {
        await dashboardApi.rejectNoResultAction(actionId, '运营人工拒绝');
        message.success('已拒绝图谱动作');
      }
      await fetchData({ ...form.getFieldsValue(), page, limit: pageSize });
    } catch (error: unknown) {
      message.error(`人工复核操作失败: ${getApiErrorMessage(error, '未知错误')}`);
    }
  }, [fetchData, form, page, pageSize]);

  const applyExecutionProgress = React.useCallback((
    record: NoResultGovernanceItem,
    progress: { status: string; actions?: Array<{ id: number; status: string; errorMessage?: string | null }> },
  ): NoResultGovernanceItem => {
    const progressById = new Map((progress.actions || []).map((action) => [action.id, action]));
    return {
      ...record,
      executionStatus: progress.status,
      actions: record.actions.map((action) => {
        const nextAction = progressById.get(action.id);
        return nextAction ? {
          ...action,
          status: nextAction.status,
          errorMessage: nextAction.errorMessage ?? action.errorMessage,
        } : action;
      }),
    };
  }, []);

  const updateRecordExecutionProgress = React.useCallback((
    queryId: number,
    progress: { status: string; actions?: Array<{ id: number; status: string; errorMessage?: string | null }> },
  ) => {
    setData((current) => current.map((record) => record.id === queryId ? applyExecutionProgress(record, progress) : record));
    setDetailRecord((current) => current?.id === queryId ? applyExecutionProgress(current, progress) : current);
  }, [applyExecutionProgress]);

  const handleExecuteTask = React.useCallback(async (record: NoResultGovernanceItem) => {
    const currentStatus = record.executionStatus || 'idle';
    try {
      const result = currentStatus === 'running'
        ? await dashboardApi.pauseNoResultQueryExecution(record.id, '暂停无结果词任务')
        : currentStatus === 'paused'
          ? await dashboardApi.resumeNoResultQueryExecution(record.id, '继续无结果词任务')
          : await dashboardApi.startNoResultQueryExecution(record.id, '无结果词一键执行');

      updateRecordExecutionProgress(record.id, result.data);
      if (result.data.status === 'completed') {
        message.success('图谱治理任务已完成');
      } else if (result.data.status === 'paused') {
        message.info('已暂停后续图谱动作');
      } else {
        message.success('已提交图谱治理任务');
      }
    } catch (error: unknown) {
      message.error(`执行失败: ${getApiErrorMessage(error, '未知错误')}`);
    }
  }, [updateRecordExecutionProgress]);

  const handleOpenDetail = React.useCallback((record: NoResultGovernanceItem) => {
    setDetailRecord(record);
  }, []);

  const columns = useMemo(() => {
    return [
      {
        title: 'Query',
        dataIndex: 'query',
        key: 'query',
        width: 180,
        fixed: 'left' as const,
        render: (value: string, record: NoResultGovernanceItem) => (
          <Flex vertical gap={2}>
            <Text strong>{value}</Text>
            <Text type="secondary">{record.brand}</Text>
          </Flex>
        ),
      },
      {
        title: '结果数变化',
        key: 'resultCount',
        width: 120,
        render: (_: unknown, record: NoResultGovernanceItem) => `${record.beforeResultCount} -> ${record.afterResultCount}`,
      },
      {
        title: '搜索 PV',
        dataIndex: 'searchPv',
        key: 'searchPv',
        width: 90,
      },
      {
        title: '状态',
        dataIndex: 'status',
        key: 'status',
        width: 120,
        render: (value: string) => <Tag color={statusColorMap[value] || 'default'}>{statusLabelMap[value] || value}</Tag>,
      },
      {
        title: 'AI 调整策略',
        dataIndex: 'finalStrategy',
        key: 'finalStrategy',
        width: 280,
        render: (value: string, record: NoResultGovernanceItem) => value || record.aiReason || '-',
      },
      {
        title: '图谱动作',
        key: 'actions',
        width: 260,
        render: (_: unknown, record: NoResultGovernanceItem) => (
          <Flex vertical gap={4}>
            {record.actions.length === 0 ? <Text type="secondary">无动作</Text> : record.actions.map((action) => (
              <Flex key={action.id} gap={6} wrap>
                <Tag color={actionStatusColorMap[action.status] || 'default'}>
                  {actionStatusLabelMap[action.status] || action.status}
                </Tag>
                <Text>{action.targetInterface}</Text>
              </Flex>
            ))}
          </Flex>
        ),
      },
      {
        title: '操作',
        key: 'actionsColumn',
        width: 240,
        fixed: 'right' as const,
        render: (_: unknown, record: NoResultGovernanceItem) => (
          <Flex gap={6} wrap>
            <Button size="small" onClick={() => handleOpenDetail(record)}>
              详情
            </Button>
            <Button
              size="small"
              type="primary"
              aria-label={executionButtonText(record.executionStatus)}
              disabled={record.executionStatus === 'completed' || (!record.executionStatus && !hasExecutableActions(record))}
              onClick={() => void handleExecuteTask(record)}
            >
              {executionButtonText(record.executionStatus)}
            </Button>
            <Popconfirm
              title="确认回滚该 Query？"
              description="系统只会回滚当前 Query 的成功图谱动作，并重新进入重打标签链路。"
              onConfirm={() => void handleRollbackQuery(record.id)}
              okText="确定"
              cancelText="取消"
            >
              <Button size="small" danger disabled={record.status === 'rolled_back'}>
                回滚Query
              </Button>
            </Popconfirm>
            <Popconfirm
              title="确认回滚该批次？"
              description="系统会先做冲突检测，再按动作逆序执行补偿；存在冲突时会转人工复核。"
              onConfirm={() => void handleRollbackBatch(record.batchId)}
              okText="确定"
              cancelText="取消"
            >
              <Button size="small" danger disabled={record.status === 'rolled_back'}>
                回滚批次
              </Button>
            </Popconfirm>
          </Flex>
        ),
      },
    ];
  }, [handleExecuteTask, handleOpenDetail, handleRollbackBatch, handleRollbackQuery]);

  return (
    <>
      {isMobile ? (
        <NoResultGovernanceMobileView
          data={data}
          loading={loading}
          page={page}
          pageSize={pageSize}
          hasMore={hasMore}
          filterVisible={filterVisible}
          pickerVisible={pickerVisible}
          mobileFilters={mobileFilters}
          mobForm={mobForm}
          onOpenDetail={handleOpenDetail}
          onExecuteTask={handleExecuteTask}
          onFetchData={fetchData}
          onLoadMore={loadMore}
          onFilterVisibleChange={setFilterVisible}
          onPickerVisibleChange={setPickerVisible}
          normalizeMobileValues={normalizeMobileValues}
          setMobileFilters={setMobileFilters}
          setPage={setPage}
        />
      ) : (
        <NoResultGovernanceDesktopView
          form={form}
          data={data}
          total={total}
          loading={loading}
          page={page}
          pageSize={pageSize}
          columns={columns}
          onSearch={handleSearch}
          onFetchPage={(nextPage, nextPageSize) => {
            setPage(nextPage);
            setPageSize(nextPageSize);
            fetchData({ ...form.getFieldsValue(), page: nextPage, limit: nextPageSize });
          }}
          onRollbackBrandDate={handleRollbackBrandDate}
        />
      )}
      <Modal
        title="动作详情"
        open={Boolean(detailRecord)}
        onCancel={() => setDetailRecord(null)}
        footer={null}
        width={920}
        destroyOnHidden
      >
        {detailRecord ? (
          <Space orientation="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions
              bordered
              size="small"
              column={2}
              items={[
                { key: 'query', label: 'Query', children: detailRecord.query },
                { key: 'brand', label: '品牌', children: detailRecord.brand },
                { key: 'status', label: '治理状态', children: statusLabelMap[detailRecord.status] || detailRecord.status },
                { key: 'result', label: '结果数变化', children: `${detailRecord.beforeResultCount} -> ${detailRecord.afterResultCount}` },
                { key: 'strategy', label: 'AI 调整策略', span: 2, children: detailRecord.finalStrategy || detailRecord.aiReason || '-' },
              ]}
            />
            <NoResultAgentTimeline events={detailRecord.agentTimeline} />
            <Button
              type="primary"
              aria-label={`详情${executionButtonText(detailRecord.executionStatus)}`}
              disabled={detailRecord.executionStatus === 'completed'}
              onClick={() => handleExecuteTask(detailRecord)}
            >
              {executionButtonText(detailRecord.executionStatus)}
            </Button>
            {detailRecord.actions.length === 0 ? <Text type="secondary">暂无图谱动作</Text> : detailRecord.actions.map((action) => (
              <NoResultActionDetail
                key={action.id}
                action={action}
                onApprove={(actionId) => handleManualAction(actionId, 'approve')}
                onReject={(actionId) => handleManualAction(actionId, 'reject')}
              />
            ))}
          </Space>
        ) : null}
      </Modal>
    </>
  );
};

export default NoResultGovernance;
