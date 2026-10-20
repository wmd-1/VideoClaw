// tasks 分隔符前端侧用例：读取与 Python 侧同一份夹具（openspec: M-1）。
// 运行：node --experimental-strip-types tmp_verify/verify_task_separators.mjs
import fs from 'node:fs';
import {
  invalidTaskTokens,
  splitTaskTokens,
} from '../video-claw/video-claw/frontend/lib/capabilityParams.ts';

const fixturePath = new URL('./fixtures/tasks_separators.json', import.meta.url);
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const failures = [];

function check(name, cond, extra = '') {
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${extra ? ` | ${extra}` : ''}`);
  if (!cond) failures.push(name);
}

for (const testCase of fixture.cases) {
  const tokens = splitTaskTokens(testCase.input);
  check(`splitTaskTokens ${testCase.name}`, JSON.stringify(tokens) === JSON.stringify(testCase.expected), JSON.stringify(tokens));
  const invalid = invalidTaskTokens(testCase.input);
  check(`invalidTaskTokens ${testCase.name}`, JSON.stringify(invalid) === JSON.stringify(testCase.invalid), JSON.stringify(invalid));
}

console.log();
if (failures.length) {
  console.log(`FAILED: ${failures.length} -> ${failures.join(', ')}`);
  process.exit(1);
}
console.log('ALL PASS');
