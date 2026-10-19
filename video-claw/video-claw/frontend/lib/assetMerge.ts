/**
 * 条目完成事件（asset_complete）的产物合并逻辑。
 *
 * 从 WorkflowPanel 原样抽出为单一实现，便于容器外/前端侧确定性验证；语义与抽出前一致：
 * - 其他阶段的条目在执行前已预存在 → 永远走"命中 id 则更新"分支（applyAppend=false）；
 * - 提示词改写阶段无预创建步骤 → 允许缺失即追加（applyAppend=true）；
 * - 改写正文只随增量事件传输、不落入服务端持久化 shell（U1=B），
 *   因此任何整体覆盖都不能把本地已收到的正文冲掉（preserveRewriteBodies）。
 */

/** 改写阶段增量事件携带的正文类字段（后端 shell 不存这些字段，仅事件传递） */
export const REWRITE_BODY_KEYS = [
  'rewritten_prompt', 'name', 'index', 'episode', 'original_prompt',
  'input_mode', 'duration', 'continuity', 'grounding', 'truncated',
] as const;

export type ArtifactLike = Record<string, unknown> | null | undefined;

export function isFillable(value: unknown): boolean {
  return value !== undefined && value !== null && value !== '';
}

export function mergeAssetVersions(current?: string[], incoming?: string[]): string[] {
  const merged: string[] = [];
  [...(Array.isArray(current) ? current : []), ...(Array.isArray(incoming) ? incoming : [])].forEach(path => {
    if (path && !merged.includes(path)) merged.push(path);
  });
  return merged;
}

export function mergeAssetUpdateItem(item: Record<string, unknown>, assetUpdate: Record<string, unknown>): Record<string, unknown> {
  const hasExistingSelection = Boolean(item?.selected);
  const updateStatus = assetUpdate.status as string | undefined;
  const nextStatus = hasExistingSelection && ['done', 'failed'].includes(updateStatus ?? '')
    ? 'done'
    : (updateStatus ?? item.status);
  const merged: Record<string, unknown> = {
    ...item,
    status: nextStatus,
    selected: hasExistingSelection
      ? item.selected
      : ((assetUpdate.selected as string) || (item.selected as string)),
    versions: assetUpdate.versions
      ? mergeAssetVersions(item.versions as string[], assetUpdate.versions as string[])
      : item.versions,
    rewrite_result: assetUpdate.rewrite_result ?? item.rewrite_result,
  };
  // 改写阶段的增量正文：事件不带这些字段时一律保持原值，其他阶段不会因此多出键
  for (const key of REWRITE_BODY_KEYS) {
    if (isFillable(assetUpdate[key])) merged[key] = assetUpdate[key];
  }
  if (assetUpdate.error !== undefined) merged.error = assetUpdate.error;
  // 失败类别与 error 同位合并：否则事件里带 error_type 而客户端产物丢了它，
  // 执行期看得到原因、刷新后反而消失（openspec design D6）
  if (assetUpdate.error_type !== undefined) merged.error_type = assetUpdate.error_type;
  return merged;
}

/** 事件里已带正文时，用事件本身构造一个可渲染条目（执行期首次出现时用）。 */
export function rewriteItemFromUpdate(assetUpdate: Record<string, unknown>): Record<string, unknown> {
  const item: Record<string, unknown> = {
    id: assetUpdate.id,
    status: assetUpdate.status ?? 'done',
    versions: [],
  };
  for (const key of REWRITE_BODY_KEYS) {
    if (isFillable(assetUpdate[key])) item[key] = assetUpdate[key];
  }
  if (assetUpdate.error !== undefined) item.error = assetUpdate.error;
  if (assetUpdate.error_type !== undefined) item.error_type = assetUpdate.error_type;
  return item;
}

/**
 * 统一的条目完成事件合并。allowAppend 仅对 prompt_rewrite 开启（串行首跑时列表为空）；
 * 其余阶段保持旧语义：产物缺失或 id 不存在时一律不改（返回 null 表示保持原状）。
 */
export function applyAssetUpdateToArtifact(
  prevArt: ArtifactLike,
  assetUpdate: Record<string, unknown>,
  allowAppend: boolean,
): Record<string, unknown> | null {
  const key = assetUpdate.type as string;
  if (!key) return null;
  const itemFromEvent = () => rewriteItemFromUpdate(assetUpdate);
  if (!prevArt) {
    return allowAppend && isFillable(assetUpdate.id) ? { [key]: [itemFromEvent()] } : null;
  }
  const prevList = prevArt[key];
  const list: unknown[] = Array.isArray(prevList) ? [...prevList] : [];
  const idx = list.findIndex(
    (item: unknown) => (item as Record<string, unknown> | null)?.id === assetUpdate.id,
  );
  if (idx >= 0) {
    list[idx] = mergeAssetUpdateItem(list[idx] as Record<string, unknown>, assetUpdate);
  } else if (allowAppend && isFillable(assetUpdate.id)) {
    list.push(itemFromEvent());
  } else {
    return null;
  }
  return { ...prevArt, [key]: list };
}

