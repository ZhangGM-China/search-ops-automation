import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const originalLogoPath = resolve(process.cwd(), 'public/brand-logo.png');
const originalLogoSha256 = 'e58071b38452578f363a2b8344349697ed76078f3fbe67569ffa5f5cd08aa09a';

describe('brand assets', () => {
  it('keeps an exact local copy of the original 128px PNG logo', () => {
    const logo = readFileSync(originalLogoPath);

    expect(logo.subarray(1, 4).toString('ascii')).toBe('PNG');
    expect(logo.readUInt32BE(16)).toBe(128);
    expect(logo.readUInt32BE(20)).toBe(128);
    expect(createHash('sha256').update(logo).digest('hex')).toBe(originalLogoSha256);
  });
});
