/**
 * 工作流 API 客户端
 */
import { apiFetch } from "@/lib/identity";
import { withBasePath } from "@/lib/basePath";

/**
 * 流式接口（SSE / 长请求）的基址。
 *
 * 部署形态：浏览器经 SLB 以 `/Bocom_Video_Generation` 前缀访问，后端端口不对浏览器开放，
 * 因此这里默认返回空串 —— 即同源相对路径，由 `apiFetch` 统一补上部署前缀后
 * 经 Next rewrite 代理到后端。如需回到「浏览器直连后端端口」的老形态，
 * 显式设置构建期变量 NEXT_PUBLIC_API_URL=http(s)://host:port 即可覆盖。
 */
function resolveDirectApiBase(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL?.trim();
  if (configured) return configured.replace(/\/+$/, '');
  return '';
}

export const DIRECT_API_BASE = resolveDirectApiBase();
const STREAM_API_BASE = DIRECT_API_BASE;

/**
 * 流式请求封装：失败时给出可操作提示（跨域/防火墙/超时下 fetch 只抛 "Failed to fetch"）。
 */
async function fetchStreamRequest(url: string, init: RequestInit): Promise<Response> {
  try {
    return await apiFetch(url, init);
  } catch (err) {
    const reason = (err as Error)?.message || 'Failed to fetch';
    throw new Error(
      `无法访问后端流式接口 ${url}（${reason}）：请确认 SLB/反向代理已放行该路径并允许长连接，或检查后端服务状态`,
    );
  }
}

export interface StageInfo {
  id: string;
  name: string;
  order: number;
  description: string;
}

export interface ProjectStatus {
  session_id: string;
  current_stage: string;
  status: Record<string, string>;
  error: string | null;
}

export interface StreamEvent {
  type: 'progress' | 'heartbeat' | 'stage_complete' | 'error' | 'content';
  message?: string;
  phase?: string;
  step_desc?: string;
  percent?: number;
  stage?: string;
  /** stage_complete 携带持久化后的状态表；其他事件可能是字符串状态 */
  status?: string | Record<string, string>;
  requires_intervention?: boolean;
  payload_summary?: unknown;
  content?: string;
  time?: number;
  data?: Record<string, unknown>;
}

export interface PipelineTask {
  task_id: string;
  pipeline: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | string;
  progress?: number;
  message?: string;
  input?: Record<string, unknown>;
  output?: Record<string, unknown>;
  artifacts?: Array<{ kind: string; name?: string; path: string; exists?: boolean; created_at?: string }>;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
  output_dir?: string;
}

/** 临时工作台任务输入（按 Sandbox/AppShell 实际读取字段声明） */
export interface SandboxTaskInput {
  prompt?: string;
  reference_image?: string;
  images?: string[];
  ratio?: string;
  resolution?: string;
  duration?: number | string;
  audio_url?: string;
  reference_videos?: string[];
}

export interface SandboxTask {
  id: string;
  tool: string;
  model: string;
  input?: SandboxTaskInput;
  status: string;
  progress?: number;
  created_at?: string;
}

export interface PipelineStartResponse {
  task_id: string;
  pipeline: string;
  status: string;
  metadata_url: string;
  output_dir: string;
}

export interface PipelineTaskEvent {
  type: 'snapshot' | 'progress' | 'artifact' | 'completed' | 'failed';
  task_id: string;
  status?: string;
  progress?: number;
  artifact?: { kind: string; name?: string; path: string; exists?: boolean; created_at?: string };
}

export interface ApiModelOption {
  id: string;
  label: string;
  provider: string;
  provider_label?: string;
  family?: string;
  media_type?: 'image' | 'video';
  model_type?: 'llm' | 'vlm' | 't2i' | 'i2i' | 'video';
  type?: string[];
  ability_type?: string;
  ability_types?: string[];
  adapter_ability_types?: string[];
  input_modalities?: string[];
  adapter_input_modalities?: string[];
  api_contract_verified?: boolean;
  capabilities?: Record<string, unknown>;
}

export interface StandardTemplateOption {
  id: string;
  name: string;
  label: string;
  size: string;
  ratio: '9:16' | '1:1' | '16:9' | string;
  width: number;
  height: number;
  media_width: number;
  media_height: number;
  media_ratio: string;
  media_resolution: string;
  supports_video?: boolean;
  fields: Array<{ key: string; type: string; default: string }>;
  preview_url: string;
}

