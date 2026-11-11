import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../web/lazy-assets.js', import.meta.url), 'utf8');
function fixture() {
  const nodes = [];
  const groups = { collaboration: ['/static/room.css', '/static/store.js', '/static/room.js'] };
  const window = {};
  const document = {
    getElementById: () => ({ textContent: JSON.stringify(groups) }),
    createElement: (tag) => ({
      tag,
      remove() {
        this.removed = true;
      },
    }),
    head: {
      append(node) {
        nodes.push(node);
      },
    },
  };
  vm.runInNewContext(source, {
    window,
    document,
    location: { href: 'https://fixture.invalid/' },
    URL,
  });
  return { load: window.CodePierAssets.load, nodes };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

test('routes load nothing on initialization and coalesce ordered dependencies', async () => {
  const { load, nodes } = fixture();
  assert.equal(nodes.length, 0);
  await load('overview');
  assert.equal(nodes.length, 0);
  const first = load('collaboration');
  const second = load('collaboration');
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0].href, '/static/room.css');
  nodes[0].onload();
  await tick();
  assert.equal(nodes.length, 2);
  assert.equal(nodes[1].src, '/static/store.js');
  nodes[1].onload();
  await tick();
  assert.equal(nodes.length, 3);
  nodes[2].onload();
  await Promise.all([first, second]);
  await load('collaboration');
  assert.equal(nodes.length, 3);
});

test('failed assets retry without re-executing successful classic scripts', async () => {
  const { load, nodes } = fixture();
  const first = load('collaboration');
  nodes[0].onload();
  await tick();
  nodes[1].onload();
  await tick();
  const rejected = assert.rejects(first, /页面资源未能加载/);
  nodes[2].onerror();
  await rejected;
  assert.equal(nodes[2].removed, true);
  const retry = load('collaboration');
  await tick();
  assert.equal(nodes.length, 4);
  assert.equal(nodes[3].src, '/static/room.js');
  nodes[3].onload();
  await retry;
});
