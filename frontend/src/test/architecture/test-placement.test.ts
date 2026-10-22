import { describe, expect, it } from 'vitest'

const mobileIntegrationTestFiles = Object.keys(import.meta.glob('../integration/mobile/*.test.tsx'))
const legacyMobilePageTestFiles = Object.keys(import.meta.glob('../../pages/mobile{Views,Callbacks}.test.tsx'))
const sourceFiles = Object.keys(import.meta.glob('../../shared/lib/error.ts'))
const colocatedUnitTestFiles = Object.keys(import.meta.glob('../../shared/lib/error.test.ts'))
const featurePageEntries = Object.keys(import.meta.glob('../../features/**/pages/**/index.tsx'))
const featureLayoutFiles = Object.keys(import.meta.glob('../../app/layouts/*.tsx'))
const legacyPageFiles = Object.keys(import.meta.glob('../../pages/**/*.{ts,tsx}'))
const legacyComponentFiles = Object.keys(import.meta.glob('../../components/**/*.{ts,tsx}'))
const legacyConstantsFiles = Object.keys(import.meta.glob('../../constants/**/*.{ts,tsx}'))
const legacyUtilityFiles = Object.keys(import.meta.glob('../../utils/**/*.{ts,tsx}'))

const hasSourceFile = (suffix: string) => sourceFiles.some((file) => file.endsWith(suffix))

describe('frontend test placement', () => {
  it('keeps cross-page mobile integration tests in the mobile integration suite', () => {
    expect(mobileIntegrationTestFiles).toContain('../integration/mobile/mobileViews.test.tsx')
    expect(mobileIntegrationTestFiles).toContain('../integration/mobile/mobileCallbacks.test.tsx')
  })

  it('removes the legacy pages-level locations for cross-page mobile integration tests', () => {
    expect(legacyMobilePageTestFiles).toEqual([])
  })

  it('keeps unit tests colocated with the code they cover', () => {
    expect(hasSourceFile('/shared/lib/error.ts')).toBe(true)
    expect(colocatedUnitTestFiles).toContain('../../shared/lib/error.test.ts')
  })

  it('keeps routed pages and their tests inside their owning features', () => {
    expect(featurePageEntries).toEqual(expect.arrayContaining([
      '../../features/auth/pages/Login/index.tsx',
      '../../features/account/pages/AccountManagement/index.tsx',
      '../../features/query-governance/pages/BrandOverview/index.tsx',
      '../../features/query-governance/pages/BrandDetail/index.tsx',
      '../../features/assistant/pages/SmartAssistant/index.tsx',
      '../../features/config/pages/Config/index.tsx',
      '../../features/operations/pages/BlacklistLogs/index.tsx',
      '../../features/operations/pages/DemotedLogs/index.tsx',
      '../../features/operations/pages/StockTasks/index.tsx',
      '../../features/no-result/pages/NoResultGovernance/index.tsx',
    ]))
  })

  it('keeps responsive layout components in app and removes legacy public buckets', () => {
    expect(featureLayoutFiles).toContain('../../app/layouts/MobileLayout.tsx')
    expect(legacyPageFiles).toEqual([])
    expect(legacyComponentFiles).toEqual([])
    expect(legacyConstantsFiles).toEqual([])
    expect(legacyUtilityFiles).toEqual([])
  })
})
