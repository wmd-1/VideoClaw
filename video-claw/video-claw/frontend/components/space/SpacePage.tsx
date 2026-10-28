"use client";

import { Calendar, Download, Film, Library, Loader2, Star, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { mediaUrl, savedMediaUrl } from "@/lib/basePath";
import { isPipelineAvailable, isPipelineHidden } from "@/lib/pipelineAvailability";
import {
  fetchCounts,
  fetchSaved,
  fetchWorks,
  saveWork,
  unsaveWork,
  type SpaceCounts,
  type SpaceDomain,
  type WorkItem,
  type WorkPage,
} from "./spaceApi";

const DOMAIN_LABEL: Record<string, string> = {
  main: "主流程",
  standard: "模板制作",
  action_transfer: "动作迁移",
  digital_human: "其他",
  all: "全部",
};

type SpaceTab = SpaceDomain | "saved";

const MODULE_TABS = [
  { id: "standard", label: "模板制作", pipeline: "standard" },
  { id: "action_transfer", label: "动作迁移", pipeline: "action_transfer" },
  { id: "digital_human", label: "其他", pipeline: "digital_human" },
] as const;

// 与侧边栏共用构建期开关：隐藏或未开放的模块不在个人空间出现。
const VISIBLE_MODULES = MODULE_TABS.filter(
  item => !isPipelineHidden(item.pipeline) && isPipelineAvailable(item.pipeline),
);
const VISIBLE_DOMAINS = new Set<string>(["main", ...VISIBLE_MODULES.map(item => item.id)]);

const TABS: Array<{ id: SpaceTab; label: string }> = [
  { id: "all", label: "全部" },
  { id: "main", label: "主流程" },
  ...VISIBLE_MODULES.map(item => ({ id: item.id as SpaceTab, label: item.label })),
  { id: "saved", label: "我的收藏" },
];

function timestamp(value?: number) {
  if (!value) return "";
  const date = new Date(value);
  return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()}`;
}

function WorkCard({ item, onSaved, onUnsaved }: {
  item: WorkItem;
  onSaved: () => void;
  onUnsaved: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const media = item.media?.[0];
  const isSaved = Boolean(item.saved_id) || item.source === "saved";
  const src = item.source === "saved" && item.saved_id
    ? savedMediaUrl(item.saved_id)
    : media?.url ? mediaUrl(media.url) : "";
  const download = item.source === "saved" && item.saved_id
    ? savedMediaUrl(item.saved_id, { download: true })
    : media?.url ? mediaUrl(media.url, { download: true }) : "";

  const toggleSave = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      if (isSaved && item.saved_id) {
        await unsaveWork(item.saved_id);
        onUnsaved();
      } else if (!isSaved) {
        await saveWork(item.work_id);
        onSaved();
      }
    } catch (exc) {
      setError((exc as Error)?.message || "操作失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm">
      <div className="relative aspect-video bg-gradient-to-br from-slate-800 to-slate-500">
        {src && item.available !== false ? (
          <video src={src} controls preload="metadata" className="h-full w-full object-contain" />
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-white/80">
            {item.source === "saved" ? "收藏副本不可用" : "暂无可播放成片"}
          </div>
        )}
        <span className="absolute left-3 top-3 rounded bg-black/55 px-2 py-1 text-[10px] font-medium text-white">
          {DOMAIN_LABEL[item.domain] || item.domain}
        </span>
      </div>
      <div className="p-4">
        <h3 className="truncate text-sm font-semibold text-gray-800" title={item.title}>
          {item.title || "未命名作品"}
        </h3>
        <div className="mt-2 flex items-center gap-2 text-xs text-gray-400">
          <Calendar className="h-3.5 w-3.5" />
          {timestamp(item.completed_at)}
          {item.episode ? <span>第 {item.episode} 集</span> : null}
          {item.source_deleted ? <span className="text-amber-600">源已删除</span> : null}
        </div>
        {error ? <p className="mt-2 text-xs text-red-500">{error}</p> : null}
        <div className="mt-4 flex items-center gap-2">
          {download ? (
            <a
              href={download}
              className="inline-flex items-center gap-1 rounded-lg bg-gray-50 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-100"
            >
              <Download className="h-3.5 w-3.5" />
              下载
            </a>
          ) : null}
          {item.source !== "saved" ? (
            <button
              type="button"
              onClick={toggleSave}
              disabled={busy || isSaved}
              className="ml-auto inline-flex items-center gap-1 rounded-lg px-3 py-1.5 text-xs disabled:opacity-40"
            >
              <Star className={`h-3.5 w-3.5 ${isSaved ? "fill-amber-400 text-amber-500" : "text-gray-400"}`} />
              {isSaved ? "已收藏" : "收藏"}
            </button>
          ) : null}
          {item.source === "saved" ? (
            <button
              type="button"
              onClick={toggleSave}
              disabled={busy}
              className="inline-flex items-center gap-1 rounded-lg px-3 py-1.5 text-xs text-red-500 hover:bg-red-50"
            >
              <Trash2 className="h-3.5 w-3.5" />
              取消
            </button>
          ) : null}
        </div>
      </div>
    </article>
  );
}

export default function SpacePage() {
  const [tab, setTab] = useState<SpaceTab>("all");
  const [page, setPage] = useState<WorkPage>({ items: [], total: 0, limit: 24, offset: 0 });
  const [counts, setCounts] = useState<SpaceCounts>({});
  const [savedVisibleCount, setSavedVisibleCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [next, nextCounts, allSaved] = await Promise.all([
        tab === "saved"
          ? fetchSaved(100, 0)
          : tab === "all"
            ? fetchWorks("all", 100, 0)
            : fetchWorks(tab, 24, 0),
        fetchCounts(),
        fetchSaved(100, 0),
      ]);
      const visibleSaved = allSaved.items.filter(item => VISIBLE_DOMAINS.has(item.domain));
      const items = next.items.filter(item => VISIBLE_DOMAINS.has(item.domain));
      setPage({
        ...next,
        items,
        total: tab === "all" || tab === "saved" ? items.length : next.total,
      });
      setCounts(nextCounts);
      setSavedVisibleCount(visibleSaved.length);
    } catch (exc) {
      setError((exc as Error)?.message || "读取个人空间失败");
    } finally {
      setLoading(false);
    }
  }, [tab]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  const allCount = useMemo(
    () => Array.from(VISIBLE_DOMAINS).reduce((sum, key) => sum + (counts[key] || 0), 0),
    [counts],
  );

  const countFor = (id: SpaceTab) => {
    if (id === "all") return allCount;
    if (id === "saved") return savedVisibleCount;
    return counts[id] || 0;
  };

  return (
    <div className="min-h-screen bg-gray-50/60">
      <main className="mx-auto w-full max-w-6xl px-6 pb-12 pt-10">
        <div className="mb-7">
          <div className="flex items-center gap-2">
            <Library className="h-7 w-7 text-blue-500" />
            <h1 className="text-2xl font-bold text-gray-800">个人空间</h1>
          </div>
          <p className="mt-2 text-sm text-gray-500">聚合已完成作品与收藏；收藏会复制到独立存储，源删除后仍可播放。</p>
        </div>

        <div className="mb-6 flex flex-wrap gap-2">
          {TABS.map(item => (
            <button
              key={item.id}
              type="button"
              onClick={() => setTab(item.id)}
              className={`rounded-xl px-4 py-2 text-sm font-medium ${
                tab === item.id ? "bg-blue-500 text-white" : "bg-white text-gray-600 hover:bg-blue-50"
              }`}
            >
              {item.label}
              <span className={`ml-1.5 text-xs ${tab === item.id ? "text-blue-100" : "text-gray-400"}`}>
                {countFor(item.id)}
              </span>
            </button>
          ))}
        </div>

        {error ? (
          <div className="rounded-2xl border border-red-100 bg-red-50 p-5 text-sm text-red-600">
            {error}
            <button type="button" onClick={() => void load()} className="ml-3 underline">重试</button>
          </div>
        ) : loading ? (
          <div className="flex h-56 items-center justify-center text-sm text-gray-400">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            正在读取作品
          </div>
        ) : page.items.length === 0 ? (
          <div className="rounded-2xl border border-gray-200 bg-white p-12 text-center text-sm text-gray-400">
            <Film className="mx-auto mb-3 h-8 w-8 text-gray-300" />
            {tab === "saved" ? "还没有收藏，去作品卡片点星标收藏吧。" : "该分类还没有成片作品。"}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
            {page.items.map(item => (
              <WorkCard
                key={item.saved_id || item.work_id}
                item={item}
                onSaved={() => void load()}
                onUnsaved={() => void load()}
              />
            ))}
          </div>
        )}

        {!loading && !error && page.total > page.items.length ? (
          <p className="mt-6 text-center text-xs text-gray-400">
            当前显示 {page.items.length} / {page.total}，后续分页将在作品数量增长后启用。
          </p>
        ) : null}
      </main>
    </div>
  );
}
