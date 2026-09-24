'use client';

import { useEffect, useState } from 'react';
import { CheckCircle, Loader2, Save, Settings, XCircle } from 'lucide-react';
import BrandHeader from '@/components/BrandHeader';
import { fetchModelGroupsByType, mergeProviderGroups } from '@/lib/modelRegistry';
import {
  CustomModelsSection,
  CustomProvidersSection,
  type EnvOverrides,
} from '@/components/settings/CustomModelsSection';
import {
  VIDEO_RATIOS,
  VIDEO_RESOLUTIONS,
  VIDEO_GENERATION_MODES,
  STYLES,
  type ProviderGroup,
} from '@/config/models';

type ConfigTree = Record<string, any>;

type Field = {
  path: string;
  label: string;
  type?: 'text' | 'number' | 'boolean' | 'password' | 'select';
  options?: Array<{ id: string; label: string }> | ProviderGroup[];
};

type ModelSelectKey = 'llm' | 'vlm' | 'image_it2i' | 'image_t2i' | 'video_first_frame' | 'video_start_end' | 'video_reference';

const EMPTY_MODEL_SELECTS: Record<ModelSelectKey, ProviderGroup[]> = {
  llm: [],
  vlm: [],
  image_it2i: [],
  image_t2i: [],
  video_first_frame: [],
  video_start_end: [],
  video_reference: [],
};

const EMPTY_ENV_OVERRIDES: EnvOverrides = { fields: [], providers: [], models: [] };

// 说明：API Server / Common 与内置供应商（OpenAI/Gemini/DashScope/ARK/Kling 等）
// 的密钥配置已不再在设置页展示，统一通过 .env 或 config.yaml 配置（见项目 .env.example）。
// 模型接入统一通过下方「自定义供应商 / 自定义模型」管理区完成（如 local_llm、local_vlm、
// local_image_t2i、local_image_it2i 等）。
const GROUPS: Array<{ title: string; description: string; fields: Field[] }> = [
  {
    title: 'Default Models',
    description: '主流程和 Pipeline 使用的默认模型；每个下拉列出的都是全部可用模型（不做类型限制，可交叉选择，如 vlm 模型放入 llm 槽位）。',
    fields: [
      { path: 'models.llm', label: 'llm 文本模型', type: 'select', options: [] },
      { path: 'models.vlm', label: 'vlm 视觉语言模型', type: 'select', options: [] },
      { path: 'models.image_it2i', label: 'image_it2i 图生图模型', type: 'select', options: [] },
      { path: 'models.image_t2i', label: 'image_t2i 文生图模型', type: 'select', options: [] },
      { path: 'models.video_first_frame', label: 'video_first_frame 首帧生视频模型', type: 'select', options: [] },
      { path: 'models.video_start_end', label: 'video_start_end 首尾帧生视频模型', type: 'select', options: [] },
      { path: 'models.video_reference', label: 'video_reference 参考图生视频模型', type: 'select', options: [] },
    ],
  },
  {
    title: '视频生成配置',
    description: '只对主流程生效：选择视频生成方式、风格、画幅比例和视频分辨率。',
    fields: [
      { path: 'generation.video_generation_mode', label: 'video_generation_mode 视频生成方式', type: 'select', options: VIDEO_GENERATION_MODES },
      { path: 'generation.style', label: 'style 风格', type: 'select', options: STYLES },
      { path: 'generation.video_ratio', label: 'video_ratio 视频长宽比', type: 'select', options: VIDEO_RATIOS },
      { path: 'generation.video_resolution', label: 'video_resolution 视频分辨率', type: 'select', options: VIDEO_RESOLUTIONS },
      { path: 'generation.video_duration', label: 'video_duration 默认时长/秒（留空跟随分镜，实际按模型能力夹取）', type: 'text' },
      { path: 'generation.video_fps', label: 'video_fps 帧率（留空使用服务端默认，需模型声明 fps 能力）', type: 'text' },
    ],
  },
];

function getValue(config: ConfigTree, path: string) {
  return path.split('.').reduce((current, key) => current?.[key], config);
}

