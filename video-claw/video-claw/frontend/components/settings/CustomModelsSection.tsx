'use client';

import React, { useState } from 'react';
import { AlertCircle, CheckCircle, Loader2, Plus, Trash2, Zap } from 'lucide-react';
import { testCustomModel, type ModelTestResult } from '@/lib/workflowApi';

/** 环境变量覆盖信息（GET /api/config 返回） */
export interface EnvOverrides {
  fields: string[];
  providers: string[];
  models: string[];
}

type ConfigTree = Record<string, any>;

/** 内置供应商：不在「自定义供应商」管理区中展示 */
export const BUILTIN_PROVIDERS = ['common', 'openai', 'gemini', 'deepseek', 'dashscope', 'ark', 'kling'];

export const PROTOCOL_OPTIONS = [
  { id: 'openai', label: 'OpenAI 标准接口 (vllm 等)' },
  { id: 'vllm-omni', label: 'vLLM-Omni' },
  { id: 'sglang', label: 'SGLang' },
];

export const MODEL_TYPE_OPTIONS = [
  { id: 'llm', label: 'LLM 文本' },
  { id: 'vlm', label: 'VLM 视觉语言' },
  { id: 't2i', label: '文生图' },
  { id: 'i2i', label: '图生图' },
  { id: 'video', label: '视频生成' },
];

export const ABILITY_OPTIONS = [
  { id: 'text_to_video', label: '文生视频' },
  { id: 'first_frame_i2v', label: '首帧生视频' },
  { id: 'start_end_frame_i2v', label: '首尾帧生视频' },
  { id: 'reference_to_video', label: '参考图生视频' },
  { id: 'image_to_video', label: '图生视频（通用）' },
  { id: 'audio_reference', label: '音频参考' },
  { id: 'video_reference', label: '参考视频' },
  { id: 'text_to_image', label: '文生图能力' },
  { id: 'image_to_image', label: '图生图能力' },
  { id: 'reference_image', label: '参考图能力' },
  { id: 'text_generation', label: '文本生成能力' },
  { id: 'vision_language', label: '视觉语言能力' },
  { id: 'image_understanding', label: '视觉理解能力' },
];

const inputClass =
  'h-9 w-full min-w-0 rounded-lg border border-gray-200 bg-white px-2.5 text-xs text-gray-700 outline-none focus:border-blue-300 disabled:bg-gray-50 disabled:text-gray-400';

function EnvBadge() {
  return (
    <span className="ml-1.5 inline-flex shrink-0 items-center rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-600">
      来自 .env
    </span>
  );
}

function FieldLabel({ text, envLocked }: { text: string; envLocked?: boolean }) {
  return (
    <span className="mb-1 flex items-center text-[11px] font-medium text-gray-500">
      {text}
      {envLocked && <EnvBadge />}
    </span>
  );
}

