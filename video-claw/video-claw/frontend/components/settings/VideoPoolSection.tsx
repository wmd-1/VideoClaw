'use client';

import React, { useEffect, useState } from 'react';
import { AlertTriangle, Loader2, Pause, Play, RefreshCw, Save } from 'lucide-react';
import { apiFetch } from '@/lib/identityGuard';
import { fetchVideoProfiles, type VideoProfilesPayload } from '@/lib/videoPools';

/** 管理员视频资源池：YAML 原文编辑 + expected_revision 保存 + 排空/停用动作。 */
export default function VideoPoolSection({ isAdmin }: { isAdmin: boolean }) {
  const [payload, setPayload] = useState<VideoProfilesPayload | null>(null);
  const [revision, setRevision] = useState('');
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [diagnostics, setDiagnostics] = useState<{ quarantined?: unknown[]; deleting_sources?: unknown[] } | null>(null);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const profiles = await fetchVideoProfiles();
      setPayload(profiles);
      if (!isAdmin) {
        setRevision(profiles.config_revision || '');
        setDraft('');
        return;
      }
      const raw = await apiFetch('/api/video/profiles/raw');
      if (!raw.ok) throw new Error('读取池文件失败（需要管理员权限）');
      const data = await raw.json();
      setRevision(data.revision || profiles.config_revision || '');
      setDraft(JSON.stringify(data.values || {}, null, 2));
      const diag = await apiFetch('/api/video/resources/diagnostics');
      if (diag.ok) setDiagnostics(await diag.json());
    } catch (e: unknown) {
      setError((e as Error)?.message || '读取资源池失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const save = async () => {
    setBusy('save');
    setError('');
    setMessage('');
    try {
      const values = JSON.parse(draft);
      const resp = await apiFetch('/api/video/profiles', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values, expected_revision: revision }),
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        throw new Error(detail.detail || '保存资源池失败');
      }
      setMessage('资源池已保存并热加载');
      await load();
    } catch (e: unknown) {
      setError((e as Error)?.message || '保存资源池失败');
    } finally {
      setBusy('');
    }
  };

  const action = async (path: string, body?: unknown) => {
    setBusy(path);
    setError('');
    try {
      const resp = await apiFetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      if (!resp.ok) throw new Error('操作失败');
      setMessage('操作已生效');
      await load();
    } catch (e: unknown) {
      setError((e as Error)?.message || '操作失败');
    } finally {
      setBusy('');
    }
  };

  return (
    <section className="rounded-2xl border border-gray-200 bg-white p-5 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-gray-800">视频自动调度 / 资源池</h2>
          <p className="mt-1 text-xs text-gray-500">
            {payload?.profiles_file || '运行时 YAML'} · 配置来源：.env 的 VC_VIDEO_PROFILES_FILE（只读）
          </p>
        </div>
        <button onClick={() => void load()} disabled={loading} className="inline-flex items-center gap-1 rounded-lg bg-gray-100 px-2.5 py-1.5 text-xs text-gray-600">
          <RefreshCw className="h-3.5 w-3.5" />刷新
        </button>
      </div>

      {!isAdmin && (
        <p className="mb-3 flex items-center gap-1 text-xs text-amber-600">
          <AlertTriangle className="h-3.5 w-3.5" />当前账号只读，池保存与调度动作仅管理员可用。
        </p>
      )}
      {loading ? (
        <p className="flex items-center gap-2 text-xs text-gray-400"><Loader2 className="h-3.5 w-3.5 animate-spin" />正在读取资源池</p>
      ) : (
        <div className="space-y-3">
          <div className="grid gap-2 md:grid-cols-2">
            {(payload?.profiles || []).map(profile => (
              <div key={profile.id} className="rounded-xl border border-gray-100 bg-gray-50/70 p-3 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-gray-700">{profile.name || profile.id}</span>
                  <span className={profile.state === 'ready' ? 'text-green-600' : profile.state === 'busy' ? 'text-amber-600' : 'text-gray-400'}>{profile.state}</span>
                </div>
                <p className="mt-1 text-[11px] text-gray-400">{profile.instances.join(', ') || '无实例'} · {profile.contracts.join('/') || '无契约'}</p>
                <div className="mt-1 text-[10px] text-gray-400">观测：{profile.observation?.state || 'unknown'}{profile.observation?.stale ? '（过期）' : ''}</div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {profile.instances.map(instanceId => (
                    <button
                      key={instanceId}
                      disabled={!isAdmin || !!busy}
                      onClick={() => {
                        if (!window.confirm(`对 ${instanceId} 执行资格检查？`)) return;
                        void action(`/api/video/instances/${instanceId}/qualify`, {
                          checks: { config: true, probe: true, jobs: true, metrics: true, download: true },
                          confirmed: true,
                          generate_test: false,
                        });
                      }}
                      className="rounded bg-white px-2 py-1 text-[10px] text-gray-600"
                    >
                      资格检查 · {instanceId}
                    </button>
                  ))}
                  {payload?.draining_profiles?.includes(profile.id) ? (
                    <button disabled={!isAdmin || !!busy} onClick={() => void action(`/api/video/profiles/${profile.id}/undrain`)} className="inline-flex items-center gap-1 rounded bg-white px-2 py-1 text-[11px] text-gray-600">恢复</button>
                  ) : (
                    <button disabled={!isAdmin || !!busy} onClick={() => void action(`/api/video/profiles/${profile.id}/drain`)} className="inline-flex items-center gap-1 rounded bg-white px-2 py-1 text-[11px] text-gray-600"><Pause className="h-3 w-3" />排空</button>
                  )}
                </div>
              </div>
            ))}
          </div>
          {diagnostics && (
            <p className="text-[11px] text-gray-500">
              诊断：隔离槽 {diagnostics.quarantined?.length || 0} · 删除中来源 {diagnostics.deleting_sources?.length || 0}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <button disabled={!isAdmin || !!busy} onClick={() => void action('/api/video/dispatch/stop')} className="inline-flex items-center gap-1 rounded-lg bg-amber-50 px-3 py-1.5 text-xs text-amber-700"><Pause className="h-3.5 w-3.5" />停用新派发</button>
            <button disabled={!isAdmin || !!busy} onClick={() => void action('/api/video/dispatch/resume')} className="inline-flex items-center gap-1 rounded-lg bg-green-50 px-3 py-1.5 text-xs text-green-700"><Play className="h-3.5 w-3.5" />恢复派发</button>
            <button disabled={!isAdmin || !!busy} onClick={() => void action('/api/video/resources/deleting/reconcile')} className="rounded-lg bg-gray-100 px-3 py-1.5 text-xs text-gray-600">删除中恢复</button>
          </div>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] text-gray-400">运行时 YAML（JSON 编辑；保存需 expected_revision={revision}）</span>
            <textarea
              value={draft}
              onChange={e => setDraft(e.target.value)}
              disabled={!isAdmin}
              className="min-h-[220px] w-full rounded-xl border border-gray-200 bg-gray-50 p-3 font-mono text-[11px] text-gray-700 outline-none"
            />
          </label>
          <button
            onClick={() => void save()}
            disabled={!isAdmin || busy === 'save'}
            className="inline-flex items-center gap-1 rounded-xl bg-blue-500 px-4 py-2 text-xs font-medium text-white disabled:bg-gray-200"
          >
            {busy === 'save' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}保存资源池
          </button>
          {message && <p className="text-xs text-green-600">{message}</p>}
          {error && <p className="text-xs text-red-600">{error}</p>}
        </div>
      )}
    </section>
  );
}
