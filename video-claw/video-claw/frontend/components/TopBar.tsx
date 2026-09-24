'use client';

import React, { useState, useRef, useEffect } from 'react';
import { CheckCircle, Circle, Loader, Edit3, AlertCircle, Square, Zap, Settings2, ChevronDown } from 'lucide-react';
import clsx from 'clsx';
import {
  VIDEO_RATIOS,
  VIDEO_RESOLUTIONS,
  VIDEO_GENERATION_MODES,
  type ProviderGroup,
  type VideoGenerationMode,
  type VideoModelCapabilities,
} from '@/config/models';
import { fetchModelGroupsByType, fetchVideoModelGroupsByAbility, fetchVideoModelCapabilities } from '@/lib/modelRegistry';

export type StageStatus = 'pending' | 'running' | 'waiting' | 'completed' | 'error' | 'stopped';

export const STAGES = [
  { id: 'script_generation', name: '剧本生成', shortName: '剧本' },
  { id: 'character_design', name: '角色设计', shortName: '角色' },
  { id: 'storyboard', name: '分镜设计', shortName: '分镜' },
  { id: 'reference_generation', name: '参考图', shortName: '参考图' },
  { id: 'prompt_rewrite', name: '提示词改写', shortName: '改写' },
  { id: 'video_generation', name: '视频生成', shortName: '视频' },
  { id: 'post_production', name: '后期剪辑', shortName: '后期' },
] as const;

export type StageId = typeof STAGES[number]['id'];

/** 禁用态（默认）的阶段集合：不含 prompt_rewrite，与引入本阶段前的六阶段一致 */
export const DEFAULT_ENABLED_STAGES: string[] = STAGES.map(s => s.id).filter(id => id !== 'prompt_rewrite');

let cachedEnabledStages: string[] | null = null;

async function fetchEnabledStages(): Promise<string[]> {
  if (cachedEnabledStages) return cachedEnabledStages;
  try {
    const resp = await fetch('/api/stages');
    if (!resp.ok) return DEFAULT_ENABLED_STAGES;
    const data = await resp.json();
    const ids: string[] = (data?.stages || []).map((s: any) => s?.id).filter(Boolean);
    const valid = ids.filter(id => STAGES.some(s => s.id === id));
    cachedEnabledStages = valid.length ? valid : DEFAULT_ENABLED_STAGES;
  } catch {
    return DEFAULT_ENABLED_STAGES;
  }
  return cachedEnabledStages;
}

/** 后端启用的阶段列表（GET /api/stages 过滤）；加载完成前按禁用态（六阶段）渲染 */
export function useEnabledStages(): string[] {
  const [enabled, setEnabled] = useState<string[]>(cachedEnabledStages || DEFAULT_ENABLED_STAGES);
  useEffect(() => {
    if (cachedEnabledStages) return;
    let cancelled = false;
    fetchEnabledStages().then(ids => {
      if (!cancelled) setEnabled(ids);
    });
    return () => { cancelled = true; };
  }, []);
  return enabled;
}

export interface ModelConfig {
  llm_model: string;
  vlm_model: string;
  image_t2i_model: string;
  image_it2i_model: string;
  video_model: string;
  video_first_frame_model: string;
  video_start_end_model: string;
  video_reference_model: string;
  video_generation_mode: VideoGenerationMode;
  video_ratio: string;
  video_resolution: string;
  /** 会话级时长覆盖（秒）；空串 = 跟随分镜时长 */
  video_duration: string;
  /** 会话级帧率；空串 = 服务端默认（不注入 fps 字段） */
  video_fps: string;
  enable_concurrency: boolean;
}

