import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { message } from 'antd';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { adminApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';
import NodeModelConfigSection from './NodeModelConfigSection';

vi.mock('@/shared/api', () => ({
  adminApi: {
    getNodeModelConfig: vi.fn(),
    createNodeModelConfigDraft: vi.fn(),
    publishNodeModelConfig: vi.fn(),
    rollbackNodeModelConfig: vi.fn(),
  },
}));

const nodes = [
  {
    nodeKey: 'query_governance.kg_agent',
    channel: 'rd_llm',
    primaryModel: 'qwen3.6-plus',
    fallbackModel: 'qwen3.7-max',
    requiredCapabilities: [],
  },
  {
    nodeKey: 'query_governance.preference_generation',
    channel: 'rd_llm',
    primaryModel: 'qwen3.6-plus',
    fallbackModel: 'qwen3.7-max',
    requiredCapabilities: [],
  },
  {
    nodeKey: 'query_governance.relevance_multimodal',
    channel: 'rd_llm',
    primaryModel: 'qwen-vl-max',
    fallbackModel: 'DeepSeek-VL3',
    requiredCapabilities: ['image'],
  },
  {
    nodeKey: 'query_governance.relevance_rules',
    channel: 'rd_llm',
    primaryModel: 'qwen3.6-plus',
    fallbackModel: 'qwen3.7-max',
    requiredCapabilities: [],
  },
];

describe('NodeModelConfigSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(adminApi.getNodeModelConfig).mockResolvedValue(axiosResponse({
      activeVersion: 6,
      nodes,
    }));
  });

  it('shows every new governance node and its multimodal requirement', async () => {
    render(<NodeModelConfigSection />);

    await waitFor(() => expect(adminApi.getNodeModelConfig).toHaveBeenCalledTimes(1));
    expect(screen.getByText('KG Agent')).toBeInTheDocument();
    expect(screen.getByText('Query 偏好生成 Agent')).toBeInTheDocument();
    expect(screen.getByText('相关性多模态 Agent（图文）')).toBeInTheDocument();
    expect(screen.getByText('相关性规则 AI 整理')).toBeInTheDocument();
    expect(screen.getByText('需要图文输入')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '编辑模型' }));
    expect(screen.getByLabelText('query_governance.kg_agent 主模型')).toBeEnabled();
    expect(screen.getByLabelText('query_governance.kg_agent 备用模型')).toBeEnabled();
    expect(screen.getByLabelText('query_governance.relevance_multimodal 主模型')).toBeEnabled();
    expect(screen.getByLabelText('query_governance.relevance_multimodal 备用模型')).toBeEnabled();
  });

  it('validates model values before publishing and supports canceling edits', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockImplementation(() => ({}) as never);
    vi.mocked(adminApi.getNodeModelConfig).mockResolvedValueOnce(axiosResponse({
      activeVersion: 6,
      nodes: [{ ...nodes[0], primarySource: 'mystery', fallbackSource: 'mystery' }],
    }));
    render(<NodeModelConfigSection />);

    await screen.findByText('当前生效 v6');
    expect(screen.getAllByText('来源：mystery')).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: '编辑模型' }));

    fireEvent.change(screen.getByLabelText('query_governance.kg_agent 主模型'), { target: { value: 'bad model' } });
    fireEvent.click(screen.getByRole('button', { name: '发布配置' }));
    expect(errorSpy).toHaveBeenCalledWith('query_governance.kg_agent 主模型格式无效');

    fireEvent.change(screen.getByLabelText('query_governance.kg_agent 主模型'), { target: { value: 'valid-primary' } });
    fireEvent.change(screen.getByLabelText('query_governance.kg_agent 备用模型'), { target: { value: 'bad fallback' } });
    fireEvent.click(screen.getByRole('button', { name: '发布配置' }));
    expect(errorSpy).toHaveBeenCalledWith('query_governance.kg_agent 备用模型格式无效');

    fireEvent.change(screen.getByLabelText('query_governance.kg_agent 备用模型'), { target: { value: 'valid-primary' } });
    fireEvent.click(screen.getByRole('button', { name: '发布配置' }));
    expect(errorSpy).toHaveBeenCalledWith('query_governance.kg_agent 主模型与备用模型不能相同');

    fireEvent.click(screen.getByRole('button', { name: '取 消' }));
    expect(screen.getByLabelText('query_governance.kg_agent 主模型')).toHaveValue('qwen3.6-plus');
    errorSpy.mockRestore();
  });

  it('uses a fresh operation request ID when retrying the same failed publish', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockImplementation(() => ({}) as never);
    vi.mocked(adminApi.createNodeModelConfigDraft)
      .mockRejectedValueOnce({ response: { data: { detail: 'Node model config payload is invalid' } } })
      .mockResolvedValueOnce(axiosResponse({
        status: 'success',
        version: 7,
        state: 'draft',
        nodes,
      }));
    vi.mocked(adminApi.publishNodeModelConfig).mockResolvedValueOnce(axiosResponse({
      status: 'success',
      activeVersion: 7,
      nodes,
    }));
    render(<NodeModelConfigSection />);

    await screen.findByText('当前生效 v6');
    fireEvent.click(screen.getByRole('button', { name: '编辑模型' }));
    fireEvent.change(screen.getByLabelText('query_governance.kg_agent 主模型'), {
      target: { value: 'deepseek-v4.1-flash' },
    });

    fireEvent.click(screen.getByRole('button', { name: '发布配置' }));
    await waitFor(() => expect(adminApi.createNodeModelConfigDraft).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith('Node model config payload is invalid'));
    await waitFor(() => expect(screen.getByRole('button', { name: '发布配置' })).toBeEnabled());

    fireEvent.click(screen.getByRole('button', { name: '发布配置' }));
    await waitFor(() => expect(adminApi.createNodeModelConfigDraft).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(adminApi.publishNodeModelConfig).toHaveBeenCalledTimes(1));

    const firstDraftPayload = vi.mocked(adminApi.createNodeModelConfigDraft).mock.calls[0][0];
    const secondDraftPayload = vi.mocked(adminApi.createNodeModelConfigDraft).mock.calls[1][0];
    const publishPayload = vi.mocked(adminApi.publishNodeModelConfig).mock.calls[0][1];
    expect(firstDraftPayload.requestId).toMatch(/^node-model-config-draft:/);
    expect(secondDraftPayload.requestId).toMatch(/^node-model-config-draft:/);
    expect(secondDraftPayload.requestId).not.toBe(firstDraftPayload.requestId);
    expect(publishPayload.requestId).toMatch(/^node-model-config-publish:/);
    expect(publishPayload.requestId?.split(':').at(-1)).toBe(secondDraftPayload.requestId?.split(':').at(-1));
    expect(errorSpy).toHaveBeenCalledWith('Node model config payload is invalid');
    errorSpy.mockRestore();
  });

  it('surfaces API errors and rejects a rollback without a target version', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockImplementation(() => ({}) as never);
    vi.mocked(adminApi.getNodeModelConfig).mockRejectedValueOnce({ response: { data: { detail: '节点配置服务不可用' } } });
    render(<NodeModelConfigSection />);
    await waitFor(() => expect(adminApi.getNodeModelConfig).toHaveBeenCalledTimes(1));
    expect(errorSpy).toHaveBeenCalledWith('节点配置服务不可用');
    errorSpy.mockRestore();
  });

  it('opens rollback dialog and validates the target version', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockImplementation(() => ({}) as never);
    vi.mocked(adminApi.getNodeModelConfig).mockResolvedValueOnce(axiosResponse({ activeVersion: 1, nodes }));
    render(<NodeModelConfigSection />);

    await screen.findByText('当前生效 v1');
    fireEvent.click(screen.getByRole('button', { name: '回滚版本' }));
    fireEvent.click(screen.getByRole('button', { name: '确认回滚' }));
    expect(errorSpy).toHaveBeenCalledWith('请输入回滚目标版本');
    fireEvent.click(screen.getByRole('button', { name: '取 消' }));
    errorSpy.mockRestore();
  });
});
