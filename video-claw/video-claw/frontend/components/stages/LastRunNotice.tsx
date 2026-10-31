'use client';

import type { ExecutionPreview } from '@/lib/workflowApi';
import { mediaUrl } from '@/lib/basePath';

const TYPE_LABELS: Record<string, string> = {
  characters: '角色',
  settings: '场景设定',
  scenes: '参考图',
  items: '提示词',
  clips: '视频片段',
};

const STATUS_LABELS: Record<string, string> = {
  done: '已完成',
  failed: '失败',
  running: '未完成',
  pending: '未开始',
};

/**
 * 上次未完成执行的只读摘要（execution_preview）。
 * 与正式结果严格区分：不改阶段产物、不推进停点，也不提供“应用/确认”动作。
 */
export default function LastRunNotice({ preview }: { preview: ExecutionPreview }) {
  const items = preview.items || [];
  const done = items.filter(item => item.status === 'done').length;
  const failed = items.filter(item => item.status === 'failed').length;
  const interrupted = preview.status === 'interrupted';

  return (
    <div
      data-testid="last-run-notice"
      className="mx-4 mt-4 rounded-xl border border-slate-300 bg-slate-50 p-4 space-y-2"
    >
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium text-slate-800">
          上次执行{interrupted ? '已中断' : '失败'}（未完成）
        </span>
        <span className="text-slate-600">
          {items.length} 项中 {done} 项已完成{failed > 0 ? `、${failed} 项失败` : ''}
        </span>
      </div>
      <p className="text-xs text-slate-600">
        以下仅为上次执行的条目摘要，未写入正式结果；当前阶段内容仍是最后一次成功结果或你已保存的修改。已完成条目的文件已保留，重新执行本阶段时会作为候选版本加入，不会自动替换当前选择。
      </p>
      {!interrupted && preview.error && (
        <p className="text-xs text-red-600 break-all">失败原因：{preview.error}</p>
      )}
      <details className="text-xs text-slate-700">
        <summary className="cursor-pointer select-none text-slate-600">查看条目</summary>
        <ul className="mt-2 space-y-1">
          {items.map(item => (
            <li key={`${item.type}:${item.id}`} className="flex flex-wrap items-center gap-2">
              <span className="text-slate-500">{TYPE_LABELS[item.type] || item.type}</span>
              <span className="font-mono">{item.id}</span>
              <span className={item.status === 'failed' ? 'text-red-600' : 'text-slate-600'}>
                {STATUS_LABELS[item.status || ''] || item.status || '未知'}
              </span>
              {item.status === 'done' && item.selected && (
                <a
                  href={mediaUrl(item.selected)}
                  target="_blank"
                  rel="noreferrer"
                  className="text-blue-600 hover:underline"
                >
                  查看产物
                </a>
              )}
              {item.error && <span className="text-red-600 break-all">{item.error}</span>}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
