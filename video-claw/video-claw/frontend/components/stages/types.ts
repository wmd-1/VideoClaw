import type { ExecutionPreview, MissingInput } from '@/lib/workflowApi';

export type StageStatus = 'pending' | 'running' | 'waiting' | 'completed' | 'error' | 'stopped';

export interface StageState {
  status: StageStatus;
  progress: number;
  progressMessage: string;
  /** 后端下发的阶段产物为动态 JSON，各阶段组件按自身结构消费时局部断言 */
  artifact: unknown;
  error: string | null;
  /**
   * 与 status 正交的“本地同步状态”（workflow-poll-timeout-visibility D1）：
   * 轮询耗尽未确认时置 'timed_out'，不改动 status（避免波及所有按状态分支）。
   * 缺省/'synced' 表示同步正常。仅前端内存态、不落盘（刷新后回默认 synced）。
   */
  syncState?: 'synced' | 'timed_out';
  /**
   * 上次未完成（失败/中断）执行的只读摘要，来自 /status 的 execution_preview。
   * 与 artifact（最后一次成功结果/用户工作稿）严格分开展示，不参与确认与停点。
   */
  lastRun?: ExecutionPreview | null;
  /** 本阶段因必需输入不可用而失败时的可定位清单（仅 error 态有效） */
  missingInputs?: MissingInput[] | null;
  /**
   * 后端状态为 `reconfirm`（产物已完成但输入已变，openspec: script-manual-edit-resync）。
   * 本地 status 仍映射为 'waiting'（不改 StageStatus 联合类型），本位仅用于文案区分。
   */
  reconfirm?: boolean;
  /** 本阶段产物可能因上游变更而过期的集号（分镜/参考图/视频；刷新后由 stage_progress 恢复） */
  staleEpisodes?: number[];
  /** 后期阶段专用：成片可能已过期（阶段状态不变，只提示） */
  stale?: boolean;
}

/** 条目卡片上的缺失标记：由失败阶段的 missingInputs 投影到来源/消费阶段 */
export interface FlaggedItem {
  message: string;
  /** 需要处理的来源阶段（与当前阶段不同时提供跳转） */
  sourceStage: string;
}

export interface StageViewProps {
  state: StageState;
  sessionId: string;
  onConfirm: () => void;
  onIntervene: (modifications: Record<string, unknown>) => void;
  onRegenerate: () => void;
  onUpdateArtifact?: (patch: Record<string, unknown>) => void;
  /**
   * 写入成功后的服务端事实回写（openspec: stage-view-resync）：
   * 把响应携带的状态表回写到**所有**阶段视图；`refreshArtifacts` 为真时再取一次
   * 只读快照，重取受本次写入影响的阶段产物（剧本写入需要）。
   */
  onArtifactMutated?: (statusMap?: Record<string, string>, opts?: { refreshArtifacts?: boolean }) => void;
  onSaveSelections?: (selections: Record<string, unknown>) => Promise<void>;
  /** 分镜阶段因剧本正文变更而过期的集号（刷新后仍从 stage_progress 恢复） */
  staleStoryboardEpisodes?: number[];
  /** 成片可能已过期（后期阶段状态不变，仅提示） */
  staleFinalVideos?: boolean;
  /** 显式重跑指定集的分镜（只能由用户点击触发，保存剧本不会自动调用） */
  onRegenerateStoryboardEpisodes?: (episodes: number[]) => void;
  /** 是否显示"确认并继续"按钮（后续阶段已执行过时为 false） */
  showConfirm?: boolean;
  isRunning: boolean;
  /** 是否有待生成的项（阶段2、4、5使用） */
  hasPendingItems?: boolean;
  /** 后续阶段是否已开始（阶段1、3使用） */
  hasNextStageStarted?: boolean;
  /** 用于组件访问其他阶段的数据 */
  artifacts?: Record<string, unknown>;
  /** 参考图阶段的 artifact（仅 VideoStage 使用，用于检查依赖） */
  referenceArtifact?: unknown;
  /**
   * 当前会话的视频生成方式（reference-mode-skip-scene-images）：
   * 参考图页与视频页的跳过形态 MUST 以它为首要判据，`skip` 标记只作辅助证据。
   */
  videoGenerationMode?: string;
  /** 剧本阶段的 artifact（用于获取剧集标题） */
  scriptArtifact?: unknown;
  /** 本阶段需要标红的条目（item id → 缺失说明） */
  flaggedItems?: Record<string, FlaggedItem>;
  /** 跳转到指定阶段（用于“前往重新选择/重生成”） */
  onNavigateStage?: (stage: string) => void;
}
