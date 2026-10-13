export type StageStatus = 'pending' | 'running' | 'waiting' | 'completed' | 'error' | 'stopped';

export interface StageState {
  status: StageStatus;
  progress: number;
  progressMessage: string;
  /** 后端下发的阶段产物为动态 JSON，各阶段组件按自身结构消费时局部断言 */
  artifact: unknown;
  error: string | null;
}

export interface StageViewProps {
  state: StageState;
  sessionId: string;
  onConfirm: () => void;
  onIntervene: (modifications: Record<string, unknown>) => void;
  onRegenerate: () => void;
  onUpdateArtifact?: (patch: Record<string, unknown>) => void;
  onSaveSelections?: (selections: Record<string, unknown>) => Promise<void>;
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
  /** 剧本阶段的 artifact（用于获取剧集标题） */
  scriptArtifact?: unknown;
}