export async function fetchStages(): Promise<StageInfo[]> {
  const resp = await apiFetch('/api/stages');
  const data = await resp.json();
  return data.stages;
}

/** 历史会话摘要（/api/sessions 下发；字段按消费方实际使用声明，其余动态字段走索引签名） */
export interface SessionSummary {
  id?: string;
  session_id?: string;
  title?: string;
  idea?: string;
  style?: string;
  date?: number;
  status?: Record<string, string>;
  [key: string]: unknown;
}

export async function fetchSessions(): Promise<SessionSummary[]> {
  const resp = await apiFetch('/api/sessions');
  const data = await resp.json();
  return data.sessions || [];
}

async function postPipelineTask(path: string, params: Record<string, unknown>): Promise<PipelineStartResponse> {
  const resp = await apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '启动任务失败' }));
    throw new Error(err.detail || '启动任务失败');
  }
  return resp.json();
}

export async function startStandardPipeline(params: Record<string, unknown>): Promise<PipelineStartResponse> {
  return postPipelineTask('/api/pipelines/standard/tasks', params);
}

export async function startActionTransferPipeline(params: Record<string, unknown>): Promise<PipelineStartResponse> {
  return postPipelineTask('/api/pipelines/action_transfer/tasks', params);
}

export async function startDigitalHumanPipeline(params: Record<string, unknown>): Promise<PipelineStartResponse> {
  return postPipelineTask('/api/pipelines/digital_human/tasks', params);
}

export async function fetchPipelineTasks(limit = 100): Promise<PipelineTask[]> {
  const resp = await apiFetch(`/api/tasks?limit=${limit}`);
  if (!resp.ok) throw new Error('获取任务历史失败');
  const data = await resp.json();
  return data.tasks || [];
}

export async function fetchPipelineTask(taskId: string): Promise<PipelineTask> {
  const resp = await apiFetch(`/api/tasks/${taskId}`);
  if (!resp.ok) throw new Error('获取任务状态失败');
  return resp.json();
}

export async function fetchSandboxTasks(): Promise<SandboxTask[]> {
  const resp = await apiFetch('/api/sandbox/tasks');
  if (!resp.ok) return [];
  const data = await resp.json();
  return data.tasks || [];
}

export async function clearTempCache(): Promise<{ status: string; deleted: number; freed_bytes?: number; freed_mb?: number; errors?: Array<{ path: string; error: string }> }> {
  const resp = await apiFetch('/api/cache/temp', { method: 'DELETE' });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '清空缓存失败' }));
    throw new Error(err.detail || '清空缓存失败');
  }
  return resp.json();
}

export async function deletePipelineTask(taskId: string): Promise<void> {
  const resp = await apiFetch(`/api/tasks/${taskId}`, { method: 'DELETE' });
  if (!resp.ok) throw new Error('删除任务失败');
}

/** 模型不可用（第 2/4 阶段预检失败）结构化错误 */
export interface ModelUnavailableDetail {
  code: string;
  stage?: string;
  field?: string;
  model?: string;
  reason?: string;
  error_code?: string;
}

export class ModelUnavailableError extends Error {
  detail: ModelUnavailableDetail;

  constructor(detail: ModelUnavailableDetail) {
    super(detail.reason || `模型不可用：${detail.model || ''}`);
    this.name = 'ModelUnavailableError';
    this.detail = detail;
  }
}

async function throwResponseError(resp: Response, fallback: string): Promise<never> {
  let detail: { code?: string; reason?: string; message?: string } | string | null = null;
  try {
    const body = await resp.json();
    detail = body?.detail ?? null;
  } catch {
    /* 非 JSON 响应 */
  }
  if (detail && typeof detail === 'object' && detail.code === 'model_unavailable') {
    throw new ModelUnavailableError(detail as ModelUnavailableDetail);
  }
  const message = typeof detail === 'string' ? detail : detail?.reason || detail?.message || fallback;
  throw new Error(message || fallback);
}

