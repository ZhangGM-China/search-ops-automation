/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react-swc'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const frontendRoot = dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(frontendRoot, 'src'),
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    exclude: ['**/node_modules/**', '**/dist/**', '**/e2e/**'],
    fileParallelism: false,
    coverage: {
      provider: 'v8',
      reporter: ['text'],
      exclude: [
        '**/node_modules/**',
        '**/dist/**',
        '**/e2e/**',
        'src/main.tsx',
        'src/App.tsx',
        'src/components/MobileLayout.tsx',
        'src/mocks/**',
        'src/types/**',
        'src/test/**',
        'src/testUtils/**',
      ],
      thresholds: {
        statements: 80,
        branches: 72,
        functions: 80,
        lines: 80,
      },
    },
  },
})
