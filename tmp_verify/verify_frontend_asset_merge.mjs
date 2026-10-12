// 前端条目合并语义确定性验收：直接 import 出货用的 lib/assetMerge.ts（Node 类型剥离，无复制实现）
// 运行（前端测试按项目约定可在宿主机执行）：
//   node --experimental-strip-types tmp_verify/verify_frontend_asset_merge.mjs
import assert from 'node:assert/strict';
import {
  applyAssetUpdateToArtifact,
  preserveRewriteBodies,
} from '../video-claw/video-claw/frontend/lib/assetMerge.ts';

let n = 0;
const ok = (name, fn) => { fn(); console.log(`[PASS] ${++n}. ${name}`); };

const rewriteEvent = (id, over = {}) => ({
  type: 'items', id, status: 'done', error: null,
  name: `第1集-片段${id.slice(-1)}`, index: 1, episode: 1,
  original_prompt: '雨夜小巷', input_mode: 'I2VA', duration: 8,
  continuity: 'tabby cat', truncated: false,
  rewritten_prompt: 'integrated_multimodal_description: 8 seconds total. full body...',
  ...over,
});

// 1. 首跑：产物缺失时按事件建列表（含正文）
ok('首跑事件可建列表并带正文（prompt_rewrite）', () => {
  const next = applyAssetUpdateToArtifact(null, rewriteEvent('seg_01_01'), true);
  assert.equal(next.items.length, 1);
  assert.ok(String(next.items[0].rewritten_prompt).length > 0);
  assert.equal(next.items[0].input_mode, 'I2VA');
});

// 2. 其他阶段：产物缺失时一律不改（旧语义）
ok('allowAppend=false 时产物缺失保持原状（其他阶段行为不变）', () => {
  assert.equal(applyAssetUpdateToArtifact(null, { type: 'scenes', id: 's1', status: 'done' }, false), null);
  const art = { scenes: [{ id: 's1', status: 'running' }] };
  assert.equal(applyAssetUpdateToArtifact(art, { type: 'scenes', id: 's9', status: 'done' }, false), null);
  assert.equal(art.scenes.length, 1, '未知 id 不得追加到其他阶段列表');
});

// 3. 重复投递幂等
ok('同一事件重复投递：条目不重复、versions 不膨胀', () => {
  let art = applyAssetUpdateToArtifact(null, rewriteEvent('seg_01_01'), true);
  art = applyAssetUpdateToArtifact(art, rewriteEvent('seg_01_01'), true);
  assert.equal(art.items.length, 1);
  assert.equal(art.items[0].versions.length, 0, '事件不带 versions 时不得凭空生成版本记录');
});

// 4. 核心：轮询回来的空壳不得冲掉已收到的正文（状态以后端为准）
ok('shell 整体覆盖时保留正文、采用后端状态', () => {
  const local = { items: [{ id: 'seg_01_01', status: 'done', rewritten_prompt: 'BODY-A', input_mode: 'I2VA',
                             name: '第1集-片段1', versions: ['v1'], selected: '' }] };
  const incoming = { items: [{ id: 'seg_01_01', status: 'failed', error: '后端失败原因',
                               rewritten_prompt: '', input_mode: '', name: '', versions: [] }] };
  const merged = preserveRewriteBodies(local, incoming);
  assert.equal(merged.items[0].rewritten_prompt, 'BODY-A', '正文不得被空壳冲掉');
  assert.equal(merged.items[0].input_mode, 'I2VA');
  assert.equal(merged.items[0].name, '第1集-片段1');
  assert.equal(merged.items[0].status, 'failed', '状态以后端为权威');
  assert.equal(merged.items[0].error, '后端失败原因');
});

// 5. 最终产物到达：完整正文覆盖截断预览，且截断标记必须消失（真机发现的缺陷回归）
ok('完整正文覆盖截断预览；truncated 标记随之清除（后端不携带该键）', () => {
  const local = { items: [{ id: 'seg_01_03', status: 'done', rewritten_prompt: 'PREFIX…(截断)', truncated: true }] };
  const incoming = { items: [{ id: 'seg_01_03', status: 'done', rewritten_prompt: 'PREFIX… 完整正文全文' }] };
  const merged = preserveRewriteBodies(local, incoming);
  assert.equal(merged.items[0].rewritten_prompt, 'PREFIX… 完整正文全文');
  assert.equal(merged.items[0].truncated, false, '完成态不得残留截断标记');
});

// 5b. 正文仍缺失（轮询拿到空壳）时，预览期的截断标记不得丢失
ok('正文缺失时保留预览期的 truncated 标记（不误洗为空壳无提示）', () => {
  const local = { items: [{ id: 'seg_01_03', status: 'done', rewritten_prompt: 'PREVIEW-8000', truncated: true }] };
  const incoming = { items: [{ id: 'seg_01_03', status: 'done', rewritten_prompt: '', truncated: false }] };
  const merged = preserveRewriteBodies(local, incoming);
  assert.equal(merged.items[0].rewritten_prompt, 'PREVIEW-8000', '正文不被空壳冲掉');
  assert.equal(merged.items[0].truncated, true, '无完整正文时保留预览标记');
});

// 6. versions 并集去重 + 用户选择保留（既有归一语义）
ok('versions 并集去重；有 selected 的条目失败仍归一为 done（U3 既有语义，本 change 不改）', () => {
  const art = { items: [{ id: 'seg_01_01', status: 'done', selected: '/x/v2.png', versions: ['a', 'b'] }] };
  const next = applyAssetUpdateToArtifact(art, { type: 'items', id: 'seg_01_01', status: 'failed',
                                                 versions: ['b', 'c'] }, true);
  assert.deepEqual(next.items[0].versions, ['a', 'b', 'c']);
  assert.equal(next.items[0].status, 'done', '有 selected 时 failed 归一为 done（既有行为）');
  assert.equal(next.items[0].selected, '/x/v2.png');
});

// 7. 其他阶段列表键不受污染（不新增改写相关键、不改结构）
//    注：rewrite_result 键是抽取前就存在的既有行为（mergeAssetUpdateItem 无条件写入），本次未改
ok('其他阶段事件（无新键）合并后不会多出改写相关键', () => {
  const art = { clips: [{ id: 'c1', status: 'running', versions: [], selected: '' }] };
  const next = applyAssetUpdateToArtifact(art, { type: 'clips', id: 'c1', status: 'done', selected: 'v' }, false);
  const keys = Object.keys(next.clips[0]).sort();
  assert.deepEqual(keys, ['id', 'rewrite_result', 'selected', 'status', 'versions'].sort());
  for (const banned of ['rewritten_prompt', 'input_mode', 'truncated', 'continuity', 'grounding', 'episode']) {
    assert.ok(!keys.includes(banned), `其他阶段条目不应多出 ${banned} 键`);
  }
  assert.equal(next.clips[0].status, 'done');
});

console.log(`\n全部 ${n} 项前端合并语义断言通过`);
