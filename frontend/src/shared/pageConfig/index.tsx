import React from 'react';
import { adminApi } from '@/shared/api';
import type { PageConfigPayload } from '@/shared/types';
import { defaultPageConfigPayload, normalizePageConfigPayload, PageConfigContext } from './context';

const defaultPageConfigLoader = async () => (await adminApi.getPageConfig()).data;

type Props = {
  children: React.ReactNode;
  loader?: () => Promise<PageConfigPayload>;
};

export const PageConfigProvider: React.FC<Props> = ({
  children,
  loader = defaultPageConfigLoader,
}) => {
  const [payload, setPayload] = React.useState<PageConfigPayload>(defaultPageConfigPayload);
  const [loading, setLoading] = React.useState(true);

  React.useEffect(() => {
    let active = true;
    loader()
      .then((next) => {
        if (!active) return;
        setPayload(normalizePageConfigPayload(next));
      })
      .catch(() => undefined)
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [loader]);

  const replacePayload = React.useCallback((next: PageConfigPayload) => {
    setPayload(normalizePageConfigPayload(next));
  }, []);

  const value = React.useMemo(() => ({ loading, payload, replacePayload }), [loading, payload, replacePayload]);
  return <PageConfigContext.Provider value={value}>{children}</PageConfigContext.Provider>;
};
