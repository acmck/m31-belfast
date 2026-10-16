import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

// Resolve public/ from the project root (astro runs with cwd = site/ via npm --prefix).
const PUBLIC = [path.resolve(process.cwd(), 'public'), path.resolve(process.cwd(), 'site/public')].find((p) =>
  existsSync(path.join(p, 'fonts')),
) ?? path.resolve(process.cwd(), 'public');
const cache = new Map<string, string>();

/** Append a short content hash so /img can be cached as immutable. */
export function versioned(url: string): string {
  if (!url.startsWith('/') || url.startsWith('//')) return url;
  const hit = cache.get(url);
  if (hit) return hit;
  let out = url;
  try {
    const buf = readFileSync(path.join(PUBLIC, decodeURIComponent(url)));
    out = `${url}?v=${createHash('sha256').update(buf).digest('hex').slice(0, 10)}`;
  } catch {
    console.warn(`[m31-belfast] missing image: ${url}`);
  }
  cache.set(url, out);
  return out;
}

