import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PageConfigProvider } from './index';
import { usePageConfig } from './context';

const Probe = () => {
  const enabled = usePageConfig('brand_overview.card.show_operational_data');
  return <span>{enabled ? 'visible' : 'hidden'}</span>;
};

describe('PageConfigProvider', () => {
  it('uses safe defaults before the server response arrives', () => {
    render(
      <PageConfigProvider loader={async () => new Promise(() => undefined)}>
        <Probe />
      </PageConfigProvider>,
    );

    expect(screen.getByText('visible')).toBeInTheDocument();
  });

  it('applies a server value for a registered key', async () => {
    const loader = vi.fn().mockResolvedValue({
      schemaVersion: 1,
      version: 3,
      values: { 'brand_overview.show_operational_data': false },
      definitions: [],
    });
    render(
      <PageConfigProvider loader={loader}>
        <Probe />
      </PageConfigProvider>,
    );

    await waitFor(() => expect(screen.getByText('hidden')).toBeInTheDocument());
    expect(loader).toHaveBeenCalledTimes(1);
  });
});
