import { describe, expect, it } from 'vitest';
import { formatBackendDateTime, parseBackendDateTime } from './backendTime';

describe('formatBackendDateTime', () => {
  it('converts a naive backend UTC timestamp to Beijing time', () => {
    expect(formatBackendDateTime('2026-10-13T16:30:17')).toBe('2026-10-14 00:30');
  });

  it('supports space-separated and second-less inputs', () => {
    expect(formatBackendDateTime('2026-10-13 16:30')).toBe('2026-10-14 00:30');
  });

  it('keeps an explicit offset timestamp unchanged semantically', () => {
    expect(formatBackendDateTime('2026-10-13T16:30:17Z')).toBe('2026-10-14 00:30');
  });

  it('returns the raw value for unparsable input', () => {
    expect(formatBackendDateTime('not-a-time')).toBe('not-a-time');
  });

  it('returns an empty string for empty input', () => {
    expect(formatBackendDateTime(null)).toBe('');
    expect(formatBackendDateTime(undefined)).toBe('');
    expect(formatBackendDateTime('')).toBe('');
  });
});

describe('parseBackendDateTime', () => {
  it('treats naive timestamps as UTC', () => {
    expect(parseBackendDateTime('2026-10-13T16:30:17')?.utcOffset(0).format('HH:mm')).toBe('16:30');
  });

  it('returns null for unparsable input', () => {
    expect(parseBackendDateTime('not-a-time')).toBeNull();
  });
});
