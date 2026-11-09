#!/usr/bin/env node
import { readFileSync, readdirSync } from 'node:fs';

const rules = [
  [/(不会|不能|不是|并非|不代表|不等于|不证明)[^。；]{0,30}[，；](也|且|并)?(不会|不能|不是|不)/u, '连续否定'],
  [/(证据|回执|原操作|验收|核对|核实|工具合约)/u, '内部术语'],
  [/\p{Script=Han}（[A-Za-z][A-Za-z ]*）/u, '英文括注'],
  [/请[^。]{0,30}，不要/u, '请……不要……'],
  [/[A-Z]{3,}( \/ [A-Z]{3,})+/u, '装饰英文'],
  [/(节点|家里电脑|映射)/u, '统一用词'],
];
const web = new URL('../web/', import.meta.url);
let total = 0;
for (const file of readdirSync(web).filter((name) => name.endsWith('.js')).sort()) {
  const lines = readFileSync(new URL(file, web), 'utf8').split('\n');
  for (const [index, line] of lines.entries()) {
    if (/^\s*(\/\/|\/\*|\*)/.test(line)) continue;
    for (const [pattern, label] of rules) {
      const match = line.match(pattern);
      if (!match) continue;
      total++;
      console.log(file + ':' + (index + 1) + '  [' + label + ']  ' + match[0].slice(0, 40));
    }
  }
}
console.log('\n共 ' + total + ' 处');
const max = Number(process.argv.find((arg) => arg.startsWith('--max='))?.slice(6) ?? Infinity);
process.exitCode = Number.isNaN(max) || total > max ? 1 : 0;
