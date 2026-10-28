import type { NextConfig } from "next";
import { readFileSync } from "node:fs";
import path from "node:path";
import { PHASE_PRODUCTION_BUILD } from "next/constants";

// Address of the backend the Next.js server-side rewrite proxy forwards to.
// - Local dev: leave unset -> falls back to http://127.0.0.1:8000 (unchanged behaviour)
// - Docker / host production build: BACKEND_INTERNAL_URL + NEXT_PUBLIC_BACKEND_PORT
//   are REQUIRED; missing values fail the build instead of silently baking :8000
//   (openspec: review-fifth-minor-followups M-4).
const REQUIRED_BUILD_ENV = ["BACKEND_INTERNAL_URL", "NEXT_PUBLIC_BACKEND_PORT"] as const;
const DEV_BACKEND_URL = "http://127.0.0.1:8000";

function missingBuildEnv(): string[] {
  return REQUIRED_BUILD_ENV.filter(name => !process.env[name]?.trim());
}

// Pipeline 入口开关（未开放 / 是否隐藏）。前端只能读构建期常量，
// 所以优先取进程环境，其次回退到仓库根 .env（与 BACKEND_PORT 同一份文件）；
// 改完 .env 必须重新构建前端产物（npm run build + 重组 .next-runtime）才生效。
// 未出现在 PIPELINE_FLAG_DEFAULTS 中的开关默认 false（= 未开放）。
const PIPELINE_FLAG_KEYS = [
  "NEXT_PUBLIC_ENABLE_STANDARD", // 模板制作：默认未开放
  "NEXT_PUBLIC_ENABLE_DIGITAL_HUMAN", // “其他”入口（原数字人口播）：默认未开放
  "NEXT_PUBLIC_HIDE_ACTION_TRANSFER", // 动作迁移：默认隐藏入口
] as const;

const PIPELINE_FLAG_DEFAULTS: Record<string, boolean> = {
  NEXT_PUBLIC_HIDE_ACTION_TRANSFER: true,
};

// SLB 统一入口前缀：外部以 /Bocom_Video_Generation/... 访问，SLB 保留前缀转发给前端，
// 所以前端整体挂在 basePath 下（页面、客户端路由、静态资源、rewrites 都会被自动加前缀）。
// 前端只能读构建期常量，因此前缀同样在构建时固化；改成空字符串即回到根路径部署。
const DEFAULT_BASE_PATH = "/Bocom_Video_Generation";

function normalizeBasePath(raw: string | undefined): string {
  const value = String(raw ?? "").trim();
  if (!value || value === "/") return "";
  const withLeading = value.startsWith("/") ? value : `/${value}`;
  return withLeading.replace(/\/+$/, "").replace(/\/{2,}/g, "/");
}

function resolveBasePath(): string {
  const fileEnv = readRepoDotEnv();
  // 显式配置优先（进程环境 > 仓库根 .env）；显式留空或设为 "/" 表示根路径部署；
  // 完全没有配置时才使用默认 SLB 前缀。
  const explicit =
    process.env.NEXT_PUBLIC_BASE_PATH !== undefined
      ? process.env.NEXT_PUBLIC_BASE_PATH
      : Object.prototype.hasOwnProperty.call(fileEnv, "NEXT_PUBLIC_BASE_PATH")
        ? fileEnv.NEXT_PUBLIC_BASE_PATH
        : undefined;
  if (explicit === undefined) return normalizeBasePath(DEFAULT_BASE_PATH);
  return normalizeBasePath(explicit);
}

function readRepoDotEnv(): Record<string, string> {
  // 构建时 cwd = frontend 目录；仓库根 .env 在向上三级
  const envPath = path.resolve(process.cwd(), "../../../.env");
  try {
    const values: Record<string, string> = {};
    for (const rawLine of readFileSync(envPath, "utf-8").split("\n")) {
      const line = rawLine.trim();
      if (!line || line.startsWith("#") || !line.includes("=")) continue;
      const key = line.slice(0, line.indexOf("=")).replace(/^export\s+/, "").trim();
      let value = line.slice(line.indexOf("=") + 1).trim();
      if (value.length >= 2 && value[0] === value[value.length - 1] && (value[0] === '"' || value[0] === "'")) {
        value = value.slice(1, -1);
      }
      if (key) values[key] = value;
    }
    return values;
  } catch {
    // 仓库根无 .env（如镜像内构建）时按未配置处理 = 入口关闭
    return {};
  }
}

