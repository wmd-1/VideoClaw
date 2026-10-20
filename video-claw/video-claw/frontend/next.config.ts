import type { NextConfig } from "next";
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

  return {
    // Produce a self-contained server under .next/standalone for slimmer production images.
    output: "standalone",
    async rewrites() {
      return [
        {
          source: "/code/:path*",
          destination: `${backendUrl}/code/:path*`,
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
