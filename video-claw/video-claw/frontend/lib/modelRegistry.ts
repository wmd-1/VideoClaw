import type { ProviderGroup, VideoModelCapabilities } from '@/config/models';
import { fetchApiModels, fetchModelAvailability } from '@/lib/workflowApi';

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
 * 首页/运行时选择器兵底：当当前已保存值不在按类型/能力过滤后的列表时，
 * 仍作为置顶、可选的占位项保留，并附不可用原因，避免静默丢值。
 * 未命中任何过滤时，行为与传入 groups 完全一致（不放宽既有过滤）。
 */
export async function withSlotPlaceholder(
  groups: ProviderGroup[],
  value: string | null | undefined,
): Promise<ProviderGroup[]> {
  const id = String(value ?? '').trim();
  if (!id) return groups;
  const exists = groups.some(group => group.models.some(model => model.id === id));
  if (exists) return groups;

  let label = `${id}（不可用）`;
  try {
    const av = await fetchModelAvailability(id);
    if (!av.available) {
      if (av.code === 'not_registered') {
        label = `${id}（未注册）`;
      } else if (av.reason) {
        label = `${id}（不可用：${av.reason}）`;
      } else {
        label = `${id}（不可用）`;
      }
    } else {
      // 已注册且可用，但仍被本槽的能力/类型过滤排除
      label = `${id}（不适用于此模式）`;
    }
  } catch {
    // 接口异常下仍保留可见占位，不因此洗选项
    label = `${id}（当前不可用）`;
  }

  return [
    { provider: '__unavailable__', label: '不可用', models: [{ id, label }] },
    ...groups,
  ];
}

/**
 * 设置页非阻塞提示：给定「本槽位下首页/运行时会使用的过滤列表」与当前保存值，
 * 返回下游不可选的原因文本（无问题返回空字符串）。不阻止保存，仅黄字展示。
 */
export async function describeSlotIssue(
  slotList: ProviderGroup[],
  value: string | null | undefined,
): Promise<string> {
  const id = String(value ?? '').trim();
  if (!id) return '';
  if (slotList.some(g => g.models.some(m => m.id === id))) return '';
  try {
    const av = await fetchModelAvailability(id);
    if (!av.available) {
      if (av.code === 'not_registered') return `${id} 未注册，首页/运行时的本槽位将不出现此选项`;
      if (av.reason) return `${id} 当前不可用：${av.reason}`;
      return `${id} 当前不可用`;
    }
    return `${id} 已注册但能力/类型不匹配本槽位，首页/运行时的本槽位将不出现此选项`;
  } catch {
    return '';
  }
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