export async function fetchApiModels(params: {
  mediaType?: 'image' | 'video';
  modelType?: 'llm' | 'vlm' | 't2i' | 'i2i' | 'video';
  ability?: string;
  verifiedOnly?: boolean;
} = {}): Promise<ApiModelOption[]> {
  const search = new URLSearchParams();
  if (params.mediaType) search.set('media_type', params.mediaType);
  if (params.modelType) search.set('model_type', params.modelType);
  if (params.ability) search.set('ability', params.ability);
  if (params.verifiedOnly) search.set('verified_only', 'true');
  // 列表随配置变更实时变化：禁止浏览器/代理缓存，避免保存后下拉仍显示旧模型
  const resp = await apiFetch(`/api/models${search.toString() ? `?${search.toString()}` : ''}`, { cache: 'no-store' });
  if (!resp.ok) throw new Error('获取模型列表失败');
  const data = await resp.json();
  return data.models || [];
}

/** 模型可用性判定（harden-model-selector-fallback）。
 * code 与后端 model_availability 对齐：not_registered / provider_missing /
 * provider_incomplete / missing_credentials / ""。不可用时 HTTP 仍为 200。
 */
export interface ModelAvailability {
  available: boolean;
  code: string;
  reason: string;
}

export async function fetchModelAvailability(model: string): Promise<ModelAvailability> {
  const search = new URLSearchParams({ model });
  const resp = await apiFetch(`/api/models/availability?${search.toString()}`, { cache: 'no-store' });
  if (!resp.ok) throw new Error('获取模型可用性失败');
  const data = await resp.json();
  return {
    available: Boolean(data?.available),
    code: String(data?.code ?? ''),
    reason: String(data?.reason ?? ''),
  };
}

export interface ModelTestResult {
  success: boolean;
  model_type: string;
  elapsed_ms: number;
  reason: string;
  detail?: string;
}

/** 自定义模型连通性测试（模型/供应商草稿或已保存条目均支持；媒体类会发起一次真实生成） */
export async function testCustomModel(payload: {
  model: Record<string, unknown>;
  provider: Record<string, unknown> | string;
  model_type: 'llm' | 'vlm' | 't2i' | 'i2i' | 'video';
}): Promise<ModelTestResult> {
  const resp = await apiFetch('/api/models/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '测试请求失败' }));
    throw new Error(typeof err.detail === 'string' ? err.detail : '测试请求失败');
  }
  return resp.json();
}

export async function fetchStandardTemplates(): Promise<StandardTemplateOption[]> {
  const resp = await apiFetch('/api/pipelines/standard/templates');
  if (!resp.ok) throw new Error('获取模版列表失败');
  const data = await resp.json();
  return data.templates || [];
}

export async function uploadMedia(file: File): Promise<{ filename: string; file_path: string }> {
  const formData = new FormData();
  formData.append('file', file);
  const resp = await apiFetch('/api/upload_media', {
    method: 'POST',
    body: formData,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '上传失败' }));
    throw new Error(err.detail || '上传失败');
  }
  return resp.json();
}

export async function uploadArtifactImage(
  sessionId: string,
  stage: string,
  itemType: string,
  itemId: string,
  file: File,
): Promise<{ status: string; path: string; artifact: unknown; status_map: Record<string, string> }> {
  const formData = new FormData();
  formData.append('item_type', itemType);
  formData.append('item_id', itemId);
  formData.append('file', file);
  const resp = await apiFetch(`/api/project/${sessionId}/artifact/${stage}/upload_image`, {
    method: 'POST',
    body: formData,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '上传图片失败' }));
    throw new Error(err.detail || '上传图片失败');
  }
  return resp.json();
}

export function subscribePipelineTask(
  taskId: string,
  onEvent: (event: PipelineTaskEvent) => void,
  onError?: () => void,
): () => void {
  const source = new EventSource(withBasePath(`${STREAM_API_BASE}/api/tasks/${taskId}/events`));
  source.onmessage = event => {
    try {
      onEvent(JSON.parse(event.data));
    } catch {
      // Ignore malformed stream events.
    }
  };
  source.onerror = () => {
    onError?.();
    source.close();
  };
  return () => source.close();
}

export async function startProject(params: {
  idea: string;
  file_path?: string;
  /** 剧本来源：idea=按灵感生成；upload=保真导入上传剧本 */
  script_source?: 'idea' | 'upload';
  style?: string;
  video_ratio?: string;
  video_resolution?: string;
  video_duration?: string;
  video_fps?: string;
  llm_model?: string;
  vlm_model?: string;
  image_t2i_model?: string;
  image_it2i_model?: string;
  video_model?: string;
  video_first_frame_model?: string;
  video_start_end_model?: string;
  video_reference_model?: string;
  video_generation_mode?: string;
  scene_number?: number;
  enable_concurrency?: boolean;
  web_search?: boolean;
  expand_idea?: boolean;
  episodes?: number;
}): Promise<{ session_id: string; status: string; params: unknown }> {
  const resp = await apiFetch('/api/project/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '项目创建失败' }));
    throw new Error(err.detail || '项目创建失败');
  }
  return resp.json();
}

