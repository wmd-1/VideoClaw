'use client';

import React, { useState, useRef, useEffect, useCallback } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import {
  startProject,
  executeStage,
  parseStreamEvents,
  getProjectStatus,
  getProjectStatusFromDisk,
  continueWorkflow,
  stopProject,
  intervene,
  getArtifact,
  fetchSessions,
  updateModels,
  deleteSession,
  saveSelections,
  ModelUnavailableError,
  type ModelUnavailableDetail,
  type ProjectStatus,
} from '@/lib/workflowApi';
import TopBar, { STAGES, useEnabledStages, type ModelConfig } from './TopBar';
import {
  applyAssetUpdateToArtifact,
  preserveRewriteBodies,
  type ArtifactLike,
} from '@/lib/assetMerge';
import ModelFallbackDialog from './ModelFallbackDialog';
import HomePage, { type ProjectParams } from './HomePage';
import {
  ScriptStage,
  CharacterStage,
  StoryboardStage,
  ReferenceStage,
  PromptRewriteStage,
  VideoStage,
  PostProductionStage,
  type StageState,
  type StageStatus,
  type StageViewProps,
} from './stages';

// 全量阶段（含 prompt_rewrite）；实际驱动/渲染的集合由 useEnabledStages 按后端开关过滤
const ALL_stageOrder: string[] = STAGES.map(s => s.id);

const STAGE_COMPONENTS: Record<string, React.ComponentType<StageViewProps>> = {
  script_generation: ScriptStage,
  character_design: CharacterStage,
  storyboard: StoryboardStage,
  reference_generation: ReferenceStage,
  prompt_rewrite: PromptRewriteStage,
  video_generation: VideoStage,
  post_production: PostProductionStage,
};

interface HistoryItem {
  id: string;
  idea: string;
  style?: string;
  date: string;
  status: string;
  stages?: Record<string, string>; // stageId => status 映射
}

/** 轮询/恢复时的项目状态快照（ProjectStatus + 内存 artifacts + 会话 meta） */
interface ProjectStatusSnapshot extends ProjectStatus {
  artifacts?: Record<string, unknown>;
  meta?: SessionMeta;
}

/** 会话级生成参数（status.meta 或旧扁平字段，按恢复逻辑实际读取字段声明） */
interface SessionMeta {
  idea?: string;
  user_textbox_input?: string;
  style?: string;
  video_ratio?: string;
  video_resolution?: string;
  video_duration?: string | number;
  video_fps?: string | number;
  llm_model?: string;
  vlm_model?: string;
  image_t2i_model?: string;
  image_it2i_model?: string;
  video_generation_mode?: string;
  video_first_frame_model?: string;
  video_start_end_model?: string;
  video_reference_model?: string;
  video_model?: string;
  expand_idea?: boolean;
  enable_concurrency?: boolean;
  web_search?: boolean;
  [key: string]: unknown;
}

/** 阶段产物中的条目（角色/场景/片段等），按消费字段声明 */
interface ArtifactItem {
  id?: string;
  selected?: string;
  description?: string;
  status?: string;
  versions?: unknown;
  [key: string]: unknown;
}

/** 分镜保存时的 segment 结构（按 handleSaveSelections 消费字段声明） */
interface SegmentLike {
  segment_id: string;
  is_new?: boolean;
  total_duration?: number | string;
  shots?: Array<{ visual_prompt?: string; plot?: string; content?: string; duration?: number | string }>;
  [key: string]: unknown;
}

/**
 * 会话归属令牌（openspec: frontend-session-state-isolation / design D1、D9）。
 * 令牌必须在**执行链启动时取一次并贯穿全链**：在写入点现读 ownerRef 会让守卫退化为永真，
 * 每条 SSE 事件重取则会让该次写入自我作废。
 */
interface SessionToken {
  sid: string | null;
  gen: number;
}

function initStageStates(): Record<string, StageState> {
  const states: Record<string, StageState> = {};
  for (const s of ALL_stageOrder) {
    states[s] = {
      status: 'pending',
      progress: 0,
      progressMessage: '',
      artifact: null,
      error: null,
    };
  }
  return states;
}

function hasDoneArtifactItems(artifact: unknown): boolean {
  if (!artifact) return false;
  const art = artifact as Record<string, unknown>;
  return ['clips', 'images', 'scenes', 'characters', 'settings'].some(key => {
    const items = art[key];
    return Array.isArray(items) && items.some((item: unknown) => {
      const entry = item as Record<string, unknown> | null;
      return entry?.status === 'done' || Boolean(entry?.selected);
    });
  });
}

/**
 * 产物条目里是否存在“正在运行”的条目（用于阶段本身非 running 时的后台单条重生成）。
 * `items` 是提示词改写阶段专用的列表键（其余阶段的 `items` 只出现在 segment 内部，不是顶层键），
 * 按阶段限定避免误改动其他阶段的轮询启动条件。
 */
function hasRunningArtifactItems(artifact: unknown, stageId?: string): boolean {
  if (!artifact) return false;
  const art = artifact as Record<string, unknown>;
  const keys = stageId === 'prompt_rewrite'
    ? ['items']
    : ['clips', 'images', 'scenes', 'characters', 'settings'];
  return keys.some(key => {
    const items = art[key];
    return Array.isArray(items) && items.some((item: unknown) => (item as Record<string, unknown> | null)?.status === 'running');
  });
}

function getStageProgressSnapshot(status: unknown, stageId: string): Partial<StageState> {
  const stageProgress = (status as { stage_progress?: Record<string, unknown> } | null | undefined)?.stage_progress;
  const snapshot = stageProgress?.[stageId] as { percent?: unknown; message?: unknown; step?: unknown } | undefined;
  if (!snapshot || typeof snapshot !== 'object') return {};
  const progress = typeof snapshot.percent === 'number'
    ? Math.max(0, Math.min(100, Math.round(snapshot.percent)))
    : undefined;
  const progressMessage = typeof snapshot.message === 'string' && snapshot.message.trim()
    ? snapshot.message
    : (typeof snapshot.step === 'string' ? snapshot.step : undefined);
  return {
    ...(typeof progress === 'number' ? { progress } : {}),
    ...(progressMessage ? { progressMessage } : {}),
  };
}

