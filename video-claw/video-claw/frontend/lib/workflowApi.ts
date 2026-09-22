/**
 * 工作流 API 客户端
 */

/**
 * Streaming endpoints must bypass the Next.js rewrite proxy because it buffers
 * the entire upstream response before forwarding, which breaks SSE real-time delivery.
 * Non-streaming endpoints can still go through the proxy (relative URL).
 *
 * 浏览器直连（SSE）的后端端口在构建期由 NEXT_PUBLIC_BACKEND_PORT 固化（缺省 8000），
 * 需与应用 .env 的 BACKEND_PORT 保持一致；也可用 NEXT_PUBLIC_API_URL 整体覆盖。
 * 主机名在运行时取当前访问地址，便于局域网访问。
 */
function resolveDirectApiBase(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL;
  if (configured) return configured;
  const port = process.env.NEXT_PUBLIC_BACKEND_PORT || '8000';
  if (typeof window !== 'undefined' && window.location?.hostname) {
    return `${window.location.protocol}//${window.location.hostname}:${port}`;
  }
  return `http://127.0.0.1:${port}`;
}

export const DIRECT_API_BASE = resolveDirectApiBase();
const STREAM_API_BASE = DIRECT_API_BASE;

/**
 * 浏览器直连（SSE）请求封装：失败时给出可操作提示。
 * 跨域/防火墙/端口不一致时 fetch 只抛 "Failed to fetch"，难以定位。
 */
