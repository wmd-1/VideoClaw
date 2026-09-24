'use client';

import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, CheckCircle, Loader, RefreshCw, Save, Sparkles, XCircle, Wand2 } from 'lucide-react';
import clsx from 'clsx';
import type { StageViewProps } from './types';

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
  versions?: Array<{ content?: string; source?: string; created_at?: string }>;
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

export default function PromptRewriteStage({
  state,
  onConfirm,
  onIntervene,
  onRegenerate,
  onUpdateArtifact,
  showConfirm,
  isRunning,
  hasPendingItems,
}: StageViewProps) {
  const artifact = state.artifact || {};
  const items: RewriteItem[] = useMemo(
    () => (Array.isArray(artifact.items) ? artifact.items : []),
    [artifact.items],
  );

  // 编辑草稿（按条目 id）：切换/重生成后以最新产物为准
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [instructions, setInstructions] = useState<Record<string, string>>({});

  useEffect(() => {
    setDrafts({});
  }, [items]);

  const pendingCount = items.filter(i => i.status === 'pending').length;
  const failedCount = items.filter(i => i.status === 'failed').length;
  const doneCount = items.filter(i => i.status === 'done').length;

  const draftOf = (item: RewriteItem) => drafts[item.id] ?? String(item.rewritten_prompt ?? '');
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
      <div className="p-6">
        <div className="rounded-2xl border border-gray-200 bg-white p-8 text-center">
          <Wand2 className="w-8 h-8 text-purple-400 mx-auto mb-3" />
          <h3 className="text-sm font-semibold text-gray-700 mb-1">提示词改写</h3>
          <p className="text-xs text-gray-500">
            {state.status === 'pending'
              ? '本阶段会把每个分镜的镜头描述改写为 MiniMax H3 规范的提示词（通过 Design Agent Platform）。请先完成分镜与参考图阶段，然后执行本阶段。'
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
    <div className="p-6 space-y-4">
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
              placeholder="改写后的 H3 提示词"
              className="w-full rounded-lg border border-gray-200 bg-gray-50/50 px-3 py-2 font-mono text-xs text-gray-700 outline-none focus:border-blue-300 focus:bg-white disabled:opacity-60 resize-y"
            />

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
