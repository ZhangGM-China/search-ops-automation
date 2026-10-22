export const asRecord = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
);

export const firstNonEmptyString = (...values: unknown[]): string | undefined => {
  for (const value of values) {
    if (typeof value === 'string') {
      const normalized = value.trim();
      if (normalized) return normalized;
    }
  }
  return undefined;
};

const imageCandidatesFromList = (value: unknown): string[] => {
  const items = Array.isArray(value) ? value : value == null ? [] : [value];
  return items.flatMap((item) => {
    if (typeof item === 'string') {
      const normalized = item.trim();
      return normalized ? [normalized] : [];
    }
    const record = asRecord(item);
    const nested = firstNonEmptyString(
      record.url,
      record.imageUrl,
      record.mainImage,
      record.src,
      record.path,
    );
    return nested ? [nested] : [];
  });
};

export const getProductImageCandidates = (product: Record<string, unknown>): string[] => [
  ...new Set([
    firstNonEmptyString(
      product.main_image,
      product.mainImage,
      product.imageUrl,
      product.image_url,
      product.image,
      product.cover,
      product.coverUrl,
      product.cover_image,
      product.pic,
      product.picture,
      product.pictureUrl,
      product.imgUrl,
      product.img,
      product.thumbnail,
      product.thumbnailUrl,
    ),
    ...imageCandidatesFromList(product.imageUrls),
    ...imageCandidatesFromList(product.images),
    ...imageCandidatesFromList(asRecord(product.media).images),
  ].filter((item): item is string => Boolean(item))),
];

export const getProductImage = (product: Record<string, unknown>): string | undefined => (
  getProductImageCandidates(product)[0]
);
