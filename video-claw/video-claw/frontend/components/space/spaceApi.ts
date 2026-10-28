"use client";

import { apiFetch } from "@/lib/identityGuard";

export type SpaceDomain = "all" | "main" | "standard" | "action_transfer" | "digital_human";

export type SpaceMedia = {
  kind: string;
  name: string;
  url: string;
  size?: number | null;
  media_type?: string;
};

export type WorkItem = {
  work_id: string;
  source: "session" | "task" | "saved";
  domain: string;
  title: string;
  episode?: number | null;
  created_at: number;
  completed_at: number;
  media: SpaceMedia[];
  saved: boolean;
  saved_id?: string;
  saved_at?: number;
  source_deleted?: boolean;
  available?: boolean;
};

export type WorkPage = {
  items: WorkItem[];
  total: number;
  limit: number;
  offset: number;
};

export type SpaceCounts = Record<string, number>;

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const detail = await resp.text().catch(() => "");
    throw new Error(detail || `请求失败（${resp.status}）`);
  }
  return resp.json();
}

export async function fetchWorks(domain: SpaceDomain, limit = 24, offset = 0): Promise<WorkPage> {
  return json<WorkPage>(
    await apiFetch(`/api/space/works?domain=${encodeURIComponent(domain)}&limit=${limit}&offset=${offset}`, {
      cache: "no-store",
    }),
  );
}

export async function fetchCounts(): Promise<SpaceCounts> {
  return json<SpaceCounts>(await apiFetch("/api/space/counts", { cache: "no-store" }));
}

export async function fetchSaved(limit = 24, offset = 0): Promise<WorkPage> {
  return json<WorkPage>(
    await apiFetch(`/api/space/saved?limit=${limit}&offset=${offset}`, { cache: "no-store" }),
  );
}

export async function saveWork(workId: string): Promise<WorkItem> {
  return json<WorkItem>(
    await apiFetch("/api/space/saved", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ work_id: workId }),
    }),
  );
}

export async function unsaveWork(savedId: string): Promise<void> {
  const resp = await apiFetch(`/api/space/saved/${encodeURIComponent(savedId)}`, { method: "DELETE" });
  if (!resp.ok) throw new Error((await resp.text().catch(() => "")) || "取消收藏失败");
}
