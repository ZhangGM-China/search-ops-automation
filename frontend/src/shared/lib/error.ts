type ApiError = {
  response?: {
    data?: {
      detail?: string | {
        message?: string;
      };
    };
  };
  message?: string;
};

export const getApiErrorMessage = (error: unknown, fallback: string) => {
  const apiError = error as ApiError;
  const detail = apiError.response?.data?.detail;
  return (typeof detail === 'string' ? detail : detail?.message) || apiError.message || fallback;
};
