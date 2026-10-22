import type { AxiosResponse } from 'axios';

export function axiosResponse<T>(data: T): AxiosResponse<T> {
  return {
    data,
    status: 200,
    statusText: 'OK',
    headers: {},
    config: {
      headers: {},
    },
  } as AxiosResponse<T>;
}
