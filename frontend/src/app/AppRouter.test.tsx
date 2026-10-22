import { describe, expect, it } from 'vitest'

import { AppRouter } from './AppRouter'

describe('AppRouter', () => {
  it('exposes the application route composition boundary', () => {
    expect(AppRouter).toBeTypeOf('function')
  })
})
