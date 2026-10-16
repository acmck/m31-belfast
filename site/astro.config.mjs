// @ts-check
import { defineConfig } from 'astro/config';

export default defineConfig({
  output: 'static',
  site: 'https://m31-belfast.pages.dev',
  build: { inlineStylesheets: 'always', format: 'file' },
  compressHTML: true,
});
