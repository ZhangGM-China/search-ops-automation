import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardApi } from '@/shared/api';
import { KGActionSteps } from './KGActionSteps';

vi.mock('@/shared/api', () => ({ dashboardApi: {
  getKnowledgeGraphTagDetails: vi.fn(), getKnowledgeGraphRelationshipDetails: vi.fn(),
} }));

const proposed = { name: '羊毛', viewTag: '羊毛面料', isRoot: '0', dimensions: ['材质'], weight: 0,
  isEnglish: 1, segmentingAgain: false, isCompose: '1', nameAnd: ['羊绒'], nameNot: [] };
const relations = [
  { actionType: 'add_relationship', params: { tagName: '羊毛', responseTagName: '材质', relationships: '902227092548485120' } },
  { actionType: 'add_relationship', params: { tagName: '羊毛', responseTagName: '羊绒', relationships: '902227138815852544' } },
  { actionType: 'delete_relationship', params: { tagName: '羊毛', responseTagName: '羊', relationships: '902227092548485120' } },
];

describe('KGActionSteps', () => {
  beforeEach(() => vi.resetAllMocks());
  it('uses ordered actions and shows only the ten proposed tag fields without an external lookup', () => {
    render(<KGActionSteps brand="atlas" actions={[{ actionType: 'add_tag', params: proposed }, ...relations]} />);
    expect(screen.getAllByRole('listitem')).toHaveLength(4);
    expect(screen.getAllByRole('listitem')[0]).toHaveTextContent('1.创建标签：羊毛');
    expect(screen.getAllByRole('listitem')[1]).toHaveTextContent('创建标签关系：羊毛从属于材质');
    expect(screen.getAllByRole('listitem')[2]).toHaveTextContent('羊毛同义于羊绒');
    expect(screen.getAllByRole('listitem')[3]).toHaveTextContent('删除标签关系：羊毛从属于羊');
    fireEvent.click(screen.getAllByRole('button', { name: '查看标签 羊毛' })[0]);
    const dialog = screen.getByRole('dialog');
    const labels = dialog.querySelectorAll('.ant-descriptions-item-label');
    expect(labels).toHaveLength(10);
    expect(dialog).toHaveTextContent('英文分词');
    expect(dialog).not.toHaveTextContent('英文标签');
    expect(dialog).not.toHaveTextContent('sceneId');
    expect(dialog).toHaveTextContent('0');
    expect(dialog).toHaveTextContent('无');
    expect(dashboardApi.getKnowledgeGraphTagDetails).not.toHaveBeenCalled();
  });

  it('opens either relationship endpoint and switches between its attributes', async () => {
    vi.mocked(dashboardApi.getKnowledgeGraphTagDetails).mockImplementation(async (_brand, name) => ({ data: { ...proposed, name, isEnglish: 0 } } as never));
    render(<KGActionSteps brand="atlas" actions={[relations[0]]} />);
    fireEvent.click(screen.getByRole('button', { name: '查看标签 材质' }));
    await waitFor(() => expect(dashboardApi.getKnowledgeGraphTagDetails).toHaveBeenCalledWith('atlas', '材质'));
    await waitFor(() => expect(within(screen.getByRole('dialog')).getByText('英文分词')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('tab', { name: '羊毛' }));
    await waitFor(() => expect(dashboardApi.getKnowledgeGraphTagDetails).toHaveBeenCalledWith('atlas', '羊毛'));
    expect(screen.getByRole('tab', { name: '羊毛' })).toHaveAttribute('aria-selected', 'true');
  });

  it('uses prior newly created tag attributes for a later relationship', () => {
    render(<KGActionSteps brand="atlas" actions={[{ actionType: 'add_tag', params: proposed }, relations[0]]} />);
    fireEvent.click(screen.getAllByRole('button', { name: '查看标签 羊毛' })[1]);
    expect(screen.getByRole('dialog')).toHaveTextContent('羊毛面料');
    expect(dashboardApi.getKnowledgeGraphTagDetails).not.toHaveBeenCalled();
  });

  it('resolves an ID-only deletion through a read and uses its real relationship type', async () => {
    vi.mocked(dashboardApi.getKnowledgeGraphRelationshipDetails).mockResolvedValue({ data: { tagName: '羊毛', responseTagName: '羊绒', relationships: ['902227138815852544'] } } as never);
    render(<KGActionSteps brand="atlas" actions={[{ actionType: 'delete_relationship', params: { id: 'r1' }, dependsOn: [1] }]} />);
    expect(screen.getByText('（关系待确认）')).toBeInTheDocument();
    await screen.findByRole('button', { name: '查看标签 羊绒' });
    expect(screen.getByRole('listitem')).toHaveTextContent('删除标签关系：羊毛同义于羊绒');
    expect(dashboardApi.getKnowledgeGraphRelationshipDetails).toHaveBeenCalledWith('atlas', 'r1');
    expect(screen.getByText('依赖步骤：1')).toBeInTheDocument();
  });

  it('shows explicit read failures with retry and an empty strategy state', async () => {
    vi.mocked(dashboardApi.getKnowledgeGraphRelationshipDetails).mockRejectedValueOnce(new Error('unavailable')).mockResolvedValueOnce({ data: { tagName: '羊毛', responseTagName: '材质', relationships: '902227092548485120' } } as never);
    const { rerender } = render(<KGActionSteps brand="atlas" actions={[{ actionType: 'delete_relationship', params: { id: 'r1' } }]} />);
    await screen.findByText('关系详情读取失败');
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await screen.findByRole('button', { name: '查看标签 羊毛' });
    vi.mocked(dashboardApi.getKnowledgeGraphTagDetails).mockRejectedValueOnce(new Error('missing')).mockResolvedValueOnce({ data: proposed } as never);
    fireEvent.click(screen.getByRole('button', { name: '查看标签 羊毛' }));
    await screen.findByText('读取标签属性失败，请稍后重试');
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await screen.findByText('标签名称');
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    rerender(<KGActionSteps brand="atlas" actions={[]} />);
    expect(screen.getByText(/暂无可执行的知识图谱调整步骤/)).toBeInTheDocument();
  });

  it('does not guess unknown relationship types or add unsupported delete-tag capability', () => {
    render(<KGActionSteps brand="atlas" actions={[{ actionType: 'delete_tag' }, { actionType: 'update_relationship', params: { tagName: '羊毛', responseTagName: '羊', relationships: 'unknown' } }]} />);
    expect(screen.queryByText('删除标签：')).not.toBeInTheDocument();
    expect(screen.getByText('（关系待确认）')).toBeInTheDocument();
  });
});