export async function getProjectStatus(sessionId: string): Promise<ProjectStatus> {
  const resp = await apiFetch(`/api/project/${sessionId}/status`);
  if (!resp.ok) throw new Error('Failed to get project status');
  return resp.json();
}

// 兼容旧路由名；后端实际会通过统一的 workflow state 入口返回状态。
export async function getProjectStatusFromDisk(sessionId: string): Promise<unknown> {
  const resp = await apiFetch(`/api/project/${sessionId}/status/from_disk`);
  if (!resp.ok) throw new Error('Failed to get project status snapshot');
  return resp.json();
}

export async function getArtifact(sessionId: string, stage: string): Promise<{ artifact: unknown }> {
  const resp = await apiFetch(`/api/project/${sessionId}/artifact/${stage}`);
  if (!resp.ok) throw new Error(`Artifact for stage '${stage}' not found`);
  return resp.json();
}

export async function checkSceneAssets(sessionId: string, sceneNumber: number): Promise<{
  scene_number: number;
  reference_images: number;
  videos: number;
  shot_count: number;
}> {
  const resp = await apiFetch(`/api/project/${sessionId}/scene/${sceneNumber}/assets`);
  if (!resp.ok) return { scene_number: sceneNumber, reference_images: 0, videos: 0, shot_count: 0 };
  return resp.json();
}

export async function executeStage(
  sessionId: string,
  stage: string,
  inputData: Record<string, unknown> = {},
  signal?: AbortSignal,
): Promise<Response> {
  const resp = await fetchStreamRequest(`${STREAM_API_BASE}/api/project/${sessionId}/execute/${stage}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(inputData),
    signal,
  });
  if (!resp.ok) await throwResponseError(resp, '阶段执行失败');
  return resp;
}

export async function intervene(
  sessionId: string,
  stage: string,
  modifications: Record<string, unknown>,
): Promise<Response> {
  // Use STREAM_API_BASE to bypass Next.js proxy (SSE endpoint)
  const resp = await fetchStreamRequest(`${STREAM_API_BASE}/api/project/${sessionId}/intervene`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ stage, modifications }),
  });
  if (!resp.ok) await throwResponseError(resp, '修改请求失败');
  return resp;
}

export async function stopProject(sessionId: string): Promise<{ status: string }> {
  const resp = await apiFetch(`/api/project/${sessionId}/stop`, {
    method: 'POST',
  });
  return resp.json();
}

export async function updateModels(
  sessionId: string,
  models: Partial<Record<string, string | boolean>>,
): Promise<{ status: string }> {
  const resp = await apiFetch(`/api/project/${sessionId}/models`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(models),
  });
  return resp.json();
}

export async function deleteSession(
  sessionId: string,
): Promise<{ status: string }> {
  const resp = await apiFetch(`/api/sessions/${sessionId}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '删除失败' }));
    throw new Error(err.detail || '删除失败');
  }
  return resp.json();
}

export async function saveSelections(
  sessionId: string,
  stage: string,
  selections: Record<string, unknown>,
): Promise<{ status: string }> {
  const resp = await apiFetch(`/api/project/${sessionId}/artifact/${stage}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(selections),
  });
  if (!resp.ok) throw new Error('保存选项失败');
  return resp.json();
}

export async function continueWorkflow(sessionId: string): Promise<{ status: string; next_stage?: string; status_map?: Record<string, string> }> {
  const resp = await apiFetch(`/api/project/${sessionId}/continue`, {
    method: 'POST',
  });
  return resp.json();
}

export async function* parseStreamEvents(response: Response): AsyncGenerator<StreamEvent> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || ''; // keep incomplete trailing line in buffer
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const event: StreamEvent = JSON.parse(line);
        if (event.type !== 'heartbeat') yield event;
      } catch { /* skip malformed */ }
    }
  }
  // Process any remaining data in buffer after stream ends
  if (buffer.trim()) {
    try {
      const event: StreamEvent = JSON.parse(buffer);
      if (event.type !== 'heartbeat') yield event;
    } catch { /* skip malformed */ }
  }
}
