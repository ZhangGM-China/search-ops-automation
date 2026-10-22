import React from 'react';
import { Form, InputNumber, Tooltip } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import { Form as MobForm, Stepper as MobStepper } from 'antd-mobile';
import useMobile from '@/shared/hooks/useMobile';

interface Props {
  disabled?: boolean;
}

const fieldLabel = (label: string, tip: string) => (
  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
    <span>{label}</span>
    <Tooltip title={tip}>
      <InfoCircleOutlined aria-label={`${label}提示`} className="ops-tooltip-icon" style={{ color: '#8c8c8c' }} />
    </Tooltip>
  </span>
);

const SafetyConfigSection: React.FC<Props> = ({ disabled }) => {
  const isMobile = useMobile();

  if (isMobile) {
    return (
      <div style={{ marginBottom: '16px' }}>
        <div style={{ fontSize: '16px', fontWeight: 'bold', marginBottom: '12px', padding: '0 4px' }}>安全配置</div>
        <MobForm.Item name={['safety_config', 'circuit_breaker_threshold']} label={fieldLabel('接口熔断阈值', '连续失败达到该次数后暂时熔断接口，避免故障持续放大。')}>
          <MobStepper disabled={disabled} />
        </MobForm.Item>
        <MobForm.Item name={['safety_config', 'max_retry_count']} label={fieldLabel('工作流重试次数', '单个工作流步骤失败后最多自动重试的次数。')}>
          <MobStepper disabled={disabled} />
        </MobForm.Item>
        <MobForm.Item name={['safety_config', 'api_delay_threshold']} label={fieldLabel('延迟告警阈值（秒）', '接口响应超过该时长时记录延迟告警，用于定位外部服务变慢。')}>
          <MobStepper disabled={disabled} />
        </MobForm.Item>
        <MobForm.Item name={['safety_config', 'health_check_stale_minutes']} label={fieldLabel('任务陈旧阈值（分钟）', '任务超过该时长没有更新仍未结束时，健康检查会将其标记为陈旧。')}>
          <MobStepper disabled={disabled} />
        </MobForm.Item>
        <MobForm.Item name={['safety_config', 'health_check_error_rate_threshold_percent']} label={fieldLabel('最近一小时失败率告警阈值（%）', '最近一小时接口失败率达到该比例时触发健康告警。')}>
          <MobStepper disabled={disabled} />
        </MobForm.Item>
        <MobForm.Item name={['safety_config', 'health_check_api_p95_threshold_seconds']} label={fieldLabel('接口 P95 告警阈值（秒）', '最近一小时接口 P95 响应时间超过该值时触发健康告警。')}>
          <MobStepper disabled={disabled} />
        </MobForm.Item>
      </div>
    );
  }

  return (
    <section className="ops-config-section ops-safety-config-section" aria-labelledby="safety-config-title">
      <div id="safety-config-title" className="ops-config-section__title">安全配置</div>
      <div className="ops-config-field-grid ops-safety-config-grid">
        <Form.Item name={['safety_config', 'circuit_breaker_threshold']} label={fieldLabel('接口连续失败熔断阈值', '连续失败达到该次数后暂时熔断接口，避免故障持续放大。')}>
          <InputNumber style={{ width: '100%' }} disabled={disabled} />
        </Form.Item>
        <Form.Item name={['safety_config', 'max_retry_count']} label={fieldLabel('工作流重试次数', '单个工作流步骤失败后最多自动重试的次数。')}>
          <InputNumber style={{ width: '100%' }} disabled={disabled} />
        </Form.Item>
        <Form.Item name={['safety_config', 'api_delay_threshold']} label={fieldLabel('接口响应延迟告警阈值（秒）', '接口响应超过该时长时记录延迟告警，用于定位外部服务变慢。')}>
          <InputNumber style={{ width: '100%' }} disabled={disabled} />
        </Form.Item>
        <Form.Item name={['safety_config', 'health_check_stale_minutes']} label={fieldLabel('任务陈旧阈值（分钟）', '任务超过该时长没有更新仍未结束时，健康检查会将其标记为陈旧。')}>
          <InputNumber style={{ width: '100%' }} disabled={disabled} min={1} />
        </Form.Item>
        <Form.Item name={['safety_config', 'health_check_error_rate_threshold_percent']} label={fieldLabel('最近一小时失败率告警阈值（%）', '最近一小时接口失败率达到该比例时触发健康告警。')}>
          <InputNumber style={{ width: '100%' }} disabled={disabled} min={0} max={100} />
        </Form.Item>
        <Form.Item name={['safety_config', 'health_check_api_p95_threshold_seconds']} label={fieldLabel('接口 P95 告警阈值（秒）', '最近一小时接口 P95 响应时间超过该值时触发健康告警。')}>
          <InputNumber style={{ width: '100%' }} disabled={disabled} min={1} />
        </Form.Item>
      </div>
    </section>
  );
};

export default SafetyConfigSection;
