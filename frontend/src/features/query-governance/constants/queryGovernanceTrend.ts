export type TrendMetricKey = 'uv' | 'pv' | 'clickUv' | 'addToCartUv' | 'purchaseUv' | 'ctr' | 'atc' | 'cvr';

export const trendMetricDefinitions: Array<{ key: TrendMetricKey; label: string; rate?: boolean }> = [
  { key: 'uv', label: '搜索UV' },
  { key: 'pv', label: 'PV' },
  { key: 'clickUv', label: '点击UV' },
  { key: 'addToCartUv', label: '加购UV' },
  { key: 'purchaseUv', label: '购买UV' },
  { key: 'ctr', label: 'CTR', rate: true },
  { key: 'atc', label: 'ATC', rate: true },
  { key: 'cvr', label: 'CVR', rate: true },
];
