import React from 'react';

import MobileLayout from './MobileLayout';
import useMobile from '@/shared/hooks/useMobile';
import { MainLayout } from './MainLayout';
import { PageConfigProvider } from '@/shared/pageConfig';

export const ResponsiveLayout: React.FC = () => {
  const isMobile = useMobile();
  return (
    <PageConfigProvider>
      {isMobile ? <MobileLayout /> : <MainLayout />}
    </PageConfigProvider>
  );
};
