// [2026-09-12] Offline check runner: bundles test/offline.test.ts with the esbuild
// that ships with Vite and executes it under plain node. No extra dependencies,
// no test framework, no browser.
// Usage (needs node_modules from `npm ci`): node test/build.mjs
import { build } from 'esbuild';

const outfile = process.env.CLONOTH_OFFLINE_OUT || '/tmp/clonoth-offline.mjs';

await build({
  entryPoints: ['test/offline.test.ts'],
  bundle: true,
  platform: 'node',
  target: 'node22',
  format: 'esm',
  outfile,
  logLevel: 'warning',
});

console.log(`bundled -> ${outfile}`);
