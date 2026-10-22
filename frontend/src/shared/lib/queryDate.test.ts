import { describe, expect, it } from 'vitest';
import dayjs from 'dayjs';
import { formatQueryDate } from './queryDate';

describe('formatQueryDate', () => {
  it('formats supported date-like values for API filters', () => {
    expect(formatQueryDate('2026-06-16T12:00:00Z')).toBe('2026-06-16');
    expect(formatQueryDate(new Date('2026-06-16T00:00:00'))).toBe('2026-06-16');
    expect(formatQueryDate(dayjs('2026-06-16'))).toBe('2026-06-16');
  });

  it('drops empty or unsupported filter values instead of leaking invalid dates', () => {
    expect(formatQueryDate(null)).toBeUndefined();
    expect(formatQueryDate(undefined)).toBeUndefined();
    expect(formatQueryDate('')).toBeUndefined();
    expect(formatQueryDate({ value: '2026-06-16' })).toBeUndefined();
  });
});
