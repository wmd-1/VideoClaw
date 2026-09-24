import type { ProviderGroup, VideoModelCapabilities } from '@/config/models';
import { fetchApiModels } from '@/lib/workflowApi';

const PROVIDER_LABELS: Record<string, string> = {
  dashscope: 'DashScope',
  ark: 'ARK (Volcengine)',
  deepseek: 'DeepSeek',
  openai: 'OpenAI',
  gemini: 'Gemini',
  kling: 'Kling',
};

export function groupModelOptions(
  models: Array<{ id: string; label?: string; provider?: string; provider_label?: string }>,
): ProviderGroup[] {
  const groups = new Map<string, ProviderGroup>();
  for (const model of models) {
    const provider = model.provider || 'unknown';
    if (!groups.has(provider)) {
      groups.set(provider, {
        provider,
        // 自定义供应商使用后端下发的 provider_label（显示名或键），内置供应商沿用静态标签映射
        label: model.provider_label || PROVIDER_LABELS[provider] || provider,
        models: [],
      });
    }
    groups.get(provider)!.models.push({
      id: model.id,
      label: model.label || model.id,
    });
  }
  return Array.from(groups.values());
}

export async function fetchModelGroupsByType(
  modelType: 'llm' | 'vlm' | 't2i' | 'i2i' | 'video',
): Promise<ProviderGroup[]> {
  const models = await fetchApiModels({ modelType });
  return groupModelOptions(models);
}

export async function fetchVideoModelGroupsByAbility(ability: string): Promise<ProviderGroup[]> {
  const models = await fetchApiModels({ mediaType: 'video', ability, verifiedOnly: true });
  return groupModelOptions(models);
}

/* 视频模型能力短缓存：切换模型联动时长/FPS/分辨率选项时避免频繁请求 */
let videoCapsCache: { at: number; map: Map<string, VideoModelCapabilities> } | null = null;
const VIDEO_CAPS_CACHE_TTL_MS = 10_000;

export async function fetchVideoModelCapabilities(modelId: string): Promise<VideoModelCapabilities | null> {
  const now = Date.now();
  if (!videoCapsCache || now - videoCapsCache.at > VIDEO_CAPS_CACHE_TTL_MS) {
    const models = await fetchApiModels({ modelType: 'video' });
    const map = new Map<string, VideoModelCapabilities>();
    for (const model of models) {
      const caps = (model as any).capabilities;
      if (caps && typeof caps === 'object') map.set(model.id, caps as VideoModelCapabilities);
    }
    videoCapsCache = { at: now, map };
  }
  return videoCapsCache.map.get(modelId) ?? null;
}

/**
 * 合并多个分组列表：按 provider 归并 models 并按 id 去重（保持首次出现顺序）。
 * 用于设置页 Default Models —— 各下拉共享同一份「全部可用模型」，可交叉选择。
 */
export function mergeProviderGroups(groupLists: ProviderGroup[][]): ProviderGroup[] {
  const merged = new Map<string, ProviderGroup>();
  for (const groups of groupLists) {
    for (const group of groups) {
      const existing = merged.get(group.provider);
      if (!existing) {
        merged.set(group.provider, { provider: group.provider, label: group.label, models: [...group.models] });
        continue;
      }
      const seen = new Set(existing.models.map(model => model.id));
      for (const model of group.models) {
        if (!seen.has(model.id)) {
          seen.add(model.id);
          existing.models.push(model);
        }
      }
    }
  }
  return Array.from(merged.values());
}