function resolvePipelineFlags(): Record<string, string> {
  const fileEnv = readRepoDotEnv();
  const flags: Record<string, string> = {};
  for (const key of PIPELINE_FLAG_KEYS) {
    const raw = (process.env[key] ?? fileEnv[key] ?? "").trim().toLowerCase();
    if (!raw) {
      // 未配置时按各开关的默认值注入（缺省 false）
      flags[key] = PIPELINE_FLAG_DEFAULTS[key] ? "true" : "false";
      continue;
    }
    flags[key] = raw === "true" || raw === "1" || raw === "yes" ? "true" : "false";
  }
  return flags;
}

export default function nextConfig(phase: string): NextConfig {
  const missing = phase === PHASE_PRODUCTION_BUILD ? missingBuildEnv() : [];
  if (missing.length > 0) {
    throw new Error(
      `前端生产构建缺少必需环境变量: ${missing.join(", ")}。` +
        "宿主构建请显式提供 BACKEND_INTERNAL_URL 与 NEXT_PUBLIC_BACKEND_PORT；" +
        "镜像构建请使用 ./build-frontend.sh（读取 .env 的 BACKEND_PORT）。" +
        "两个值会被烤进 standalone 产物，缺失时无法在运行时修复。",
    );
  }
  const backendUrl =
    process.env.BACKEND_INTERNAL_URL?.trim() || DEV_BACKEND_URL;
  const basePath = resolveBasePath();

  return {
    // Produce a self-contained server under .next/standalone for slimmer production images.
    output: "standalone",
    // SLB 统一入口前缀（空 = 部署在根路径）；basePath 会自动作用于页面、
    // 客户端路由、静态资源与下面 rewrites 的 source。
    basePath: basePath || undefined,
    // 注入到浏览器侧的构建期常量（见 resolvePipelineFlags / resolveBasePath）
    env: { ...resolvePipelineFlags(), NEXT_PUBLIC_BASE_PATH: basePath },
    async rewrites() {
      return [
        {
          source: "/code/:path*",
          destination: `${backendUrl}/code/:path*`,
        },
        {
          source: "/api/identity/:path*",
          destination: `${backendUrl}/api/identity/:path*`,
        },
        {
          source: "/api/sessions",
          destination: `${backendUrl}/api/sessions`,
        },
        {
          source: "/api/sessions/:path*",
          destination: `${backendUrl}/api/sessions/:path*`,
        },
        // 工作流 API
        {
          source: "/api/project/:path*",
          destination: `${backendUrl}/api/project/:path*`,
        },
        {
          source: "/api/stages",
          destination: `${backendUrl}/api/stages`,
        },
        {
          source: "/api/upload_media",
          destination: `${backendUrl}/api/upload_media`,
        },
        {
          // 首页文档上传（txt/doc/docx/md/pdf）：曾经只在浏览器侧硬编码寻址，Docker 部署下
          // 必失败；现与其他接口一致走同源相对路径 + rewrite。
          // 寻址约束见 OpenSpec capability「frontend-api-addressing」。
          source: "/api/upload_file",
          destination: `${backendUrl}/api/upload_file`,
        },
        {
          source: "/api/models",
          destination: `${backendUrl}/api/models`,
        },
        {
          source: "/api/models/:path*",
          destination: `${backendUrl}/api/models/:path*`,
        },
        {
          source: "/api/config",
          destination: `${backendUrl}/api/config`,
        },
        {
          source: "/api/cache/:path*",
          destination: `${backendUrl}/api/cache/:path*`,
        },
        // 一键 pipeline API
        {
          source: "/api/pipelines",
          destination: `${backendUrl}/api/pipelines`,
        },
        {
          source: "/api/pipelines/:path*",
          destination: `${backendUrl}/api/pipelines/:path*`,
        },
        {
          source: "/api/tasks",
          destination: `${backendUrl}/api/tasks`,
        },
        {
          source: "/api/tasks/:path*",
          destination: `${backendUrl}/api/tasks/:path*`,
        },
        // 临时工作台 API
        {
          source: "/api/sandbox/:path*",
          destination: `${backendUrl}/api/sandbox/:path*`,
        },
      ];
    },
  };
}
