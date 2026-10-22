import React from 'react';

interface ErrorBoundaryProps {
  children: React.ReactNode;
  onReload?: () => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

// 全局兜底：任何未捕获的渲染异常都展示可恢复的提示，而不是卸载整棵树导致白屏。
export class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error('Unhandled UI error:', error, errorInfo.componentStack);
  }

  private handleReload = () => {
    const { onReload } = this.props;
    if (onReload) {
      onReload();
      return;
    }
    window.location.reload();
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="app-loading-shell">
        <div className="app-loading-panel" role="alert">
          <h2 style={{ margin: '0 0 8px', fontSize: 18, color: 'var(--text-primary)' }}>页面出现异常</h2>
          <p style={{ margin: '0 0 16px' }}>系统可能刚刚更新，请刷新页面恢复。</p>
          <button
            type="button"
            onClick={this.handleReload}
            style={{
              padding: '8px 24px',
              border: 'none',
              borderRadius: 'var(--radius-md)',
              background: 'var(--brand-primary)',
              color: '#fff',
              fontSize: 14,
              cursor: 'pointer',
            }}
          >
            刷新页面
          </button>
        </div>
      </div>
    );
  }
}
