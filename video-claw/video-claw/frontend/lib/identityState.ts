/**
 * 前端有效身份解析（无 React 依赖）。
 *
 * 单一来源：API 头（`identityHeaders`）与媒体 URL（`basePath` 的 `?user_login=`）
 * MUST 都读本模块的 `getLoginName()`。曾经媒体侧直接读 localStorage，而 API 侧每次
 * 重新解析 `?User` 并回写存储，导致 single 模式下 API 固定 `local`、媒体 URL 却变成
 * `alice`，媒体网关按 owner 比对后 404。
 *
 * 解析顺序：single 固定 `local` → 已解析身份（`/api/identity/me` 的结论） →
 * 首次解析前的 `?User` 调试参数 → 本机缓存。
 */
export const LOGIN_KEY = "video-claw.login-name";
export const SINGLE_LOGIN = "local";

let resolvedMode: "guip" | "single" | null = null;
let resolvedLogin: string | null = null;

export function getUrlLogin(): string {
  if (typeof window === "undefined") return "";
  try {
    const params = new URLSearchParams(window.location.search);
    return (params.get("User") || params.get("user") || "").trim();
  } catch {
    return "";
  }
}

export function getStoredLogin(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(LOGIN_KEY) || "";
  } catch {
    return "";
  }
}

export function setStoredLogin(login: string) {
  if (typeof window === "undefined") return;
  const value = login.trim();
  try {
    if (value) window.localStorage.setItem(LOGIN_KEY, value);
    else window.localStorage.removeItem(LOGIN_KEY);
  } catch {
    // 隐私模式下 localStorage 不可写：身份仍以内存解析结果为准
  }
}

export function getLoginName(): string {
  if (typeof window === "undefined") return "";
  if (resolvedMode === "single") return SINGLE_LOGIN;
  if (resolvedLogin !== null) return resolvedLogin;
  const urlLogin = getUrlLogin();
  if (urlLogin) {
    setStoredLogin(urlLogin);
    return urlLogin;
  }
  return getStoredLogin();
}

/** 身份模式确认后固定有效身份；此后不再被 `?User` 重新覆盖。 */
export function setResolvedIdentity(mode: "guip" | "single", login: string): void {
  resolvedMode = mode;
  resolvedLogin = mode === "single" ? SINGLE_LOGIN : login.trim();
  setStoredLogin(resolvedLogin);
}
