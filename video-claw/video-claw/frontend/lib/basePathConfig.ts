/**
 * 部署前缀的纯常量模块（无身份依赖）。
 *
 * RootLayout 需要在服务端拼接静态资源 URL（如 GuipAPI.xc2.js），
 * 不应为此引入身份模块；客户端逻辑仍从 `lib/basePath.ts` 取得同一常量。
 */

export function normalizeBasePath(raw: string | undefined): string {
  const value = String(raw ?? '').trim();
  // '/' 与空值等价：不挂在任何前缀下
  if (!value || value === '/') return '';
  const withLeading = value.startsWith('/') ? value : `/${value}`;
  return withLeading.replace(/\/+$/, '');
}

export const BASE_PATH = normalizeBasePath(
  process.env.NEXT_PUBLIC_BASE_PATH ?? '/Bocom_Video_Generation',
);