export default function WorkflowPanel() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [activeStage, setActiveStage] = useState<string | null>(null);
  const [stageStates, setStageStates] = useState<Record<string, StageState>>(initStageStates());
  const [isRunning, setIsRunning] = useState(false);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [projectParams, setProjectParams] = useState<ProjectParams | null>(null);
  const [autoMode, setAutoMode] = useState(false);
  // 模型不可用（预检 409 / 运行期失败）兜底弹窗状态
  const [modelFallback, setModelFallback] = useState<{ detail: ModelUnavailableDetail; stage: string } | null>(null);
  // 用于顶栏流程图状态判断
  const [currentStageFromSession, setCurrentStageFromSession] = useState<string | null>(null);
  const [completedStagesFromSession, setCompletedStagesFromSession] = useState<string[]>([]);
  // 顶栏流程图用的后端阶段状态映射（提前声明：pollForCompletion 等闭包先于其使用点定义）
  const [globalStatusMap, setGlobalStatusMap] = useState<Record<string, string>>({});
  // 会话载入空态与失败态（design D5）：两者与 showHome 的优先级固定为
  // sessionLoadError > sessionLoading > showHome，否则复位置空 activeStage 会闪一下首页。
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionLoadError, setSessionLoadError] = useState<string | null>(null);
  // 超时态“停止任务”失败反馈（后端不可达时保留横幅不静默清除，tasks 4.3）
  const [timeoutStopError, setTimeoutStopError] = useState<string | null>(null);
  /**
   * 执行链并发闸门。与 `isRunning` **必须分开**：isRunning 是会话视图态（切会话时要复位，
   * 否则"执行中/停止"会当成上一会话的残留跟着走），而本 ref 跟的是"本标签页有没有链在跑"，
   * 不能随视图复位。若继续用 isRunning 做闸门，切走会话会误将闸门打开，允许开第二条链。
   */
  const chainRunningRef = useRef(false);
  // 后端启用的阶段列表：禁用提示词改写时为原六阶段（不渲染、不驱动该阶段）
  const stageOrder = useEnabledStages();

  const abortRef = useRef<AbortController | null>(null);
  const stoppedRef = useRef(false);
  const autoModeRef = useRef(autoMode);
  const pollRef = useRef<Set<string>>(new Set());
  // 会话归属真源：sid + 装载代数。任何会话态写入都要带上链启动时的令牌。
  const ownerRef = useRef<SessionToken>({ sid: null, gen: 0 });
  // sessionId 的同步镜像：异步闭包读 state 会拿到旧值，归属判定一律走 ref。
  const sessionRef = useRef<string | null>(null);

  useEffect(() => {
    autoModeRef.current = autoMode;
  }, [autoMode]);

  const handleAutoModeChange = useCallback((nextAutoMode: boolean) => {
    autoModeRef.current = nextAutoMode;
    setAutoMode(nextAutoMode);
  }, []);

  // 清理轮询
  useEffect(() => {
    return () => { pollRef.current.clear(); };
  }, []);

  // 领取新归属：代数自增，使此前所有在途异步写入自动失效。MUST 与 sessionId 同时发生。
  const enterSession = useCallback((sid: string | null) => {
    const token: SessionToken = { sid, gen: ownerRef.current.gen + 1 };
    ownerRef.current = token;
    sessionRef.current = sid;
    setSessionId(sid);
    return token;
  }, []);

  // 令牌是否仍代表当前归属（sid 与 gen 双等；单看 sid 不足以判定，见 design D2）
  const owns = useCallback((token: SessionToken): boolean => {
    return token.sid === ownerRef.current.sid && token.gen === ownerRef.current.gen;
  }, []);

  // 当前归属快照：仅供**链启动时**取令牌使用，绝不得出现在写入点（design D9）
  const currentToken = useCallback((): SessionToken => ownerRef.current, []);

  // 阶段状态更新（提前声明：pollForCompletion / handleResumeProject 等闭包先于其使用点定义）
  const updateStageState = useCallback((stageId: string, update: Partial<StageState>) => {
    setStageStates(prev => {
      const current = prev[stageId];
      const nextUpdate = { ...update };
      // 改写阶段：轮询/状态回填的整体覆盖不得冲掉已收到的增量正文（仅限定本阶段）
      if (stageId === 'prompt_rewrite' && update.artifact) {
        nextUpdate.artifact = preserveRewriteBodies(current?.artifact as ArtifactLike, update.artifact as ArtifactLike);
      }
      if (
        current?.status === 'running' &&
        typeof update.progress === 'number' &&
        update.progress < (current.progress || 0) &&
        update.status !== 'completed' &&
        update.status !== 'waiting'
      ) {
        nextUpdate.progress = current.progress;
      }
      return {
        ...prev,
        [stageId]: { ...current, ...nextUpdate },
      };
    });
  }, []);

  /**
   * 漏斗：所有阶段状态写入 MUST 经过此处（design D1）。
   * 只丢弃**状态写入**；因令牌失效而改变控制流的只有轮询循环本身（design D11）。
   */
  const commitStageState = useCallback((token: SessionToken, stageId: string, update: Partial<StageState>) => {
    if (!owns(token)) return;
    updateStageState(stageId, update);
  }, [owns, updateStageState]);

  // 漏斗：SSE 增量合并、手动停止回填等函数式整体更新也要带令牌
  const commitStageStates = useCallback((
    token: SessionToken,
    updater: (prev: Record<string, StageState>) => Record<string, StageState>,
  ) => {
    if (!owns(token)) return;
    setStageStates(prev => updater(prev));
  }, [owns]);

  /**
   * 漏斗：会话视图态（顶栏状态映射、当前/已完成阶段、会话级生成参数等）的带归属写入。
   * MUST NOT 包含 setIsRunning / stoppedRef——它们跟的是"本标签页有没有在跑的任务"，不是"哪个会话的数据"（design D11）。
   */
  const commitSessionView = useCallback((token: SessionToken, view: {
    globalStatusMap?: Record<string, string>;
    currentStage?: string | null;
    completedStages?: string[] | ((prev: string[]) => string[]);
    projectParams?: ProjectParams | null;
    activeStage?: string | null;
  }) => {
    if (!owns(token)) return;
    if (view.globalStatusMap) setGlobalStatusMap(view.globalStatusMap);
    if (view.currentStage !== undefined) setCurrentStageFromSession(view.currentStage);
    // 允许函数式更新：长执行链里多阶段累加完成记录时，必须基于最新 prev 而非启动时的快照
    const nextCompleted = view.completedStages;
    if (nextCompleted !== undefined) {
      setCompletedStagesFromSession(prev => (typeof nextCompleted === 'function' ? nextCompleted(prev) : nextCompleted));
    }
    if (view.projectParams !== undefined) setProjectParams(view.projectParams);
    if (view.activeStage !== undefined) setActiveStage(view.activeStage);
  }, [owns]);

  /**
   * 统一的会话态复位入口（design D3/D4）：四个退出路径共用。
   * 只清前端视图与本地轮询订阅；MUST NOT abort SSE、MUST NOT 调 stopProject——
   * 阶段执行跑在 SSE 请求生命周期内的同进程协程，中断读取会杀死后端正在跑的阶段。
   */
  const resetSessionView = useCallback((nextSid: string | null): SessionToken => {
    const token = enterSession(nextSid);
    // 清掉所有在途轮询 key：各循环下一 tick 的 has(key) 检查会自然退出
    pollRef.current.clear();
    setStageStates(initStageStates());
    setGlobalStatusMap({});
    setCurrentStageFromSession(null);
    setCompletedStagesFromSession([]);
    setProjectParams(null);
    setModelFallback(null);
    setActiveStage(null);
    setIsRunning(false);
    stoppedRef.current = false;
    // 载入态必须一并清除：离开会话时，在途的 resume 会因令牌失效静默退出而不清这两个标志，
    // 而渲染优先级是 sessionLoading > showHome，残留会把首页永久顶成"正在载入"。
    setSessionLoading(false);
    setSessionLoadError(null);
    return token;
  }, [enterSession]);

  // 轮询等待后端阶段完成。运行中优先读后端内存，后端重启后再回退到磁盘快照。
  const pollForCompletion = useCallback(async (sid: string, stageId: string) => {
    const key = `${sid}:${stageId}`;
    // 入口去重守卫（design D10）：既有代码只有退出侧 has() 检查，重复调用会叠加并发循环
    if (pollRef.current.has(key)) return;
    pollRef.current.add(key);
    // 轮询在调用入门时取当前归属快照作为贯穿整轮轮询的令牌（design D9）
    const token = currentToken();
    // 进入一轮同步：清除上一轮可能遗留的超时态（“重新查询”复用同一函数时）。
    commitStageState(token, stageId, { syncState: 'synced' });
    for (let i = 0; i < 300; i++) {  // 增加轮询次数（最多10分钟）
      await new Promise(r => setTimeout(r, 2000));
      if (!pollRef.current.has(key)) return;
      try {
        let status: ProjectStatusSnapshot;
        try {
          status = await getProjectStatus(sid);
        } catch {
          status = (await getProjectStatusFromDisk(sid)) as ProjectStatusSnapshot;
        }
        if (!pollRef.current.has(key)) return;
        // getProjectStatus 是一个 await，返回后归属可能已变；先校令牌再落写入（design D1/D11）
        if (!owns(token)) return;
        commitSessionView(token, { globalStatusMap: status.status || {} });
        const done = Object.keys(status.status || {}).filter(k => ["completed", "session_completed"].includes(status.status[k]));
        const currentStageStatus = status.status?.[stageId] || 'idle';
        const artifacts = status.artifacts || {};
        const currentArtifact = artifacts[stageId];
        const hasRunningItems = hasRunningArtifactItems(currentArtifact, stageId);
        if (done.includes(stageId) && !hasRunningItems) {
          const isWait = currentStageStatus === 'waiting' && status.current_stage === stageId;
          let artifact: unknown = null;
          try { artifact = (await getArtifact(sid, stageId)).artifact; } catch {}
          // 此处多了一个 await：取完产物后必须重新校归属，否则迟到的旧会话会覆盖当前会话
          if (!owns(token)) return;
          commitStageState(token, stageId, {
            status: isWait ? 'waiting' : 'completed',
            progress: 100,
            progressMessage: isWait ? '等待确认' : '已完成',
            artifact,
          });
          pollRef.current.delete(key);
          return;
        }
        if (currentStageStatus === 'error' && !hasRunningItems) {
          commitStageState(token, stageId, {
            status: 'error',
            error: status.error || '执行出错',
            progressMessage: '执行失败',
          });
          pollRef.current.delete(key);
          return;
        }
        if (currentStageStatus === 'stopped' && !hasRunningItems) {
          commitStageState(token, stageId, {
            status: 'stopped',
            error: '手动停止',
            progressMessage: '已停止',
          });
          pollRef.current.delete(key);
          return;
        }
        // 后台单卡重生成时，阶段状态可能仍是 waiting/completed；只要 item 还在 running 就继续轮询。
        if (currentStageStatus !== 'running' && !hasRunningItems) {
          commitStageState(token, stageId, {
            status: currentStageStatus === 'waiting'
              ? 'waiting'
              : (done.includes(stageId) ? 'completed' : 'pending'),
            progress: done.includes(stageId) ? 100 : undefined,
            progressMessage: currentStageStatus === 'waiting'
              ? '等待确认'
              : (done.includes(stageId) ? '已完成' : undefined),
            artifact: currentArtifact || null,
          });
          pollRef.current.delete(key);
          return;
        }
        // 更新进度信息（优先使用后端持久化的 stage_progress 快照）
        const update: Partial<StageState> = {
          status: 'running',
          ...getStageProgressSnapshot(status, stageId),
        };

        // 无论何种状态，只要有数据就更新 artifact
        if (currentArtifact) {
          update.artifact = currentArtifact;
        }

        if (!update.progressMessage && currentArtifact) {
          // 检查是否有已生成的参考图/视频/分镜
          const art = currentArtifact as Record<string, unknown>;
          const hasNonEmptyList = (key: string): boolean => {
            const list = art[key];
            return Array.isArray(list) && list.length > 0;
          };
          const hasNestedList = (key: string, nestedKey: string): boolean => {
            const list = art[key];
            return Array.isArray(list) && list.some((item: unknown) => {
              const nested = (item as Record<string, unknown> | null)?.[nestedKey];
              return Array.isArray(nested) && nested.length > 0;
            });
          };
          const hasProgress = hasNestedList('scenes', 'versions') ||
                             hasNestedList('clips', 'versions') ||
                             hasNestedList('episodes', 'segments') ||
                             hasNonEmptyList('characters') ||
                             hasNonEmptyList('shots');
          if (hasProgress) {
            update.progressMessage = '执行中...（已生成部分资源）';
          } else {
            update.progressMessage = '执行中...';
          }
        } else if (!update.progressMessage) {
          update.progressMessage = '执行中...';
        }

        if (Object.keys(update).length > 0) {
          commitStageState(token, stageId, update);
        }
      } catch { /* retry */ }
    }
    // 循环自然耗尽：阶段仍停在 running 未推进 → 进入与“执行中/失败”都不同的“状态同步超时”正交态（D1/D2）。
    // 不改动 status、不判失败；仅叠加 syncState，使面板/顶栏显示“尚未确认”横幅与恢复动作。
    if (owns(token)) {
      commitStageState(token, stageId, { syncState: 'timed_out' });
    }
    pollRef.current.delete(key);
  }, [commitSessionView, commitStageState, currentToken, owns]);

  // ── 恢复历史项目 ──
  const handleResumeProject = async (sid: string, targetStage: string | null = null) => {
    // 已经在展示同一个会话（归属未被别的会话接管）：只移指针，不取数也不复位。
    // 本分支还承担两个职责（design D7）：会话内阶段点击的快路径，以及截断
    // "resume → router.push → URL effect → resume" 的自激回路。
    // 判定走 sessionRef 而非 sessionId state：异步闭包读 state 会拿到旧值。
    // MUST NOT 在失败态下走早退：否则"载入失败 → 重试"会被当成同会话导航而跳过取数（design D5/D7 的交叉点）。
    if (sid === sessionRef.current && !sessionLoadError) {
      const runningStage = stageOrder.find(s => stageStates[s]?.status === 'running');
      const waitingStage = stageOrder.find(s => stageStates[s]?.status === 'waiting');
      const lastCompleted = [...stageOrder].reverse().find(s => stageStates[s]?.status === 'completed');
      setActiveStage(targetStage || runningStage || waitingStage || lastCompleted || stageOrder[0]);
      // 这里 MUST NOT 清 sessionLoading：handleResumeProject 自己会 router.push 改变 searchParams，
      // 从而触发本 effect 第二次进入并命中此早退；若在此清标志，首次尚未完成的取数会被
      // "空阶段面板"提前顶掉载入态（实测：人为拖慢 20s 时，“正在载入”完全不出现）。
      // 载入态的置/清只属于完整取数路径（成功末尾 / catch / resetSessionView）。
      return;
    }

    // 先清后拉（tasks 2.3）：复位上一会话的视图态并领取新归属令牌（含 enterSession 写 sessionId），
    // 使 await 空窗呈现"该会话尚无数据"而不是上一个会话的数据。
    // 不得在此之外再写裸的 setSessionId（tasks 1.2）。
    const token = resetSessionView(sid);
    setSessionLoading(true);
    setSessionLoadError(null);
    // 更新 URL 参数（包含阶段）
    const stageParam = targetStage ? `&stage=${targetStage}` : '';
    router.push(`/?session=${sid}${stageParam}`);
    try {
      let status: ProjectStatusSnapshot;
      try {
        status = await getProjectStatus(sid);
      } catch {
        status = (await getProjectStatusFromDisk(sid)) as ProjectStatusSnapshot;
      }
      // 取数后归属可能已变（用户又切了一次）：迟到响应直接放弃，不写界面
      if (!owns(token)) return;
      commitSessionView(token, { globalStatusMap: status.status || {} });
      const newStates = initStageStates();
      const stMap = status.status || {};
      const allStatusStages = Object.keys(stMap);
      const currentStage = status.current_stage;

      // 保存到 state 供顶栏流程图使用
      const completedStages = allStatusStages.filter(k => ["completed", "session_completed"].includes(stMap[k]));
      commitSessionView(token, { currentStage: currentStage || null, completedStages });

      // 仅处理已知工作流阶段（按全量已知集合，覆盖尚未 resolve 的可选注入阶段；
      // status map 里的 init 等内部键不在 ALL_stageOrder 中，仍会被跳过避免无谓 404）
      for (const sName of allStatusStages.filter(name => ALL_stageOrder.includes(name))) {
        const cStatus = stMap[sName];
        if (cStatus === 'pending' || cStatus === 'idle') {
          // 即使是 pending，如果 artifacts 中有数据，也尝试恢复数据（用于初始占位显示）
          if (status.artifacts?.[sName]) {
            newStates[sName].artifact = status.artifacts[sName];
          }
          continue;
        }

        const isCompleted = ["completed", "session_completed"].includes(cStatus);
        const isError = cStatus === 'error';
        const isWaiting = cStatus === 'waiting';
        const isStopped = cStatus === 'stopped';
        const isRunningStatus = cStatus === 'running';
        const progressSnapshot = getStageProgressSnapshot(status, sName);

        newStates[sName] = {
          status: isError ? 'error' : (isWaiting ? 'waiting' : (isStopped ? 'stopped' : (isCompleted ? 'completed' : 'running'))),
          progress: isCompleted ? 100 : (typeof progressSnapshot.progress === 'number' ? progressSnapshot.progress : 0),
          progressMessage: isError
            ? '执行失败'
            : (isWaiting
              ? '等待确认'
              : (isStopped
                ? '已停止'
                : (isCompleted
                  ? '已完成'
                  : (progressSnapshot.progressMessage || (isRunningStatus ? '执行中...' : '进行中'))))),
          artifact: status.artifacts?.[sName] || null,
          error: isError ? (status.error || '执行出错') : (isStopped ? '手动停止' : null),
        };

        // 如果内存中没有，尝试异步拉取
        if (!newStates[sName].artifact) {
          try {
            const artResult = await getArtifact(sid, sName);
            // 串行 await 共 N 次：任一次返回后归属可能已变，迟到的直接放弃整轮恢复
            if (!owns(token)) return;
            newStates[sName].artifact = artResult.artifact;
          } catch { /* ignore */ }
        }
      }

      // 恢复项目参数：会话级生成配置统一从 meta 中读取，兼容少量旧扁平字段。
      const meta: SessionMeta = status.meta || {};
      const s: SessionMeta = Object.keys(meta).length > 0 ? meta : (status as unknown as SessionMeta);
      if (s.idea || s.user_textbox_input) {
        // Legacy session compatibility: old sessions only stored one video_model, so hydrate it as the first-frame model.
        const restoredFirstFrameVideoModel = s.video_first_frame_model || s.video_model || '';
        commitSessionView(token, {
          projectParams: {
            idea: s.idea || s.user_textbox_input || '',
            style: s.style || '',
            video_ratio: s.video_ratio || '16:9',
            video_resolution: s.video_resolution || '720P',
            video_duration: s.video_duration ? String(s.video_duration) : '',
            video_fps: s.video_fps ? String(s.video_fps) : '',
            llm_model: s.llm_model || '',
            vlm_model: s.vlm_model || '',
            image_t2i_model: s.image_t2i_model || '',
            image_it2i_model: s.image_it2i_model || '',
            video_generation_mode: (s.video_generation_mode || 'first_frame') as ProjectParams['video_generation_mode'],
            video_first_frame_model: restoredFirstFrameVideoModel,
            video_start_end_model: s.video_start_end_model || 'wan2.7-i2v',
            video_reference_model: s.video_reference_model || 'wan2.7-r2v',
            video_model: s.video_model || '',
            expand_idea: s.expand_idea || false,
            enable_concurrency: s.enable_concurrency || false,
            web_search: s.web_search || false,
          },
        });
      }

      // 确定要显示的阶段（优先使用 URL 中的目标阶段）
      let finalStage = targetStage;
      if (!finalStage) {
        finalStage = currentStage || (completedStages.length > 0 ? completedStages[completedStages.length - 1] : stageOrder[0]);
      }
      commitSessionView(token, { activeStage: finalStage });
      // 整体替换：仅当确实换了会话才会走到这里（同 sid 已走上面的早退），
      // 它会把 SSE 驱动的进度打回后端快照值——属预期且已记入 design D7 的闪烁说明。
      commitStageStates(token, () => newStates);
      setSessionLoading(false);

      // 轮询正在执行的阶段；后台单卡重生成时阶段可能不是 running，但 item 会是 running。
      // 遍历必须基于会话状态里实际出现的阶段（用 ALL_stageOrder 过滤 init 等内部键），
      // 不能用 stageOrder：冷启动恢复时 /api/stages 尚未 resolve，它还是默认六阶段，
      // 会漏掉可选注入阶段导致其永不轮询。不会误启动已关闭的阶段：引擎只在阶段真正
      // 执行时置 running（后台重生成走另一分支），禁用时该键只会停留在 pending/waiting。
      for (const stageId of Object.keys(stMap).filter(name => ALL_stageOrder.includes(name))) {
        if (stMap[stageId] === 'running' || hasRunningArtifactItems(status.artifacts?.[stageId], stageId)) {
          pollForCompletion(sid, stageId);
        }
      }
    } catch {
      // 载入失败 MUST NOT 回落上一个会话的产物（tasks 3.2/3.3）：
      // 保留已复位的空 stageStates 与 sessionId，进入可区分失败态，由用户主动重试。
      if (!owns(token)) return;
      setSessionLoading(false);
      setSessionLoadError('未能载入该会话，请检查后端服务或该会话是否仍存在');
    }
  };

  // ── 返回首页 ──
  // 处理阶段点击，更新 URL
  const handleStageClick = (stage: string) => {
    setActiveStage(stage);
    if (sessionId) {
      router.push(`/?session=${sessionId}&stage=${stage}`);
    }
  };

  // 检查 URL 参数，加载指定的 session 和阶段
  useEffect(() => {
    const sessionParam = searchParams.get('session');
    const stageParam = searchParams.get('stage');
    if (sessionParam) {
      // 目标阶段按「已知全量阶段」校验，不能用 stageOrder：冷启动瞬间 /api/stages 尚未
      // resolve，stageOrder 还是默认六阶段，会把可选注入阶段（prompt_rewrite）误判为非法
      // 并丢弃 URL 参数（该 effect 之后不会随 stageOrder 更新重跑）。
      const targetStage = stageParam && ALL_stageOrder.includes(stageParam) ? stageParam : null;
      // 延后到微任务执行：handleResumeProject 传递包含 setState 的函数调用，
      // effect 体内同步调用会触发级联渲染（react-hooks/set-state-in-effect）。
      // 不需要 cancelled 守卫：迟到的恢复由归属令牌丢弃写入（design D12）。
      queueMicrotask(() => { handleResumeProject(sessionParam, targetStage); });
      return;
    }
    // 第四个退出入口（tasks 2.6）：侧栏首页链接与浏览器后退不带 session 参数。
    // 它们不会走 handleGoHome，若不在此复位，上一会话会残留在顶栏；前置条件限定为
    // "确实来自某个会话"，避免首页首次挂载与无关参数变化时多余复位。
    if (sessionRef.current !== null) {
      resetSessionView(null);
    }
    // deps 仅取 searchParams：本 effect 的语义是"URL 变了才载入"。handleResumeProject 每渲染重建，
    // 放进 deps 会变成 effect → setState → 重渲染 → effect 的死循环（design R6）。
    // 闭包隔代带来的陈旧 state 读取已由归属令牌接管：入口判定走 sessionRef，写入走 owns()。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // 页面加载时从后端获取历史记录
  useEffect(() => {
    fetchSessions()
      .then(sessions => {
        setHistory(
          sessions.map(s => ({
            id: s.id || '',
            idea: (s.title || s.idea || 'Untitled').slice(0, 60),
            style: s.style || '',
            date: s.date
              ? new Date(s.date * 1000).toLocaleDateString('zh-CN')
              : '',
            status: Object.keys(s.status || {}).length > 0 ? 'partial' : 'new',
            stages: s.status || {},
          }))
        );
      })
      .catch(() => {});
  }, []);

  // ── 停止执行 ──
  const handleStop = async () => {
    // 令牌在**点击时**捕获：500ms 延迟回调不能读回调时的归属（design D9）
    const token = currentToken();
    stoppedRef.current = true;
    // 1. 断开前端 SSE 流
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    // 2. 通知后端停止
    if (sessionId) {
      try { await stopProject(sessionId); } catch { /* ignore */ }
    }
    setIsRunning(false);
    // 3. 将所有 running 阶段标记为 stopped，保留已有的 artifact
    commitStageStates(token, prev => {
      const next = { ...prev };
      for (const s of stageOrder) {
        if (next[s]?.status === 'running') {
          // 如果已有 artifact 数据（如部分已生成的视频片段），保留为 waiting 状态以便用户操作
          const hasArtifact = hasDoneArtifactItems(next[s]?.artifact);
          if (hasArtifact) {
            next[s] = { ...next[s], status: 'waiting', error: null, progressMessage: '已停止（保留已完成内容）', syncState: 'synced' };
          } else {
            next[s] = { ...next[s], status: 'error', error: '已手动停止', progressMessage: '已停止', syncState: 'synced' };
          }
        }
      }
      return next;
    });
    // 4. 尝试从后端获取最新 artifact（后端可能已保存部分结果）
    const stopSid = sessionId;
    if (stopSid) {
      setTimeout(async () => {
        for (const s of stageOrder) {
          try {
            const artResult = await getArtifact(stopSid, s);
            if (artResult?.artifact) {
              // 500ms 内用户可能已切会话：令牌不匹配则整批放弃，不得回填上一会话的"已停止"
              if (!owns(token)) return;
              commitStageStates(token, prev => {
                const cur = prev[s];
                if (!cur || cur.status === 'completed') return prev;
                // 检查 artifact 是否含有效内容
                const hasDone = hasDoneArtifactItems(artResult.artifact);
                if (hasDone) {
                  return { ...prev, [s]: { ...cur, status: 'waiting', artifact: artResult.artifact, error: null, progressMessage: '已停止（保留已完成内容）' } };
                }
                return prev;
              });
            }
          } catch { /* 该阶段无 artifact，跳过 */ }
        }
      }, 500);
    }
  };

  // 超时横幅专用“停止任务”：先探后端可达性——失败则保留超时横幅 + 错误反馈（MUST NOT 静默清除，tasks 4.3）；
  // 成功则复用既有 handleStop 路径标记 stopped（其内已将 running 阶段的 syncState 置 synced 以清除横幅）。
  const handleTimeoutStop = async () => {
    if (!sessionId) return;
    try {
      await stopProject(sessionId);
    } catch {
      setTimeoutStopError('停止请求失败：后端不可达或无响应，超时提示已保留，可再次重试。');
      return;
    }
    setTimeoutStopError(null);
    await handleStop();
  };

  // ─ 模型不可用兜底：刷新阶段产物（拿到种子化条目）并弹出引导弹窗 ──
  const refreshStageArtifact = async (token: SessionToken, sid: string, stageId: string) => {
    try {
      const artResult = await getArtifact(sid, stageId);
      if (artResult?.artifact) {
        commitStageState(token, stageId, { artifact: artResult.artifact });
      }
    } catch {
      /* 阶段尚无产物，忽略 */
    }
  };

  const showModelUnavailable = (token: SessionToken, sid: string, stageId: string, detail: ModelUnavailableDetail) => {
    if (!owns(token)) return;
    setModelFallback(prev => prev ?? { stage: stageId, detail });
    void refreshStageArtifact(token, sid, stageId);
  };

  /**
   * 该阶段实际使用的模型字段（用于弹窗说出"是哪个模型"）。
   * 选字段口径与后端 `_preflight_stage_models` 一致：视频阶段按 generation mode，并保留 legacy 回退。
   */
  const stageModelField = (stageId: string): string => {
    const params = projectParams as Record<string, unknown> | null | undefined;
    if (stageId === 'character_design') return 'image_t2i_model';
    if (stageId === 'reference_generation') {
      // 参考图主要用图生图；该字段为空时回退到文生图
      return params?.image_it2i_model ? 'image_it2i_model' : 'image_t2i_model';
    }
    if (stageId === 'video_generation') {
      const byMode: Record<string, string> = {
        first_frame: 'video_first_frame_model',
        start_end_frame: 'video_start_end_model',
        reference: 'video_reference_model',
      };
      const modeField = byMode[String(params?.video_generation_mode || 'first_frame')] || byMode.first_frame;
      if (params?.[modeField]) return modeField;
      return params?.video_model ? 'video_model' : modeField;
    }
    return '';
  };

  // 阶段结束后扫描失败条目：存在 model_unavailable 时同样给出引导（每次运行最多弹一次）
  const maybeShowFallbackFromArtifact = (stageId: string, artifact: unknown) => {
    if (!artifact || typeof artifact !== 'object') return;
    const art = artifact as Record<string, unknown>;
    // 遍历已知条目容器，而非只认图片阶段的三类键（否则视频/改写阶段的模型级失败静默无提示）
    const groups = [art.characters, art.settings, art.scenes, art.clips, art.items];
    for (const group of groups) {
      if (!Array.isArray(group)) continue;
      const failed = group.find((item: unknown) => (item as Record<string, unknown> | null)?.error_type === 'model_unavailable') as Record<string, unknown> | undefined;
      if (failed) {
        const field = stageModelField(stageId);
        const model = field ? String((projectParams as Record<string, unknown> | null | undefined)?.[field] ?? '') : '';
        // 兜底文案按模型类型分流（按字段前缀，不再重复维护一份阶段白名单）
        const fallbackReason = field.startsWith('image_')
          ? '存在因模型不可用而失败的条目，可上传图片继续该步骤'
          : '存在因模型不可用而失败的条目，请更换可用的视频模型后重试';
        setModelFallback(prev => prev ?? {
          stage: stageId,
          detail: {
            code: 'model_unavailable',
            stage: stageId,
            field,
            model,
            reason: (failed.error as string) || fallbackReason,
            error_code: 'runtime_failed',
          },
        });
        return;
      }
    }
  };

  // ── 执行单个阶段 ──
  // 令牌由调用方（执行链启动处）传入：链内不得重取，否则用户切走会话后，
  // 本链下一个阶段会捕获到新归属的令牌并把旧会话数据写进当前界面（design D9）。
  const runStage = async (token: SessionToken, sid: string, stageId: string, inputData: Record<string, unknown>) => {
    if (stoppedRef.current) throw new Error('Stopped');
    commitStageState(token, stageId, { status: 'running', progress: 0, progressMessage: '启动中...', error: null });
    commitSessionView(token, { activeStage: stageId });

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const response = await executeStage(sid, stageId, inputData, controller.signal);

      for await (const event of parseStreamEvents(response)) {
        if (event.type === 'progress') {
          commitStageState(token, stageId, {
            progress: event.percent || 0,
            progressMessage: event.message || '',
          });
          // 处理素材预览和逐个完成事件
          if (event.data?.assets_preview) {
            commitStageState(token, stageId, { artifact: event.data.assets_preview });
          }
          if (event.data?.asset_complete) {
            const assetUpdate = event.data.asset_complete as Record<string, unknown>;
            commitStageStates(token, prev => {
              const nextArtifact = applyAssetUpdateToArtifact(
                prev[stageId]?.artifact as ArtifactLike,
                assetUpdate,
                stageId === 'prompt_rewrite',
              );
              if (!nextArtifact) return prev;
              return { ...prev, [stageId]: { ...prev[stageId], artifact: nextArtifact } };
            });
          }
          // 处理剧本逐幕增量事件
          if (event.data?.beat_sheet) {
            const beatSheet = event.data.beat_sheet;
            commitStageStates(token, prev => {
              const prevArt = (prev[stageId]?.artifact || {}) as Record<string, unknown>;
              return {
                ...prev,
                [stageId]: {
                  ...prev[stageId],
                  artifact: { ...prevArt, phase: 'generating', beat_sheet: beatSheet },
                },
              };
            });
          }
          if (event.data?.act_complete) {
            const actData = event.data.act_complete;
            commitStageStates(token, prev => {
              const prevArt = (prev[stageId]?.artifact || {}) as Record<string, unknown>;
              const prevActs = Array.isArray(prevArt.completed_acts) ? prevArt.completed_acts : [];
              return {
                ...prev,
                [stageId]: {
                  ...prev[stageId],
                  artifact: {
                    ...prevArt,
                    phase: 'generating',
                    completed_acts: [...prevActs, actData],
                  },
                },
              };
            });
          }
          // 处理分镜逐场完成事件
          if (event.data?.scene_shots_complete) {
            const sceneData = event.data.scene_shots_complete as { shots?: unknown[] };
            commitStageStates(token, prev => {
              const prevArt = (prev[stageId]?.artifact || {}) as Record<string, unknown>;
              const prevShots = Array.isArray(prevArt.shots) ? prevArt.shots : [];
              return {
                ...prev,
                [stageId]: {
                  ...prev[stageId],
                  artifact: {
                    ...prevArt,
                    phase: 'generating',
                    shots: [...prevShots, ...(sceneData.shots || [])],
                  },
                },
              };
            });
          }
        } else if (event.type === 'stage_complete') {
          const newStatus: StageStatus = event.requires_intervention ? 'waiting' : 'completed';

          let artifact: unknown = null;
          try {
            const artResult = await getArtifact(sid, stageId);
            artifact = artResult.artifact;
          } catch { /* ignore */ }

          // 如果是后处理阶段，即使 artifact 为空也尝试从事件中读取预览数据
          if (stageId === 'post_production' && !artifact && event.data?.assets_preview) {
            artifact = event.data.assets_preview;
          }

          commitStageState(token, stageId, {
            status: newStatus,
            progress: 100,
            progressMessage: newStatus === 'waiting' ? '等待确认' : '已完成',
            artifact,
          });
          if (artifact && owns(token)) maybeShowFallbackFromArtifact(stageId, artifact);
        } else if (event.type === 'error') {
          // 取消/停止类错误：尝试获取部分结果而不是直接报错
          const isCancelError = /cancel|取消|停止/i.test(event.content || '');
          if (isCancelError) {
            let artifact: unknown = null;
            try {
              const artResult = await getArtifact(sid, stageId);
              artifact = artResult?.artifact;
            } catch { /* ignore */ }
            // 如果有已完成的内容，显示为 waiting 状态
            const hasDone = hasDoneArtifactItems(artifact);
            if (hasDone) {
              commitStageState(token, stageId, {
                status: 'waiting',
                progress: 100,
                progressMessage: '已停止（保留已完成内容）',
                artifact,
                error: null,
              });
              return; // 不抛异常，让工作流正常停下
            }
          }
          commitStageState(token, stageId, {
            status: 'error',
            error: event.content || 'Unknown error',
            progressMessage: '执行失败',
          });
          throw new Error(event.content);
        }
      }
    } catch (error: unknown) {
      if ((error as { name?: string }).name !== 'AbortError') {
        if (error instanceof ModelUnavailableError) {
          // 预检失败：刷新阶段产物（种子化条目）并弹出上传兜底引导
          showModelUnavailable(token, sid, stageId, error.detail);
        }
        commitStageState(token, stageId, {
          status: 'error',
          error: (error as Error).message,
          progressMessage: '执行失败',
        });
      }
      // 控制流继续向上抛：令牌失效只丢写入，不得中断执行链（design D11）
      throw error;
    }
  };

  // ── 启动新项目 ──
  const handleStartProject = async (params: ProjectParams, autoOverride?: boolean) => {
    if (chainRunningRef.current) return;
    const useAutoMode = autoOverride !== undefined ? autoOverride : autoMode;
    if (autoOverride !== undefined) handleAutoModeChange(autoOverride);
    // 启动新项目 MUST 复用同一复位语义（tasks 2.4），不另摘一份初值；
    // 控制态（stoppedRef / isRunning）按 D11 在复位后直接置，不受令牌约束。
    resetSessionView(null);
    chainRunningRef.current = true;
    setIsRunning(true);

    try {
      const result = await startProject({
        idea: params.idea,
        file_path: params.file_path, // 修复：将上传的文件路径传给后端
        style: params.style,
        video_ratio: params.video_ratio,
        video_resolution: params.video_resolution,
        video_duration: params.video_duration,
        video_fps: params.video_fps,
        llm_model: params.llm_model,
        vlm_model: params.vlm_model,
        image_t2i_model: params.image_t2i_model,
        image_it2i_model: params.image_it2i_model,
        video_generation_mode: params.video_generation_mode,
        video_first_frame_model: params.video_first_frame_model,
        video_start_end_model: params.video_start_end_model,
        video_reference_model: params.video_reference_model,
        video_model: params.video_model,
        enable_concurrency: params.enable_concurrency,
        web_search: params.web_search,
        expand_idea: params.expand_idea,
        episodes: params.episodes,
      });
      // 链启动点取一次令牌并贯穿整个阶段循环（design D9）
      const token = enterSession(result.session_id);
      commitSessionView(token, { projectParams: params });

      // 添加到历史
      setHistory(prev => [
        {
          id: result.session_id,
          idea: params.idea.slice(0, 60),
          style: params.style,
          date: new Date().toLocaleDateString('zh-CN'),
          status: 'running',
          stages: {},
        },
        ...prev,
      ]);

      const inputData: Record<string, unknown> = {
        idea: params.idea,
        session_id: result.session_id,
        style: params.style,
        video_ratio: params.video_ratio,
        video_resolution: params.video_resolution,
        video_duration: params.video_duration,
        video_fps: params.video_fps,
        llm_model: params.llm_model,
        vlm_model: params.vlm_model,
        image_t2i_model: params.image_t2i_model,
        image_it2i_model: params.image_it2i_model,
        video_generation_mode: params.video_generation_mode,
        video_first_frame_model: params.video_first_frame_model,
        video_start_end_model: params.video_start_end_model,
        video_reference_model: params.video_reference_model,
        video_model: params.video_model,
        scene_number: (result.params as { scene_number?: number } | undefined)?.scene_number,
        expand_idea: params.expand_idea,
        enable_concurrency: params.enable_concurrency,
        web_search: params.web_search,
        episodes: params.episodes,
        auto_mode: useAutoMode,
      };

      for (const stageId of stageOrder) {
        if (stoppedRef.current) break;
        await runStage(token, result.session_id, stageId, inputData);

        // 剧本生成完成后，用实际场景数更新 scene_number
        if (stageId === 'script_generation') {
          try {
            const artResult = await getArtifact(result.session_id, 'script_generation');
            const scenes = (artResult?.artifact as { scenes?: unknown[] } | undefined)?.scenes;
            if (scenes?.length) {
              inputData.scene_number = scenes.length;
            }
          } catch { /* ignore */ }
        }

        const status = await getProjectStatus(result.session_id);
        // 写入由漏斗按令牌丢弃；即使用户已切走，本链仍必须跑完（后端继续执行，design D11）
        const stageStatus = status?.status?.[stageId];
        commitSessionView(token, { globalStatusMap: status.status || {} });
        // waiting: 等待用户介入（如选择角色/图片），不能自动 continue
        // completed: 阶段完成，等待确认进入下一阶段
        if (stageStatus === 'completed' || stageStatus === 'session_completed') {
          if (autoModeRef.current) {
            // 代理模式：自动确认并继续
            await continueWorkflow(result.session_id);
            commitStageState(token, stageId, { status: 'completed', progressMessage: '已自动确认' });
          } else {
            commitStageState(token, stageId, { status: 'completed' });
            commitSessionView(token, { activeStage: stageId });
            setIsRunning(false);
            return;
          }
        } else if (stageStatus === 'waiting') {
          // 用户需要介入，停止自动执行
          commitStageState(token, stageId, { status: 'waiting' });
          commitSessionView(token, { activeStage: stageId });
          setIsRunning(false);
          return;
        }
      }
    } catch (error: unknown) {
      if (!stoppedRef.current) console.error('Workflow error:', error);
    } finally {
      chainRunningRef.current = false;
      setIsRunning(false);
    }
  };

  // ── 确认阶段并继续 ──
  // eslint-disable-next-line react-hooks/exhaustive-deps -- 处理器每渲染重建（闭包捕获最新 state）；onConfirmActiveStage 的 useCallback 仅为满足 react-hooks/refs 的结构包装，不追求记忆化收益
  const handleConfirmStage = async (stageId: string) => {
    if (!sessionId || chainRunningRef.current) return;
    // 本函数是一条新执行链的起点：在此取一次归属令牌贯穿全程（design D9）
    const token = currentToken();
    // 点击「确认并继续」= 明确要继续：清除历史停止标记（否则 runStage 会直接抛 Stopped）
    stoppedRef.current = false;
    chainRunningRef.current = true;
    setIsRunning(true);

    try {
      const result = await continueWorkflow(sessionId);
      commitStageState(token, stageId, { status: 'completed', progressMessage: '已确认' });

      // 更新顶栏状态
      commitSessionView(token, {
        globalStatusMap: result.status_map,
        completedStages: prev => [...prev, stageId],
        currentStage: result.next_stage ?? undefined,
      });

      if (result.next_stage) {
        const idx = stageOrder.indexOf(stageId);

        // 构建完整的 inputData
        const inputData: Record<string, unknown> = {
          session_id: sessionId,
          style: projectParams?.style,
          llm_model: projectParams?.llm_model,
          vlm_model: projectParams?.vlm_model,
          image_t2i_model: projectParams?.image_t2i_model,
          image_it2i_model: projectParams?.image_it2i_model,
          video_generation_mode: projectParams?.video_generation_mode,
          video_first_frame_model: projectParams?.video_first_frame_model,
          video_start_end_model: projectParams?.video_start_end_model,
          video_reference_model: projectParams?.video_reference_model,
          video_model: projectParams?.video_model,
          video_ratio: projectParams?.video_ratio,
          video_resolution: projectParams?.video_resolution,
          video_duration: projectParams?.video_duration,
          video_fps: projectParams?.video_fps,
          video_sound: 'on',
          video_shot_type: 'multi',
        };
        // 从剧本产物获取实际场景数
        try {
          const scriptArt = await getArtifact(sessionId, 'script_generation');
          const scenes = (scriptArt?.artifact as { scenes?: unknown[] } | undefined)?.scenes;
          if (scenes?.length) {
            inputData.scene_number = scenes.length;
          }
        } catch { /* ignore */ }

        // 从参考图产物获取用户选择的图片版本，传递给视频生成阶段
        if (stageId === 'reference_generation') {
          const refArt = stageStates['reference_generation']?.artifact as { scenes?: ArtifactItem[] } | undefined;
          if (refArt?.scenes) {
            const selectedImages: Record<string, string> = {};
            refArt.scenes.forEach(s => {
              if (s.id && s.selected) {
                selectedImages[s.id] = s.selected;
              }
            });
            inputData.selected_images = selectedImages;
          }
        }

        // 从视频产物获取用户选择的视频版本，传递给后期制作阶段
        if (stageId === 'video_generation') {
          const vidArt = stageStates['video_generation']?.artifact as { clips?: ArtifactItem[] } | undefined;
          if (vidArt?.clips) {
            const selectedClips: Record<string, string> = {};
            vidArt.clips.forEach(c => {
              if (c.id && c.selected) {
                selectedClips[c.id] = c.selected;
              }
            });
            inputData.selected_clips = selectedClips;
          }
        }

        for (let i = idx + 1; i < stageOrder.length; i++) {
          if (stoppedRef.current) break;
          const nextStage = stageOrder[i];
          await runStage(token, sessionId, nextStage, inputData);

          const status = await getProjectStatus(sessionId);
          // 写入统一经漏斗按令牌丢弃；本链 MUST NOT 因归属变化而提前 return（design D11）
          commitSessionView(token, { globalStatusMap: status.status || {} });
          const nStageStatus = status?.status?.[nextStage];
          // waiting: 等待用户介入（如选择角色/图片），不能自动 continue
          // completed: 阶段完成，等待确认进入下一阶段，可以自动 continue
          if (nStageStatus === 'completed' || nStageStatus === 'session_completed') {
            if (autoModeRef.current) {
              await continueWorkflow(sessionId);
              commitStageState(token, nextStage, { status: 'completed', progressMessage: '已自动确认' });
              commitSessionView(token, { completedStages: prev => [...prev, nextStage], currentStage: nextStage });
            } else {
              commitStageState(token, nextStage, { status: 'completed' });
              commitSessionView(token, { completedStages: prev => [...prev, nextStage], currentStage: nextStage, activeStage: nextStage });
              break;
            }
          } else if (nStageStatus === 'waiting') {
            // 用户需要介入，停止自动执行
            commitStageState(token, nextStage, { status: 'waiting' });
            commitSessionView(token, { currentStage: nextStage, activeStage: nextStage });
            break;
          }
        }
      }
    } catch (error: unknown) {
      const err = error as { message?: string; name?: string } | null;
      const message = String(err?.message || '');
      const isInterrupted =
        err?.name === 'AbortError' || /abort|stopped/i.test(message) || message === 'Stopped';
      if (isInterrupted) {
        // 流被中止（停止/刷新等），不当作错误：尽力同步后端真实状态
        console.warn('Continue interrupted:', message || err?.name);
        try {
          const status = await getProjectStatus(sessionId);
          commitSessionView(token, { globalStatusMap: status.status || {} });
        } catch {
          /* ignore */
        }
      } else {
        console.error('Continue error:', error);
      }
    } finally {
      chainRunningRef.current = false;
      setIsRunning(false);
    }
  };

  // 供 StageViewProps.onConfirm 直接引用：经 useCallback 包装，避免渲染期箭头
  // 引用读取 ref 的处理器（react-hooks/refs）
  const onConfirmActiveStage = useCallback(() => {
    if (activeStage) void handleConfirmStage(activeStage);
  }, [activeStage, handleConfirmStage]);

  // ── 用户介入修改 ──
  const handleIntervene = async (stageId: string, modifications: Record<string, unknown>) => {
    if (!sessionId) return;
    // 介入同样是一条执行链的起点：链内写入均用本令牌（design D9）
    const token = currentToken();
    const isBackgroundItemRegeneration =
      (stageId === 'character_design' && (
        Array.isArray(modifications.regenerate_characters) ||
        Array.isArray(modifications.regenerate_settings)
      )) ||
      (stageId === 'reference_generation' && Array.isArray(modifications.regenerate_scenes)) ||
      (stageId === 'prompt_rewrite' && (
        Array.isArray(modifications.regenerate_items) || Array.isArray(modifications.revise_items)
      )) ||
      (stageId === 'video_generation' && Array.isArray(modifications.regenerate_clips)) ||
      (stageId === 'post_production' && Array.isArray(modifications.regenerate_episodes));

    // 构建完整的 inputData 传给后端，确保比例等参数能传递
    const inputData: Record<string, unknown> = {
      ...modifications,
      session_id: sessionId,
      style: projectParams?.style,
      video_ratio: projectParams?.video_ratio,
      video_resolution: projectParams?.video_resolution,
      video_duration: projectParams?.video_duration,
      video_fps: projectParams?.video_fps,
      llm_model: projectParams?.llm_model,
      vlm_model: projectParams?.vlm_model,
      image_t2i_model: projectParams?.image_t2i_model,
      image_it2i_model: projectParams?.image_it2i_model,
      video_generation_mode: projectParams?.video_generation_mode,
      video_first_frame_model: projectParams?.video_first_frame_model,
      video_start_end_model: projectParams?.video_start_end_model,
      video_reference_model: projectParams?.video_reference_model,
      video_model: projectParams?.video_model,
      video_sound: 'on',
      video_shot_type: 'multi',
    };

    // 设置 running 状态以便显示进度条（如 Logline 选择后生成剧本）
    // 若选择了 Logline，将其保存到 artifact 以便 ScriptStage 在生成期间展示
    // 若选择了模式，保留已选 Logline 并设置 generating 状态
    const currentArtifact = stageStates[stageId]?.artifact as { selected_logline?: unknown } | undefined;
    const artifactPatch = modifications.selected_logline
      ? { phase: 'generating', selected_logline: modifications.selected_logline }
      : modifications.selected_mode
        ? { phase: 'generating', selected_logline: currentArtifact?.selected_logline }
        : undefined;
    if (!isBackgroundItemRegeneration) {
      setIsRunning(true);
      commitStageState(token, stageId, { status: 'running', progress: 0, progressMessage: '处理中...', ...(artifactPatch ? { artifact: artifactPatch } : {}) });
    } else if (artifactPatch) {
      commitStageState(token, stageId, { artifact: artifactPatch });
    }
    try {
      const response = await intervene(sessionId, stageId, inputData);
      for await (const event of parseStreamEvents(response)) {
        if (event.type === 'progress') {
          if (!isBackgroundItemRegeneration) {
            commitStageState(token, stageId, {
              progress: event.percent || 0,
              progressMessage: event.message || '',
            });
          }
          // 处理 asset_complete 实时事件
          if (event.data?.asset_complete) {
            const assetUpdate = event.data.asset_complete as Record<string, unknown>;
            commitStageStates(token, prev => {
              const nextArtifact = applyAssetUpdateToArtifact(
                prev[stageId]?.artifact as ArtifactLike,
                assetUpdate,
                stageId === 'prompt_rewrite',
              );
              if (!nextArtifact) return prev;
              return { ...prev, [stageId]: { ...prev[stageId], artifact: nextArtifact } };
            });
          }
          // 处理剧本逐幕增量事件 (介入模式)
          if (event.data?.beat_sheet) {
            const beatSheet = event.data.beat_sheet;
            commitStageStates(token, prev => {
              const prevArt = (prev[stageId]?.artifact || {}) as Record<string, unknown>;
              return {
                ...prev,
                [stageId]: {
                  ...prev[stageId],
                  artifact: { ...prevArt, phase: 'generating', beat_sheet: beatSheet },
                },
              };
            });
          }
          if (event.data?.act_complete) {
            const actData = event.data.act_complete;
            commitStageStates(token, prev => {
              const prevArt = (prev[stageId]?.artifact || {}) as Record<string, unknown>;
              const prevActs = Array.isArray(prevArt.completed_acts) ? prevArt.completed_acts : [];
              return {
                ...prev,
                [stageId]: {
                  ...prev[stageId],
                  artifact: {
                    ...prevArt,
                    phase: 'generating',
                    completed_acts: [...prevActs, actData],
                  },
                },
              };
            });
          }
          // 处理分镜逐场完成事件 (介入模式)
          if (event.data?.scene_shots_complete) {
            const sceneData = event.data.scene_shots_complete as { shots?: unknown[] };
            commitStageStates(token, prev => {
              const prevArt = (prev[stageId]?.artifact || {}) as Record<string, unknown>;
              const prevShots = Array.isArray(prevArt.shots) ? prevArt.shots : [];
              return {
                ...prev,
                [stageId]: {
                  ...prev[stageId],
                  artifact: {
                    ...prevArt,
                    phase: 'generating',
                    shots: [...prevShots, ...(sceneData.shots || [])],
                  },
                },
              };
            });
          }
        } else if (event.type === 'stage_complete') {
          const artResult = await getArtifact(sessionId, stageId);
          if (isBackgroundItemRegeneration) {
            commitStageState(token, stageId, { artifact: artResult.artifact });
          } else {
            commitStageState(token, stageId, { artifact: artResult.artifact, status: 'waiting' });
          }
        } else if (event.type === 'error') {
          console.error('Intervention error:', event.content);
        }
      }
    } catch (error: unknown) {
      console.error('Intervention error:', error);
      if (error instanceof ModelUnavailableError) {
        showModelUnavailable(token, sessionId as string, stageId, error.detail);
      }
    } finally {
      if (!isBackgroundItemRegeneration) {
        setIsRunning(false);
      }
    }
  };
  
  // ── 本地更新产物（不触发服务端调用）──
  const handleUpdateArtifact = (stageId: string, patch: Record<string, unknown>) => {
    // 同步写入（无 await）：取当前归属即可，它代表用户此刻正在看的会话
    commitStageStates(currentToken(), prev => ({
      ...prev,
      [stageId]: {
        ...prev[stageId],
        artifact: { ...(prev[stageId]?.artifact || {}), ...patch },
      },
    }));
  };

  // ── 保存用户选择到服务端 ──
  const handleSaveSelections = async (stageId: string, selections: Record<string, unknown>): Promise<void> => {
    if (!sessionId) return;
    // 本函数在 `await saveSelections` 之后仍有写入：取链起点令牌防止跨会话串染（design D9）
    const token = currentToken();
    try {
      // 构建更新: 把用户选择写回 artifact 中每个 item 的 selected 字段
      const art = (stageStates[stageId]?.artifact || {}) as Record<string, unknown>;
      if (!stageStates[stageId]?.artifact) return;
      const artItems = (key: string): ArtifactItem[] => {
        const list = art[key];
        return Array.isArray(list) ? (list as ArtifactItem[]) : [];
      };

      let patch: Record<string, unknown> = {};
      if (stageId === 'script_generation') {
        // 剧本阶段：直接保存整个 data 覆盖 artifact
        patch = selections;
      } else if (stageId === 'character_design') {
        const { _editDescs, ...restSelections } = selections;
        const editDescs = _editDescs as { characters?: Record<string, string>; settings?: Record<string, string> } | undefined;
        const chars = artItems('characters').map(c => ({
          ...c,
          selected: (restSelections[c.id as string] as string) || c.selected,
          description: editDescs?.characters?.[c.id as string] ?? c.description,
        }));
        const sets = artItems('settings').map(s => ({
          ...s,
          selected: (restSelections[s.id as string] as string) || s.selected,
          description: editDescs?.settings?.[s.id as string] ?? s.description,
        }));
        patch = { characters: chars, settings: sets };
      } else if (stageId === 'storyboard') {
        // 分镜阶段：保存 shots 数据（排除 original_shots，只保留 artifact 需要的字段）
        const { original_shots, ...rest } = selections;
        patch = { ...rest, user_modified: true };
      } else if (stageId === 'reference_generation') {
        const { _editDescs, ...restSelections } = selections;
        const editDescs = _editDescs as Record<string, string> | undefined;
        const scenes = artItems('scenes').map(s => ({
          ...s,
          selected: (restSelections[s.id as string] as string) || s.selected,
          description: editDescs?.[s.id as string] ?? s.description,
        }));
        patch = { scenes };
      } else if (stageId === 'video_generation') {
        const { _editDescs, ...restSelections } = selections;
        const editDescs = _editDescs as Record<string, string> | undefined;
        const clips = artItems('clips').map(c => ({
          ...c,
          selected: Object.prototype.hasOwnProperty.call(restSelections, c.id as string)
            ? restSelections[c.id as string]
            : c.selected,
          description: editDescs?.[c.id as string] ?? c.description,
        }));
        patch = { clips };
      }

      // 本地更新
      handleUpdateArtifact(stageId, patch);

      // 服务端持久化
      await saveSelections(sessionId, stageId, patch);
      // 保存成功后标记为已完成，顶栏显示对勾
      commitStageState(token, stageId, { status: 'completed' });

      // 如果是分镜阶段保存，刷新第3和第4阶段的 artifact
      console.log('[handleSaveSelections] stageId:', stageId, 'patch:', patch);
      if (stageId === 'storyboard') {
        // 更新本地 artifact 以清除 is_new 标记
        handleUpdateArtifact(stageId, patch);

        // 获取第4阶段当前已有 artifact
        const oldRefArtifact = stageStates['reference_generation']?.artifact as { scenes?: ArtifactItem[] } | undefined;
        const oldScenes = oldRefArtifact?.scenes || [];
        const oldScenesMap = new Map(oldScenes.map(s => [s.id, s]));

        // 使用 segments 构建第4阶段 scenes 和第5阶段 clips
        const sourceSegments = (Array.isArray(patch.segments) ? patch.segments : []) as SegmentLike[];
        console.log('[handleSaveSelections] sourceSegments:', sourceSegments);

        const newScenes = sourceSegments.map((seg, idx) => {
          const seg_id = seg.segment_id;
          const oldScene = oldScenesMap.get(seg_id);
          const isNew = seg.is_new;

          const shots = seg.shots || [];
          const desc_ref = shots.map(sh => sh.visual_prompt || sh.plot || sh.content || '').join(' ').trim();

          return {
            id: seg_id,
            name: `片段_${seg_id}`,
            index: idx + 1,
            description: desc_ref,
            selected: isNew ? '' : (oldScene?.selected || ''),
            versions: isNew ? [] : (oldScene?.versions || []),
            status: isNew ? 'pending' : 'done',
          };
        });

        // 更新第4阶段的 artifact
        commitStageState(token, 'reference_generation', {
          artifact: { session_id: sessionId, scenes: newScenes },
          status: 'completed'
        });

        // 同步更新第5阶段（video_generation）
        const oldVidArtifact = stageStates['video_generation']?.artifact as { clips?: ArtifactItem[] } | undefined;
        const oldClips = oldVidArtifact?.clips || [];
        const oldClipsMap = new Map(oldClips.map(c => [c.id, c]));

        const newClips = sourceSegments.map((seg, idx) => {
          const seg_id = seg.segment_id;
          const oldClip = oldClipsMap.get(seg_id);

          const shots = seg.shots || [];
          const desc_video = shots.map(sh => sh.plot || sh.content || '').join(' ').trim();
          const total_dur = seg.total_duration || shots.reduce((acc: number, sh) => acc + ((sh.duration as number) || 0), 0) || 10;

          return {
            id: seg_id,
            name: `片段_${seg_id}`,
            index: idx + 1,
            description: desc_video,
            duration: total_dur,
            selected: oldClip?.selected || '',
            versions: oldClip?.versions || [],
            status: 'pending',
          };
        });

        // 更新第5阶段的 artifact
        commitStageState(token, 'video_generation', {
          artifact: { session_id: sessionId, clips: newClips },
          status: 'completed'
        });
        console.log('[handleSaveSelections] 第5阶段已更新');
      }
    } catch (error) {
      console.error('Save selections error:', error);
      throw error; // 抛出让 StageActions 捕获以恢复按钮状态
    }
  };

  // ── 重新生成当前阶段 ──
  // eslint-disable-next-line react-hooks/exhaustive-deps -- 处理器每渲染重建（闭包捕获最新 state）；onRegenerateActiveStage 的 useCallback 仅为满足 react-hooks/refs 的结构包装，不追求记忆化收益
  const handleRegenerate = async (stageId: string) => {
    if (!sessionId || chainRunningRef.current) return;
    // 重生成的链起点：此令牌贯穿 getArtifact / runStage / getProjectStatus 后的全部写入（design D9）
    const token = currentToken();
    stoppedRef.current = false;
    chainRunningRef.current = true;
    setIsRunning(true);

    // 清空当前阶段状态
    commitStageState(token, stageId, {
      status: 'running',
      progress: 0,
      progressMessage: '重新生成中...',
      artifact: null,
      error: null,
    });

    // 将该阶段之后的所有阶段重置为 pending
    const idx = stageOrder.indexOf(stageId);
    for (let i = idx + 1; i < stageOrder.length; i++) {
      commitStageState(token, stageOrder[i], {
        status: 'pending',
        progress: 0,
        progressMessage: '',
        artifact: null,
        error: null,
      });
    }

    try {
      const inputData: Record<string, unknown> = {
        session_id: sessionId,
        style: projectParams?.style,
        llm_model: projectParams?.llm_model,
        vlm_model: projectParams?.vlm_model,
        image_t2i_model: projectParams?.image_t2i_model,
        image_it2i_model: projectParams?.image_it2i_model,
        video_generation_mode: projectParams?.video_generation_mode,
        video_first_frame_model: projectParams?.video_first_frame_model,
        video_start_end_model: projectParams?.video_start_end_model,
        video_reference_model: projectParams?.video_reference_model,
        video_model: projectParams?.video_model,
        video_ratio: projectParams?.video_ratio,
        video_resolution: projectParams?.video_resolution,
        video_duration: projectParams?.video_duration,
        video_fps: projectParams?.video_fps,
      };

      // 尝试获取场景数
      try {
        const scriptArt = await getArtifact(sessionId, 'script_generation');
        const scenes = (scriptArt?.artifact as { scenes?: unknown[] } | undefined)?.scenes;
        if (scenes?.length) {
          inputData.scene_number = scenes.length;
        }
      } catch { /* ignore */ }

      await runStage(token, sessionId, stageId, inputData);

      const status = await getProjectStatus(sessionId);
      if (status?.status?.[stageId] === 'waiting') {
        commitStageState(token, stageId, { status: 'waiting' });
        commitSessionView(token, { activeStage: stageId });
      }
    } catch (error: unknown) {
      if (!stoppedRef.current) console.error('Regenerate error:', error);
    } finally {
      chainRunningRef.current = false;
      setIsRunning(false);
    }
  };

  // 供 StageViewProps.onRegenerate 直接引用：经 useCallback 包装，避免渲染期箭头
  // 引用读取 ref 的处理器（react-hooks/refs）
  const onRegenerateActiveStage = useCallback(() => {
    if (activeStage) void handleRegenerate(activeStage);
  }, [activeStage, handleRegenerate]);

  const handleGoHome = () => {
    // 回主界面 MUST 走统一复位（tasks 2.2）：不清则顶栏仍按 hasSession 保留上一会话元素
    resetSessionView(null);
    router.push('/');
  };

  // 载入失败后的重试：失败态下 handleResumeProject 的同会话早退已被 sessionLoadError 跳过，
  // 因此会正常走"复位 + 取数"，并在这里拿到新代令牌（design D5/D7）。
  const retryLoadSession = (sid: string) => {
    void handleResumeProject(sid);
  };

  // ── 删除历史记录 ──
  const handleDeleteSession = async (sid: string) => {
    await deleteSession(sid);
    setHistory(prev => prev.filter(h => h.id !== sid));
  };

  // ── 模型配置变更处理 ──
  const handleModelConfigChange = (config: ModelConfig) => {
    setProjectParams(prev => prev ? { ...prev, ...config } : null);
    // 同步到后端会话元数据
    if (sessionId) {
      const payload: Partial<Record<string, string | boolean>> = { ...config };
      updateModels(sessionId, payload).catch(console.error);
    }
  };

  // 构建 modelConfig for TopBar
  const modelConfig: ModelConfig | undefined = projectParams
    ? {
        llm_model: projectParams.llm_model,
        vlm_model: projectParams.vlm_model,
        image_t2i_model: projectParams.image_t2i_model,
        image_it2i_model: projectParams.image_it2i_model,
        // Legacy session compatibility: projectParams may come from an old session with only video_model.
        video_generation_mode: projectParams.video_generation_mode || 'first_frame',
        video_first_frame_model: projectParams.video_first_frame_model || projectParams.video_model,
        video_start_end_model: projectParams.video_start_end_model || 'wan2.7-i2v',
        video_reference_model: projectParams.video_reference_model || 'wan2.7-r2v',
        video_model: projectParams.video_model,
        video_ratio: projectParams.video_ratio,
        video_resolution: projectParams.video_resolution || '720P',
        video_duration: projectParams.video_duration || '',
        video_fps: projectParams.video_fps || '',
        enable_concurrency: projectParams.enable_concurrency || false,
        // web_search: projectParams.web_search || false,
        // expand_idea: projectParams.expand_idea || false,
      }
    : undefined;

  // ── 计算 stageStatuses for TopBar ──
  // 直接根据后端返回的 status 数据计算图标状态
  const stageStatuses: Record<string, StageStatus> = {};

  for (let i = 0; i < stageOrder.length; i++) {
    const s = stageOrder[i];
    const backendStatus = globalStatusMap[s]; // get status from global mapping updated from backend

    if (backendStatus === 'completed') {
      stageStatuses[s] = 'completed';
    } else if (backendStatus === 'waiting') {
      stageStatuses[s] = 'waiting';
    } else if (backendStatus === 'running') {
      stageStatuses[s] = 'running';
    } else if (backendStatus === 'error') {
      stageStatuses[s] = 'error';
    } else {
      stageStatuses[s] = 'pending';
    }
  }

  // ── 计算项目状态 ──
  // 后端阶段状态: pending, running, waiting, completed, stopped, error
  // 前端 StageStatus: pending, running, waiting, completed, error
  // 语义：阶段 completed = 已完成（若仍有 pending 阶段，则项目处于「等待确认继续」）
  const hasRunning = Object.values(stageStates).some(s => s.status === 'running');
  const hasWaiting = Object.values(stageStates).some(s => s.status === 'waiting');
  const hasError = Object.values(stageStates).some(s => s.status === 'error');
  const hasStopped = Object.values(stageStates).some(s => s.status === 'stopped');
  const completedCoreStages = stageOrder.filter(stage => stageStates[stage]?.status === 'completed').length;
  const effectiveIsRunning = isRunning || hasRunning;

  let computedStatus: string;
  if (hasRunning) computedStatus = 'running';
  else if (hasWaiting) computedStatus = 'waiting';
  else if (hasError) computedStatus = 'error';
  else if (hasStopped) computedStatus = 'stopped';
  else if (completedCoreStages >= stageOrder.length) computedStatus = 'completed'; // 六个阶段全部完成
  else if (completedCoreStages > 0) computedStatus = 'waiting'; // 已完成部分阶段：等待确认继续
  else computedStatus = 'pending';

  const projectStatus = sessionId ? computedStatus : undefined;

  // ── 渲染阶段内容 ──
  const renderStageContent = () => {
    if (!activeStage) return null;
    const Component = STAGE_COMPONENTS[activeStage];
    if (!Component) return null;

    const state = stageStates[activeStage];

    // 判断后续阶段是否已执行过：如有，则隐藏"确认并继续"
    const idx = stageOrder.indexOf(activeStage);
    // 后续阶段「真正执行过」才隐藏「确认并继续」；waiting/stopped 为可重做的中间态
    // （如后端暂停/手动停止保留的现场），不应使当前阶段的确认按钮消失。
    const hasSubsequentExecution = stageOrder.slice(idx + 1).some(s => {
      const st = stageStates[s]?.status;
      return Boolean(st) && st !== 'pending' && st !== 'waiting' && st !== 'stopped';
    });
    const showConfirm = !hasSubsequentExecution;

    // 计算是否有待生成的项（阶段2、4、5）；失败条目（含历史版本）也视为待处理，允许阶段级重跑
    let hasPendingItems = false;
    if (activeStage === 'character_design') {
      const art = state?.artifact as { characters?: ArtifactItem[]; settings?: ArtifactItem[] } | undefined;
      const chars = art?.characters || [];
      const sets = art?.settings || [];
      hasPendingItems =
        chars.some(c => !c.selected || c.status === 'failed') ||
        sets.some(s => !s.selected || s.status === 'failed');
    } else if (activeStage === 'reference_generation') {
      const scenes = (state?.artifact as { scenes?: ArtifactItem[] } | undefined)?.scenes || [];
      hasPendingItems = scenes.some(s => !s.selected || s.status === 'failed');
    } else if (activeStage === 'prompt_rewrite') {
      const items = (state?.artifact as { items?: Array<{ status?: string }> } | undefined)?.items || [];
      hasPendingItems = items.some(i => i.status === 'pending' || i.status === 'failed');
    } else if (activeStage === 'video_generation') {
      const clips = (state?.artifact as { clips?: ArtifactItem[] } | undefined)?.clips || [];
      // 检查是否有未选中或失败的 clips
      const hasUnselected = clips.some(c => !c.selected || c.status === 'failed');
      // 检查参考图阶段的 scenes 数量是否大于 video clips 数量
      // 如果是，说明有新分镜需要生成
      const refScenes = (stageStates['reference_generation']?.artifact as { scenes?: unknown[] } | undefined)?.scenes || [];
      const hasNewShots = refScenes.length > clips.length;
      hasPendingItems = hasUnselected || hasNewShots;
    }

    // 计算后续阶段是否已开始（阶段1、3）
    const hasNextStageStarted = hasSubsequentExecution;

    // 传递第4阶段的 artifact 到第5阶段用于依赖检查
    const referenceArtifact = activeStage === 'video_generation'
      ? stageStates['reference_generation']?.artifact
      : undefined;

    // 传递第1阶段的 artifact 到第4、5、6阶段用于获取剧集标题
    const scriptArtifact = (activeStage === 'reference_generation' || activeStage === 'video_generation' || activeStage === 'post_production')
      ? stageStates['script_generation']?.artifact
      : undefined;

    return (
      // key 绑定会话身份（tasks 5.1）：切会话时整枝卸载重建，阶段组件内的
      // editDescs/selectedVersions/drafts/episodeDraft 等本地态不会跳到新会话的同号条目上。
      <div id="stage-artifact-area" key={`session:${sessionId ?? 'none'}`} className="h-full">
      {state?.syncState === 'timed_out' && (
        <div className="mx-4 mt-4 rounded-xl border border-amber-300 bg-amber-50 p-4 space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-sm text-amber-800 font-medium">
              本地状态同步已超时，实际执行状态尚未确认（后台任务可能仍在运行）。
            </span>
            <div className="ml-auto flex items-center gap-2">
              <button
                onClick={() => { setTimeoutStopError(null); if (sessionId) void pollForCompletion(sessionId, activeStage); }}
                className="px-3 py-1.5 rounded-lg bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors"
              >
                重新查询
              </button>
              <button
                onClick={() => { void handleTimeoutStop(); }}
                className="px-3 py-1.5 rounded-lg bg-red-50 text-red-600 ring-1 ring-red-200 text-xs font-medium hover:bg-red-100 transition-colors"
              >
                停止任务
              </button>
            </div>
          </div>
          {timeoutStopError && <p className="text-xs text-red-600">{timeoutStopError}</p>}
        </div>
      )}
      <Component
        state={state}
        sessionId={sessionId || ''}
        onConfirm={onConfirmActiveStage}
        onIntervene={mods => handleIntervene(activeStage, mods)}
        onRegenerate={onRegenerateActiveStage}
        onUpdateArtifact={patch => handleUpdateArtifact(activeStage, patch)}
        onSaveSelections={selections => handleSaveSelections(activeStage, selections)}
        showConfirm={showConfirm}
        isRunning={effectiveIsRunning}
        hasPendingItems={hasPendingItems}
        hasNextStageStarted={hasNextStageStarted}
        referenceArtifact={referenceArtifact}
        scriptArtifact={scriptArtifact}
        artifacts={Object.keys(stageStates).reduce((acc: Record<string, unknown>, key) => {
          acc[key] = stageStates[key].artifact;
          return acc;
        }, {})}
      />
      </div>
    );
  };

  const showHome = activeStage === null;

  // 渲染优先级固定为 sessionLoadError > sessionLoading > showHome（design D5）：
  // 复位会把 activeStage 置 null，若不先判载入态，切会话瞬间会闪一下首页。
  const mainContent = sessionLoadError && sessionId ? (
    <div className="flex h-full items-center justify-center p-6">
      <div className="max-w-md text-center">
        <p className="text-sm text-gray-700">{sessionLoadError}</p>
        <button
          onClick={() => retryLoadSession(sessionId)}
          className="mt-4 px-4 py-2 rounded-lg bg-blue-600 text-white text-sm hover:bg-blue-700 transition-colors"
        >
          重试
        </button>
      </div>
    </div>
  ) : sessionLoading ? (
    <div className="flex h-full items-center justify-center p-6">
      <p className="text-sm text-gray-500">正在载入该会话…</p>
    </div>
  ) : showHome ? (
    <HomePage
      onStartProject={handleStartProject}
      onResumeProject={handleResumeProject}
      onDeleteSession={handleDeleteSession}
      history={history}
    />
  ) : (
    renderStageContent()
  );

  return (
    <div className="flex h-screen w-full min-w-0 flex-col bg-gray-50/50">
      <TopBar
        activeStage={activeStage}
        stageStatuses={stageStatuses}
        onStageClick={handleStageClick}
        onHomeClick={handleGoHome}
        hasSession={sessionId !== null}
        isRunning={effectiveIsRunning}
        onStop={handleStop}
        autoMode={autoMode}
        onAutoModeChange={handleAutoModeChange}
        modelConfig={modelConfig}
        onModelConfigChange={handleModelConfigChange}
        projectStatus={projectStatus}
        timedOut={activeStage ? stageStates[activeStage]?.syncState === 'timed_out' : false}
      />

      <main className="flex-1 min-w-0 overflow-hidden">
        {mainContent}
      </main>

      {modelFallback && (
        <ModelFallbackDialog
          detail={modelFallback.detail}
          stage={modelFallback.stage}
          onClose={() => setModelFallback(null)}
          onUpload={() => {
            const target = modelFallback.stage;
            setModelFallback(null);
            setActiveStage(target);
            // 子树可能因会话重建而尚未挂载（tasks 5.2）：定位不到时再试一次
            const scrollToArtifact = () => {
              const el = document.getElementById('stage-artifact-area');
              if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            };
            setTimeout(scrollToArtifact, 60);
            setTimeout(scrollToArtifact, 240);
          }}
          onChangeModel={() => {
            setModelFallback(null);
            window.dispatchEvent(new CustomEvent('open-model-selector'));
          }}
          onRetry={() => {
            const target = modelFallback.stage;
            setModelFallback(null);
            if (sessionId) runStage(currentToken(), sessionId, target, {}).catch(() => {});
          }}
        />
      )}
    </div>
  );
}
