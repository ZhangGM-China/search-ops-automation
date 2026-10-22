import React, { useState, useEffect } from 'react';
import { Table, Card, Form, Button, Space, message, Popconfirm, Tag, Flex } from 'antd';
import {
  Card as MobCard,
  Button as MobButton,
  SearchBar as MobSearchBar,
  Popup as MobPopup,
  Form as MobForm,
  Selector as MobSelector,
  Space as MobSpace,
  Tag as MobTag,
  Divider as MobDivider,
  ActionSheet,
  InfiniteScroll
} from 'antd-mobile';
import { FilterOutline } from 'antd-mobile-icons';
import dayjs from 'dayjs';
import { dashboardApi } from '@/shared/api';
import type { InventoryTask, QueryFilters } from '@/shared/types';
import useMobile from '@/shared/hooks/useMobile';
import { getApiErrorMessage } from '@/shared/lib/error';
import { OperationsLogTabs, OperationsLogFilterBar, ProductIdentityCell } from '../../components/OperationsLogShared';
import { readOperationsLogFilters } from '../../components/operationsLogFilters';
import OperationsProductDetailModal from '../../components/OperationsProductDetailModal';

const StockTasks: React.FC = () => {
  const savedFilters = readOperationsLogFilters();
  const savedDate = typeof savedFilters.date === 'string' ? savedFilters.date : undefined;
  const savedBrand = typeof savedFilters.brand === 'string' ? savedFilters.brand : undefined;
  const savedSearch = typeof savedFilters.search === 'string' ? savedFilters.search : undefined;
  const savedStatus = typeof savedFilters.status === 'string' ? savedFilters.status : 'monitoring';
  const [form] = Form.useForm<QueryFilters>();
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<InventoryTask[]>([]);
  const [pageSize, setPageSize] = useState(50);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [filterVisible, setFilterVisible] = useState(false);
  const [selectedProduct, setSelectedProduct] = useState<InventoryTask | null>(null);
  const [mobileFilters, setMobileFilters] = useState<QueryFilters>({});
  const isMobile = useMobile();

  const [mobForm] = MobForm.useForm();
  const pageSizeRef = React.useRef(pageSize);

  useEffect(() => {
    pageSizeRef.current = pageSize;
  }, [pageSize]);

  // 获取用户权限范围内的品牌
  const userStr = localStorage.getItem('user');
  const user = userStr ? JSON.parse(userStr) : null;
  const userBrands = user?.brandPermissions || [];

  const allBrands = [
    { label: 'Atlas', value: 'atlas' },
    { label: 'HARBOR', value: 'harbor' },
    { label: 'Summit', value: 'summit' },
    { label: 'Meadow', value: 'meadow' },
    { label: 'Riviera', value: 'riviera' },
  ];

  const allowedBrands = allBrands.filter(b => userBrands.includes(b.value));

  const fetchData = React.useCallback(async (values: QueryFilters = {}, append: boolean = false) => {
    setLoading(true);
    try {
      const { page: p, limit: l, ...rest } = values;
      const currentPage = Number(p || 1);
      const currentLimit = Number(l || pageSizeRef.current);

      // 默认查询监测中的任务
      const params: QueryFilters = {
        status: 'monitoring',
        ...rest,
        page: currentPage,
        limit: currentLimit
      };

      // 移除空参数
      Object.keys(params).forEach(key => {
        if (params[key] === undefined || params[key] === null || params[key] === '') {
          delete params[key];
        }
      });

      const res = await dashboardApi.getStockTasks(params);
      const newData = res.data.items || [];
      const newTotal = res.data.total || 0;

      setData(prev => {
        const nextData = append ? [...prev, ...newData] : newData;
        setHasMore(nextData.length < newTotal);
        return nextData;
      });
      setTotal(newTotal);
      setPage(currentPage);

      if (isMobile) {
        setMobileFilters(params);
      }
    } catch (error: unknown) {
      console.error('Failed to fetch stock tasks:', error);
      message.error(`获取数据失败: ${getApiErrorMessage(error, '未知错误')}`);
    } finally {
      setLoading(false);
    }
  }, [isMobile]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      // DatePicker 需要 Dayjs 实例；sessionStorage 里存的是日期字符串，直接传入会渲染崩溃
      const initial = { date: savedDate ? dayjs(savedDate) : undefined, brand: savedBrand, search: savedSearch, status: savedStatus, page: 1 };
      form.setFieldsValue(initial);
      fetchData(initial);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [fetchData, form, savedDate, savedBrand, savedSearch, savedStatus]);

  const loadMore = async () => {
    if (loading) return;
    await fetchData({ ...mobileFilters, page: page + 1 }, true);
  };

  const handleCancelDemotion = async (record: InventoryTask) => {
    try {
      await dashboardApi.cancelDemotion({
        brand: record.brand,
        query: record.query,
        sku: record.sku,
        taskId: record.id
      });
      message.success('已取消分桶调整并更新监测状态');
      // 局部刷新
      setData(prev => prev.map(item =>
        item.id === record.id ? { ...item, status: 'recovered' } : item
      ));
    } catch {
      message.error('取消失败');
    }
  };

  const handleToggleStatus = async (record: InventoryTask, newStatus: 'monitoring' | 'manually_stopped') => {
    try {
      await dashboardApi.toggleStockTaskStatus(record.id, newStatus);
      message.success(newStatus === 'monitoring' ? '已开启追踪' : '已停止追踪');
      fetchData(form.getFieldsValue());
    } catch {
      message.error('操作失败');
    }
  };

  const getStatusTag = (status: string) => {
    switch(status) {
      case 'monitoring': return <Tag color="blue">监测中</Tag>;
      case 'recovered': return <Tag color="green">已恢复</Tag>;
      case 'auto_stopped': return <Tag color="orange">自动停止</Tag>;
      case 'manually_stopped': return <Tag color="default">手动停止</Tag>;
      default: return <Tag>{status}</Tag>;
    }
  };

  const getMobStatusTag = (status: string) => {
    switch(status) {
      case 'monitoring': return <MobTag color="primary">监测中</MobTag>;
      case 'recovered': return <MobTag color="success">已恢复</MobTag>;
      case 'auto_stopped': return <MobTag color="warning">自动停止</MobTag>;
      case 'manually_stopped': return <MobTag color="default">手动停止</MobTag>;
      default: return <MobTag>{status}</MobTag>;
    }
  };

  const formatBrokenRate = (record: InventoryTask) => {
    const brokenRate = record.lastBrokenRate ?? record.lastStock;
    return brokenRate === undefined || brokenRate === null || brokenRate < 0 ? '-' : `${brokenRate.toFixed(0)}%`;
  };

  const showActionSheet = (record: InventoryTask) => {
    const actions: { text: string; key: string; danger?: boolean }[] = [];
    if (record.status === 'monitoring') {
      actions.push({ text: '取消分桶调整', key: 'cancel' });
      actions.push({ text: '停止追踪', key: 'stop', danger: true });
    } else if (record.status === 'auto_stopped' || record.status === 'manually_stopped') {
      actions.push({ text: '开启追踪', key: 'start' });
    }

    if (actions.length === 0) return;

    ActionSheet.show({
      actions,
      onAction: (action) => {
        if (action.key === 'cancel') handleCancelDemotion(record);
        if (action.key === 'stop') handleToggleStatus(record, 'manually_stopped');
        if (action.key === 'start') handleToggleStatus(record, 'monitoring');
      },
    });
  };

  const renderMobileView = () => (
    <div className="ops-page" style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
      <OperationsLogTabs />
      <div className="ops-surface" style={{ padding: '8px', position: 'sticky', top: 0, zIndex: 5 }}>
        <Flex align="center" gap="small">
          <MobSearchBar
            placeholder='SKU/名称/Query'
            style={{ flex: 1 }}
            onSearch={(val) => fetchData({ ...mobileFilters, search: val, page: 1 })}
            onClear={() => fetchData({ ...mobileFilters, search: '', page: 1 })}
          />
          <MobButton size='small' onClick={() => {
            mobForm.setFieldsValue(mobileFilters);
            setFilterVisible(true);
          }}>
            <FilterOutline /> 筛选
          </MobButton>
        </Flex>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {data.length > 0 ? (
          <>
            {data.map(record => (
              <MobCard
                key={record.id}
                title={<ProductIdentityCell name={record.productName} sku={record.sku} imageUrl={record.imageUrl} />}
                extra={record.brand.toUpperCase()}
                headerStyle={{ fontWeight: 'bold' }}
                style={{ borderRadius: '8px' }}
                onClick={() => showActionSheet(record)}
              >
                <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                  <div style={{ marginTop: '4px' }}>Query: <span style={{ color: 'var(--text-primary)' }}>{record.query}</span></div>
                  <MobDivider style={{ margin: '8px 0' }} />
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <span>断码: {formatBrokenRate(record)}</span>
                    {getMobStatusTag(record.status)}
                  </div>
                  <div style={{ marginTop: '8px', fontSize: '12px' }}>最近检查: {record.updatedAt}</div>
                </div>
              </MobCard>
            ))}
            <InfiniteScroll loadMore={loadMore} hasMore={hasMore} />
          </>
        ) : <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>暂无追踪记录</div>}
      </div>

      <MobPopup
        visible={filterVisible}
        onMaskClick={() => setFilterVisible(false)}
        position='right'
        bodyStyle={{ width: '80vw' }}
      >
        <div style={{ padding: '20px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
            <h3 style={{ margin: 0 }}>筛选条件</h3>
            <Button type="link" size="small" onClick={() => {
              mobForm.setFieldsValue({ brand: undefined, status: 'monitoring' });
            }}>重置</Button>
          </div>
          <MobForm
            form={mobForm}
            layout='vertical'
            onFinish={(vals) => { fetchData({ ...vals, page: 1 }); setFilterVisible(false); }}
            initialValues={mobileFilters}
            footer={
              <MobSpace block direction='vertical'>
                <MobButton block type='submit' color='primary'>确认</MobButton>
                <MobButton block onClick={() => { setFilterVisible(false); }}>取消</MobButton>
              </MobSpace>
            }
          >
            <MobForm.Item name='brand' label='品牌'>
              <MobSelector
                options={allowedBrands}
              />
            </MobForm.Item>
            <MobForm.Item name='status' label='状态'>
              <MobSelector
                options={[
                  { label: '追踪中', value: 'monitoring' },
                  { label: '已停止', value: 'stopped' },
                  { label: '已恢复', value: 'recovered' }
                ]}
              />
            </MobForm.Item>
          </MobForm>
        </div>
      </MobPopup>
    </div>
  );

  const columns = [
    { title: '商品', dataIndex: 'productName', key: 'productName', width: 300, render: (_: unknown, record: InventoryTask) => <ProductIdentityCell name={record.productName} sku={record.sku} imageUrl={record.imageUrl} onClick={() => setSelectedProduct(record)} /> },
    { title: '品牌', dataIndex: 'brand', key: 'brand', width: 110 },
    { title: 'Query', dataIndex: 'query', key: 'query', width: 180 },
    { title: '最近断码', key: 'lastBrokenRate', width: 130, render: (_: unknown, record: InventoryTask) => formatBrokenRate(record) },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status: string) => getStatusTag(status)
    },
    { title: '最近检查时间', dataIndex: 'updatedAt', key: 'updatedAt', width: 180 },
    {
      title: '操作',
      key: 'action',
      width: 200,
      fixed: 'right' as const,
      render: (_: unknown, record: InventoryTask) => (
        <Space size="small">
          {record.status === 'monitoring' && (
            <Popconfirm
              title="确定要取消该商品的分桶调整吗？"
              onConfirm={() => handleCancelDemotion(record)}
              okText="确定"
              cancelText="取消"
            >
              <Button type="link" size="small">取消分桶调整</Button>
            </Popconfirm>
          )}
          {record.status === 'monitoring' ? (
            <Button type="link" size="small" danger onClick={() => handleToggleStatus(record, 'manually_stopped')}>停止追踪</Button>
          ) : (record.status === 'auto_stopped' || record.status === 'manually_stopped') ? (
            <Button type="link" size="small" onClick={() => handleToggleStatus(record, 'monitoring')}>开启追踪</Button>
          ) : null}
        </Space>
      ),
    },
  ];

  return (
    isMobile ? renderMobileView() : (
      <Flex vertical gap="small" className="ops-page">
        <OperationsLogTabs />
        <OperationsLogFilterBar form={form} allowedBrands={allowedBrands} includeStatus onChange={(vals) => fetchData({ ...vals, page: 1 })} />
        <Card size="small" className="operations-log-list-card">
          <Table
            columns={columns}
            dataSource={data}
            loading={loading}
            rowKey="id"
            size="small"
            pagination={{
              current: page,
              pageSize: pageSize,
              total: total,
              showTotal: (value) => `共 ${value} 条`,
              showSizeChanger: true,
              onShowSizeChange: (_, size) => setPageSize(size),
              onChange: (p, s) => {
                fetchData({ ...form.getFieldsValue(), page: p, limit: s });
              },
              pageSizeOptions: ['20', '50', '100'],
              size: 'small'
            }}
            scroll={{ x: 'max-content', y: 'calc(100vh - 360px)' }}
            sticky
          />
        </Card>
        <OperationsProductDetailModal open={Boolean(selectedProduct)} product={selectedProduct} onClose={() => setSelectedProduct(null)} />
      </Flex>
    )
  );
};

export default StockTasks;
