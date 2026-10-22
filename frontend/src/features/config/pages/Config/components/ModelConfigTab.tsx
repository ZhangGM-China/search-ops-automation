import React from 'react';
import NodeModelConfigSection from './NodeModelConfigSection';

interface Props {
  onDirtyChange: (dirty: boolean) => void;
}

const ModelConfigTab: React.FC<Props> = ({ onDirtyChange }) => (
  <div className="ops-page model-config-tab" style={{ paddingBottom: '20px' }}>
    <NodeModelConfigSection onDirtyChange={onDirtyChange} />
  </div>
);

export default ModelConfigTab;
