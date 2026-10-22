import dayjs from 'dayjs';
import utc from 'dayjs/plugin/utc';

dayjs.extend(utc);

// The backend persists naive UTC timestamps (for example
// "2026-10-13T16:30:17") without an offset suffix. dayjs parses those as
// local time, so they must be marked as UTC before display.
const NAIVE_UTC_PATTERN = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$/;

export function parseBackendDateTime(value: string): dayjs.Dayjs | null {
  const trimmed = value.trim();
  if (NAIVE_UTC_PATTERN.test(trimmed)) {
    const parsed = dayjs.utc(trimmed.replace(' ', 'T'));
    return parsed.isValid() ? parsed : null;
  }
  const fallback = dayjs(trimmed);
  return fallback.isValid() ? fallback : null;
}

/** Render a backend UTC timestamp in Beijing time (UTC+8). */
export function formatBackendDateTime(
  value: string | null | undefined,
  format = 'YYYY-MM-DD HH:mm',
): string {
  if (!value) return '';
  const parsed = typeof value === 'string' ? parseBackendDateTime(value) : null;
  return parsed ? parsed.utcOffset(8).format(format) : value;
}