/**
 * 改写阶段执行期正文保留：正文只经增量事件到达、不落入服务端 shell，
 * 因此任何一次产物整体覆盖（2s 轮询回填、状态快照）都不能把已收到的正文冲掉。
 * 状态类字段（status/selected/versions/error）以后端为准；仅按条目 id 保留本地已有的内容字段。
 *
 * `truncated` 例外：它只属于“事件视图”，服务端最终产物不携带该键。若沿用“缺失即保留”，
 * 完成后“已超出事件传输上限被截断”的提示会永远残留（已实测到）。因此：只要该条目已拿到
 * 非空正文，截断标记就完全以后端值为准（缺省视为 false）；正文仍缺失时才保留预览期的标记。
 */
export function preserveRewriteBodies(prevArtifact: ArtifactLike, incomingArtifact: ArtifactLike): ArtifactLike {
  const prevVal = prevArtifact?.items;
  const nextVal = incomingArtifact?.items;
  if (!incomingArtifact || !Array.isArray(prevVal) || !Array.isArray(nextVal)) {
    return incomingArtifact ?? null;
  }
  const prevById = new Map<string, Record<string, unknown>>(
    prevVal.map((item: Record<string, unknown>) => [item.id as string, item]),
  );
  const items = nextVal.map((rawItem: Record<string, unknown>) => {
    const item = { ...rawItem };
    const previous = prevById.get(item.id as string);
    if (!previous) return item;
    // 先看后端本条是否真的交付了正文（必须在回填本地值之前判定）
    const delivered = isFillable(rawItem.rewritten_prompt);
    for (const key of REWRITE_BODY_KEYS) {
      if (key === 'truncated') continue;
      if (!isFillable(item[key]) && isFillable(previous[key])) {
        item[key] = previous[key];
      }
    }
    if (delivered) {
      item.truncated = rawItem.truncated === true;
    } else if (previous.truncated !== undefined) {
      item.truncated = previous.truncated;
    }
    return item;
  });
  // M-4：轮询快照可能在服务端 merge 前发出、晚于 SSE 事件到达，导致这份快照不含 SSE 刚追加的条目。
  // 把 prev 有而 next 缺失的条目按 id 补回尾部，消除“条目闪没”；2s 后下一轮轮询会带上真实快照自然对齐。
  // 补回项本就是本地已合并过正文的条目，原样带上即可（沿用“缺失才保留本地值”，不产生空壳）。
  // 前提：后端无合法条目删除路径（分镜集合在阶段内固定），否则补回会让删除看似不生效。
  const nextIds = new Set<string>(
    nextVal.map((rawItem: Record<string, unknown>) => rawItem.id as string),
  );
  const prevOnly = prevVal.filter(
    (item: Record<string, unknown>) => !nextIds.has(item.id as string),
  );
  const merged = [
    ...items,
    ...prevOnly.map((item: Record<string, unknown>) => ({ ...item })),
  ];
  return { ...incomingArtifact, items: merged };
}

/**
 * 草稿清理（I-2）：产物更新时只清除“正文实际变化”条目的草稿，内容未变条目的草稿原样保留。
 * 判定以条目内容（id + rewritten_prompt）为依据， MUST NOT 依赖产物/数组引用身份；
 * `truncated`/`status` 等事件视图字段变化不影响（签名只取 rewritten_prompt）。
 * 条目移除视为变化（清掉该条草稿）。无需改动时返回入参引用，便于 React 短路多余渲染。
 * 抽为纯函数以便容器外确定性断言（与组件内同一实现，不复制）。
 */
export function pruneDraftsByContent(
  prevDrafts: Record<string, string>,
  prevItems: Array<{ id: string; rewritten_prompt?: unknown }>,
  nextItems: Array<{ id: string; rewritten_prompt?: unknown }>,
): Record<string, string> {
  const prevBody = new Map<string, string>(prevItems.map(i => [i.id, String(i.rewritten_prompt ?? '')]));
  const nextBody = new Map<string, string>(nextItems.map(i => [i.id, String(i.rewritten_prompt ?? '')]));
  const next: Record<string, string> = {};
  let touched = false;
  for (const [id, draft] of Object.entries(prevDrafts)) {
    // 条目仍存在且正文与上一帧一致 → 保留草稿；正文变化或条目移除 → 清除
    if (nextBody.has(id) && prevBody.get(id) === nextBody.get(id)) {
      next[id] = draft;
    } else {
      touched = true;
    }
  }
  return touched ? next : prevDrafts;
}
