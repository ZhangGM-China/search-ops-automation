import React from 'react';
import { Button, Card, Descriptions, Flex, Form, Input, Popconfirm, Select, Table, Tag, Typography } from 'antd';
import {
  Button as MobButton,
  Card as MobCard,
  DatePicker as MobDatePicker,
  Form as MobForm,
  InfiniteScroll,
  Popup as MobPopup,
  SearchBar as MobSearchBar,
  Selector as MobSelector,
  Space as MobSpace,
  Tag as MobTag,
} from 'antd-mobile';
import type { ColumnsType } from 'antd/es/table';
import { FilterOutline } from 'antd-mobile-icons';
import dayjs from 'dayjs';
import type { NoResultAgentTimelineEvent, NoResultGovernanceAction, NoResultGovernanceItem, QueryFilters } from '@/shared/types';
import {
  actionStatusColorMap,
  actionStatusLabelMap,
  brandOptions,
  formatJson,
  getMobileStatusColor,
  riskColorMap,
  rollbackStatusColorMap,
  rollbackStatusLabelMap,
  statusLabelMap,
} from './constants';
import type { NoResultGovernanceFilterValues } from './constants';
import { executionButtonText, hasExecutableActions } from './columns';
import { OperationsLogTabs, OperationsLogDateSwitcher } from '../../../operations/components/OperationsLogShared';

const { Text } = Typography;

const asDisplayText = (value: unknown) => typeof value === 'string' && value.trim() ? value : '-';

type NoResultAgentTimelineProps = {
  events?: NoResultAgentTimelineEvent[];
};

export const NoResultAgentTimeline: React.FC<NoResultAgentTimelineProps> = ({ events = [] }) => (
  <div className="no-result-agent-timeline">
    <Text strong>Agent 推理过程</Text>
    {events.length === 0 ? (
      <div className="no-result-agent-timeline__empty">
        <Text type="secondary">暂无 Agent 留痕</Text>
      </div>
    ) : (
      <div className="no-result-agent-timeline__list">
        {events.map((event) => (
          <div className="no-result-agent-timeline__item" key={event.id}>
            <Flex gap={6} wrap align="center">
              <Tag>{event.stage}</Tag>
              <Tag color="blue">{event.eventType}</Tag>
              {event.toolName ? <Tag color="purple">{event.toolName}</Tag> : null}
              <Text type="secondary">{event.createdAt}</Text>
            </Flex>
            <div className="no-result-agent-timeline__summary">{event.summary || '-'}</div>
            <pre>{formatJson(event.payload)}</pre>
          </div>
        ))}
      </div>
    )}
  </div>
);

type NoResultActionDetailProps = {
  action: NoResultGovernanceAction;
  onApprove: (actionId: number) => void;
  onReject: (actionId: number) => void;
};

export const NoResultActionDetail: React.FC<NoResultActionDetailProps> = ({
  action,
  onApprove,
  onReject,
}) => (
  <div className="no-result-action-detail">
    <Descriptions
      bordered
      size="small"
      column={1}
      items={[
        { key: 'type', label: '动作类型', children: action.actionType },
        { key: 'interface', label: '目标接口', children: action.targetInterface },
        {
          key: 'status',
          label: '执行状态',
          children: <Tag color={actionStatusColorMap[action.status] || 'default'}>{actionStatusLabelMap[action.status] || action.status}</Tag>,
        },
        {
          key: 'risk',
          label: '风险等级',
          children: <Tag color={riskColorMap[action.riskLevel] || 'default'}>{action.riskLevel}</Tag>,
        },
        { key: 'reason', label: 'Agent 判断原因', children: action.agentReason || '-' },
        { key: 'riskExplanation', label: '风险说明', children: asDisplayText(action.requestPayload?.riskExplanation) },
        { key: 'rollbackSuggestion', label: '回滚建议', children: asDisplayText(action.requestPayload?.rollbackSuggestion) },
        { key: 'error', label: '失败/复核原因', children: action.errorMessage || '-' },
        {
          key: 'rollback',
          label: '回滚状态',
          children: action.rollbackStatus ? (
            <Tag color={rollbackStatusColorMap[action.rollbackStatus] || 'default'}>
              {rollbackStatusLabelMap[action.rollbackStatus] || action.rollbackStatus}
            </Tag>
          ) : '-',
        },
        { key: 'created', label: '记录时间', children: action.createdAt || '-' },
      ]}
    />
    {action.status === 'manual_review' ? (
      <Flex gap={8} wrap>
        <Button size="small" type="primary" onClick={() => onApprove(action.id)}>
          批准执行
        </Button>
        <Button size="small" danger onClick={() => onReject(action.id)}>
          拒绝动作
        </Button>
      </Flex>
    ) : null}
    <div className="no-result-action-detail__json-grid">
      <div>
        <Text strong>请求参数</Text>
        <pre>{formatJson(action.requestPayload)}</pre>
      </div>
      <div>
        <Text strong>响应结果</Text>
        <pre>{formatJson(action.responsePayload)}</pre>
      </div>
      <div>
        <Text strong>执行前快照</Text>
        <pre>{formatJson(action.beforeSnapshot)}</pre>
      </div>
      <div>
        <Text strong>回滚 payload</Text>
        <pre>{formatJson(action.rollbackPayload)}</pre>
      </div>
    </div>
  </div>
);

