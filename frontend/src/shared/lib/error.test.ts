import { describe, expect, it } from 'vitest';
import { getApiErrorMessage } from './error';

describe('getApiErrorMessage', () => {
  it('prefers backend detail, then error message, then fallback', () => {
    expect(getApiErrorMessage({ response: { data: { detail: 'backend detail' } } }, 'fallback')).toBe('backend detail');
    expect(getApiErrorMessage({ response: { data: { detail: { message: 'structured conflict' } } } }, 'fallback')).toBe('structured conflict');
    expect(getApiErrorMessage({ message: 'plain message' }, 'fallback')).toBe('plain message');
    expect(getApiErrorMessage({}, 'fallback')).toBe('fallback');
  });
});
