import React from 'react';
import { createRoot } from 'react-dom/client';
import type { TopQueryGovernancePlan } from '../../src/shared/types';
import { GovernancePlans } from '../../src/features/query-governance/components/QueryDetail/GovernancePlans';
import '../../src/styles/index.css';

const plans: TopQueryGovernancePlan[] = [{
  id: 'relevance-22', taskId: 1, actionId: 22, actionIds: [22], type: 'kg', actionType: 'relevance_plan',
  title: '相关性治理调整方案', objective: '', executionMode: 'human_approval', status: 'pending',
  statusLabel: '待执行', executable: true, steps: [], evidence: [], products: [],
  causeGroups: [
    { groupId: 'kg-1', rootCause: 'kg_issue', actionIds: [22], executable: true, status: 'pending', statusLabel: '待执行',
      products: [{ productId: 'P1', productName: '羊毛外套', status: 'blacklisted', reason: '错误的从属关系' }],
      actions: [
        { actionType: 'add_tag', params: { name: '羊毛', viewTag: '羊毛', isRoot: '0', dimensions: ['材质'], weight: 8, isEnglish: 0, segmentingAgain: '1', isCompose: '0', nameAnd: [], nameNot: [] } },
        { actionType: 'add_relationship', params: { tagName: '羊毛', responseTagName: '材质', relationships: '902227092548485120' }, dependsOn: [1] },
      ],
    },
    { groupId: 'individual-2', rootCause: 'individual', executable: false,
      products: [{ productId: 'P2', productName: '针织上衣', status: 'blacklisted', reason: '个例不相关' }], actions: [] },
  ],
}];

createRoot(document.getElementById('root')!).render(<div style={{ maxWidth: 980, margin: '32px auto', padding: 24, background: '#fff' }}>
  <GovernancePlans plans={plans} brand="atlas" query="羊毛外套" displayLimit={16} inventoryBrokenRateThreshold={0.25} onOpenProduct={() => undefined}
    onExecutePlan={(plan) => { void fetch(`/api/query-governance/actions/${plan.actionId}/execute`, { method: 'POST', body: JSON.stringify({ groupId: plan.causeGroupId }) }); }}
    onBatchBlacklistProducts={async (plan, products, action) => {
      const response = await fetch('/api/e2e/batch', { method: 'POST', body: JSON.stringify({ groupId: plan.causeGroupId, products: products.map((product) => product.productId), action }) });
      return response.ok;
    }} />
</div>);