type NoResultGovernanceMobileViewProps = {
  data: NoResultGovernanceItem[];
  loading: boolean;
  page: number;
  pageSize: number;
  hasMore: boolean;
  filterVisible: boolean;
  pickerVisible: boolean;
  mobileFilters: NoResultGovernanceFilterValues;
  mobForm: ReturnType<typeof MobForm.useForm<NoResultGovernanceFilterValues>>[0];
  onOpenDetail: (record: NoResultGovernanceItem) => void;
  onExecuteTask: (record: NoResultGovernanceItem) => void;
  onFetchData: (values: QueryFilters, append?: boolean) => Promise<void>;
  onLoadMore: () => Promise<void>;
  onFilterVisibleChange: (visible: boolean) => void;
  onPickerVisibleChange: (visible: boolean) => void;
  normalizeMobileValues: (values: NoResultGovernanceFilterValues) => NoResultGovernanceFilterValues;
  setMobileFilters: React.Dispatch<React.SetStateAction<NoResultGovernanceFilterValues>>;
  setPage: React.Dispatch<React.SetStateAction<number>>;
};

export const NoResultGovernanceMobileView: React.FC<NoResultGovernanceMobileViewProps> = ({
  data,
  hasMore,
  filterVisible,
  pickerVisible,
  mobileFilters,
  mobForm,
  onOpenDetail,
  onExecuteTask,
  onFetchData,
  onLoadMore,
  onFilterVisibleChange,
  onPickerVisibleChange,
  normalizeMobileValues,
  setMobileFilters,
  setPage,
  pageSize,
}) => (
  <div className="ops-page no-result-mobile-page">
    <OperationsLogTabs />
    <div className="ops-surface no-result-mobile-filter">
      <Flex align="center" gap="small">
        <MobSearchBar
          placeholder="搜索 Query"
          style={{ flex: 1 }}
          onSearch={(value) => onFetchData({ ...mobileFilters, query_text: value, page: 1, limit: pageSize })}
          onClear={() => onFetchData({ ...mobileFilters, query_text: '', page: 1, limit: pageSize })}
        />
        <MobButton
          size="small"
          onClick={() => {
            mobForm.setFieldsValue(mobileFilters);
            onFilterVisibleChange(true);
          }}
        >
          <FilterOutline /> 筛选
        </MobButton>
      </Flex>
    </div>

    <div className="no-result-mobile-list">
      {data.length > 0 ? (
        <>
          {data.map((record) => {
            return (
              <MobCard
                key={record.id}
                title={record.query}
                extra={record.brand.toUpperCase()}
                onClick={() => onOpenDetail(record)}
              >
                <div className="no-result-mobile-card">
                  <div className="no-result-mobile-card__meta">
                    <MobTag color={getMobileStatusColor(record.status)}>
                      {statusLabelMap[record.status] || record.status}
                    </MobTag>
                    <span>{record.beforeResultCount} {'->'} {record.afterResultCount} 结果</span>
                    <span>PV {record.searchPv}</span>
                  </div>
                  <div className="no-result-mobile-card__strategy">
                    {record.finalStrategy || record.aiReason || '暂无 AI 调整策略'}
                  </div>
                  <div className="no-result-mobile-card__actions">
                    <MobButton
                      size="mini"
                      fill="outline"
                      onClick={(event) => {
                        event.stopPropagation();
                        onOpenDetail(record);
                      }}
                    >
                      详情
                    </MobButton>
                    <MobButton
                      size="mini"
                      color="primary"
                      disabled={record.executionStatus === 'completed' || (!record.executionStatus && !hasExecutableActions(record))}
                      onClick={(event) => {
                        event.stopPropagation();
                        onExecuteTask(record);
                      }}
                    >
                      {executionButtonText(record.executionStatus)}
                    </MobButton>
                  </div>
                </div>
              </MobCard>
            );
          })}
          <InfiniteScroll loadMore={onLoadMore} hasMore={hasMore} />
        </>
      ) : (
        <div className="app-empty-state">
          <h2>暂无无结果词记录</h2>
          <p>调整日期、品牌或状态后重新查询。</p>
        </div>
      )}
    </div>

    <MobPopup
      visible={filterVisible}
      onMaskClick={() => onFilterVisibleChange(false)}
      position="right"
      bodyStyle={{ width: '84vw' }}
    >
      <div className="no-result-mobile-popup">
        <Flex justify="space-between" align="center">
          <h3>筛选条件</h3>
          <Button
            type="link"
            size="small"
            onClick={() => {
              const nextValues = { date: dayjs().toDate(), brand: undefined, status: undefined };
              mobForm.setFieldsValue(nextValues);
              setMobileFilters(nextValues);
            }}
          >
            重置
          </Button>
        </Flex>
        <MobForm
          form={mobForm}
          layout="vertical"
          initialValues={mobileFilters}
          onFinish={(values) => {
            const nextValues = normalizeMobileValues(values);
            setPage(1);
            onFetchData({ ...nextValues, page: 1, limit: pageSize });
            onFilterVisibleChange(false);
          }}
          footer={
            <MobSpace block direction="vertical">
              <MobButton block type="submit" color="primary">确认</MobButton>
              <MobButton block onClick={() => onFilterVisibleChange(false)}>取消</MobButton>
            </MobSpace>
          }
        >
          <MobForm.Item
            name="date"
            label="日期"
            trigger="onConfirm"
            onClick={() => onPickerVisibleChange(true)}
          >
            <MobDatePicker
              visible={pickerVisible}
              onClose={() => onPickerVisibleChange(false)}
              precision="day"
              onConfirm={(value) => {
                mobForm.setFieldsValue({ date: value });
              }}
            >
              {(value) => value ? dayjs(value).format('YYYY-MM-DD') : '请选择日期'}
            </MobDatePicker>
          </MobForm.Item>
          <MobForm.Item name="brand" label="品牌">
            <MobSelector options={brandOptions} />
          </MobForm.Item>
          <MobForm.Item name="status" label="状态">
            <MobSelector options={Object.entries(statusLabelMap).map(([value, label]) => ({ value, label }))} />
          </MobForm.Item>
        </MobForm>
      </div>
    </MobPopup>
  </div>
);

