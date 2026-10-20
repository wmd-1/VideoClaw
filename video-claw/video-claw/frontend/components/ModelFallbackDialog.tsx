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

type FallbackCopy = {
  title: string;
  body: string;
  hint: string;
  canUpload: boolean;
};

// 图片阶段文案必须逐字保持不变（openspec: review-fifth-minor-followups M-7）。
const IMAGE_COPY: FallbackCopy = {
  title: '文生图/图生图模型未配置或调用失败',
  body: '你可以为每个条目录上传自己的图片完成本步骤（不依赖模型生成），也可以更换模型后重试。',
  hint: '提示：若生成队列仍在执行，可先点击「停止」——已上传/已完成的条目会保留，随后即可「确认并继续」；重新执行本步骤时，已上传的条目会被自动跳过。',
  canUpload: true,
};

const VIDEO_COPY: FallbackCopy = {
  title: '视频模型未配置或调用失败',
  body: '请更换为可用的视频模型后重试；已完成的片段会保留，重新执行时只跑未成功的片段。',
  hint: '提示：若生成队列仍在执行，可先点击「停止」——已完成的片段会保留，随后即可「确认并继续」；重新执行本步骤时，已完成的片段会被自动跳过。',
  canUpload: false,
};

// 未知阶段使用不指向具体模型的通用文案；不展示 prompt_rewrite 等内部 ID。
const DEFAULT_COPY: FallbackCopy = {
  title: '模型未配置或调用失败',
  body: '当前步骤存在因模型不可用而失败的条目，请检查模型配置或更换模型后重试。',
  hint: '提示：若生成队列仍在执行，可先点击「停止」——已完成的内容会保留，随后即可「确认并继续」。',
  canUpload: false,
};

const STAGE_COPY: Record<string, FallbackCopy> = {
  character_design: IMAGE_COPY,
  reference_generation: IMAGE_COPY,
  video_generation: VIDEO_COPY,
};

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
  const copy = STAGE_COPY[stage] ?? DEFAULT_COPY;
  const stageLabel = STAGE_LABELS[stage] || '模型生成';
  const fieldLabel = detail.field ? FIELD_LABELS[detail.field] || '' : '';
  const canUpload = copy.canUpload;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-2xl bg-white p-5 shadow-2xl"
        onClick={event => event.stopPropagation()}
      >
        <div className="mb-3 flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" />
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-gray-800">{copy.title}</h3>
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
          <p className="mt-1.5 text-amber-600">{copy.body}</p>
          <p className="mt-1.5 text-amber-600">{copy.hint}</p>
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
