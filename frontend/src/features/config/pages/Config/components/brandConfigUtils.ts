import { weatherCityOptions } from '../weatherCityOptions';

const weatherCityByCode = new Map(weatherCityOptions.map((item) => [String(item.city_code), item]));
const weatherCityByName = new Map(weatherCityOptions.map((item) => [item.city, item]));

const splitNaturalLanguageList = (value: string) => value
  .split(/[、,，]/)
  .map((item) => item.trim())
  .filter(Boolean);

export const weatherCitySelectOptions = weatherCityOptions.map((item) => ({
  value: String(item.city_code),
  label: item.city,
}));

export const normalizeWeatherCityCodes = (value: unknown) => {
  if (!Array.isArray(value)) return [];
  return value.map((code) => {
    const option = weatherCityByCode.get(String(code));
    return option ? { city: option.city, city_code: option.city_code } : { city: String(code), city_code: Number(code) };
  });
};

export const formatSocialTrendsText = (value: unknown): string => {
  if (!Array.isArray(value)) return '';
  return value
    .map((item) => {
      if (!item || typeof item !== 'object') return '';
      const record = item as Record<string, unknown>;
      const description = typeof record.description === 'string' ? record.description.trim() : '';
      if (description) return description;
      const keywords = Array.isArray(record.keywords) ? record.keywords.map(String).filter(Boolean) : [];
      const tags = Array.isArray(record.tags) ? record.tags.map(String).filter(Boolean) : [];
      if (!keywords.length && !tags.length) return '';
      return tags.length ? `${keywords.join('、')}：${tags.join('、')}` : keywords.join('、');
    })
    .filter(Boolean)
    .join('\n');
};

export const normalizeSocialTrendsText = (value: unknown) => {
  if (typeof value !== 'string') return value;
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [keywordText, tagText = ''] = line.split(/[:：]/, 2);
      const keywords = splitNaturalLanguageList(keywordText);
      const tags = splitNaturalLanguageList(tagText);
      return { enabled: true, keywords: keywords.length ? keywords : [line], tags };
    });
};

export const weatherCitiesToCodes = (value: unknown): string[] => {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (typeof item === 'string' || typeof item === 'number') {
        const text = String(item);
        return weatherCityByCode.has(text) ? text : weatherCityByName.get(text)?.city_code.toString();
      }
      if (item && typeof item === 'object') {
        const record = item as { city?: unknown; city_code?: unknown };
        const code = record.city_code == null ? undefined : String(record.city_code);
        return code && weatherCityByCode.has(code) ? code : weatherCityByName.get(String(record.city ?? ''))?.city_code.toString();
      }
      return undefined;
    })
    .filter((item): item is string => Boolean(item));
};