interface TopBarProps {
  /** null = 首页 */
  activeStage: string | null;
  stageStatuses: Record<string, StageStatus>;
  onStageClick: (stageId: string) => void;
  onHomeClick: () => void;
  /** 是否处于工作流中（有 sessionId） */
  hasSession: boolean;
  /** 是否正在执行 */
  isRunning: boolean;
  /** 停止执行 */
  onStop: () => void;
  /** 代理模式（自动执行全流程） */
  autoMode: boolean;
  onAutoModeChange: (auto: boolean) => void;
  /** 当前模型配置 */
  modelConfig?: ModelConfig;
  /** 模型配置变更 */
  onModelConfigChange?: (config: ModelConfig) => void;
  /** 项目状态（如 running, waiting, completed, stopped, idle, error 等） */
  projectStatus?: string;
}

/* ─── 带 Provider 分组的 <select> ─── */
function ProviderSelect({
  value,
  providers,
  onChange,
}: {
  value: string;
  providers: ProviderGroup[];
  onChange: (val: string) => void;
}) {
  return (
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      className="bg-gray-50 border border-gray-200 rounded-lg px-2 py-1.5 text-xs text-gray-700 outline-none w-full"
    >
      {providers.map(pg => (
        <optgroup key={pg.provider} label={pg.label}>
          {pg.models.map(m => (
            <option key={m.id} value={m.id}>{m.label}</option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}

/* ─── 模型选择下拉面板 ─── */
function ModelSelector({
  config,
  onChange,
}: {
  config: ModelConfig;
  onChange: (config: ModelConfig) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [videoCaps, setVideoCaps] = useState<VideoModelCapabilities | null>(null);
  const [clampHint, setClampHint] = useState('');
  const [llmProviders, setLlmProviders] = useState<ProviderGroup[]>([]);
  const [vlmProviders, setVlmProviders] = useState<ProviderGroup[]>([]);
  const [t2iProviders, setT2iProviders] = useState<ProviderGroup[]>([]);
  const [i2iProviders, setI2iProviders] = useState<ProviderGroup[]>([]);
  const [firstFrameVideoProviders, setFirstFrameVideoProviders] = useState<ProviderGroup[]>([]);
  const [startEndVideoProviders, setStartEndVideoProviders] = useState<ProviderGroup[]>([]);
  const [referenceVideoProviders, setReferenceVideoProviders] = useState<ProviderGroup[]>([]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    if (open) document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  // 供模型不可用兜底弹窗「更换模型」按钮唤起本面板
  useEffect(() => {
    const openHandler = () => setOpen(true);
    window.addEventListener('open-model-selector', openHandler);
    return () => window.removeEventListener('open-model-selector', openHandler);
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchModelGroupsByType('llm')
      .then(groups => { if (!cancelled) setLlmProviders(groups); })
      .catch(() => {});
    fetchModelGroupsByType('vlm')
      .then(groups => { if (!cancelled) setVlmProviders(groups); })
      .catch(() => {});
    fetchModelGroupsByType('t2i')
      .then(groups => { if (!cancelled) setT2iProviders(groups); })
      .catch(() => {});
    fetchModelGroupsByType('i2i')
      .then(groups => { if (!cancelled) setI2iProviders(groups); })
      .catch(() => {});
    fetchVideoModelGroupsByAbility('first_frame_i2v')
      .then(groups => { if (!cancelled) setFirstFrameVideoProviders(groups); })
      .catch(() => {});
    fetchVideoModelGroupsByAbility('start_end_frame_i2v')
      .then(groups => { if (!cancelled) setStartEndVideoProviders(groups); })
      .catch(() => {});
    fetchVideoModelGroupsByAbility('reference_to_video')
      .then(groups => { if (!cancelled) setReferenceVideoProviders(groups); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  const update = (key: keyof ModelConfig, val: string | boolean) => {
    onChange({ ...config, [key]: val });
  };

  const activeVideoModel =
    config.video_generation_mode === 'start_end_frame'
      ? config.video_start_end_model
      : config.video_generation_mode === 'reference'
        ? config.video_reference_model
        : config.video_first_frame_model;
  const activeVideoProviders =
    config.video_generation_mode === 'start_end_frame'
      ? startEndVideoProviders
      : config.video_generation_mode === 'reference'
        ? referenceVideoProviders
        : firstFrameVideoProviders;
  const activeVideoLabel = VIDEO_GENERATION_MODES.find(item => item.id === config.video_generation_mode)?.label || '首帧生视频';

  // 供模型切换联动读取最新 config（避免 effect 依赖 config 导致重复触发）
  const configRef = useRef(config);
  configRef.current = config;

  // 切换模型时按新模型能力联动：夹取当前值并提示（能力未声明的维度不限制）
  useEffect(() => {
    let cancelled = false;
    setClampHint('');
    if (!activeVideoModel) return;
    fetchVideoModelCapabilities(activeVideoModel)
      .then(caps => {
        if (cancelled) return;
        setVideoCaps(caps);
        if (!caps) return;
        const current = configRef.current;
        const hints: string[] = [];
        const next: Partial<ModelConfig> = {};
        if (caps.ratios?.length && !caps.ratios.includes(current.video_ratio)) {
          next.video_ratio = caps.ratios[0];
          hints.push(`画幅 ${current.video_ratio}→${caps.ratios[0]}`);
        }
        if (caps.resolutions?.length && !caps.resolutions.includes(current.video_resolution)) {
          next.video_resolution = caps.resolutions[0];
          hints.push(`分辨率 ${current.video_resolution}→${caps.resolutions[0]}`);
        }
        if (current.video_duration) {
          const val = Number(current.video_duration);
          const min = caps.duration?.min;
          const max = caps.duration?.max;
          if (Number.isFinite(val)) {
            if (min != null && val < min) {
              next.video_duration = String(min);
              hints.push(`时长 ${val}→${min}s`);
            } else if (max != null && val > max) {
              next.video_duration = String(max);
              hints.push(`时长 ${val}→${max}s`);
            }
          }
        }
        if (current.video_fps) {
          const fpsList = caps.fps?.length ? caps.fps : null;
          if (!fpsList) {
            next.video_fps = '';
            hints.push('FPS 已重置（模型未声明）');
          } else if (!fpsList.includes(Number(current.video_fps))) {
            const nearest = fpsList.reduce((a, b) =>
              Math.abs(b - Number(current.video_fps)) < Math.abs(a - Number(current.video_fps)) ? b : a);
            next.video_fps = String(nearest);
            hints.push(`FPS ${current.video_fps}→${nearest}`);
          }
        }
        if (Object.keys(next).length) {
          onChange({ ...current, ...next });
          setClampHint(`已按模型能力调整：${hints.join('，')}`);
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeVideoModel]);

  const ratioOptions = videoCaps?.ratios?.length
    ? VIDEO_RATIOS.filter(r => videoCaps.ratios!.includes(r.id))
    : VIDEO_RATIOS;
  const resolutionOptions = videoCaps?.resolutions?.length
    ? VIDEO_RESOLUTIONS.filter(r => videoCaps.resolutions!.includes(r.id))
    : VIDEO_RESOLUTIONS;
  const fpsOptions = videoCaps?.fps?.length ? videoCaps.fps : [];
  const durationMin = Math.max(1, Math.min(videoCaps?.duration?.min ?? 2, videoCaps?.duration?.max ?? 15));
  const durationMax = Math.max(durationMin, Math.min(videoCaps?.duration?.max ?? 15, durationMin + 19));
  const durationOptions: number[] = [];
  for (let v = durationMin; v <= durationMax; v++) durationOptions.push(v);

  const updateVideoMode = (mode: VideoGenerationMode) => {
    const nextModel =
      mode === 'start_end_frame'
        ? config.video_start_end_model
        : mode === 'reference'
          ? config.video_reference_model
          : config.video_first_frame_model;
    onChange({ ...config, video_generation_mode: mode, video_model: nextModel });
  };

  const updateActiveVideoModel = (model: string) => {
    if (config.video_generation_mode === 'start_end_frame') {
      onChange({ ...config, video_start_end_model: model, video_model: model });
    } else if (config.video_generation_mode === 'reference') {
      onChange({ ...config, video_reference_model: model, video_model: model });
    } else {
      onChange({ ...config, video_first_frame_model: model, video_model: model });
    }
  };

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={clsx(
          'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all',
          open
            ? 'bg-blue-50 text-blue-700 ring-1 ring-blue-200'
            : 'text-gray-500 hover:bg-gray-50'
        )}
        title="生成配置"
      >
        <Settings2 className="w-3.5 h-3.5" />
        <span>生成配置</span>
        <ChevronDown className={clsx('w-3 h-3 transition-transform', open && 'rotate-180')} />
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-72 bg-white rounded-xl shadow-lg border border-gray-200 p-3 z-50 space-y-2.5">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] text-gray-400 font-medium">LLM 模型</span>
            <ProviderSelect value={config.llm_model} providers={llmProviders} onChange={v => update('llm_model', v)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] text-gray-400 font-medium">VLM 评估模型</span>
            <ProviderSelect value={config.vlm_model} providers={vlmProviders} onChange={v => update('vlm_model', v)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] text-gray-400 font-medium">文生图</span>
            <ProviderSelect value={config.image_t2i_model} providers={t2iProviders} onChange={v => update('image_t2i_model', v)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] text-gray-400 font-medium">图生图</span>
            <ProviderSelect value={config.image_it2i_model} providers={i2iProviders} onChange={v => update('image_it2i_model', v)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] text-gray-400 font-medium">视频生成方式</span>
            <select
              value={config.video_generation_mode}
              onChange={e => updateVideoMode(e.target.value as VideoGenerationMode)}
              className="bg-gray-50 border border-gray-200 rounded-lg px-2 py-1.5 text-xs text-gray-700 outline-none w-full"
            >
              {VIDEO_GENERATION_MODES.map(item => (
                <option key={item.id} value={item.id}>{item.label}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] text-gray-400 font-medium">{activeVideoLabel}模型</span>
            <ProviderSelect value={activeVideoModel} providers={activeVideoProviders} onChange={updateActiveVideoModel} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] text-gray-400 font-medium">视频比例</span>
            <div className="flex gap-0.5">
              {ratioOptions.map(r => (
                <button
                  key={r.id}
                  onClick={() => update('video_ratio', r.id)}
                  className={`flex flex-col items-center gap-0.5 p-1 rounded border transition-all ${
                    config.video_ratio === r.id
                      ? 'border-indigo-500 bg-indigo-50'
                      : 'border-gray-200 hover:border-gray-300'
                  }`}
                  title={r.label}
                >
                  <div
                    className="bg-gray-600 rounded-sm"
                    style={{
                      width: r.ratio === '16:9' ? '16px' :
                             r.ratio === '9:16' ? '9px' :
                             r.ratio === '1:1' ? '12px' :
                             r.ratio === '4:3' ? '14px' :
                             r.ratio === '3:4' ? '10px' :
                             '18px',
                      height: r.ratio === '16:9' ? '9px' :
                             r.ratio === '9:16' ? '16px' :
                             r.ratio === '1:1' ? '12px' :
                             r.ratio === '4:3' ? '10px' :
                             r.ratio === '3:4' ? '14px' :
                             '7px',
                    }}
                  />
                  <span className="text-[8px] text-gray-500">{r.label}</span>
                </button>
              ))}
            </div>
          </label>
          {/* 高级参数：默认折叠，可选项按所选模型能力联动 */}
          <div className="rounded-lg border border-gray-200">
            <button
              type="button"
              onClick={() => setAdvancedOpen(!advancedOpen)}
              className="flex w-full items-center justify-between px-2 py-1.5 text-[10px] text-gray-400 font-medium"
            >
              <span>高级参数（时长 / FPS / 分辨率）</span>
              <ChevronDown className={clsx('w-3 h-3 transition-transform', advancedOpen && 'rotate-180')} />
            </button>
            {advancedOpen && (
              <div className="space-y-2.5 px-2 pb-2">
                <label className="flex flex-col gap-1">
                  <span className="text-[10px] text-gray-400 font-medium">视频时长（秒）</span>
                  <select
                    value={config.video_duration}
                    onChange={e => update('video_duration', e.target.value)}
                    className="bg-gray-50 border border-gray-200 rounded-lg px-2 py-1.5 text-xs text-gray-700 outline-none w-full"
                  >
                    <option value="">跟随分镜</option>
                    {durationOptions.map(v => (
                      <option key={v} value={String(v)}>{v}s</option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-[10px] text-gray-400 font-medium">帧率 FPS</span>
                  <select
                    value={config.video_fps}
                    onChange={e => update('video_fps', e.target.value)}
                    className="bg-gray-50 border border-gray-200 rounded-lg px-2 py-1.5 text-xs text-gray-700 outline-none w-full"
                  >
                    <option value="">默认（服务端）</option>
                    {fpsOptions.map(v => (
                      <option key={v} value={String(v)}>{v}</option>
                    ))}
                  </select>
                  {!fpsOptions.length && (
                    <span className="text-[9px] text-gray-300">当前模型未声明 FPS 能力，设置后将被忽略</span>
                  )}
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-[10px] text-gray-400 font-medium">视频分辨率</span>
                  <select
                    value={config.video_resolution}
                    onChange={e => update('video_resolution', e.target.value)}
                    className="bg-gray-50 border border-gray-200 rounded-lg px-2 py-1.5 text-xs text-gray-700 outline-none w-full"
                  >
                    {resolutionOptions.map(item => (
                      <option key={item.id} value={item.id}>{item.label}</option>
                    ))}
                  </select>
                </label>
              </div>
            )}
          </div>
          {clampHint && (
            <p className="text-[10px] text-amber-600 leading-snug">{clampHint}</p>
          )}
          <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
            <input
              type="checkbox"
              checked={!!config.enable_concurrency}
              onChange={e => update('enable_concurrency', e.target.checked)}
              className="w-3.5 h-3.5 rounded border-gray-300 text-blue-500 focus:ring-blue-500/30"
            />
            <span className="text-gray-500">并发生成</span>
          </label>
        </div>
      )}
    </div>
  );
}

export default function TopBar({
  activeStage,
  stageStatuses,
  onStageClick,
  onHomeClick,
  hasSession,
  isRunning,
  onStop,
  autoMode,
  onAutoModeChange,
  modelConfig,
  onModelConfigChange,
  projectStatus,
}: TopBarProps) {
  const enabledStages = useEnabledStages();
  const visibleStages = STAGES.filter(s => enabledStages.includes(s.id));
  const getStageIcon = (status: StageStatus, isActive: boolean) => {
    switch (status) {
      case 'completed':
        return <CheckCircle className="w-4 h-4 text-green-500" />;
      case 'running':
        return <Loader className="w-4 h-4 text-blue-500 animate-spin" />;
      case 'waiting':
        return <Edit3 className="w-4 h-4 text-amber-500" />;
      case 'error':
        return <AlertCircle className="w-4 h-4 text-red-500" />;
      default:
        return (
          <Circle
            className={clsx('w-4 h-4', isActive ? 'text-blue-400' : 'text-gray-300')}
          />
        );
    }
  };

  return (
    <>
    <header className="fixed top-0 right-0 left-[var(--app-sidebar-width)] z-30 h-14 bg-white border-b border-gray-200 flex items-center px-4 min-w-0 transition-[left] duration-300">
      {/* Logo & 名称 */}
      <button
        onClick={onHomeClick}
        className="flex items-center gap-2 mr-6 hover:opacity-80 transition-opacity flex-shrink-0"
      >
        <img
          src="/logo.jpg"
          alt="Logo"
          className="w-8 h-8 rounded-lg object-contain"
        />
        <div className="flex flex-col leading-tight">
          <span className="font-bold text-sm text-gray-800 tracking-tight">
            Video-Claw
          </span>
        </div>
      </button>

      {/* 分隔线 */}
      {hasSession && <div className="w-px h-6 bg-gray-200 mr-4 flex-shrink-0" />}

      {/* 阶段进度条 */}
      {hasSession && (
        <nav className="flex items-center gap-1 overflow-x-auto flex-1 min-w-0">
          {visibleStages.map((stage, idx) => {
            const status = stageStatuses[stage.id] || 'pending';
            const isActive = activeStage === stage.id;

            return (
              <React.Fragment key={stage.id}>
                {idx > 0 && (
                  <div
                    className={clsx(
                      'w-6 h-px flex-shrink-0',
                      stageStatuses[visibleStages[idx - 1].id] === 'completed'
                        ? 'bg-green-300'
                        : 'bg-gray-200'
                    )}
                  />
                )}
                <button
                  onClick={() => onStageClick(stage.id)}
                  className={clsx(
                    'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all whitespace-nowrap flex-shrink-0',
                    isActive
                      ? 'bg-blue-50 text-blue-700 ring-1 ring-blue-200'
                      : status === 'completed'
                        ? 'text-green-700 hover:bg-green-50'
                        : status === 'error'
                          ? 'text-red-600 hover:bg-red-50'
                          : 'text-gray-500 hover:bg-gray-50'
                  )}
                >
                  {getStageIcon(status, isActive)}
                  <span>{stage.shortName}</span>
                </button>
              </React.Fragment>
            );
          })}
        </nav>
      )}

      {/* 右侧控制区 */}
      <div className="ml-auto flex min-w-0 items-center justify-end gap-2 flex-shrink-0">
        {/* 模型选择 */}
        {hasSession && modelConfig && onModelConfigChange && (
          <ModelSelector config={modelConfig} onChange={onModelConfigChange} />
        )}

        {/* 代理模式切换 */}
        {hasSession && (
          <button
            onClick={() => onAutoModeChange(!autoMode)}
            className={clsx(
              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all',
              autoMode
                ? 'bg-amber-50 text-amber-700 ring-1 ring-amber-200'
                : 'text-gray-500 hover:bg-gray-50'
            )}
            title={autoMode ? '代理模式：自动执行全流程' : '手动模式：每阶段需确认'}
          >
            <Zap className="w-3.5 h-3.5" />
            <span>{autoMode ? '自动' : '手动'}</span>
          </button>
        )}

        {/* 停止按钮 */}
        {isRunning && (
          <button
            onClick={onStop}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-red-50 text-red-600 hover:bg-red-100 rounded-lg text-xs font-medium transition-colors ring-1 ring-red-200"
            title="停止生成"
          >
            <Square className="w-3.5 h-3.5 fill-current" />
            <span>停止生成</span>
          </button>
        )}

        {/* 项目状态 */}
        {hasSession && projectStatus && (
          <div
            className={clsx(
              'px-2 py-1 rounded-lg text-xs font-medium flex items-center gap-1',
              projectStatus === 'running' && 'bg-blue-50 text-blue-700',
              projectStatus === 'waiting' && 'bg-amber-50 text-amber-700',
              projectStatus === 'completed' && 'bg-green-50 text-green-700',
              projectStatus === 'pending' && 'bg-gray-50 text-gray-600',
              projectStatus === 'error' && 'bg-red-50 text-red-700',
              projectStatus === 'stopped' && 'bg-orange-50 text-orange-700'
            )}
            title="项目状态"
          >
            {projectStatus === 'running' && <Loader className="w-3 h-3 animate-spin" />}
            {projectStatus === 'waiting' && <Edit3 className="w-3 h-3" />}
            {projectStatus === 'error' && <AlertCircle className="w-3 h-3" />}
            <span>
              {projectStatus === 'running' ? '执行中' :
               projectStatus === 'waiting' ? '等待确认' :
               projectStatus === 'completed' ? '已完成' :
               projectStatus === 'pending' ? '空闲' :
               projectStatus === 'stopped' ? '已停止' :
               projectStatus === 'error' ? '出错' : projectStatus}
            </span>
          </div>
        )}

      </div>
    </header>
    <div className="h-14 flex-shrink-0" />
    </>
  );
}
