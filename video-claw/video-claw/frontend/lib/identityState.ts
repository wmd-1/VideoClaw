/**
 * 前端有效身份解析（无 React 依赖，纯内存）。
 *
 * 单一来源：API 头（`identityHeaders`）与媒体 URL（`basePath` 的 `?user_login=`）
 * MUST 都读本模块的 `getLoginName()`。身份不落盘：刷新 / 重开标签页重新解析，
 * 避免共享机器串号。
 *
 * 解析顺序：single 固定 `local` → `IdentityProvider` 写入的有效身份。
 * `?User=` 仅由引导流程通过 `getUrlLogin()` 读取；本模块不再回退 URL 或本机存储。
 */
export const LEGACY_LOGIN_KEY = "video-claw.login-name";
export const SINGLE_LOGIN = "local";

let resolvedMode: "guip" | "single" | null = null;
let resolvedLogin: string | null = null;

/** 清理历史版本遗留的身份缓存键；只清身份键，不触碰 UI 偏好。 */
function purgeLegacyLoginKey(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(LEGACY_LOGIN_KEY);
  } catch {
    // 存储不可用时无需清理
  }
}

purgeLegacyLoginKey();

export function getUrlLogin(): string {
  if (typeof window === "undefined") return "";
  try {
    const params = new URLSearchParams(window.location.search);
    return (params.get("User") || params.get("user") || "").trim();
  } catch {
    return "";
  }
}

/** 当前有效身份；未解析时为空串，不回退 `?User=` 或本机存储。 */
export function getLoginName(): string {
  if (typeof window === "undefined") return "";
  if (resolvedMode === "single") return SINGLE_LOGIN;
  return resolvedLogin ?? "";
}

/** 身份模式确认后固定有效身份；此后不再被 `?User` 重新覆盖。 */
export function setResolvedIdentity(mode: "guip" | "single", login: string): void {
  resolvedMode = mode;
  resolvedLogin = mode === "single" ? SINGLE_LOGIN : login.trim();
}
