/**
 * 统一部署前缀（SLB 入口）。
 *
 * 生产经 SLB 以 `/Bocom_Video_Generation` 前缀统一对外，SLB 保留前缀转发给前端，
 * 因此前端必须整体挂在该前缀下（Next `basePath`）+ 所有站内绝对路径自行补前缀。
 *
 * 取值：构建期常量 `NEXT_PUBLIC_BASE_PATH`（由 next.config.ts 注入，默认 `/Bocom_Video_Generation`）。
 * 设为空字符串即关闭前缀（回到根路径部署）。
 */

function normalizeBasePath(raw: string | undefined): string {
  const value = String(raw ?? '').trim();
  // '/' 与空值等价：不挂在任何前缀下
  if (!value || value === '/') return '';
  const withLeading = value.startsWith('/') ? value : `/${value}`;
  return withLeading.replace(/\/+$/, '');
}

export const BASE_PATH = normalizeBasePath(
  process.env.NEXT_PUBLIC_BASE_PATH ?? '/Bocom_Video_Generation',
);

/**
 * 给站内绝对路径补上部署前缀；外部 URL、协议相对地址、相对路径原样返回。
 * 已带前缀的路径不会重复添加（幂等）。
 */
export function withBasePath(path: string): string {
  if (!path || !path.startsWith('/')) return path;
  if (path.startsWith('//')) return path;
  if (!BASE_PATH) return path;
  if (path === BASE_PATH || path.startsWith(`${BASE_PATH}/`)) return path;
  return `${BASE_PATH}${path}`;
}

/** 站内资源路径前缀：后端的媒体（/code/）与接口（/api/）都在这些前缀下 */
const SITE_PATH_PREFIXES = ['/code/', '/api/'];

/**
 * 规整「后端下发的媒体地址」：保留站内路径、补上部署前缀，丢掉域名。
 *
 * 后端部分接口会返回带域名的绝对地址（如 `http://<host>/code/result/video/x.mp4`），
 * 这个地址既没有部署前缀、也可能指向浏览器不可达的内网端口。统一改写成
 * 同源 + 前缀的相对地址（`/Bocom_Video_Generation/code/...`），由前端 rewrite 转发。
 *
 * 外部 CDN 地址、data:/blob: 原样返回。
 */
export function withBasePathUrl(path: string): string {
  if (!path) return path;
  if (path.startsWith('data:') || path.startsWith('blob:')) return path;
  if (path.startsWith('/')) return withBasePath(path);
  if (/^https?:\/\//i.test(path)) {
    try {
      const url = new URL(path);
      if (SITE_PATH_PREFIXES.some(prefix => url.pathname.startsWith(prefix))) {
        return withBasePath(url.pathname) + url.search;
      }
    } catch {
      // 非法 URL 原样返回
    }
    return path;
  }
  return withBasePath(`/${path}`);
}
