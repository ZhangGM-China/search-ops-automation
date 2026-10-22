import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import 'antd-mobile/es/global'
import './styles/index.css'
import App from './App.tsx'
import { ErrorBoundary } from './app/ErrorBoundary'
import { installChunkReloadGuard } from './app/chunkReloadGuard'

const container = document.getElementById('root');
if (!container) {
  throw new Error('Root container not found');
}

installChunkReloadGuard();

const root = createRoot(container);
root.render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