type NoResultGovernanceDesktopViewProps = {
  form: ReturnType<typeof Form.useForm<NoResultGovernanceFilterValues>>[0];
  data: NoResultGovernanceItem[];
  total: number;
  loading: boolean;
  page: number;
  pageSize: number;
  columns: ColumnsType<NoResultGovernanceItem>;
  onSearch: (values: NoResultGovernanceFilterValues) => void;
  onFetchPage: (page: number, pageSize: number) => void;
  onRollbackBrandDate: () => void;
};

export const NoResultGovernanceDesktopView: React.FC<NoResultGovernanceDesktopViewProps> = ({
  form,
  data,
  total,
  loading,
  page,
  pageSize,
  columns,
  onSearch,
  onFetchPage,
  onRollbackBrandDate,
}) => (
  <Flex vertical gap="small" className="ops-page">
    <OperationsLogTabs />
    <div className="ops-page__header">
      <div>
        <div className="ops-page-title">无结果词</div>
      </div>
    </div>

    <Card size="small" className="ops-filter-card">
      <Form form={form} layout="inline" onFinish={onSearch} initialValues={{ date: dayjs() }} size="small">
        <Form.Item name="date" noStyle>
          <OperationsLogDateSwitcher onChange={(date) => { form.setFieldValue('date', date); onSearch(form.getFieldsValue()); }} />
        </Form.Item>
        <Form.Item name="brand" noStyle>
          <Select allowClear options={brandOptions} style={{ width: 160 }} />
        </Form.Item>
        <Form.Item name="query_text" noStyle>
          <Input placeholder="搜索 Query" allowClear style={{ width: 180 }} />
        </Form.Item>
        <Form.Item name="status" label="状态">
          <Select
            allowClear
            style={{ width: 150 }}
            options={Object.entries(statusLabelMap).map(([value, label]) => ({ value, label }))}
          />
        </Form.Item>
        <Form.Item>
          <Button type="primary" htmlType="submit">查询</Button>
        </Form.Item>
        <Form.Item>
          <Popconfirm
            title="确认回滚当前品牌日期？"
            description="需先选择品牌和日期；系统会回滚该品牌该日期下的治理批次。"
            onConfirm={onRollbackBrandDate}
            okText="确定"
            cancelText="取消"
          >
            <Button danger>回滚品牌日期</Button>
          </Popconfirm>
        </Form.Item>
      </Form>
    </Card>

    <Card
      size="small"
      title="无结果词列表"
      extra={<Text type="secondary">共 {total} 条</Text>}
    >
      <Table
        rowKey="id"
        loading={loading}
        dataSource={data}
        columns={columns}
        size="small"
        sticky
        scroll={{ x: 1300, y: 'calc(100vh - 300px)' }}
        pagination={{
          current: page,
          pageSize,
          total,
          size: 'small',
          showSizeChanger: true,
          showTotal: (value) => `共 ${value} 条`,
          onChange: onFetchPage,
        }}
      />
    </Card>
  </Flex>
);
