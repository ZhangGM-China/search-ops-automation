import React from 'react';
import { Button, Typography } from 'antd';
import { dashboardApi } from '@/shared/api';
import type { TopQueryGovernanceActionStep } from '@/shared/types';
import { KGTagDetailModal, type KGTagReference } from './KGTagDetailModal';

const { Text } = Typography;
const relations: Record<string, string> = {
  '902227138815852544': '同义于',
  '902227092548485120': '从属于',
  '902227674009042944': '互斥于',
};
const verbs: Record<string, string> = {
  add_tag: '创建标签', create_tag: '创建标签', update_tag: '更新标签',
  add_relationship: '创建标签关系', update_relationship: '更新标签关系', delete_relationship: '删除标签关系',
};
const typeOf = (action: TopQueryGovernanceActionStep) => action.actionType || action.action_type || '';
const tagNameOf = (params: Record<string, unknown>) => String(params.name || params.tagName || '').trim();

const KGActionStep: React.FC<{
  brand?: string;
  action: TopQueryGovernanceActionStep;
  index: number;
  proposedTags: Record<string, Record<string, unknown>>;
  showTag: (references: KGTagReference[]) => void;
}> = ({ brand, action, index, proposedTags, showTag }) => {
  const type = typeOf(action);
  const params = action.params || {};
  const isRelationship = type.endsWith('_relationship');
  const [relationship, setRelationship] = React.useState<Record<string, unknown>>({});
  const [readFailed, setReadFailed] = React.useState(false);
  const [retry, setRetry] = React.useState(0);
  React.useEffect(() => {
    if (!brand || !isRelationship || !params.id || (params.tagName && params.responseTagName)) return;
    let active = true;
    void dashboardApi.getKnowledgeGraphRelationshipDetails(brand, String(params.id)).then((response) => {
      if (active) setRelationship(response.data);
    }).catch(() => { if (active) setReadFailed(true); });
    return () => { active = false; };
  }, [brand, isRelationship, params.id, params.tagName, params.responseTagName, retry]);
  const source = { ...relationship, ...params };
  const child = isRelationship ? String(source.tagName || '').trim() : tagNameOf(params);
  const parent = String(source.responseTagName || '').trim();
  const rawRelation = source.relationships;
  const relation = relations[String(Array.isArray(rawRelation) ? rawRelation[0] : rawRelation)] || '（关系待确认）';
  const names = isRelationship ? [child, parent].filter(Boolean) : [child].filter(Boolean);
  const references = names.map((name) => ({
    name,
    proposed: !isRelationship ? params : proposedTags[name],
  }));
  const label = (name: string) => name ? <Button type="link" size="small" className="ops-kg-step__tag" aria-label={`查看标签 ${name}`} disabled={!brand} onClick={() => showTag([...references.filter((tag) => tag.name === name), ...references.filter((tag) => tag.name !== name)])}>{name}</Button> : <Text type="secondary">待确认</Text>;
  return <li className="ops-kg-step">
    <span className="ops-kg-step__number">{index + 1}.</span>
    <span>
      {verbs[type] ? <><strong>{verbs[type]}：</strong>{label(child)}{isRelationship ? <>{relation}{label(parent)}</> : null}</> : <Text>{action.title || '不支持的知识图谱动作，请人工复核'}</Text>}
      {readFailed ? <span className="ops-kg-step__read-error">关系详情读取失败<Button type="link" size="small" onClick={() => { setReadFailed(false); setRetry((value) => value + 1); }}>重试</Button></span> : null}
      {(action.dependsOn || action.depends_on)?.length ? <div className="ops-kg-step__dependency">依赖步骤：{(action.dependsOn || action.depends_on)?.join('、')}</div> : null}
    </span>
  </li>;
};

export const KGActionSteps: React.FC<{ brand?: string; actions: TopQueryGovernanceActionStep[] }> = ({ brand, actions }) => {
  const [tags, setTags] = React.useState<KGTagReference[]>([]);
  const [open, setOpen] = React.useState(false);
  const showTag = (references: KGTagReference[]) => { setTags(references); setOpen(true); };
  if (!actions.length) return <div className="ops-governance-plan__empty-steps">暂无可执行的知识图谱调整步骤，请人工复核</div>;
  const proposedTags: Record<string, Record<string, unknown>> = {};
  return <>
    <ol className="ops-governance-plan__steps ops-kg-steps">
      {actions.map((action, index) => {
        const priorTags = { ...proposedTags };
        if (['add_tag', 'create_tag'].includes(typeOf(action)) && action.params) proposedTags[tagNameOf(action.params)] = action.params;
        return <KGActionStep key={JSON.stringify([brand, action.actionId || action.action_id || index, typeOf(action), action.params])} brand={brand} action={action} index={index} proposedTags={priorTags} showTag={showTag} />;
      })}
    </ol>
    {brand ? <KGTagDetailModal brand={brand} tags={tags} open={open} onClose={() => setOpen(false)} /> : null}
  </>;
};
