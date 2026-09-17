'use client';

import React from 'react';
import { AlertTriangle, RefreshCw, Settings2, Upload, X } from 'lucide-react';
import type { ModelUnavailableDetail } from '@/lib/workflowApi';

const STAGE_LABELS: Record<string, string> = {
  character_design: '角色/场景设计',
  reference_generation: '参考图生成',
};

const FIELD_LABELS: Record<string, string> = {
  image_t2i_model: '文生图模型',
  image_it2i_model: '图生图模型',
};

/** 模型不可用引导弹窗：上传图片 / 更换模型 / 重试 */
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

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-2xl bg-white p-5 shadow-2xl"
        onClick={event => event.stopPropagation()}
      >
        <div className="mb-3 flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" />
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-gray-800">文生图/图生图模型未配置或调用失败</h3>
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
          <p className="mt-1.5 text-amber-600">
            你可以为每个条目录上传自己的图片完成本步骤（不依赖模型生成），也可以更换模型后重试。
          </p>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onUpload}
            className="inline-flex items-center gap-1.5 rounded-xl bg-emerald-500 px-4 py-2 text-xs font-medium text-white shadow-sm hover:bg-emerald-600"
          >
            <Upload className="h-3.5 w-3.5" />
            上传图片
          </button>
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