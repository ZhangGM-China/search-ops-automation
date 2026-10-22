import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { execSync } from 'node:child_process'
import { realpathSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const TOP_QUERY_FRONTEND_WORKTREE = dirname(fileURLToPath(import.meta.url))
const TOP_QUERY_FRONTEND_REALPATH = realpathSync(TOP_QUERY_FRONTEND_WORKTREE)
const API_PROXY_TARGET = process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8014'

const isFrontendValidationPort = process.argv.some((arg) => arg === '5174' || arg.includes('5174'))
const currentBranch = () => {
  try {
    return execSync('git branch --show-current', { encoding: 'utf8' }).trim()
  } catch {
    return ''
  }
}
const currentFrontendPath = () => {
  try {
    return realpathSync(process.cwd())
  } catch {
    return process.cwd()
  }
}

if (
  process.env.AIOPS_ALLOW_FRONTEND_5174 !== 'true'
  && isFrontendValidationPort
  && currentFrontendPath() !== TOP_QUERY_FRONTEND_REALPATH
) {
  throw new Error([
    'Port 5174 is reserved for the Query Governance frontend validation worktree.',
    `Start it from: ${TOP_QUERY_FRONTEND_WORKTREE}`,
    `Current branch: ${currentBranch() || 'unknown'}`,
    'Use AIOPS_ALLOW_FRONTEND_5174=true only for an intentional override.',
  ].join('\n'))
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(TOP_QUERY_FRONTEND_WORKTREE, 'src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: API_PROXY_TARGET,
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) {
            return
          }
          if (id.includes('@ant-design/charts') || id.includes('@ant-design/plots') || id.includes('/@antv/')) {
            return
          }
          if (id.includes('react') || id.includes('react-dom') || id.includes('react-router-dom')) {
            return 'react-vendor'
          }
          if (id.includes('antd-mobile')) {
            return 'antd-mobile-vendor'
          }
          if (id.includes('/antd/es/table') || id.includes('/antd/es/pagination')) {
            return 'antd-table-vendor'
          }
          if (id.includes('/antd/es/form') || id.includes('/antd/es/input') || id.includes('/antd/es/input-number')) {
            return 'antd-form-vendor'
          }
          if (id.includes('/antd/es/select') || id.includes('/antd/es/date-picker')) {
            return 'antd-picker-vendor'
          }
          if (id.includes('/antd/es/modal') || id.includes('/antd/es/message') || id.includes('/antd/es/notification')) {
            return 'antd-feedback-vendor'
          }
          if (id.includes('/antd/es/layout') || id.includes('/antd/es/menu') || id.includes('/antd/es/dropdown') || id.includes('/antd/es/breadcrumb')) {
            return 'antd-layout-vendor'
          }
          if (id.includes('/antd/es/row') || id.includes('/antd/es/col') || id.includes('/antd/es/grid')) {
            return 'antd-grid-vendor'
          }
          if (id.includes('/antd/es/typography')) {
            return 'antd-typography-vendor'
          }
          if (
            id.includes('/antd/es/statistic') ||
            id.includes('/antd/es/progress') ||
            id.includes('/antd/es/timeline') ||
            id.includes('/antd/es/badge') ||
            id.includes('/antd/es/empty') ||
            id.includes('/antd/es/skeleton')
          ) {
            return 'antd-display-vendor'
          }
          if (id.includes('/antd/es/drawer') || id.includes('/antd/es/alert')) {
            return 'antd-panel-vendor'
          }
          if (
            id.includes('/antd/es/table') ||
            id.includes('/antd/es/pagination') ||
            id.includes('/antd/es/_util/responsiveObserver')
          ) {
            return 'antd-table-vendor'
          }
          if (
            id.includes('/antd/es/form') ||
            id.includes('/antd/es/input') ||
            id.includes('/antd/es/input-number') ||
            id.includes('/antd/es/radio') ||
            id.includes('/antd/es/slider') ||
            id.includes('/antd/es/tooltip') ||
            id.includes('/antd/es/upload')
          ) {
            return 'antd-form-vendor'
          }
          if (
            id.includes('/antd/es/select') ||
            id.includes('/antd/es/date-picker') ||
            id.includes('/antd/es/time-picker') ||
            id.includes('/antd/es/picker') ||
            id.includes('/antd/es/cascader')
          ) {
            return 'antd-picker-vendor'
          }
          if (
            id.includes('/antd/es/modal') ||
            id.includes('/antd/es/message') ||
            id.includes('/antd/es/notification') ||
            id.includes('/antd/es/popover') ||
            id.includes('/antd/es/popconfirm')
          ) {
            return 'antd-feedback-vendor'
          }
          if (
            id.includes('/antd/es/list') ||
            id.includes('/antd/es/descriptions') ||
            id.includes('/antd/es/avatar') ||
            id.includes('/antd/es/tabs') ||
            id.includes('/antd/es/steps')
          ) {
            return 'antd-data-vendor'
          }
          if (
            id.includes('/antd/es/button') ||
            id.includes('/antd/es/space') ||
            id.includes('/antd/es/flex') ||
            id.includes('/antd/es/card') ||
            id.includes('/antd/es/tag') ||
            id.includes('/antd/es/spin') ||
            id.includes('/antd/es/switch') ||
            id.includes('/antd/es/checkbox') ||
            id.includes('/antd/es/divider') ||
            id.includes('/antd/es/result') ||
            id.includes('/antd/es/tooltip')
          ) {
            return 'antd-ui-vendor'
          }
          if (id.includes('/antd/es/config-provider') || id.includes('/antd/es/theme')) {
            return 'antd-theme-vendor'
          }
          if (id.includes('/antd/es/_util') || id.includes('/antd/es/style') || id.includes('/antd/es/locale')) {
            return 'antd-infra-vendor'
          }
          if (id.includes('@ant-design/cssinjs')) {
            return 'ant-style-vendor'
          }
          if (id.includes('@ant-design/colors') || id.includes('@ant-design/fast-color')) {
            return 'ant-color-vendor'
          }
          if (id.includes('@ant-design/react-slick')) {
            return 'ant-react-slick-vendor'
          }
          if (id.includes('@ant-design/icons')) {
            return 'ant-icons-vendor'
          }
          if (id.includes('/@rc-component/') || id.includes('/rc-')) {
            return 'rc-vendor'
          }
          if (id.includes('/antd/')) {
            return 'antd-core-vendor'
          }
          if (id.includes('@ant-design') && !id.includes('@ant-design/charts') && !id.includes('@ant-design/plots')) {
            return 'ant-design-vendor'
          }
          if (id.includes('dayjs')) {
            return 'dayjs-vendor'
          }
          return 'vendor'
        },
      },
    },
  },
})
