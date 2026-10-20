#!/usr/bin/env node
// 宿主机 `npm run build` 的快速失败：两个端口变量必须显式提供（openspec: M-4）。
// 镜像构建路径由 Dockerfile.frontend 的 ARG 断言与 build-frontend.sh 保证。
const required = ["BACKEND_INTERNAL_URL", "NEXT_PUBLIC_BACKEND_PORT"];
const missing = required.filter(name => !process.env[name]?.trim());

if (missing.length > 0) {
  console.error(
    [
      `错误：前端生产构建缺少必需环境变量: ${missing.join(", ")}。`,
      "修复：宿主构建请显式提供这两个变量，例如：",
      "  BACKEND_INTERNAL_URL=http://backend:8500 NEXT_PUBLIC_BACKEND_PORT=8500 npm run build",
      "镜像构建请使用 ./build-frontend.sh（从仓库根 .env 读取 BACKEND_PORT）。",
      "后果：端口会被烤进 standalone 产物，缺失时表现为页面正常但 /api/* 全部 500。",
    ].join("\n"),
  );
  process.exit(1);
}
