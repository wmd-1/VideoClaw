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
  return { ...incomingArtifact, items };
}
