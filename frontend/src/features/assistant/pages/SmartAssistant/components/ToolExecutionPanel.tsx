import React from 'react';
import { Button, Tag } from 'antd';
import { CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined } from '@ant-design/icons';
import type { AssistantToolCall, AssistantToolResult } from '@/shared/types';

type ToolExecutionPanelProps = {
  calls: AssistantToolCall[];
  results: AssistantToolResult[];
};

const statusMeta = (call: AssistantToolCall, result?: AssistantToolResult) => {
  const failed = result?.status === 'failed' || call.status === 'failed' || Boolean(result?.error || call.error);
  if (failed) return { color: 'error', label: '执行失败', icon: <CloseCircleOutlined /> } as const;
  if (result) return { color: 'success', label: '执行完成', icon: <CheckCircleOutlined /> } as const;
  return { color: 'processing', label: '执行中', icon: <LoadingOutlined spin /> } as const;
};

export const ToolExecutionPanel = React.memo<ToolExecutionPanelProps>(({ calls, results }) => {
  const displayCalls = React.useMemo(() => {
    const knownResultIds = new Set(calls.map((call) => call.id).filter(Boolean));
    const knownResultNames = new Set(calls.map((call) => call.name));
    const resultOnlyCalls = results
      .filter((result) => (result.toolCallId && !knownResultIds.has(result.toolCallId)) || (!result.toolCallId && !knownResultNames.has(result.toolName)))
      .map<AssistantToolCall>((result, index) => ({
        id: result.toolCallId || `result-${result.toolName}-${index}`,
        name: result.toolName,
        status: result.status === 'completed' ? 'done' : result.status,
        arguments: undefined,
        output: result.output,
        error: result.error,
        exitCode: result.exitCode,
      }));
    return [...calls, ...resultOnlyCalls];
  }, [calls, results]);
  const [expandedIds, setExpandedIds] = React.useState<Set<string>>(() => new Set());

  return (
    <div className="ops-smart-assistant-tool-group" aria-label="工具调用分组">
      <div className="ops-smart-assistant-tool-group__title">工具调用</div>
      {displayCalls.map((call, index) => {
        const callId = call.id || `${call.name}-${index}`;
        const result = results.find((item) => item.toolCallId === call.id)
          || results.find((item, resultIndex) => item.toolName === call.name && resultIndex === index)
          || results.find((item) => item.toolName === call.name);
        const meta = statusMeta(call, result);
        const expanded = expandedIds.has(callId);
        const output = result?.output || call.output || result?.summary;
        const error = result?.error || call.error;
        const exitCode = result?.exitCode ?? call.exitCode;
        return (
          <section key={callId} className="ops-smart-assistant-tool-execution">
            <div className="ops-smart-assistant-tool-execution__header">
              <div className="ops-smart-assistant-tool-execution__identity">
                <span className="ops-smart-assistant-tool-execution__icon" aria-hidden="true">{meta.icon}</span>
                <div><strong>{call.name}</strong><span>{result?.summary || '等待工具返回结果'}{call.arguments ? <code>{call.arguments}</code> : null}</span></div>
              </div>
              <div className="ops-smart-assistant-tool-execution__actions">
                <Tag color={meta.color}>{meta.label}</Tag>
                <Button
                  type="text"
                  size="small"
                  aria-label={`查看工具 ${call.name} 详情`}
                  onClick={() => setExpandedIds((current) => {
                    const next = new Set(current);
                    if (next.has(callId)) next.delete(callId); else next.add(callId);
                    return next;
                  })}
                >
                  {expanded ? '收起' : '详情'}
                </Button>
              </div>
            </div>
            {expanded ? (
              <div className="ops-smart-assistant-tool-execution__details">
                {output && output !== result?.summary ? <div><span>输出</span><pre>{output}</pre></div> : null}
                {error ? <div><span>错误</span><pre className="ops-smart-assistant-tool-execution__error">{error}</pre></div> : null}
                {exitCode !== undefined && exitCode !== null ? <small>退出码 {exitCode}</small> : null}
                {result?.durationMs !== undefined && result.durationMs !== null ? <small>耗时 {result.durationMs} ms</small> : null}
              </div>
            ) : null}
          </section>
        );
      })}
    </div>
  );
});

ToolExecutionPanel.displayName = 'ToolExecutionPanel';
