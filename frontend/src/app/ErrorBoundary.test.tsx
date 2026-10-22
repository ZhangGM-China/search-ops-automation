import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ErrorBoundary } from './ErrorBoundary';

const Boom = () => {
  throw new Error('render failed');
};

const originalConsoleError = console.error;

describe('ErrorBoundary', () => {
  beforeEach(() => {
    // React 会打印组件崩溃堆栈，测试中静音
    console.error = vi.fn();
  });

  afterEach(() => {
    console.error = originalConsoleError;
  });

  it('renders children when there is no error', () => {
    render(
      <ErrorBoundary>
        <div>healthy-page</div>
      </ErrorBoundary>
    );
    expect(screen.getByText('healthy-page')).toBeInTheDocument();
  });

  it('shows a recoverable fallback instead of unmounting the whole app', () => {
    const onReload = vi.fn();
    render(
      <ErrorBoundary onReload={onReload}>
        <Boom />
      </ErrorBoundary>
    );

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('页面出现异常')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '刷新页面' }));
    expect(onReload).toHaveBeenCalledTimes(1);
  });
});
