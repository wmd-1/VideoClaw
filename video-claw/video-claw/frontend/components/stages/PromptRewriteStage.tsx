'use client';

import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, CheckCircle, Loader, RefreshCw, Save, Sparkles, XCircle, Wand2 } from 'lucide-react';
import clsx from 'clsx';
import type { StageViewProps } from './types';
import StageProgress from './StageProgress';
import { pruneDraftsByContent } from '@/lib/assetMerge';

interface RewriteItem {
  id: string;
  name?: string;
  index?: number;
  episode?: number;
  original_prompt?: string;
  rewritten_prompt?: string;
  input_mode?: string;
  duration?: number;
  status?: 'done' | 'failed' | 'pending' | 'running';
  error?: string;
  /** 执行期增量事件正文是否被截断（完整正文以阶段产物为权威） */
  truncated?: boolean;
  continuity?: string;
  grounding?: { described?: boolean; text_only?: boolean; image_hash?: string };
  versions?: Array<{ content?: string; source?: string; created_at?: string }>;
}

/**
 * 分镜产物里单个 episode 的片段条数。
 * 只读 `segments` 一个字段，所以用 unknown + 结构收窄而不是 any：
 * 分镜的完整类型（Episode/Segment/Shot）目前私有在 StoryboardStage.tsx 里未导出，
 * 跨阶段引用会把两个组件不必要地耦合，待后续统一提到 stages/types.ts。
 */
function countEpisodeSegments(episode: unknown): number {
  if (!episode || typeof episode !== 'object') return 0;
  const { segments } = episode as { segments?: unknown };
  return Array.isArray(segments) ? segments.length : 0;
}

/** 状态图标 */
function StatusIcon({ status }: { status?: RewriteItem['status'] }) {
  switch (status) {
    case 'done':
      return <CheckCircle className="w-4 h-4 text-green-500 flex-shrink-0" />;
    case 'failed':
      return <XCircle className="w-4 h-4 text-red-500 flex-shrink-0" />;
    case 'running':
      return <Loader className="w-4 h-4 text-blue-500 animate-spin flex-shrink-0" />;
    default:
      return <AlertCircle className="w-4 h-4 text-amber-500 flex-shrink-0" />;
  }
}

const STATUS_LABEL: Record<string, string> = {
  done: '已完成',
  failed: '失败',
  running: '改写中',
  pending: '待改写',
};

// 未启用判定的权威源：/api/stages 的 prompt_rewrite_enabled。
// 不得拿本地 stageOrder 判定：冷启动瞬间它是默认六阶段，会在启用态误报「未启用」。
// 加载中与请求失败均归为「未知」：不提示未启用（宁可不说，不可说错）。
let rewriteEnabledCache: boolean | null = null;

function usePromptRewriteEnabled(): { known: boolean; enabled: boolean } {
  const [flag, setFlag] = useState<{ known: boolean; enabled: boolean }>(() =>
    rewriteEnabledCache === null
      ? { known: false, enabled: true }
      : { known: true, enabled: rewriteEnabledCache },
  );

  useEffect(() => {
    if (rewriteEnabledCache !== null) return;
    let cancelled = false;
    fetch('/api/stages')
      .then(resp => (resp.ok ? resp.json() : Promise.reject(new Error('stages unavailable'))))
      .then(data => {
        const enabled = Boolean(data?.prompt_rewrite_enabled);
        rewriteEnabledCache = enabled;
        if (!cancelled) setFlag({ known: true, enabled });
      })
      .catch(() => {
        // 拿不到权威答复：保持不提示，执行仍由后端 check_enabled() 拦截
        if (!cancelled) setFlag({ known: false, enabled: true });
      });
    return () => { cancelled = true; };
  }, []);

  return flag;
}

/** 本阶段 artifact 的本地结构（后端动态 JSON，按消费字段声明） */
interface PromptRewriteArtifact {
  items?: RewriteItem[];
}

