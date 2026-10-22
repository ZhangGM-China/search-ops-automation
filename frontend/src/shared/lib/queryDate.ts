import dayjs from 'dayjs';

export function formatQueryDate(value: unknown): string | undefined {
  if (value === null || value === undefined || value === '') {
    return undefined;
  }

  if (dayjs.isDayjs(value)) {
    return value.format('YYYY-MM-DD');
  }

  if (
    typeof value === 'string' ||
    typeof value === 'number' ||
    value instanceof Date
  ) {
    return dayjs(value).format('YYYY-MM-DD');
  }

  return undefined;
}
