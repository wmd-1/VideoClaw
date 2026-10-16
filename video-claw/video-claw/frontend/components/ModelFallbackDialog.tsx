'use client';

import React from 'react';
import { AlertTriangle, RefreshCw, Settings2, Upload, X } from 'lucide-react';
import type { ModelUnavailableDetail } from '@/lib/workflowApi';

const STAGE_LABELS: Record<string, string> = {
  character_design: '角色/场景设计',
  reference_generation: '参考图生成',
  video_generation: '视频生成',
};

const FIELD_LABELS: Record<string, string> = {
  image_t2i_model: '文生图模型',
  image_it2i_model: '图生图模型',
  video_first_frame_model: '首帧视频模型',
  video_start_end_model: '首尾帧视频模型',
  video_reference_model: '参考图生视频模型',
  video_model: '视频模型',
};

// 仅图片阶段可用“上传图片”兜底（按阶段配置表达，非根据回调存在与否隐式推断）；
// 视频阶段条目无上传入口，且上传单图不能替代不可用的视频模型。
const UPLOAD_CAPABLE_STAGES = ['character_design', 'reference_generation'];

/** 模型不可用引导弹窗：上传图片（仅图片阶段）/ 更换模型 / 重试 */
export default function ModelFallbackDialog({
  detail,
  stage,
  onUpload,
  onChangeModel,
  onRetry,
  onClose,
}: {
  detail: ModelUnavailableDetail;
  stage: string;
  onUpload: () => void;
  onChangeModel: () => void;
  onRetry: () => void;
  onClose: () => void;
}) {
  const stageLabel = STAGE_LABELS[stage] || stage;
  const fieldLabel = detail.field ? FIELD_LABELS[detail.field] || detail.field : '';
  const canUpload = UPLOAD_CAPABLE_STAGES.includes(stage);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-2xl bg-white p-5 shadow-2xl"
        onClick={event => event.stopPropagation()}
      >
        <div className="mb-3 flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" />
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-gray-800">
              {canUpload ? '文生图/图生图模型未配置或调用失败' : '视频模型未配置或调用失败'}
            </h3>
            <p className="mt-0.5 text-xs text-gray-500">
              {stageLabel}
              {fieldLabel ? ` · ${fieldLabel}` : ''}
              {detail.model ? `：${detail.model}` : ''}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="rounded-xl bg-amber-50/70 p-3 text-xs leading-relaxed text-amber-700">
          <p>原因：{detail.reason || '模型不可用'}</p>
          {canUpload ? (
            <p className="mt-1.5 text-amber-600">
              你可以为每个条目录上传自己的图片完成本步骤（不依赖模型生成），也可以更换模型后重试。
            </p>
          ) : (
            <p className="mt-1.5 text-amber-600">
              请更换为可用的视频模型后重试；已完成的片段会保留，重新执行时只跑未成功的片段。
            </p>
          )}
          <p className="mt-1.5 text-amber-600">
            {canUpload
              ? '提示：若生成队列仍在执行，可先点击「停止」——已上传/已完成的条目会保留，随后即可「确认并继续」；重新执行本步骤时，已上传的条目会被自动跳过。'
              : '提示：若生成队列仍在执行，可先点击「停止」——已完成的片段会保留，随后即可「确认并继续」；重新执行本步骤时，已完成的片段会被自动跳过。'}
          </p>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          {canUpload && (
            <button
              type="button"
              onClick={onUpload}
              className="inline-flex items-center gap-1.5 rounded-xl bg-emerald-500 px-4 py-2 text-xs font-medium text-white shadow-sm hover:bg-emerald-600"
            >
              <Upload className="h-3.5 w-3.5" />
              上传图片
            </button>
          )}
          <button
            type="button"
            onClick={onChangeModel}
            className="inline-flex items-center gap-1.5 rounded-xl bg-gray-100 px-4 py-2 text-xs font-medium text-gray-700 hover:bg-gray-200"
          >
            <Settings2 className="h-3.5 w-3.5" />
            更换模型
          </button>
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex items-center gap-1.5 rounded-xl bg-gray-100 px-4 py-2 text-xs font-medium text-gray-700 hover:bg-gray-200"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            重试
          </button>
        </div>
      </div>
    </div>
  );
}