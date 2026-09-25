import { join } from 'node:path';
import { build } from 'esbuild';
import { codeOptions } from './build-options.mjs';
import { readFile, writeFile, readdir, stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';

const packageMeta = JSON.parse(await readFile('package.json', 'utf8'));
const sdkVersion = packageMeta.dependencies?.['@modelcontextprotocol/ext-apps'];
if (!sdkVersion) throw new Error('Missing @modelcontextprotocol/ext-apps dependency');

// Escape multiline SDK strings without altering their runtime content.
const result = await build({
  entryPoints: ['app.js'],
  bundle: true,
  write: false,
  format: 'iife',
  platform: 'browser',
  ...codeOptions,
});
const js = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const css = await readFile('app.css', 'utf8');
const outputs = {};
for (const kind of ['workspace', 'changes']) {
  const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CodePier · ${kind === 'workspace' ? '项目工作区' : '改动审阅'}</title><style>${css}</style><body data-kind="${kind}"><main id="app" aria-busy="true"><p class="muted">正在连接项目…</p></main><script>${js}</script></body></html>`;
  await writeFile(`${kind}-v1.html`, html);
  outputs[`${kind}-v1.html`] = {
    bytes: Buffer.byteLength(html),
    sha256: createHash('sha256').update(html).digest('hex'),
  };
}
await writeFile(
  'manifest.json',
  JSON.stringify({ sdk: `@modelcontextprotocol/ext-apps@${sdkVersion}`, outputs }, null, 2),
);
console.log(JSON.stringify(outputs));

// Ship the license/notice text from every package in the exact installed lock.
const seen = new Set(),
  notices = [];
async function collect(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    if (!entry.isDirectory() || entry.name.startsWith('.')) continue;
    const path = join(directory, entry.name);
    if (entry.name.startsWith('@')) {
      await collect(path);
      continue;
    }
    let meta;
    try {
      meta = JSON.parse(await readFile(join(path, 'package.json'), 'utf8'));
    } catch {
      continue;
    }
    const key = meta.name + '@' + meta.version;
    if (seen.has(key)) continue;
    seen.add(key);
    const names = (await readdir(path, { withFileTypes: true }))
      .filter((e) => e.isFile() && /^(LICENSE|LICENCE|NOTICE|COPYING)/i.test(e.name))
      .map((e) => e.name)
      .sort();
    const text = [];
    for (const name of names) {
      if ((await stat(join(path, name))).size >= 200000) continue;
      const license = (await readFile(join(path, name), 'utf8'))
        .replace(/\r\n?/g, '\n')
        .replace(/[ \t]+$/gm, '')
        .trimEnd();
      text.push('-- ' + name + ' --\n' + license);
    }
    if (text.length)
      notices.push(
        '='.repeat(72) +
          '\n' +
          key +
          '\nLicense: ' +
          (meta.license || 'see text') +
          '\n\n' +
          text.join('\n\n'),
      );
    try {
      await collect(join(path, 'node_modules'));
    } catch (e) {
      if (e.code !== 'ENOENT') throw e;
    }
  }
}
await collect('node_modules');
const noticeText =
  notices
    .join('\n\n')
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t]+$/gm, '')
    .trimEnd() + '\n';
await writeFile('THIRD_PARTY_NOTICES.txt', noticeText);