function setValue(config: ConfigTree, path: string, value: any): ConfigTree {
  const next = structuredClone(config || {});
  const parts = path.split('.');
  let current = next;
  for (const part of parts.slice(0, -1)) {
    current[part] = current[part] || {};
    current = current[part];
  }
  current[parts[parts.length - 1]] = value;
  return next;
}

function formatConfigPath(path: string) {
  if (!path) return 'backend/config.yaml';
  const normalized = path.replace(/\\/g, '/');
  const marker = '/video-claw/video-claw/';
  const markerIndex = normalized.lastIndexOf(marker);
  if (markerIndex >= 0) return normalized.slice(markerIndex + marker.length);
  const backendIndex = normalized.lastIndexOf('/backend/config.yaml');
  if (backendIndex >= 0) return normalized.slice(backendIndex + 1);
  return normalized;
}

function maskSecret(value: unknown) {
  const text = String(value ?? '');
  if (!text) return '';
  if (text.length <= 10) return '*'.repeat(text.length);
  return `${text.slice(0, 5)}${'*'.repeat(Math.min(12, text.length - 10))}${text.slice(-5)}`;
}

function isProviderOptions(options: Field['options']): options is ProviderGroup[] {
  return Array.isArray(options) && options.some(option => 'models' in option);
}

/** 当前值不在可选列表时（如条目被改名/删除）注入「未注册」占位，避免下拉静默空白。 */
function withMissingPlaceholder(options: ProviderGroup[], value: unknown): ProviderGroup[] {
  const id = String(value ?? '');
  if (!id) return options;
  const exists = options.some(group => group.models.some(model => model.id === id));
  if (exists) return options;
  return [
    { provider: '__missing__', label: '未注册', models: [{ id, label: `${id}（未注册）` }] },
    ...options,
  ];
}

