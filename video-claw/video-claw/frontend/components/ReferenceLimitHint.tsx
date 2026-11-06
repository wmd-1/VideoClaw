'use client';

import { useEffect, useState } from 'react';
import type { ImageModelCapabilities } from '@/config/models';
import { fetchImageModelCapabilities } from '@/lib/modelRegistry';

/**
 * 图生图参考图上限的发送前非阻塞提示（openspec: custom-image-adapter-fidelity 5.2）。
 *
 * 上限值只来自后端下发的模型声明（capabilities.max_reference_images），前端不猜测默认值；
 * 首页与项目页两处"生成配置"面板共用本组件，避免同一功能多处实现分叉。
 * 已知边界：能力查询只走 `model_type=i2i`（参考图只在图生图链路使用），因此纯 t2i 模型即使声明了
 * 上限也不会在此提示；面板不掌握分段级参考图数量，提示为按声明常驻，逐次实际裁剪以适配层日志为权威。
 * 状态以 {key, caps} 一体写入：effect 内不做无条件 setState（避免渲染期同步 setState 反模式），
 * 且 key 不匹配时不显示旧模型的声明，防止提示与所选模型不一致。
 */
export default function ReferenceLimitHint({ modelId, className }: { modelId: string; className?: string }) {
  const [state, setState] = useState<{ key: string; caps: ImageModelCapabilities | null }>({ key: '', caps: null });

  useEffect(() => {
    if (!modelId) return;
    let cancelled = false;
    fetchImageModelCapabilities(modelId)
      .then(caps => {
        if (!cancelled) setState({ key: modelId, caps });
      })
      .catch(() => {
        if (!cancelled) setState({ key: modelId, caps: null });
      });
    return () => { cancelled = true; };
  }, [modelId]);

  const limit = state.key === modelId ? state.caps?.max_reference_images : undefined;
  if (!limit) return null;

  return (
    <span className={className ?? 'text-[10px] text-amber-600'}>
      该模型声明参考图上限 {limit} 张：超出部分按顺序保留，具体裁剪原因由后端记录
    </span>
  );
}
