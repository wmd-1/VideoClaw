'use client';

import { useEffect, useState } from 'react';
import type { ProviderGroup } from '@/config/models';
import { withSlotPlaceholder } from '@/lib/modelRegistry';

/**
 * 带「Provider 分组的 <select>」，并为不可用的当前值注入置顶占位项。
 *
 * 当 value 不在按类型/能力过滤后的 providers 中时，异步向 /api/model_availability
 * 查询并给出「未注册 / 不可用:<原因> / 不适用于此模式」的可见占位，避免下拉静默
 * 空白或回落到列表首项。占位项与正常项共存，保留用户/历史会话的选择；据占位值
 * 发起生成时，仍由既有工作流预检 409 兜底弹窗处理。
 *
 * 未命中过滤条件时（value 已在 providers 中），行为与原始 <select> 完全一致。
 */
export default function ProviderSelect({
  value,
  providers,
  onChange,
  className,
}: {
  value: string;
  providers: ProviderGroup[];
  onChange: (val: string) => void;
  className?: string;
}) {
  // 同步预置占位（避免首帧闪空白），随后异步替换为带原因的准确标签
  const [resolved, setResolved] = useState<ProviderGroup[]>(() =>
    applySyncPlaceholder(providers, value),
  );

  useEffect(() => {
    let cancelled = false;
    setResolved(applySyncPlaceholder(providers, value));
    withSlotPlaceholder(providers, value)
      .then(groups => {
        if (!cancelled) setResolved(groups);
      })
      .catch(() => {
        /* withSlotPlaceholder 内部已消化异常，此处仅防御 */
      });
    return () => {
      cancelled = true;
    };
  }, [providers, value]);

  return (
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      className={className}
    >
      {resolved.map(pg => (
        <optgroup key={pg.provider} label={pg.label}>
          {pg.models.map(m => (
            <option key={m.id} value={m.id}>{m.label}</option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}

function applySyncPlaceholder(groups: ProviderGroup[], value: string): ProviderGroup[] {
  const id = String(value ?? '').trim();
  if (!id) return groups;
  const exists = groups.some(g => g.models.some(m => m.id === id));
  if (exists) return groups;
  if (groups.some(g => g.provider === '__unavailable__')) return groups;
  return [
    { provider: '__unavailable__', label: '不可用', models: [{ id, label: `${id}（检查可用性…）` }] },
    ...groups,
  ];
}