export default function SettingsPage() {
  const [config, setConfig] = useState<ConfigTree>({});
  const [path, setPath] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [secretDrafts, setSecretDrafts] = useState<Record<string, string>>({});
  const [modelSelects, setModelSelects] = useState<Record<ModelSelectKey, ProviderGroup[]>>(EMPTY_MODEL_SELECTS);
  const [envOverrides, setEnvOverrides] = useState<EnvOverrides>(EMPTY_ENV_OVERRIDES);
  // 已保存配置基线：用于提示“当前编辑值未保存”（工作流实际使用已保存值）
  const [savedConfig, setSavedConfig] = useState<ConfigTree>({});

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      setError('');
      try {
        const resp = await fetch('/api/config');
        if (!resp.ok) throw new Error('读取配置失败');
        const data = await resp.json();
        setConfig(data.config || {});
        setPath(data.path || '');
        setSecretDrafts({});
        setEnvOverrides(data.env_overrides || EMPTY_ENV_OVERRIDES);
        setSavedConfig(data.config || {});
      } catch (e: any) {
        setError(e.message || '读取配置失败');
      } finally {
        setLoading(false);
      }
    };
    load();
  }, []);

  // Default Models 各下拉共享同一份「全部可用模型」：不做类型/能力过滤，
  // 任何模型（含 vlm/图像/视频）都可以被任一下拉选中（保持可交叉选择的逻辑）。
  const refreshModelSelects = () => {
    Promise.all([
      fetchModelGroupsByType('llm'),
      fetchModelGroupsByType('vlm'),
      fetchModelGroupsByType('i2i'),
      fetchModelGroupsByType('t2i'),
      fetchModelGroupsByType('video'),
    ])
      .then(groupsList => {
        const merged = mergeProviderGroups(groupsList);
        setModelSelects({
          llm: merged,
          vlm: merged,
          image_it2i: merged,
          image_t2i: merged,
          video_first_frame: merged,
          video_start_end: merged,
          video_reference: merged,
        });
      })
      .catch(() => {});
  };

  useEffect(() => {
    refreshModelSelects();
  }, []);

  const groups = GROUPS.map(group => {
    if (group.title !== 'Default Models') return group;
    return {
      ...group,
      fields: group.fields.map(field => {
        // 当前值未注册（条目被改名/删除）时注入「未注册」占位，避免下拉静默空白
        const currentValue = getValue(config, field.path);
        if (field.path === 'models.llm') return { ...field, options: withMissingPlaceholder(modelSelects.llm, currentValue) };
        if (field.path === 'models.vlm') return { ...field, options: withMissingPlaceholder(modelSelects.vlm, currentValue) };
        if (field.path === 'models.image_it2i') return { ...field, options: withMissingPlaceholder(modelSelects.image_it2i, currentValue) };
        if (field.path === 'models.image_t2i') return { ...field, options: withMissingPlaceholder(modelSelects.image_t2i, currentValue) };
        if (field.path === 'models.video_first_frame') return { ...field, options: withMissingPlaceholder(modelSelects.video_first_frame, currentValue) };
        if (field.path === 'models.video_start_end') return { ...field, options: withMissingPlaceholder(modelSelects.video_start_end, currentValue) };
        if (field.path === 'models.video_reference') return { ...field, options: withMissingPlaceholder(modelSelects.video_reference, currentValue) };
        return field;
      }),
    };
  });

  const updateField = (field: Field, raw: string | boolean) => {
    const value = field.type === 'number' ? Number(raw) || 0 : raw;
    setConfig(current => setValue(current, field.path, value));
  };

  const save = async () => {
    // 保存前预检：自定义模型必须已选类型（types），避免提交后被后端校验拒绝（400）
    const invalidModels = (Array.isArray(config.custom_models) ? config.custom_models : [])
      .map((item: any, index: number) => ({ id: String(item?.id || `第 ${index + 1} 条`), types: item?.types }))
      .filter(item => !Array.isArray(item.types) || item.types.length === 0);
    if (invalidModels.length > 0) {
      setSaving(false);
      setMessage('');
      setError(`请先为这些自定义模型选择类型（types）：${invalidModels.map(item => item.id).join('、')}`);
      return;
    }
    setSaving(true);
    setMessage('');
    setError('');
    try {
      const resp = await fetch('/api/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values: config }),
      });
      if (!resp.ok) {
        // 透出后端硬校验的具体原因（400 detail，如 protocol/base_url/id 冲突等）
        let reason = '';
        try {
          const body = await resp.json();
          reason = typeof body?.detail === 'string' ? body.detail : '';
        } catch {
          /* 非 JSON 响应 */
        }
        throw new Error(reason || '保存配置失败');
      }
      const data = await resp.json();
      setConfig(data.config || {});
      setPath(data.path || '');
      setSecretDrafts({});
      setEnvOverrides(data.env_overrides || EMPTY_ENV_OVERRIDES);
      setSavedConfig(data.config || {});
      refreshModelSelects();
      setMessage('配置已保存');
    } catch (e: any) {
      setError(e.message || '保存配置失败');
    } finally {
      setSaving(false);
    }
  };

  const updateSecretField = (field: Field, raw: string) => {
    setSecretDrafts(current => ({ ...current, [field.path]: raw }));
    setConfig(current => setValue(current, field.path, raw));
  };

  return (
    <div className="min-h-screen bg-gray-50/50">
      <BrandHeader />
      <main className="w-full max-w-6xl mx-auto px-6 pt-10 pb-12">
        <div className="mb-8 text-center">
          <div className="inline-flex items-center gap-2 mb-3">
            <Settings className="w-7 h-7 text-blue-500" />
            <h1 className="text-2xl font-bold text-gray-800">设置</h1>
          </div>
          <p className="text-sm text-gray-500">
            修改后端配置并保存到 <span className="font-mono">{formatConfigPath(path)}</span>
          </p>
          <p className="mt-1 text-xs text-gray-400">
            API Server / Common 与内置供应商密钥已改为通过 <span className="font-mono">.env</span> 或 config.yaml 配置（参考项目 .env.example）；模型接入请使用下方「自定义供应商 / 自定义模型」。
          </p>
        </div>

        {loading ? (
          <div className="h-56 rounded-2xl border border-gray-200 bg-white flex items-center justify-center text-sm text-gray-400">
            <Loader2 className="w-4 h-4 mr-2 animate-spin" />
            正在读取配置
          </div>
        ) : (
          <div className="space-y-5">
            {groups.map(group => (
              <section key={group.title} className="rounded-2xl border border-gray-200 bg-white p-5 shadow-sm">
                <div className="mb-4">
                  <h2 className="text-sm font-semibold text-gray-800">{group.title}</h2>
                  <p className="mt-1 text-xs text-gray-500">{group.description}</p>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {group.fields.map(field => {
                    const value = getValue(config, field.path);
                    return (
                      <label key={field.path} className="flex flex-col gap-1.5 min-w-0">
                        <span className="text-xs font-medium text-gray-500">{field.label}</span>
                        {field.type === 'boolean' ? (
                          <select
                            value={String(Boolean(value))}
                            onChange={event => updateField(field, event.target.value === 'true')}
                            className="h-10 rounded-lg border border-gray-200 bg-white px-3 text-sm text-gray-700 outline-none focus:border-blue-300"
                          >
                            <option value="true">true</option>
                            <option value="false">false</option>
                          </select>
                        ) : field.type === 'select' ? (
                          <select
                            value={String(value ?? '')}
                            onChange={event => updateField(field, event.target.value)}
                            className="h-10 rounded-lg border border-gray-200 bg-white px-3 text-sm text-gray-700 outline-none focus:border-blue-300"
                          >
                            {isProviderOptions(field.options) ? (
                              field.options.map(group => (
                                <optgroup key={group.provider} label={group.label}>
                                  {group.models.map(model => (
                                    // 自定义模型的 label 即 id；内置模型保留友好显示名
                                    <option key={model.id} value={model.id}>{model.label}</option>
                                  ))}
                                </optgroup>
                              ))
                            ) : (
                              (field.options || []).map(option => (
                                <option key={option.id} value={option.id}>{option.label}</option>
                              ))
                            )}
                          </select>
                        ) : field.type === 'password' ? (
                          <input
                            type="text"
                            value={secretDrafts[field.path] ?? maskSecret(value)}
                            onFocus={event => event.currentTarget.select()}
                            onChange={event => updateSecretField(field, event.target.value)}
                            placeholder="输入新密钥覆盖"
                            className="h-10 rounded-lg border border-gray-200 bg-white px-3 font-mono text-sm text-gray-700 outline-none focus:border-blue-300"
                          />
                        ) : (
                          <input
                            type={field.type === 'number' ? 'number' : 'text'}
                            value={String(value ?? '')}
                            onChange={event => updateField(field, event.target.value)}
                            className="h-10 rounded-lg border border-gray-200 bg-white px-3 text-sm text-gray-700 outline-none focus:border-blue-300"
                          />
                        )}
                      </label>
                    );
                  })}
                </div>
              </section>
            ))}

            <CustomProvidersSection config={config} savedConfig={savedConfig} setConfig={setConfig} envOverrides={envOverrides} />
            <CustomModelsSection config={config} savedConfig={savedConfig} setConfig={setConfig} envOverrides={envOverrides} />

            <div className="sticky bottom-4 flex items-center gap-3 rounded-2xl border border-gray-200 bg-white/95 p-3 shadow-lg backdrop-blur">
              {message && (
                <span className="flex items-center gap-1.5 text-sm text-green-600">
                  <CheckCircle className="w-4 h-4" />
                  {message}
                </span>
              )}
              {error && (
                <span className="flex items-center gap-1.5 text-sm text-red-600">
                  <XCircle className="w-4 h-4" />
                  {error}
                </span>
              )}
              <button
                onClick={save}
                disabled={saving}
                className="ml-auto flex items-center gap-2 rounded-xl bg-blue-500 px-5 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-blue-600 disabled:cursor-not-allowed disabled:bg-gray-200"
              >
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                保存配置
              </button>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