async function fetchStreamRequest(url: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch (err) {
    const reason = (err as Error)?.message || 'Failed to fetch';
    throw new Error(
      `无法直连后端 ${DIRECT_API_BASE}（${reason}）：请确认浏览器可直接访问该地址（防火墙/安全组已放行后端端口），且前端构建时的 NEXT_PUBLIC_BACKEND_PORT 与后端端口一致`,
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
  status?: string;
  requires_intervention?: boolean;
  payload_summary?: any;
  content?: string;
  time?: number;
  data?: any;
}

export interface PipelineTask {
  task_id: string;
  pipeline: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | string;
  progress?: number;
  message?: string;
  input?: Record<string, any>;
  output?: Record<string, any>;
  artifacts?: Array<{ kind: string; name?: string; path: string; exists?: boolean; created_at?: string }>;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
  output_dir?: string;
}

export interface SandboxTask {
  id: string;
  tool: string;
  model: string;
  input?: Record<string, any>;
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
  capabilities?: Record<string, any>;
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
  const resp = await fetch('/api/stages');
  const data = await resp.json();
  return data.stages;
}

export async function fetchSessions(): Promise<any[]> {
  const resp = await fetch('/api/sessions');
  const data = await resp.json();
  return data.sessions || [];
}

async function postPipelineTask(path: string, params: Record<string, any>): Promise<PipelineStartResponse> {
  const resp = await fetch(path, {
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

export async function startStandardPipeline(params: Record<string, any>): Promise<PipelineStartResponse> {
  return postPipelineTask('/api/pipelines/standard/tasks', params);
}

export async function startActionTransferPipeline(params: Record<string, any>): Promise<PipelineStartResponse> {
  return postPipelineTask('/api/pipelines/action_transfer/tasks', params);
}

export async function startDigitalHumanPipeline(params: Record<string, any>): Promise<PipelineStartResponse> {
  return postPipelineTask('/api/pipelines/digital_human/tasks', params);
}

export async function fetchPipelineTasks(limit = 100): Promise<PipelineTask[]> {
  const resp = await fetch(`/api/tasks?limit=${limit}`);
  if (!resp.ok) throw new Error('获取任务历史失败');
  const data = await resp.json();
  return data.tasks || [];
}

export async function fetchPipelineTask(taskId: string): Promise<PipelineTask> {
  const resp = await fetch(`/api/tasks/${taskId}`);
  if (!resp.ok) throw new Error('获取任务状态失败');
  return resp.json();
}

export async function fetchSandboxTasks(): Promise<SandboxTask[]> {
  const resp = await fetch('/api/sandbox/tasks');
  if (!resp.ok) return [];
  const data = await resp.json();
  return data.tasks || [];
}

export async function clearTempCache(): Promise<{ status: string; deleted: number; freed_bytes?: number; freed_mb?: number; errors?: Array<{ path: string; error: string }> }> {
  const resp = await fetch('/api/cache/temp', { method: 'DELETE' });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '清空缓存失败' }));
    throw new Error(err.detail || '清空缓存失败');
  }
  return resp.json();
}

export async function deletePipelineTask(taskId: string): Promise<void> {
  const resp = await fetch(`/api/tasks/${taskId}`, { method: 'DELETE' });
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
  let detail: any = null;
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
  const resp = await fetch(`/api/models${search.toString() ? `?${search.toString()}` : ''}`, { cache: 'no-store' });
  if (!resp.ok) throw new Error('获取模型列表失败');
  const data = await resp.json();
  return data.models || [];
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
  model: Record<string, any>;
  provider: Record<string, any> | string;
  model_type: 'llm' | 'vlm' | 't2i' | 'i2i' | 'video';
}): Promise<ModelTestResult> {
  const resp = await fetch('/api/models/test', {
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
  const resp = await fetch('/api/pipelines/standard/templates');
  if (!resp.ok) throw new Error('获取模版列表失败');
  const data = await resp.json();
  return data.templates || [];
}

export async function uploadMedia(file: File): Promise<{ filename: string; file_path: string }> {
  const formData = new FormData();
  formData.append('file', file);
  const resp = await fetch('/api/upload_media', {
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
): Promise<{ status: string; path: string; artifact: any; status_map: Record<string, string> }> {
  const formData = new FormData();
  formData.append('item_type', itemType);
  formData.append('item_id', itemId);
  formData.append('file', file);
  const resp = await fetch(`/api/project/${sessionId}/artifact/${stage}/upload_image`, {
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
  const source = new EventSource(`${STREAM_API_BASE}/api/tasks/${taskId}/events`);
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
  style?: string;
  video_ratio?: string;
  video_resolution?: string;
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
}): Promise<{ session_id: string; status: string; params: any }> {
  const resp = await fetch('/api/project/start', {
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
  const resp = await fetch(`/api/project/${sessionId}/status`);
  if (!resp.ok) throw new Error('Failed to get project status');
  return resp.json();
}

// 兼容旧路由名；后端实际会通过统一的 workflow state 入口返回状态。
export async function getProjectStatusFromDisk(sessionId: string): Promise<any> {
  const resp = await fetch(`/api/project/${sessionId}/status/from_disk`);
  if (!resp.ok) throw new Error('Failed to get project status snapshot');
  return resp.json();
}

export async function getArtifact(sessionId: string, stage: string): Promise<any> {
  const resp = await fetch(`/api/project/${sessionId}/artifact/${stage}`);
  if (!resp.ok) throw new Error(`Artifact for stage '${stage}' not found`);
  return resp.json();
}

export async function checkSceneAssets(sessionId: string, sceneNumber: number): Promise<{
  scene_number: number;
  reference_images: number;
  videos: number;
  shot_count: number;
}> {
  const resp = await fetch(`/api/project/${sessionId}/scene/${sceneNumber}/assets`);
  if (!resp.ok) return { scene_number: sceneNumber, reference_images: 0, videos: 0, shot_count: 0 };
  return resp.json();
}

export async function executeStage(
  sessionId: string,
  stage: string,
  inputData: Record<string, any> = {},
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
  modifications: Record<string, any>,
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
  const resp = await fetch(`/api/project/${sessionId}/stop`, {
    method: 'POST',
  });
  return resp.json();
}

export async function updateModels(
  sessionId: string,
  models: Partial<Record<string, string | boolean>>,
): Promise<{ status: string }> {
  const resp = await fetch(`/api/project/${sessionId}/models`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(models),
  });
  return resp.json();
}

export async function deleteSession(
  sessionId: string,
): Promise<{ status: string }> {
  const resp = await fetch(`/api/sessions/${sessionId}`, {
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
  selections: Record<string, any>,
): Promise<{ status: string }> {
  const resp = await fetch(`/api/project/${sessionId}/artifact/${stage}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(selections),
  });
  if (!resp.ok) throw new Error('保存选项失败');
  return resp.json();
}

export async function continueWorkflow(sessionId: string): Promise<{ status: string; next_stage?: string }> {
  const resp = await fetch(`/api/project/${sessionId}/continue`, {
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