/** 自定义供应商管理区：管理 api_providers 中的非内置条目 */
export function CustomProvidersSection({
  config,
  savedConfig,
  setConfig,
  envOverrides,
}: {
  config: ConfigTree;
  savedConfig: ConfigTree;
  setConfig: React.Dispatch<React.SetStateAction<ConfigTree>>;
  envOverrides: EnvOverrides;
}) {
  const [draft, setDraft] = useState({ key: '', protocol: 'openai', base_url: '', api_key: '' });
  const [error, setError] = useState('');

  const providers: Array<[string, any]> = Object.entries(config.api_providers || {}).filter(
    ([key]) => !BUILTIN_PROVIDERS.includes(key),
  );
  const savedProviders: Record<string, any> = (savedConfig.api_providers as Record<string, any>) || {};

  const isEnvField = (key: string, field: string) => envOverrides.fields.includes(`api_providers.${key}.${field}`);
  const isEnvOnly = (key: string) => envOverrides.providers.includes(key);
  // 当前编辑值与已保存值不一致：工作流实际使用已保存配置
  const isProviderDirty = (key: string) =>
    JSON.stringify((config.api_providers || {})[key] ?? null) !== JSON.stringify(savedProviders[key] ?? null);

  const updateProvider = (key: string, field: string, value: any) => {
    setConfig(current => {
      const next = structuredClone(current || {});
      next.api_providers = next.api_providers || {};
      next.api_providers[key] = { ...(next.api_providers[key] || {}), [field]: value };
      return next;
    });
  };

  const removeProvider = (key: string) => {
    const referenced = (config.custom_models || []).filter((m: any) => m?.provider === key).map((m: any) => m.id);
    if (referenced.length > 0) {
      setError(`供应商 ${key} 仍被模型引用（${referenced.join('、')}），请先删除或改绑这些模型`);
      return;
    }
    setError('');
    setConfig(current => {
      const next = structuredClone(current || {});
      if (next.api_providers) delete next.api_providers[key];
      return next;
    });
  };

  const addProvider = () => {
    const key = draft.key.trim();
    if (!key) {
      setError('请填写供应商名称（key）');
      return;
    }
    if (!/^[A-Za-z0-9_-]+$/.test(key)) {
      setError('供应商名称仅支持字母、数字、下划线和中划线');
      return;
    }
    if (BUILTIN_PROVIDERS.includes(key) || (config.api_providers || {})[key]) {
      setError(`供应商名称已存在：${key}`);
      return;
    }
    if (!draft.base_url.trim()) {
      setError('请填写 Base URL');
      return;
    }
    setError('');
    setConfig(current => {
      const next = structuredClone(current || {});
      next.api_providers = next.api_providers || {};
      next.api_providers[key] = {
        protocol: draft.protocol,
        base_url: draft.base_url.trim(),
        api_key: draft.api_key,
        enable_proxy: false,
      };
      return next;
    });
    setDraft({ key: '', protocol: 'openai', base_url: '', api_key: '' });
  };

  return (
    <section className="rounded-2xl border border-gray-200 bg-white p-5 shadow-sm">
      <div className="mb-4">
        <h2 className="text-sm font-semibold text-gray-800">自定义供应商</h2>
        <p className="mt-1 text-xs text-gray-500">
          每台部署的推理服务器登记为一个供应商（protocol 区分 openai / vllm-omni / sglang）；建议按能力命名
          （如 local_llm / local_vlm / local_image_t2i / local_image_it2i / local_video）。被 .env 覆盖的字段只读展示，保存不会写回配置文件。
        </p>
      </div>

      {providers.length === 0 && <p className="mb-3 text-xs text-gray-400">暂无自定义供应商</p>}

      <div className="space-y-3">
        {providers.map(([key, provider]) => {
          const envOnly = isEnvOnly(key);
          return (
            <div key={key} className="rounded-xl border border-gray-100 bg-gray-50/60 p-3">
              <div className="mb-2 flex items-center gap-2">
                <span className="font-mono text-xs font-semibold text-gray-700">{key}</span>
                {envOnly && <EnvBadge />}
                {!envOnly && isProviderDirty(key) && (
                  <span className="inline-flex items-center rounded bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium text-sky-600">
                    未保存
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => removeProvider(key)}
                  disabled={envOnly}
                  className="ml-auto inline-flex items-center gap-1 rounded-lg bg-red-50 px-2 py-1 text-[11px] font-medium text-red-600 hover:bg-red-100 disabled:cursor-not-allowed disabled:bg-gray-100 disabled:text-gray-400"
                >
                  <Trash2 className="h-3 w-3" />
                  删除
                </button>
              </div>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-4">
                <label className="flex flex-col">
                  <FieldLabel text="protocol 协议" envLocked={isEnvField(key, 'protocol')} />
                  <select
                    className={inputClass}
                    value={provider.protocol || ''}
                    disabled={envOnly || isEnvField(key, 'protocol')}
                    onChange={e => updateProvider(key, 'protocol', e.target.value)}
                  >
                    <option value="">请选择</option>
                    {PROTOCOL_OPTIONS.map(option => (
                      <option key={option.id} value={option.id}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col">
                  <FieldLabel text="base_url 接口地址" envLocked={isEnvField(key, 'base_url')} />
                  <input
                    className={inputClass}
                    value={provider.base_url || ''}
                    disabled={envOnly || isEnvField(key, 'base_url')}
                    placeholder="http://127.0.0.1:8000/v1"
                    onChange={e => updateProvider(key, 'base_url', e.target.value)}
                  />
                </label>
                <label className="flex flex-col">
                  <FieldLabel text="api_key（可空）" envLocked={isEnvField(key, 'api_key')} />
                  <input
                    className={inputClass}
                    type="text"
                    value={envOnly || isEnvField(key, 'api_key') ? provider.api_key || '' : (provider.api_key ?? '')}
                    disabled={envOnly || isEnvField(key, 'api_key')}
                    placeholder="零鉴权服务留空即可"
                    onChange={e => updateProvider(key, 'api_key', e.target.value)}
                  />
                </label>
              </div>
            </div>
          );
        })}
      </div>

      <div className="mt-4 rounded-xl border border-dashed border-gray-200 p-3">
        <p className="mb-2 text-xs font-medium text-gray-600">新增供应商</p>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-4">
          <input className={inputClass} placeholder="名称（如 local_llm）" value={draft.key} onChange={e => setDraft({ ...draft, key: e.target.value })} />
          <select className={inputClass} value={draft.protocol} onChange={e => setDraft({ ...draft, protocol: e.target.value })}>
            {PROTOCOL_OPTIONS.map(option => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
          <input className={inputClass} placeholder="Base URL" value={draft.base_url} onChange={e => setDraft({ ...draft, base_url: e.target.value })} />
        </div>
        <div className="mt-2 flex items-center gap-2">
          <input className={`${inputClass} max-w-xs`} placeholder="api_key（可空）" value={draft.api_key} onChange={e => setDraft({ ...draft, api_key: e.target.value })} />
          <button
            type="button"
            onClick={addProvider}
            className="inline-flex items-center gap-1 rounded-lg bg-blue-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-600"
          >
            <Plus className="h-3 w-3" />
            添加
          </button>
        </div>
      </div>

      {error && (
        <p className="mt-2 flex items-center gap-1 text-xs text-red-600">
          <AlertCircle className="h-3.5 w-3.5" />
          {error}
        </p>
      )}
    </section>
  );
}

/** 自定义模型管理区：管理 custom_models 注册列表（引用供应商） */
export function CustomModelsSection({
  config,
  savedConfig,
  setConfig,
  envOverrides,
}: {
  config: ConfigTree;
  savedConfig: ConfigTree;
  setConfig: React.Dispatch<React.SetStateAction<ConfigTree>>;
  envOverrides: EnvOverrides;
}) {
  const [error, setError] = useState('');
  const [testing, setTesting] = useState<Record<string, boolean>>({});
  const [testResults, setTestResults] = useState<Record<string, ModelTestResult | string>>({});

  const models: any[] = Array.isArray(config.custom_models) ? config.custom_models : [];
  const providerKeys = Object.keys(config.api_providers || {}).filter(key => !BUILTIN_PROVIDERS.includes(key));
  const savedModelsById = new Map<string, any>(
    (Array.isArray(savedConfig.custom_models) ? savedConfig.custom_models : [])
      .filter((item: any) => item?.id)
      .map((item: any) => [item.id, item]),
  );

  const isEnvField = (id: string, field: string) => envOverrides.fields.includes(`custom_models[${id}].${field}`);
  const isEnvOnly = (id: string) => envOverrides.models.includes(id);
  // 当前编辑值与已保存值不一致：工作流实际使用已保存配置
  const isModelDirty = (entry: any) => JSON.stringify(entry ?? null) !== JSON.stringify(savedModelsById.get(entry?.id) ?? null);

  const updateModel = (index: number, field: string, value: any) => {
    setConfig(current => {
      const next = structuredClone(current || {});
      const list = Array.isArray(next.custom_models) ? next.custom_models : [];
      list[index] = { ...(list[index] || {}), [field]: value };
      next.custom_models = list;
      return next;
    });
  };

  const toggleListValue = (index: number, field: 'types' | 'abilities', value: string) => {
    const list: string[] = models[index]?.[field] || [];
    const next = list.includes(value) ? list.filter(item => item !== value) : [...list, value];
    updateModel(index, field, next);
  };

  const removeModel = (index: number) => {
    setConfig(current => {
      const next = structuredClone(current || {});
      const list = Array.isArray(next.custom_models) ? next.custom_models : [];
      list.splice(index, 1);
      next.custom_models = list;
      return next;
    });
  };

  const addModel = () => {
    const base = 'my-model';
    let candidate = base;
    let seq = 1;
    const existing = new Set(models.map(item => item?.id));
    while (existing.has(candidate)) {
      candidate = `${base}-${seq++}`;
    }
    setConfig(current => {
      const next = structuredClone(current || {});
      const list = Array.isArray(next.custom_models) ? next.custom_models : [];
      list.push({
        id: candidate,
        provider: providerKeys[0] || '',
        model: '',
        types: [],
        abilities: [],
      });
      next.custom_models = list;
      return next;
    });
  };

  const runTest = async (index: number, modelType: string) => {
    const entry = models[index] || {};
    const key = `${entry.id}:${modelType}`;
    setTesting(prev => ({ ...prev, [key]: true }));
    setTestResults(prev => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
    try {
      const providerKey = entry.provider || '';
      const providerDraft = { key: providerKey, ...(config.api_providers?.[providerKey] || {}) };
      const result = await testCustomModel({
        model: { id: entry.id, model: entry.model, abilities: entry.abilities || [] },
        provider: providerDraft,
        model_type: modelType as 'llm' | 'vlm' | 't2i' | 'i2i' | 'video',
      });
      setTestResults(prev => ({ ...prev, [key]: result }));
    } catch (e: any) {
      setTestResults(prev => ({ ...prev, [key]: e?.message || '测试请求失败' }));
    } finally {
      setTesting(prev => ({ ...prev, [key]: false }));
    }
  };

  return (
    <section className="rounded-2xl border border-gray-200 bg-white p-5 shadow-sm">
      <div className="mb-4">
        <h2 className="text-sm font-semibold text-gray-800">自定义模型</h2>
        <p className="mt-1 text-xs text-gray-500">
          模型通过 provider 引用「自定义供应商」；concurrency 缺省 1（本地服务最保守，可显式调高）。媒体类连通测试会发起一次真实生成。
          「测试连接」使用当前编辑值验证；<b>开始工作流使用已保存配置</b>，修改后请先点击底部「保存配置」。
        </p>
      </div>

      {models.length === 0 && <p className="mb-3 text-xs text-gray-400">暂无自定义模型</p>}

      <div className="space-y-3">
        {models.map((entry, index) => {
          const envOnly = isEnvOnly(entry.id);
          const selectedTypes: string[] = entry.types || [];
          // key 使用稳定的行下标：编辑 id 时不触发重挂载（否则输入一个字符就失焦）
          return (
            <div key={index} className="rounded-xl border border-gray-100 bg-gray-50/60 p-3">
              <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-4">
                <label className="flex flex-col">
                  <FieldLabel text="id 模型标识" envLocked={isEnvField(entry.id, 'id')} />
                  <input
                    className={inputClass}
                    value={entry.id || ''}
                    disabled={envOnly || isEnvField(entry.id, 'id')}
                    onChange={e => updateModel(index, 'id', e.target.value)}
                  />
                </label>
                <label className="flex flex-col">
                  <FieldLabel text="provider 供应商" envLocked={isEnvField(entry.id, 'provider')} />
                  <select
                    className={inputClass}
                    value={entry.provider || ''}
                    disabled={envOnly || isEnvField(entry.id, 'provider')}
                    onChange={e => updateModel(index, 'provider', e.target.value)}
                  >
                    <option value="">请选择</option>
                    {providerKeys.map(key => (
                      <option key={key} value={key}>
                        {config.api_providers?.[key]?.name || key}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col">
                  <FieldLabel text="model 请求模型名（可空=id）" envLocked={isEnvField(entry.id, 'model')} />
                  <input
                    className={inputClass}
                    value={entry.model || ''}
                    disabled={envOnly || isEnvField(entry.id, 'model')}
                    onChange={e => updateModel(index, 'model', e.target.value)}
                  />
                </label>
              </div>

              <div className="mt-2 flex flex-wrap items-start gap-x-6 gap-y-2">
                <div>
                  <FieldLabel text="types 模型类型" envLocked={isEnvField(entry.id, 'types')} />
                  <div className="flex flex-wrap gap-1.5">
                    {MODEL_TYPE_OPTIONS.map(option => (
                      <button
                        key={option.id}
                        type="button"
                        disabled={envOnly || isEnvField(entry.id, 'types')}
                        onClick={() => toggleListValue(index, 'types', option.id)}
                        className={`rounded-lg border px-2 py-1 text-[11px] font-medium transition-colors disabled:cursor-not-allowed ${
                          selectedTypes.includes(option.id)
                            ? 'border-blue-300 bg-blue-50 text-blue-600'
                            : 'border-gray-200 bg-white text-gray-500 hover:border-gray-300'
                        }`}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <FieldLabel text="abilities 能力标签（可留空按类型推导）" envLocked={isEnvField(entry.id, 'abilities')} />
                  <div className="flex flex-wrap gap-1.5">
                    {ABILITY_OPTIONS.map(option => (
                      <button
                        key={option.id}
                        type="button"
                        disabled={envOnly || isEnvField(entry.id, 'abilities')}
                        onClick={() => toggleListValue(index, 'abilities', option.id)}
                        className={`rounded-lg border px-2 py-1 text-[11px] font-medium transition-colors disabled:cursor-not-allowed ${
                          (entry.abilities || []).includes(option.id)
                            ? 'border-emerald-300 bg-emerald-50 text-emerald-600'
                            : 'border-gray-200 bg-white text-gray-500 hover:border-gray-300'
                        }`}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                </div>
                <label className="flex w-32 flex-col">
                  <FieldLabel text="concurrency 并发" envLocked={isEnvField(entry.id, 'concurrency')} />
                  <input
                    className={inputClass}
                    type="number"
                    min={1}
                    placeholder="1"
                    value={entry.concurrency ?? ''}
                    disabled={envOnly || isEnvField(entry.id, 'concurrency')}
                    onChange={e => updateModel(index, 'concurrency', e.target.value === '' ? undefined : Number(e.target.value))}
                  />
                </label>
              </div>

              <div className="mt-2 flex flex-wrap items-center gap-2">
                {selectedTypes.length === 0 && <span className="text-[11px] text-gray-400">选择类型后可测试连通性</span>}
                {selectedTypes.map(type => {
                  const key = `${entry.id}:${type}`;
                  const result = testResults[key];
                  return (
                    <span key={type} className="inline-flex items-center gap-1">
                      <button
                        type="button"
                        disabled={Boolean(testing[key]) || envOnly}
                        onClick={() => runTest(index, type)}
                        className="inline-flex items-center gap-1 rounded-lg bg-violet-50 px-2 py-1 text-[11px] font-medium text-violet-600 hover:bg-violet-100 disabled:cursor-wait disabled:bg-gray-100 disabled:text-gray-400"
                      >
                        {testing[key] ? <Loader2 className="h-3 w-3 animate-spin" /> : <Zap className="h-3 w-3" />}
                        测试 {type}
                      </button>
                      {result && typeof result !== 'string' && (
                        <span className={`inline-flex items-center gap-1 text-[11px] ${result.success ? 'text-green-600' : 'text-red-600'}`}>
                          {result.success ? <CheckCircle className="h-3 w-3" /> : <AlertCircle className="h-3 w-3" />}
                          {result.success ? `通过 ${result.elapsed_ms}ms` : result.reason.slice(0, 60)}
                        </span>
                      )}
                      {typeof result === 'string' && <span className="text-[11px] text-red-600">{result.slice(0, 60)}</span>}
                    </span>
                  );
                })}
                {!envOnly && isModelDirty(entry) && (
                  <span className="inline-flex items-center rounded bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium text-sky-600">
                    未保存
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => removeModel(index)}
                  disabled={envOnly}
                  className="ml-auto inline-flex items-center gap-1 rounded-lg bg-red-50 px-2 py-1 text-[11px] font-medium text-red-600 hover:bg-red-100 disabled:cursor-not-allowed disabled:bg-gray-100 disabled:text-gray-400"
                >
                  <Trash2 className="h-3 w-3" />
                  删除
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="mt-4 flex items-center gap-2">
        <button
          type="button"
          onClick={addModel}
          className="inline-flex items-center gap-1 rounded-lg bg-blue-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-600"
        >
          <Plus className="h-3 w-3" />
          新增模型
        </button>
        <span className="text-[11px] text-gray-400">保存后自动出现在主流程、沙盒与 Pipeline 的模型选择列表中</span>
      </div>

      {error && (
        <p className="mt-2 flex items-center gap-1 text-xs text-red-600">
          <AlertCircle className="h-3.5 w-3.5" />
          {error}
        </p>
      )}
    </section>
  );
}