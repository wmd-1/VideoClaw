/**
 * Pipeline 入口可用性 / 可见性（纯前端构建期开关）。
 *
 * 两个维度，均由 next.config.ts 读取环境变量后注入（见 resolvePipelineFlags）：
 *   1) 是否开放（未开放 = 点击只弹提示，直接访问 URL 也只看到提示）：
 *      NEXT_PUBLIC_ENABLE_STANDARD（模板制作）、NEXT_PUBLIC_ENABLE_DIGITAL_HUMAN（“其他”入口，原数字人口播）
 *   2) 是否隐藏（隐藏 = 侧边栏不出现该入口）：
 *      NEXT_PUBLIC_HIDE_ACTION_TRANSFER（动作迁移）
 * 取值顺序：构建进程环境 > 仓库根 .env。改完 .env 后必须重新构建前端产物
 * （npm run build + 重组 .next-runtime）才生效，运行时不读 .env。
 */

/** 未开放 Pipeline 的点击提示文案（各入口共用） */
export const PIPELINE_UNAVAILABLE_MESSAGE = '该功能暂未开发，敬请期待！';

function flagEnabled(value: string | undefined): boolean {
  const normalized = String(value ?? '').trim().toLowerCase();
  return normalized === 'true' || normalized === '1' || normalized === 'yes';
}

/** “是否隐藏”类开关：缺省隐藏，仅显式 false/0/no 时显示 */
function flagHidden(value: string | undefined): boolean {
  const normalized = String(value ?? '').trim().toLowerCase();
  return normalized !== 'false' && normalized !== '0' && normalized !== 'no';
}

/** 未开放的 pipeline（默认关闭；需显式开启才可用） */
const PIPELINE_AVAILABILITY: Record<string, boolean> = {
  standard: flagEnabled(process.env.NEXT_PUBLIC_ENABLE_STANDARD),
  digital_human: flagEnabled(process.env.NEXT_PUBLIC_ENABLE_DIGITAL_HUMAN),
};

/** 需要从导航中隐藏的 pipeline（默认隐藏；需显式关闭隐藏才出现） */
const HIDDEN_PIPELINES: Record<string, boolean> = {
  action_transfer: flagHidden(process.env.NEXT_PUBLIC_HIDE_ACTION_TRANSFER),
};

/** 该 pipeline 入口是否可用；未登记的 id 视为可用（不影响既有流程） */
export function isPipelineAvailable(pipeline: string): boolean {
  return PIPELINE_AVAILABILITY[pipeline] !== false;
}

/** 该 pipeline 入口是否应从导航中隐藏；未登记的 id 视为不隐藏 */
export function isPipelineHidden(pipeline: string): boolean {
  return HIDDEN_PIPELINES[pipeline] === true;
}
