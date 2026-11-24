/** 视频资源池只读 API 类型（后端 /api/video/profiles）。 */
import { apiFetch } from '@/lib/identityGuard';

export interface VideoPoolObservation {
  state: string;
  observed_at?: number;
  coverage?: string;
  stale?: boolean;
}

export interface VideoProfile {
  id: string;
  name?: string;
  revision?: number;
  enabled: boolean;
  state: 'ready' | 'busy' | 'not_ready' | string;
  dispatchable?: boolean;
  contracts: string[];
  instances: string[];
  missing?: Array<{ field: string; code: string }>;
  blocking_reasons?: string[];
  observation?: VideoPoolObservation;
}

export interface VideoProfilesPayload {
  dispatch_enabled: boolean;
  dispatch_stopped?: boolean;
  draining_profiles?: string[];
  draining_instances?: string[];
  config_revision: string;
  profiles_file?: string;
  has_default_recommendation: boolean;
  recommendation_reason: string;
  profiles: VideoProfile[];
}

export async function fetchVideoProfiles(): Promise<VideoProfilesPayload> {
  const resp = await apiFetch('/api/video/profiles');
  if (!resp.ok) {
    throw new Error('读取视频资源池失败');
  }
  return resp.json();
}


export interface VideoJobView {
  job_id: string;
  state: string;
  dispatch_phase?: string;
  error_detail?: unknown;
}

export async function fetchVideoJob(jobId: string): Promise<VideoJobView> {
  const resp = await apiFetch(`/api/video/jobs/${jobId}`);
  if (!resp.ok) throw new Error('读取视频作业失败');
  return resp.json();
}