export default function PromptRewriteStage({
  state,
  artifacts,
  onConfirm,
  onIntervene,
  onRegenerate,
  onUpdateArtifact,
  showConfirm,
  isRunning,
  hasPendingItems,
}: StageViewProps) {
  const artifact = (state.artifact || {}) as PromptRewriteArtifact;
  const items: RewriteItem[] = useMemo(
    () => (Array.isArray(artifact.items) ? artifact.items : []),
    [artifact.items],
  );

  // 编辑草稿（按条目 id）：切换/重生成后以最新产物为准
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [instructions, setInstructions] = useState<Record<string, string>>({});

  // 编辑草稿（按条目 id）：仅清除「正文实际变化」条目的草稿，内容未变条目的草稿保留（I-2）。
  // 清理依据是条目内容签名而非产物对象/数组引用身份：轮询、SSE 增量、后台重生成回填都会
  // 产生新 items 引用，旧实现据此全量清空草稿，用户在其他条目未保存的草稿会被连带清掉。
  // 签名只取 id + rewritten_prompt，truncated/status 等事件视图字段变化不触发清理（D1/2.2）。
  // 渲染期完成同步（替代 effect 内 setState，避免级联渲染）。
  const contentSignature = useMemo(
    () => items.map(i => `${i.id}\u0000${String(i.rewritten_prompt ?? '')}`).join('\n'),
    [items],
  );
  const [draftsSync, setDraftsSync] = useState<{ sig: string; items: RewriteItem[] }>(
    () => ({ sig: contentSignature, items }),
  );
  if (draftsSync.sig !== contentSignature) {
    const prevItems = draftsSync.items;
    setDraftsSync({ sig: contentSignature, items });
    setDrafts(prevDrafts => pruneDraftsByContent(prevDrafts, prevItems, items));
  }

  const pendingCount = items.filter(i => i.status === 'pending').length;
  const failedCount = items.filter(i => i.status === 'failed').length;
  const doneCount = items.filter(i => i.status === 'done').length;

  // 逐分镜串行改写：执行期事件只带条目身份与状态（改写正文随阶段完成才完整），
  // 故用分镜条数 + SSE percent 换算实时条数
  const segmentTotal = useMemo(() => {
    const episodes = (artifacts?.storyboard as { episodes?: unknown[] } | undefined)?.episodes;
    if (!Array.isArray(episodes)) return 0;
    return episodes.reduce((sum: number, episode) => sum + countEpisodeSegments(episode), 0);
  }, [artifacts]);
  const rewrittenCount = segmentTotal
    ? Math.min(segmentTotal, Math.round((segmentTotal * state.progress) / 100))
    : doneCount;

  // 进度条：仅在本阶段执行中展示（与其他阶段一致，按 state.status 而非全局 isRunning 判定）
  const progressNode = state.status === 'running' ? (
    <div className="mb-2">
      <StageProgress
        message={state.progressMessage}
        fallback="正在逐条改写分镜提示词..."
        progress={state.progress}
        color="violet"
      />
      {segmentTotal > 0 && (
        <p className="-mt-3 text-xs text-gray-500">
          已改写 {rewrittenCount}/{segmentTotal} 条（每个分镜串行一次模型调用，全部完成后逐条展示结果）
        </p>
      )}
    </div>
  ) : null;

  const rewriteFlag = usePromptRewriteEnabled();

  // 未启用提示：仅在拿到权威答复且确认关闭时展示；不改 URL、不禁用操作，
  // 后端 check_enabled() 仍是真正的执行拦截。
  const disabledNotice = rewriteFlag.known && !rewriteFlag.enabled ? (
    <div className="mb-4 rounded-2xl border border-amber-200 bg-amber-50 p-4 flex items-start gap-3">
      <AlertCircle className="w-4 h-4 text-amber-500 flex-shrink-0 mt-0.5" />
      <div className="space-y-1 text-xs text-amber-700">
        <p className="font-medium">
          提示词改写阶段未启用：请在设置页「H3 提示词改写」开启 enable，或在 .env 配置
          VC_H3_REWRITE__ENABLE=true
        </p>
        <p className="text-amber-600">
          该阶段为可选注入阶段（启用后插在参考图与视频生成之间）。未启用时在此发起执行会被后端直接拒绝并返回上述开启指引。修改 .env 需执行 docker compose up -d backend 重建才生效（restart 不重读 env_file）；在设置页保存则热生效。
        </p>
      </div>
    </div>
  ) : null;

  const draftOf = (item: RewriteItem) => drafts[item.id] ?? String(item.rewritten_prompt ?? '');

  /**
   * 执行期正文预览说明：正文只随增量事件到达且不落入服务端 shell，
   * 因此“已完成但无正文”是正常中间态（典型为执行中刷新），不得表现为错误归因。
   */
  const previewNote = (item: RewriteItem): string | null => {
    if (item.status !== 'done') return null;
    const body = String(item.rewritten_prompt ?? '').trim();
    if (!body) {
      return '正文尚未随增量事件到达（常见于执行中刷新页面）；阶段完成后将展示完整改写结果。';
    }
    if (item.truncated) {
      return '内容为执行中预览，已超出事件传输上限被截断；阶段完成后替换为完整改写结果。';
    }
    if (state.status === 'running') {
      return '内容为执行中预览，阶段完成后以最终产物为准。';
    }
    return null;
  };

  const isDirty = (item: RewriteItem) => {
    const draft = drafts[item.id];
    return typeof draft === 'string' && draft !== String(item.rewritten_prompt ?? '');
  };

  const saveEdit = (item: RewriteItem) => {
    onUpdateArtifact?.({ items: [{ id: item.id, rewritten_prompt: draftOf(item) }] });
  };

  const regenerateItem = (item: RewriteItem) => {
    onIntervene({ regenerate_items: [item.id] });
  };

  const reviseItem = (item: RewriteItem) => {
    const instruction = (instructions[item.id] || '').trim();
    if (!instruction) return;
    onIntervene({ revise_items: [{ id: item.id, instruction }] });
    setInstructions(prev => ({ ...prev, [item.id]: '' }));
  };

  // 阶段尚未执行
  if (items.length === 0) {
    return (
      <div className="h-full overflow-y-auto p-4 sm:p-6 custom-scrollbar">
        {disabledNotice}
        {progressNode}
        <div className="rounded-2xl border border-gray-200 bg-white p-8 text-center">
          <Wand2 className="w-8 h-8 text-purple-400 mx-auto mb-3" />
          <h3 className="text-sm font-semibold text-gray-700 mb-1">提示词改写</h3>
          <p className="text-xs text-gray-500">
            {state.status === 'running'
              ? `正在串行改写 ${segmentTotal || ''} 个分镜的提示词，请稍候…`
              : state.status === 'pending'
                ? '本阶段会把每个分镜的镜头描述改写为 MiniMax H3 规范的提示词（原生实现：知识资产内嵌 + 本地 LLM 调用，输入模式由视频生成方式与素材角色推导）。请先完成分镜与参考图阶段，然后执行本阶段。'
                : state.status === 'error'
                  ? `执行失败：${state.error || '未知错误'}`
                  : '暂无改写结果'}
          </p>
          {state.status === 'error' && !isRunning && (
            <button
              onClick={() => onRegenerate()}
              className="mt-4 inline-flex items-center gap-1.5 px-4 py-2 bg-blue-500 text-white rounded-lg text-sm font-medium hover:bg-blue-600 transition-colors"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              重新执行
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-4 sm:p-6 space-y-4 custom-scrollbar">
      {disabledNotice}
      {progressNode}
      {/* 汇总条 */}
      <div className="rounded-2xl border border-gray-200 bg-white p-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-gray-500">
        <span className="flex items-center gap-1.5 font-medium text-gray-700">
          <Sparkles className="w-3.5 h-3.5 text-purple-500" />
          提示词改写结果
        </span>
        <span>成功 <span className="text-green-600 font-medium">{doneCount}</span></span>
        {failedCount > 0 && <span>失败 <span className="text-red-600 font-medium">{failedCount}</span></span>}
        {pendingCount > 0 && <span>待改写 <span className="text-amber-600 font-medium">{pendingCount}</span></span>}
        <span className="ml-auto text-gray-400">改写正文为英文（H3 规范），对话与画面文字保留原语言</span>
      </div>

      {/* 条目列表 */}
      <div className="space-y-3">
        {items.map(item => (
          <div key={item.id} className="rounded-2xl border border-gray-200 bg-white p-4 space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <StatusIcon status={item.status} />
              <span className="text-sm font-medium text-gray-800">{item.name || item.id}</span>
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 font-mono">{item.id}</span>
              {typeof item.duration === 'number' && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-50 text-blue-600">{item.duration}s</span>
              )}
              {item.input_mode && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-50 text-purple-600">{item.input_mode}</span>
              )}
              <span className={clsx(
                'ml-auto text-[10px] px-1.5 py-0.5 rounded font-medium',
                item.status === 'done' ? 'bg-green-50 text-green-600'
                  : item.status === 'failed' ? 'bg-red-50 text-red-600'
                  : 'bg-amber-50 text-amber-600',
              )}>
                {STATUS_LABEL[item.status || 'pending']}
              </span>
            </div>

            {/* 原分镜描述摘要 */}
            {item.original_prompt && (
              <p className="text-xs text-gray-400 line-clamp-2 leading-relaxed">
                原描述：{item.original_prompt}
              </p>
            )}
            {item.status === 'failed' && item.error && (
              <p className="text-xs text-red-500">错误：{item.error}</p>
            )}

            {/* 改写结果（可编辑） */}
            <textarea
              value={draftOf(item)}
              disabled={isRunning || item.status === 'pending'}
              onChange={event => setDrafts(prev => ({ ...prev, [item.id]: event.target.value }))}
              rows={Math.min(10, Math.max(3, Math.ceil(draftOf(item).length / 80)))}
              placeholder={draftOf(item) ? '改写后的 H3 提示词' : '改写中的 H3 提示词（完成后展示）'}
              className="w-full rounded-lg border border-gray-200 bg-gray-50/50 px-3 py-2 font-mono text-xs text-gray-700 outline-none focus:border-blue-300 focus:bg-white disabled:opacity-60 resize-y"
            />

            {/* 执行期正文预览/待恢复占位（仅靠事件传输，不入服务端 shell） */}
            {previewNote(item) && (
              <p className="text-xs text-gray-500">{previewNote(item)}</p>
            )}

            {/* 操作区 */}
            <div className="flex flex-wrap items-center gap-2">
              {isDirty(item) && (
                <button
                  onClick={() => saveEdit(item)}
                  disabled={isRunning}
                  className="inline-flex items-center gap-1 px-3 py-1.5 bg-blue-500 text-white rounded-lg text-xs font-medium hover:bg-blue-600 transition-colors disabled:opacity-50"
                >
                  <Save className="w-3 h-3" />
                  保存修改
                </button>
              )}
              <button
                onClick={() => regenerateItem(item)}
                disabled={isRunning}
                className="inline-flex items-center gap-1 px-3 py-1.5 bg-gray-50 border border-gray-200 text-gray-600 rounded-lg text-xs font-medium hover:bg-gray-100 transition-colors disabled:opacity-50"
              >
                <RefreshCw className="w-3 h-3" />
                重新生成
              </button>
              <div className="flex items-center gap-1.5 ml-auto min-w-0">
                <input
                  value={instructions[item.id] || ''}
                  disabled={isRunning || item.status !== 'done'}
                  onChange={event => setInstructions(prev => ({ ...prev, [item.id]: event.target.value }))}
                  onKeyDown={event => { if (event.key === 'Enter') reviseItem(item); }}
                  placeholder="按修改意见修订（如：改成雨天）"
                  className="h-8 w-56 rounded-lg border border-gray-200 bg-white px-2.5 text-xs text-gray-700 outline-none focus:border-blue-300 disabled:opacity-50"
                />
                <button
                  onClick={() => reviseItem(item)}
                  disabled={isRunning || item.status !== 'done' || !(instructions[item.id] || '').trim()}
                  className="inline-flex items-center gap-1 px-3 py-1.5 bg-purple-50 border border-purple-200 text-purple-700 rounded-lg text-xs font-medium hover:bg-purple-100 transition-colors disabled:opacity-50"
                >
                  <Wand2 className="w-3 h-3" />
                  修订
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* 停点操作 */}
      {state.status === 'waiting' && !isRunning && (
        <div className="rounded-2xl border border-gray-200 bg-white p-4 flex flex-wrap items-center gap-3">
          {failedCount > 0 && (
            <span className="flex items-center gap-1.5 text-xs text-amber-600">
              <AlertCircle className="w-3.5 h-3.5" />
              有 {failedCount} 条改写失败，可直接继续（视频阶段将回退默认拼装）或逐条重新生成
            </span>
          )}
          {showConfirm && (
            <button
              onClick={onConfirm}
              className="ml-auto inline-flex items-center gap-1.5 px-5 py-2 bg-blue-500 text-white rounded-lg text-sm font-medium hover:bg-blue-600 transition-colors"
            >
              <CheckCircle className="w-4 h-4" />
              确认改写结果并继续
            </button>
          )}
        </div>
      )}

      {/* 待改写条目提示 */}
      {hasPendingItems && !isRunning && state.status !== 'waiting' && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 flex items-center gap-3">
          <AlertCircle className="w-4 h-4 text-amber-500 flex-shrink-0" />
          <span className="text-xs text-amber-700">有 {pendingCount} 条分镜待改写（分镜内容可能已更新）</span>
          <button
            onClick={() => onRegenerate()}
            disabled={isRunning}
            className="ml-auto inline-flex items-center gap-1.5 px-4 py-1.5 bg-amber-500 text-white rounded-lg text-xs font-medium hover:bg-amber-600 transition-colors disabled:opacity-50"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            重新生成
          </button>
        </div>
      )}
    </div>
  );
}
