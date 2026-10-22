import React from 'react';
import { createPortal } from 'react-dom';

interface ConfigContextActionsProps {
  children: React.ReactNode;
  targetId?: string;
}

const ConfigContextActions: React.FC<ConfigContextActionsProps> = ({ children, targetId = 'config-context-actions' }) => {
  const [target, setTarget] = React.useState<HTMLElement | null>(null);
  React.useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) setTarget(document.getElementById(targetId));
    });
    return () => { active = false; };
  }, [targetId]);
  return target ? createPortal(children, target) : <>{children}</>;
};

export default ConfigContextActions;
