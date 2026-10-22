import { describe, expect, it } from 'vitest'

const accountUiFiles = Object.keys(import.meta.glob('../../features/account/pages/AccountManagement/components/AssistantAccessConfigSection.tsx'))
const productionUiSources = import.meta.glob('../../features/**/*.tsx', {
  eager: true,
  query: '?raw',
  import: 'default',
})

const MAX_PRODUCTION_UI_LINES = 500

const oversizedProductionUiFiles = Object.entries(productionUiSources)
  .filter(([path, source]) => path.endsWith('.tsx') && !path.endsWith('.test.tsx') && typeof source === 'string')
  .map(([path, source]) => ({ path, lines: (source as string).split('\n').length }))
  .filter(({ lines }) => lines > MAX_PRODUCTION_UI_LINES)

describe('large production UI boundaries', () => {
  it('retires the standalone assistant access configuration from account management', () => {
    expect(accountUiFiles).toEqual([])
  })

  it('keeps every production UI module within the default split threshold', () => {
    expect(oversizedProductionUiFiles).toEqual([])
  })
})
