import React, { useState, useEffect } from 'react';
import { Table, Card, Form, Input, Button, message, Popconfirm, Flex, Space } from 'antd';
import { 
  Card as MobCard, 
  Button as MobButton, 
  SearchBar as MobSearchBar,
  Popup as MobPopup,
  Form as MobForm,
  DatePicker as MobDatePicker,
  Selector as MobSelector,
  Space as MobSpace,
  Tag as MobTag,
  Divider as MobDivider,
  ActionSheet,
  InfiniteScroll
} from 'antd-mobile';
import { FilterOutline } from 'antd-mobile-icons';
import { dashboardApi } from '@/shared/api';
import type { ActionLog, QueryFilters } from '@/shared/types';
import dayjs from 'dayjs';
import useMobile from '@/shared/hooks/useMobile';
import { getApiErrorMessage } from '@/shared/lib/error';
import { formatQueryDate } from '@/shared/lib/queryDate';
import { OperationsLogTabs, OperationsLogFilterBar, ProductIdentityCell } from '../../components/OperationsLogShared';
import { readOperationsLogFilters } from '../../components/operationsLogFilters';
import OperationsProductDetailModal from '../../components/OperationsProductDetailModal';

const BlacklistLogs: React.FC = () => {
  const savedFilters = readOperationsLogFilters();
  const savedDate = typeof savedFilters.date === 'string' ? savedFilters.date : '';
  const savedBrand = typeof savedFilters.brand === 'string' ? savedFilters.brand : undefined;
  const savedSearch = typeof savedFilters.search === 'string' ? savedFilters.search : undefined;
  const [form] = Form.useForm<QueryFilters>();
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<ActionLog[]>([]);
  const [pageSize, setPageSize] = useState(50);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [filterVisible, setFilterVisible] = useState(false);
  const [pickerVisible, setPickerVisible] = useState(false);
  const [selectedProduct, setSelectedProduct] = useState<ActionLog | null>(null);
  const [mobileFilters, setMobileFilters] = useState<QueryFilters>({ date: dayjs().toDate() });
  const [actionKey, setActionKey] = useState<string | null>(null);
  const [bulkLoading, setBulkLoading] = useState(false);
  const isMobile = useMobile();

  const [mobForm] = MobForm.useForm();

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
    { label: 'Lumen', value: 'lumen' },
  ];

  const allowedBrands = allBrands.filter(b => userBrands.includes(b.value));

  const fetchData = React.useCallback(async (values: QueryFilters = {}, append: boolean = false) => {
    setLoading(true);
    try {
      const { date, page: p, limit: l, ...rest } = values;
      const currentPage = Number(p || 1);
      const currentLimit = Number(l || pageSize);
      
      const params: QueryFilters = {
        ...rest,
        date: formatQueryDate(date),
        page: currentPage,
        limit: currentLimit,
      };

      // 移除空参数
      Object.keys(params).forEach(key => {
        if (params[key] === undefined || params[key] === null || params[key] === '') {
          delete params[key];
        }
      });

      const res = await dashboardApi.getBlacklistLogs(params);
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
        setMobileFilters(values);
      }
    } catch (error: unknown) {
      console.error('Failed to fetch blacklist logs:', error);
      message.error(`获取数据失败: ${getApiErrorMessage(error, '未知错误')}`);
    } finally {
      setLoading(false);
    }
  }, [isMobile, pageSize]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const initial = {
        date: savedDate ? dayjs(savedDate) : dayjs(),
        brand: savedBrand,
        search: savedSearch,
        page: 1,
      };
      form.setFieldsValue(initial);
      fetchData(initial);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [fetchData, form, savedDate, savedBrand, savedSearch]);

  const onSearch = (vals: QueryFilters) => {
    fetchData({ ...vals, page: 1 });
  };

  const loadMore = async () => {
    if (loading) return;
    await fetchData({ ...mobileFilters, page: page + 1 }, true);
  };

  const handleCancelBlacklist = async (record: ActionLog, reason = '') => {
    const currentActionKey = `${record.id}:cancel`;
    setActionKey(currentActionKey);
    try {
      const payload: { brand: string; query: string; sku: string; reason?: string } = {
        brand: record.brand,
        query: record.query,
        sku: record.sku,
      };
      if (reason.trim()) payload.reason = reason.trim();
      await dashboardApi.cancelBlacklist(payload);
      message.success('已取消拉黑并同步至系统');
      // 局部刷新：更新对应行的取消状态
      setData(prev => prev.map(item => 
        item.id === record.id ? { ...item, isCanceled: true } : item
      ));
      setCancelReasons((prev) => {
        const next = { ...prev };
        delete next[record.id];
        return next;
      });
    } catch {
      message.error('取消拉黑失败');
    } finally {
      setActionKey((current) => current === currentActionKey ? null : current);
    }
  };

  const handleBlacklist = async (record: ActionLog, reason = '') => {
    const currentActionKey = `${record.id}:blacklist`;
    setActionKey(currentActionKey);
    try {
      const payload: { brand: string; query: string; sku: string; productName?: string; reason?: string } = {
        brand: record.brand,
        query: record.query,
        sku: record.sku,
        productName: record.productName,
      };
      if (reason.trim()) payload.reason = reason.trim();
      await dashboardApi.blacklistProduct(payload);
      message.success('已重新拉黑并同步至系统');
      setData(prev => prev.map(item =>
        item.id === record.id ? { ...item, isCanceled: false } : item
      ));
      setCancelReasons((prev) => {
        const next = { ...prev };
        delete next[record.id];
        return next;
      });
    } catch {
      message.error('重新拉黑失败');
    } finally {
      setActionKey((current) => current === currentActionKey ? null : current);
    }
  };

  const [cancelReasons, setCancelReasons] = useState<Record<number, string>>({});
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);

  const showActionSheet = (record: ActionLog) => {
    const isCanceled = Boolean(record.isCanceled);
    ActionSheet.show({
      actions: [
        { text: isCanceled ? '拉黑' : '取消拉黑', key: isCanceled ? 'blacklist' : 'cancel', danger: !isCanceled },
      ],
      onAction: (action) => {
        if (action.key === 'cancel') {
          const reason = window.prompt('可填写取消理由（可不填）', '') || '';
          handleCancelBlacklist(record, reason);
        } else if (action.key === 'blacklist') {
          const reason = window.prompt('可填写拉黑理由（可不填）', '') || '';
          handleBlacklist(record, reason);
        }
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
                  <div>原因: {record.reason}</div>
                  {record.isCanceled && (
                    <div style={{ marginTop: '8px', textAlign: 'right' }}>
                      <MobTag color='default'>已取消</MobTag>
                    </div>
                  )}
                </div>
              </MobCard>
            ))}
            <InfiniteScroll loadMore={loadMore} hasMore={hasMore} />
          </>
        ) : <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>暂无拉黑记录</div>}
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
              mobForm.setFieldsValue({ date: dayjs().toDate(), brand: undefined });
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
            <MobForm.Item 
              name='date' 
              label='日期'
              trigger='onConfirm'
              onClick={() => setPickerVisible(true)}
            >
               <MobDatePicker 
                 visible={pickerVisible}
                 onClose={() => setPickerVisible(false)}
                 precision='day'
                 onConfirm={(val) => {
                   mobForm.setFieldsValue({ date: val });
                 }}
               >
                {value => value ? dayjs(value).format('YYYY-MM-DD') : '请选择日期'}
               </MobDatePicker>
            </MobForm.Item>
            <MobForm.Item name='brand' label='品牌'>
              <MobSelector
                options={allowedBrands}
              />
            </MobForm.Item>
          </MobForm>
        </div>
      </MobPopup>
    </div>
  );

  const handleBulkCancel = async () => {
    const selected = data.filter((record) => selectedRowKeys.includes(record.id) && !record.isCanceled);
    if (!selected.length) {
      message.warning('请先选择要取消拉黑的记录');
      return;
    }
    setBulkLoading(true);
    try {
      const result = await dashboardApi.cancelBlacklistBatch(selected.map((record) => ({ brand: record.brand, query: record.query, sku: record.sku })));
      const succeeded = result.data?.succeeded ?? selected.length;
      const failed = result.data?.failed ?? Math.max(0, selected.length - succeeded);
      const responseItems = Array.isArray(result.data?.items) ? result.data.items : [];
      const successfulSkus = new Set(responseItems.filter((item: { status?: string }) => item.status === 'success').map((item: { sku: string }) => item.sku));
      const successfulIds = responseItems.length
        ? new Set(selected.filter((record) => successfulSkus.has(record.sku)).map((record) => record.id))
        : new Set(selected.slice(0, succeeded).map((record) => record.id));
      setData((prev) => prev.map((item) => successfulIds.has(item.id) ? { ...item, isCanceled: true } : item));
      setSelectedRowKeys((current) => current.filter((key) => !successfulIds.has(Number(key))));
      if (failed > 0) {
        message.warning(`已批量取消 ${succeeded} 条，${failed} 条失败`);
      } else {
        message.success(`已批量取消 ${succeeded} 条`);
      }
    } catch {
      message.error('批量取消拉黑失败');
    } finally {
      setBulkLoading(false);
    }
  };

  const renderBlacklistAction = (record: ActionLog) => {
    const isCanceled = Boolean(record.isCanceled);
    const currentActionKey = `${record.id}:${isCanceled ? 'blacklist' : 'cancel'}`;
    return (
      <Space size={4}>
        <Popconfirm
          title={(
            <div style={{ width: 280 }}>
              <div style={{ marginBottom: 8 }}>{isCanceled ? '确定要重新拉黑该商品吗？' : '确定要取消拉黑该商品吗？'}</div>
              <Input.TextArea
                value={cancelReasons[record.id] || ''}
                onChange={(event) => setCancelReasons((prev) => ({ ...prev, [record.id]: event.target.value }))}
                placeholder={isCanceled ? '可填写拉黑理由（可不填）' : '可填写取消理由（可不填）'}
                autoSize={{ minRows: 2, maxRows: 4 }}
              />
            </div>
          )}
          onConfirm={() => {
            // Let the row action own its loading state instead of keeping the
            // confirmation popover open for the remote write/readback chain.
            if (isCanceled) {
              void handleBlacklist(record, cancelReasons[record.id] || '');
            } else {
              void handleCancelBlacklist(record, cancelReasons[record.id] || '');
            }
          }}
          okText="确定"
          cancelText="取消"
        >
          <Button
            type="link"
            size="small"
            danger={!isCanceled}
            loading={actionKey === currentActionKey}
          >
            {isCanceled ? '拉黑' : '取消拉黑'}
          </Button>
        </Popconfirm>
      </Space>
    );
  };

  const columns = [
    { title: '商品', dataIndex: 'productName', key: 'productName', width: 300, render: (_: unknown, record: ActionLog) => <ProductIdentityCell name={record.productName} sku={record.sku} imageUrl={record.imageUrl} onClick={() => setSelectedProduct(record)} /> },
    { title: '品牌', dataIndex: 'brand', key: 'brand', width: 110 },
    { title: 'Query', dataIndex: 'query', key: 'query', width: 180 },
    { title: '处理依据', dataIndex: 'reason', key: 'reason', width: 300 },
    {
      title: '操作',
      key: 'action',
      width: 120,
      fixed: 'right' as const,
      render: (_: unknown, record: ActionLog) => (
        renderBlacklistAction(record)
      ),
    },
  ];

  return (
    isMobile ? renderMobileView() : (
      <Flex vertical gap="small" className="ops-page">
        <OperationsLogTabs />
        <OperationsLogFilterBar
          form={form}
          allowedBrands={allowedBrands}
          onChange={onSearch}
          trailing={<Button danger loading={bulkLoading} onClick={handleBulkCancel}>批量取消拉黑</Button>}
        />
        <Card size="small" className="operations-log-list-card">
          <Table 
            columns={columns} 
            dataSource={data} 
            loading={loading} 
            rowKey="id"
            rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys, getCheckboxProps: (record: ActionLog) => ({ disabled: Boolean(record.isCanceled) }) }}
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

export default BlacklistLogs;
