export interface PromptRewriteQuality {
  expectedIds: string[];
  itemIds: string[];
  hasMeaningfulArtifact: boolean;
  hasMissingItems: boolean;
  hasIncompleteItems: boolean;
  hasFailedItems: boolean;
  missingIds: string[];
  incompleteIds: string[];
  failedIds: string[];
  storyboardReconfirm: boolean;
  canContinue: boolean;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? value as Record<string, unknown> : null;
}

export function expectedPromptRewriteIds(storyboard: unknown): string[] {
  const board = record(storyboard);
  const episodes = board?.episodes;
  if (!Array.isArray(episodes)) return [];
  const ids: string[] = [];
  const seen = new Set<string>();
  for (const episodeValue of episodes) {
    const episode = record(episodeValue);
    const segments = episode?.segments;
    if (!Array.isArray(segments)) continue;
    for (const segmentValue of segments) {
      const segment = record(segmentValue);
      const id = String(segment?.segment_id || '').trim();
      if (id && !seen.has(id)) {
        seen.add(id);
        ids.push(id);
      }
    }
  }
  return ids;
}

/** 与后端 core.prompt_rewrite_quality.assess_prompt_rewrite 保持同一业务语义。 */
export function assessPromptRewrite(
  storyboard: unknown,
  rewrite: unknown,
  storyboardReconfirm = false,
): PromptRewriteQuality {
  const expectedIds = expectedPromptRewriteIds(storyboard);
  const artifact = record(rewrite);
  const rawItems = artifact?.items;
  const items = Array.isArray(rawItems)
    ? rawItems.map(record).filter((item): item is Record<string, unknown> => item !== null)
    : [];
  const byId = new Map<string, Record<string, unknown>>();
  for (const item of items) {
    const id = String(item.id || '').trim();
    if (id) byId.set(id, item);
  }
  const missingIds = expectedIds.filter(id => !byId.has(id));
  const incompleteIds = expectedIds.filter(id => {
    const item = byId.get(id);
    return item !== undefined && item.status !== 'done' && item.status !== 'failed';
  });
  const failedIds = expectedIds.filter(id => byId.get(id)?.status === 'failed');
  const hasMeaningfulArtifact = expectedIds.length > 0 && expectedIds.some(id => {
    const item = byId.get(id);
    return item !== undefined && (
      item.status === 'done' || item.status === 'failed'
      || String(item.rewritten_prompt || '').trim().length > 0
    );
  });
  return {
    expectedIds,
    itemIds: [...byId.keys()],
    hasMeaningfulArtifact,
    hasMissingItems: missingIds.length > 0,
    hasIncompleteItems: incompleteIds.length > 0,
    hasFailedItems: failedIds.length > 0,
    missingIds,
    incompleteIds,
    failedIds,
    storyboardReconfirm,
    canContinue: hasMeaningfulArtifact && missingIds.length === 0
      && incompleteIds.length === 0 && !storyboardReconfirm,
  };
}
