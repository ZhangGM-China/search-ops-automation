import React from 'react';
import { Alert, Card, Descriptions, Tag, Typography } from 'antd';
import type { TopQueryExperimentReportData } from '@/shared/types';
import ExperimentActionAudit from './ExperimentActionAudit';
import ExperimentDailyEffectTable from './ExperimentDailyEffectTable';
import ExperimentEffectSummary from './ExperimentEffectSummary';
import ExperimentQualityGate from './ExperimentQualityGate';
import ExperimentQueryDailyTable from './ExperimentQueryDailyTable';
import ExperimentQueryTable from './ExperimentQueryTable';
import ExperimentTrendPanel from './ExperimentTrendPanel';
import { formatQualityError, hasCompleteQualityEvidence } from './experimentQuality';
import { calculationVersionKind, calculationVersionLabel, experimentStatusLabel } from './experimentLabels';

interface Props { report: TopQueryExperimentReportData; isMobile: boolean }

const baselineErrorLabels: Record<string, string> = {
  baseline_window_incomplete: '7日基线数据不完整',
  baseline_zero_search_uv: '7日基线 Search UV 为 0',
  baseline_not_locked: '7日基线尚未锁定',
  invalid_baseline: '7日基线数据不完整',
};

const ExperimentReport: React.FC<Props> = ({ report, isMobile }) => {
  const versionKind = calculationVersionKind(report.calculationVersion);
  const supportedVersion = versionKind !== 'unsupported';
  const baselinePassed = supportedVersion && report.baselineQuality.status === 'complete' && report.baselineQuality.windowValid && report.baselineQuality.locked;
  const qualityPassed = report.quality.status === 'complete' && hasCompleteQualityEvidence(report.quality);
  const conclusionAvailable = baselinePassed && qualityPassed && Boolean(report.effect);
  const dailyEffectAllowed = baselinePassed;
  const baselineRange = report.experiment.baselineStartDate && report.experiment.baselineEndDate
    ? `${report.experiment.baselineStartDate} 至 ${report.experiment.baselineEndDate}`
    : '-';
  const baselineError = baselineErrorLabels[report.baselineQuality.error ?? ''] ?? formatQualityError(report.baselineQuality.error);
  const trend = { experimentId: report.experiment.experimentId, brand: report.experiment.brand, items: report.trend };

  return <div className="experiment-report">
    <section className="experiment-report__section">
      <Typography.Title level={4}>1. 效果结论</Typography.Title>
      <Card size="small">
        <Descriptions size="small" column={isMobile ? 1 : 3}>
          <Descriptions.Item label="实验">{report.experiment.experimentName || `A/B Test #${report.experiment.experimentId}`}</Descriptions.Item>
          <Descriptions.Item label="品牌">{report.experiment.brand.toUpperCase()}</Descriptions.Item>
          <Descriptions.Item label="状态">{experimentStatusLabel(report.experiment.status)}</Descriptions.Item>
          <Descriptions.Item label="基线窗口">{baselineRange}</Descriptions.Item>
          {report.experiment.status === 'running'
            ? <Descriptions.Item label="截至数据日">{report.dataEndDate ?? '-'}</Descriptions.Item>
            : <Descriptions.Item label="观测窗口">{report.dataStartDate && report.dataEndDate ? `${report.dataStartDate} 至 ${report.dataEndDate}` : '-'}</Descriptions.Item>}
          <Descriptions.Item label="计算版本"><Tag color={supportedVersion ? 'blue' : 'red'}>{calculationVersionLabel(report.calculationVersion)}</Tag></Descriptions.Item>
          <Descriptions.Item label="基线质量"><Tag color={baselinePassed ? 'green' : 'red'}>{baselinePassed ? '完整' : '异常'}</Tag></Descriptions.Item>
          <Descriptions.Item label="实验质量"><Tag color={qualityPassed ? 'green' : 'red'}>{qualityPassed ? '完整' : '异常'}</Tag></Descriptions.Item>
          <Descriptions.Item label="生成时间">{report.generatedAt}（{report.timezone}）</Descriptions.Item>
        </Descriptions>
      </Card>
      {!supportedVersion ? <Alert type="error" showIcon title="不支持的计算版本" description="正式效果结论已隐藏，请升级报告口径后重试。" /> : null}
      {!baselinePassed ? <Alert type="error" showIcon title={baselineError} description="正式效果结论已隐藏，以下仍保留每日数据、Query 与动作审计。" /> : null}
      {baselinePassed && !qualityPassed ? <Alert type="error" showIcon title={formatQualityError(report.quality.error)} description="正式效果结论已隐藏，以下仍保留审计证据。" /> : null}
      {conclusionAvailable ? <ExperimentEffectSummary overview={report.overview} /> : null}
    </section>

    <section className="experiment-report__section">
      <Typography.Title level={4}>2. 每日组效果</Typography.Title>
      <ExperimentTrendPanel trend={trend} qualityPassed={dailyEffectAllowed} />
      <ExperimentDailyEffectTable items={report.trend} isMobile={isMobile} qualityPassed={dailyEffectAllowed} />
    </section>

    <section className="experiment-report__section">
      <Typography.Title level={4}>3. Query 综合效果</Typography.Title>
      <ExperimentQueryTable items={report.queries} qualityPassed={conclusionAvailable} isMobile={isMobile} title={false} />
    </section>

    <section className="experiment-report__section">
      <Typography.Title level={4}>4. Query 每日变化</Typography.Title>
      <ExperimentQueryDailyTable items={report.queryDailyMetrics} isMobile={isMobile} />
    </section>

    <section className="experiment-report__section">
      <Typography.Title level={4}>5. 治理动作与质量审计</Typography.Title>
      <ExperimentActionAudit actions={report.actions} operations={report.operations} isMobile={isMobile} />
      <ExperimentQualityGate quality={report.quality} />
    </section>

    <section className="experiment-report__section">
      <Typography.Title level={4}>6. 方法说明</Typography.Title>
      <Card size="small">
        <Typography.Paragraph>页面只展示服务端统一报告 DTO：CTR、CVR、组内变化和净效果均由服务端计算，前端不重新聚合。</Typography.Paragraph>
        <Typography.Paragraph>CTR = Click UV / Search UV；CVR = Purchase UV / Search UV。仅已观测、无污染且数据质量完整的记录计入效果。</Typography.Paragraph>
        <Typography.Paragraph style={{ marginBottom: 0 }}>净效果 = 实验组相对基线变化 - 对照组相对基线变化；PV 仅用于审计展示。</Typography.Paragraph>
      </Card>
    </section>
  </div>;
};

export default ExperimentReport;
