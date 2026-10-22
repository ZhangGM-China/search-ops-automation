import { describe, expect, it } from 'vitest'
import viteConfig from './vite.config'

describe('Vite chunk strategy', () => {
  it('keeps AntV dependencies out of the initial vendor preload graph', () => {
    const manualChunks = viteConfig.build?.rollupOptions?.output?.manualChunks

    expect(typeof manualChunks).toBe('function')

    if (typeof manualChunks !== 'function') return

    expect(manualChunks('/workspace/node_modules/@ant-design/charts/es/index.js')).toBeUndefined()
    expect(manualChunks('/workspace/node_modules/@antv/g2/es/index.js')).toBeUndefined()
    expect(manualChunks('/workspace/node_modules/react/index.js')).toBe('react-vendor')
  })
})
