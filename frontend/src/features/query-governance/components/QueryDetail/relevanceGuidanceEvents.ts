export const RELEVANCE_GUIDANCE_UPDATED_EVENT = 'ops:relevance-guidance-updated';

export const notifyRelevanceGuidanceUpdated = (guidance: { brand: string; query: string; rules: string; status: string }) => {
  window.dispatchEvent(new CustomEvent(RELEVANCE_GUIDANCE_UPDATED_EVENT, { detail: guidance }));
};
