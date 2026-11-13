/**
 * 轻量字符级 diff：用于剧本质检弹窗标出新增/删除片段。
 *
 * 单条建议通常只有几十到几百字符；超过 250k 组合时退化为
 * “公共前缀 + 中段整体替换 + 公共后缀”，避免弹窗卡顿。
 */
export type DiffPartType = 'same' | 'add' | 'remove';

export interface DiffPart {
  type: DiffPartType;
  text: string;
}

function mergeChars(chars: Array<{ type: DiffPartType; char: string }>): DiffPart[] {
  const parts: DiffPart[] = [];
  for (const item of chars) {
    const last = parts[parts.length - 1];
    if (last && last.type === item.type) {
      last.text += item.char;
    } else {
      parts.push({ type: item.type, text: item.char });
    }
  }
  return parts;
}

function prefixSuffixDiff(a: string[], b: string[]): DiffPart[] {
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1;

  let endA = a.length - 1;
  let endB = b.length - 1;
  while (endA >= start && endB >= start && a[endA] === b[endB]) {
    endA -= 1;
    endB -= 1;
  }

  const parts: DiffPart[] = [];
  if (start > 0) {
    parts.push({ type: 'same', text: a.slice(0, start).join('') });
  }
  if (endA >= start) {
    parts.push({ type: 'remove', text: a.slice(start, endA + 1).join('') });
  }
  if (endB >= start) {
    parts.push({ type: 'add', text: b.slice(start, endB + 1).join('') });
  }
  if (endA + 1 < a.length) {
    parts.push({ type: 'same', text: a.slice(endA + 1).join('') });
  }
  return parts;
}

export function diffText(before: string, after: string): DiffPart[] {
  const a = Array.from(before || '');
  const b = Array.from(after || '');
  if (!a.length) {
    return b.length ? [{ type: 'add', text: b.join('') }] : [];
  }
  if (!b.length) {
    return [{ type: 'remove', text: a.join('') }];
  }
  if (a.length * b.length > 250_000) {
    return prefixSuffixDiff(a, b);
  }

  const n = a.length;
  const m = b.length;
  const dp: Uint16Array[] = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = 1; i <= n; i += 1) {
    for (let j = 1; j <= m; j += 1) {
      dp[i][j] = a[i - 1] === b[j - 1]
        ? dp[i - 1][j - 1] + 1
        : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }

  const chars: Array<{ type: DiffPartType; char: string }> = [];
  let i = n;
  let j = m;
  while (i > 0 && j > 0) {
    if (a[i - 1] === b[j - 1]) {
      chars.push({ type: 'same', char: a[i - 1] });
      i -= 1;
      j -= 1;
    } else if (dp[i - 1][j] >= dp[i][j - 1]) {
      chars.push({ type: 'remove', char: a[i - 1] });
      i -= 1;
    } else {
      chars.push({ type: 'add', char: b[j - 1] });
      j -= 1;
    }
  }
  while (i > 0) {
    i -= 1;
    chars.push({ type: 'remove', char: a[i] });
  }
  while (j > 0) {
    j -= 1;
    chars.push({ type: 'add', char: b[j] });
  }
  chars.reverse();
  return mergeChars(chars);
}
