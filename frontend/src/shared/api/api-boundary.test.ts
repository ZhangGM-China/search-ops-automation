import { describe, expect, it } from 'vitest'

import { AUTH_EXPIRED_EVENT, authApi } from '@/shared/api'

describe('shared API boundary', () => {
  it('exposes the existing authentication contract from the shared API entry', () => {
    expect(AUTH_EXPIRED_EVENT).toBe('search-ops:auth-expired')
    expect(authApi).toBeTypeOf('object')
  })
})
