import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

// Pull the pure diff helpers out of app.js and run them in isolation.
const app = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');
const start = app.indexOf('function diffParse(');
const end = app.indexOf('async function previewSave(');
assert.ok(start > 0 && end > start, 'diff helpers not found in web/app.js');
const esc = (v) =>
  String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const ctx = { esc };
vm.runInNewContext(`${app.slice(start, end)}\nthis.api = { diffParse, diffRowsHTML, diffHTML };`, ctx);
const { diffParse, diffRowsHTML, diffHTML } = ctx.api;
// Arrays from the vm context belong to another realm; copy before deepEqual.
const kinds = (rows) => Array.from(rows, (r) => r.kind);

test('file headers are only recognised outside hunks', () => {
  const diff = [
    '--- a/schema.sql',
    '+++ b/schema.sql',
    '@@ -1,3 +1,3 @@',
    ' select 1;',
    '--- old comment',
    '+++i;',
    ' end;',
    '',
  ].join('\n');
  const rows = diffParse(diff);
  assert.deepEqual(kinds(rows), ['head', 'head', 'hunk', 'ctx', 'minus', 'plus', 'ctx']);
  assert.equal(rows[4].a, 2);
  assert.equal(rows[5].b, 2);
  assert.deepEqual([rows[6].a, rows[6].b], [3, 3]);
});

test('"\\ No newline at end of file" does not advance line numbers', () => {
  const diff = ['@@ -1,2 +1,2 @@', ' keep', '-old', '\\ No newline at end of file', '+new', '\\ No newline at end of file'].join(
    '\n',
  );
  const rows = diffParse(diff);
  assert.deepEqual(kinds(rows), ['hunk', 'ctx', 'minus', 'note', 'plus', 'note']);
  assert.equal(rows[2].a, 2);
  assert.equal(rows[4].b, 2);
});

test('a second file header after a finished hunk is a header again', () => {
  const diff = ['@@ -1 +1 @@', '-a', '+b', '--- a/two', '+++ b/two', '@@ -5,1 +5,1 @@', '-x', '+y'].join('\n');
  const rows = diffParse(diff);
  assert.deepEqual(kinds(rows), ['hunk', 'minus', 'plus', 'head', 'head', 'hunk', 'minus', 'plus']);
  assert.equal(rows[6].a, 5);
});

test('paired changed lines get a word-level highlight', () => {
  const html = diffRowsHTML(['@@ -1 +1 @@', '-const timeout = 30;', '+const timeout = 60;'].join('\n'));
  assert.match(html, /<mark class="diff-word">30<\/mark>/);
  assert.match(html, /<mark class="diff-word">60<\/mark>/);
  // Unrelated lines are not highlighted.
  const other = diffRowsHTML(['@@ -1 +1 @@', '-alpha', '+zzzzzz'].join('\n'));
  assert.doesNotMatch(other, /diff-word/);
});

test('content is escaped and bare +/- text still renders', () => {
  const html = diffHTML({ diff: '-<b>\n+<script>', added_lines: 1, removed_lines: 1 });
  assert.match(html, /&lt;b&gt;/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<b>/);
  assert.match(html, /diff-line minus/);
  assert.match(html, /diff-line plus/);
  assert.match(diffHTML({ diff: '' }), /没有内容差异/);
});
