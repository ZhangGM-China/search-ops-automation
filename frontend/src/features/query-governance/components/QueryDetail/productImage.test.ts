import { describe, expect, it } from 'vitest';
import {
  asRecord,
  firstNonEmptyString,
  getProductImage,
  getProductImageCandidates,
} from './productImage';

describe('productImage helpers', () => {
  it('normalizes records and selects the first non-empty string', () => {
    expect(asRecord(null)).toEqual({});
    expect(asRecord(['x'])).toEqual({});
    expect(asRecord({ id: 1 })).toEqual({ id: 1 });
    expect(firstNonEmptyString('  ', null, '  image.jpg  ', 'later')).toBe('image.jpg');
    expect(firstNonEmptyString('', 123, undefined)).toBeUndefined();
  });

  it('collects image aliases and nested list/media values in stable order', () => {
    const product = {
      main_image: ' main.jpg ',
      imageUrl: 'duplicate.jpg',
      imageUrls: ['url-a.jpg', { url: 'nested-a.jpg' }, { src: 'nested-b.jpg' }, 'url-a.jpg'],
      images: [{ imageUrl: 'image-a.jpg' }, 'image-b.jpg'],
      media: { images: [{ path: 'media-a.jpg' }, { url: 'main.jpg' }] },
    };
    expect(getProductImageCandidates(product)).toEqual([
      'main.jpg', 'url-a.jpg', 'nested-a.jpg', 'nested-b.jpg', 'image-a.jpg', 'image-b.jpg', 'media-a.jpg',
    ]);
    expect(getProductImage(product)).toBe('main.jpg');
  });

  it('ignores malformed or empty image sources', () => {
    expect(getProductImageCandidates({ imageUrls: [null, '  ', 1], images: {}, media: null })).toEqual([]);
    expect(getProductImage({})).toBeUndefined();
  });
});
