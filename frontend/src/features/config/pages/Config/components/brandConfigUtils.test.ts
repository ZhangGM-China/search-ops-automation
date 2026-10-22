import { describe, expect, it } from 'vitest';
import {
  formatSocialTrendsText,
  normalizeSocialTrendsText,
  normalizeWeatherCityCodes,
  weatherCitiesToCodes,
} from './brandConfigUtils';

describe('brandConfigUtils', () => {
  it('normalizes weather city values from persisted objects, codes, and names', () => {
    expect(normalizeWeatherCityCodes(['101020100', '999999'])).toEqual([
      { city: '上海', city_code: 101020100 },
      { city: '999999', city_code: 999999 },
    ]);
    expect(normalizeWeatherCityCodes('101020100')).toEqual([]);

    expect(weatherCitiesToCodes([
      '上海',
      101010100,
      { city: '广州市' },
      { city_code: 101280101 },
      null,
      'unknown',
    ])).toEqual(['101020100', '101010100', '101280101']);
    expect(weatherCitiesToCodes('上海市')).toEqual([]);
  });

  it('round-trips social trend descriptions and natural language keyword lists', () => {
    expect(formatSocialTrendsText([
      { description: '夏季趋势：无袖、轻薄', keywords: ['ignored'], tags: ['ignored'] },
      { keywords: ['连衣裙', '半裙'], tags: ['无袖', '轻薄'] },
      { keywords: ['防晒衣'], tags: [] },
      { keywords: [], tags: [] },
      null,
    ])).toBe('夏季趋势：无袖、轻薄\n连衣裙、半裙：无袖、轻薄\n防晒衣');
    expect(formatSocialTrendsText('not-array')).toBe('');

    expect(normalizeSocialTrendsText('连衣裙、半裙：无袖，轻薄\n防晒衣')).toEqual([
      { enabled: true, keywords: ['连衣裙', '半裙'], tags: ['无袖', '轻薄'] },
      { enabled: true, keywords: ['防晒衣'], tags: [] },
    ]);
    const passthrough = [{ enabled: false, keywords: ['旧配置'], tags: [] }];
    expect(normalizeSocialTrendsText(passthrough)).toBe(passthrough);
  });
});
