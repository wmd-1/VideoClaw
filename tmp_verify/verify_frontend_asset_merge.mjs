// 前端条目合并语义确定性验收：直接 import 出货用的 lib/assetMerge.ts（Node 类型剥离，无复制实现）
// 运行（前端测试按项目约定可在宿主机执行）：
//   node --experimental-strip-types tmp_verify/verify_frontend_asset_merge.mjs
import assert from 'node:assert/strict';
import {
  applyAssetUpdateToArtifact,
  preserveRewriteBodies,
  pruneDraftsByContent,
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

// 9. 草稿按内容裁剪（I-2/2.1）：仅清除正文变化条目的草稿，内容未变条目保留
ok('正文变化条目清草稿；内容未变条目草稿保留（不依赖数组引用）', () => {
  const prevItems = [
    { id: 'A', rewritten_prompt: 'old-A' },
    { id: 'B', rewritten_prompt: 'body-B' },
  ];
  const nextItems = [
    { id: 'A', rewritten_prompt: 'new-A' },   // 重生成：正文变化
    { id: 'B', rewritten_prompt: 'body-B' },   // 未变
  ];
  const drafts = { A: 'draft-A', B: 'draft-B' };
  const out = pruneDraftsByContent(drafts, prevItems, nextItems);
  assert.ok(!('A' in out), '正文变化的条目草稿应被清除');
  assert.equal(out.B, 'draft-B', '内容未变条目的草稿应原样保留');
});

// 10. truncated/status 等事件视图字段变化不触发清草稿（D1/2.2）
ok('仅 truncated/status 变化、正文不变时草稿全部保留', () => {
  const prevItems = [{ id: 'A', rewritten_prompt: 'BODY', truncated: true, status: 'done' }];
  const nextItems = [{ id: 'A', rewritten_prompt: 'BODY', truncated: false, status: 'running' }];
  const drafts = { A: 'draft-A' };
  const out = pruneDraftsByContent(drafts, prevItems, nextItems);
  assert.equal(out.A, 'draft-A', '事件视图字段变化不得误清草稿');
  assert.equal(out, drafts, '无草稿变化时应返回入参引用（避免多余渲染）');
});

// 11. 条目移除视为变化 → 清该条草稿（前提：后端无删除路径，全量重生成才会出现）
ok('条目从列表移除时其草稿被清除', () => {
  const prevItems = [{ id: 'A', rewritten_prompt: 'a' }, { id: 'B', rewritten_prompt: 'b' }];
  const nextItems = [{ id: 'A', rewritten_prompt: 'a' }];
  const out = pruneDraftsByContent({ A: 'dA', B: 'dB' }, prevItems, nextItems);
  assert.equal(out.A, 'dA', '未变条目保留');
  assert.ok(!('B' in out), '移除条目草稿被清');
});

// 12. 合并按 id 补回 prev-only 条目（M-4/3.1）：轮询快照缺条目时界面不闪没
ok('prev 有而 next 缺的条目按 id 补回尾部，正文不丢（无空壳）', () => {
  const prev = { items: [
    { id: 'seg_01_01', status: 'done', rewritten_prompt: 'BODY-1' },
    { id: 'seg_01_02', status: 'done', rewritten_prompt: 'BODY-2' },
  ] };
  // 轮询快照晚于 SSE 到达，不含刚追加的 seg_01_02
  const incoming = { items: [
    { id: 'seg_01_01', status: 'done', rewritten_prompt: 'BODY-1' },
  ] };
  const merged = preserveRewriteBodies(prev, incoming);
  const ids = merged.items.map(i => i.id);
  assert.ok(ids.includes('seg_01_02'), '补回 prev-only 条目，不出现闪没');
  const seg2 = merged.items.find(i => i.id === 'seg_01_02');
  assert.equal(seg2.rewritten_prompt, 'BODY-2', '补回条目沿用本地正文，不产生空壳');
});

// 13. 补回不重复、不打乱 next 自身顺序（已有 id 的条目以 next 为准只出现一次）
ok('补回只追加 prev-only 条目，不与 next 重复且保持 next 顺序在前', () => {
  const prev = { items: [
    { id: 'X', status: 'done', rewritten_prompt: 'old-X' },
    { id: 'Y', status: 'done', rewritten_prompt: 'Y' },
  ] };
  const incoming = { items: [
    { id: 'X', status: 'done', rewritten_prompt: 'new-X' },
  ] };
  const merged = preserveRewriteBodies(prev, incoming);
  assert.deepEqual(merged.items.map(i => i.id), ['X', 'Y'], 'next 在前方、prev-only 在尾部，X 不重复');
  assert.equal(merged.items[0].rewritten_prompt, 'new-X', 'X 以后端正文为准');
});

// 14. running 事件不冲掉已有正文与 versions（prompt-rewrite-item-running-events 2.2）
ok('running 事件置为改写中但不冲掉旧正文与 versions', () => {
  const art = { items: [{ id: 'seg_01_01', status: 'done', rewritten_prompt: 'BODY-A', versions: ['v1'], selected: '' }] };
  const running = { type: 'items', id: 'seg_01_01', status: 'running', name: '第1集-片段1', input_mode: 'I2VA' };
  const next = applyAssetUpdateToArtifact(art, running, true);
  assert.equal(next.items[0].status, 'running');
  assert.equal(next.items[0].rewritten_prompt, 'BODY-A', 'running 无正文不得冲掉旧正文');
  assert.deepEqual(next.items[0].versions, ['v1'], 'running 不追加/清空 versions');
});

// 15. running 事件对未预存条目按 id 追加为改写中、不含正文（首跑 2.1）
ok('running 事件新建条目为改写中、不含正文、带渲染字段', () => {
  const next = applyAssetUpdateToArtifact(
    null,
    { type: 'items', id: 'seg_01_02', status: 'running', name: '片段2', original_prompt: '雨夜' },
    true,
  );
  assert.equal(next.items.length, 1);
  assert.equal(next.items[0].status, 'running');
  assert.ok(!next.items[0].rewritten_prompt, 'running 事件不含正文');
  assert.equal(next.items[0].name, '片段2');
});

// 16. syncState 与产物合并层正交（workflow-poll-timeout-visibility 5.1）：
// syncState 是 StageState 层的本地同步态；合并层从干净输入 MUST NOT 凭空引入 syncState（否则会被当作阶段状态消费）。
ok('合并层不凭空引入 syncState（与阶段状态正交）', () => {
  const prev = { items: [{ id: 'seg_01_01', status: 'done', rewritten_prompt: 'BODY-1' }] };
  const incoming = { items: [{ id: 'seg_01_01', status: 'done', rewritten_prompt: 'BODY-1' }] };
  const merged = preserveRewriteBodies(prev, incoming);
  assert.ok(!('syncState' in merged), 'artifact 顶层不得多出 syncState');
  assert.ok(merged.items.every(it => !('syncState' in it)), '条目不得多出 syncState');
  const next = applyAssetUpdateToArtifact(
    { items: [{ id: 'x', status: 'done', rewritten_prompt: 'B' }] },
    { type: 'items', id: 'x', status: 'running' },
    true,
  );
  assert.ok(!('syncState' in next), '事件合并输出不得多出 syncState');
  assert.ok(!('syncState' in next.items[0]), '条目不得多出 syncState');
  assert.equal(next.items[0].status, 'running', 'status 仍由事件驱动，与 syncState 无关');
});

console.log(`\n全部 ${n} 项前端合并语义断言通过`);
