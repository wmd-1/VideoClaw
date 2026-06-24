import type { NextConfig } from "next";

// Address of the backend the Next.js server-side rewrite proxy forwards to.
// - Local dev: leave unset -> falls back to http://127.0.0.1:8000 (unchanged behaviour)
// - Docker / remote: set BACKEND_INTERNAL_URL, e.g. http://backend:8000 (compose service name)
const BACKEND_URL = process.env.BACKEND_INTERNAL_URL || "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // Produce a self-contained server under .next/standalone for slimmer production images.
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/code/:path*",
        destination: `${BACKEND_URL}/code/:path*`,
      },
      {
        source: "/api/sessions",
        destination: `${BACKEND_URL}/api/sessions`,
      },
      {
        source: "/api/sessions/:path*",
        destination: `${BACKEND_URL}/api/sessions/:path*`,
      },
      // 工作流 API
      {
        source: "/api/project/:path*",
        destination: `${BACKEND_URL}/api/project/:path*`,
      },
      {
        source: "/api/stages",
        destination: `${BACKEND_URL}/api/stages`,
      },
      {
        source: "/api/upload_media",
        destination: `${BACKEND_URL}/api/upload_media`,
      },
      {
        source: "/api/models",
        destination: `${BACKEND_URL}/api/models`,
      },
      {
        source: "/api/config",
        destination: `${BACKEND_URL}/api/config`,
      },
      {
        source: "/api/cache/:path*",
        destination: `${BACKEND_URL}/api/cache/:path*`,
      },
      // 一键 pipeline API
      {
        source: "/api/pipelines",
        destination: `${BACKEND_URL}/api/pipelines`,
      },
      {
        source: "/api/pipelines/:path*",
        destination: `${BACKEND_URL}/api/pipelines/:path*`,
      },
      {
        source: "/api/tasks",
        destination: `${BACKEND_URL}/api/tasks`,
      },
      {
        source: "/api/tasks/:path*",
        destination: `${BACKEND_URL}/api/tasks/:path*`,
      },
      // 临时工作台 API
      {
        source: "/api/sandbox/:path*",
        destination: `${BACKEND_URL}/api/sandbox/:path*`,
      },
    ];
  },
};

export default nextConfig;
