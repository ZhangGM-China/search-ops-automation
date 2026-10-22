import React from 'react';
import { Card, Select, Typography } from 'antd';
import BrandExperimentPanel from './BrandExperimentPanel';

interface BrandOption { key: string; name: string }

interface Props {
  brand: string;
  brands: BrandOption[];
  canManage: boolean;
  experimentId?: number;
  view: 'config' | 'effect';
  onBrandChange: (brand: string) => void;
  onExperimentChange: (experimentId?: number, mode?: 'push' | 'replace') => void;
  onViewChange: (view: 'config' | 'effect') => void;
  onDraftDirtyChange: (dirty: boolean) => void;
  confirmNavigation: () => boolean;
}

const ExperimentTab: React.FC<Props> = ({ brand, brands, canManage, experimentId, view, onBrandChange, onExperimentChange, onViewChange, onDraftDirtyChange, confirmNavigation }) => (
  <div className="experiment-workbench">
    <Card size="small" className="experiment-workbench__intro">
      <div className="experiment-workbench__intro-row">
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>实验工作台</Typography.Title>
          <Typography.Text type="secondary">固定 20 个实验组持续治理，20 个对照组只观测，用统一质量门禁评估净效果。</Typography.Text>
        </div>
        <Select
          aria-label="A/B Test 品牌"
          value={brand}
          onChange={onBrandChange}
          options={brands.map((item) => ({ value: item.key, label: item.name }))}
          style={{ minWidth: 160 }}
        />
      </div>
    </Card>
    <BrandExperimentPanel
      key={brand}
      brand={brand}
      canManage={canManage}
      embedded
      experimentId={experimentId}
      view={view}
      onExperimentChange={onExperimentChange}
      onViewChange={onViewChange}
      onDirtyChange={onDraftDirtyChange}
      confirmNavigation={confirmNavigation}
    />
  </div>
);

export default ExperimentTab;
