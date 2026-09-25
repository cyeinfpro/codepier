import { build } from 'esbuild';
await build({
  entryPoints: ['../core/index.mjs'],
  bundle: true,
  format: 'iife',
  globalName: 'CP',
  platform: 'browser',
  target: 'es2022',
  minify: false,
  legalComments: 'eof',
  outfile: '../core/bundle.js',
});
