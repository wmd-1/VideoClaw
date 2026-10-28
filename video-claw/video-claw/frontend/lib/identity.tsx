"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { withBasePath } from "@/lib/basePath";

const LOGIN_KEY = "video-claw.login-name";
/** 未设置登录名时的兜底身份：不再拦截登录，直接进入应用 */
const FALLBACK_LOGIN = "local";

type IdentityState = {
  ready: boolean;
  mode: "guip" | "single";
  login: string;
  isAdmin: boolean;
  error: string;
};

type IdentityContextValue = IdentityState & {
  setLogin: (login: string) => void;
  logout: () => void;
};

const IdentityContext = createContext<IdentityContextValue | null>(null);

export function getLoginName(): string {
  if (typeof window === "undefined") return "";
  // 门禁取消后：本地没存登录名时按 FALLBACK_LOGIN 处理，保证请求始终带身份头
  return window.localStorage.getItem(LOGIN_KEY) || FALLBACK_LOGIN;
}

export function setStoredLogin(login: string) {
  if (typeof window === "undefined") return;
  const value = login.trim();
  if (value) window.localStorage.setItem(LOGIN_KEY, value);
  else window.localStorage.removeItem(LOGIN_KEY);
}

export function identityHeaders(headers?: HeadersInit): Headers {
  const merged = new Headers(headers);
  const login = getLoginName();
  if (login) merged.set("X-User-Login-Name", login);
  return merged;
}

/** 站内绝对路径统一补上部署前缀（SLB 入口），外部 URL 原样透传 */
function resolveRequestTarget(input: RequestInfo | URL): RequestInfo | URL {
  if (typeof input === "string") return withBasePath(input);
  return input;
}

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  return fetch(resolveRequestTarget(input), { ...init, headers: identityHeaders(init?.headers) });
}

async function loadMe(): Promise<{ login: string; is_admin: boolean; mode: string }> {
  const resp = await apiFetch("/api/identity/me", { cache: "no-store" });
  if (!resp.ok) {
    const detail = await resp.text().catch(() => "");
    throw new Error(detail || `身份校验失败（${resp.status}）`);
  }
  return resp.json();
}

export function IdentityProvider({ children }: { children: React.ReactNode }) {
  // 门禁已取消：初始即为 ready，任何时候都直接渲染子节点
  const [state, setState] = useState<IdentityState>({
    ready: true,
    mode: "guip",
    login: "",
    isAdmin: false,
    error: "",
  });

  const refresh = useCallback(async () => {
    let mode: "guip" | "single" = "guip";
    try {
      const modeResp = await apiFetch("/api/identity/mode", { cache: "no-store" });
      const modeBody = await modeResp.json();
      mode = modeBody.mode === "single" ? "single" : "guip";
    } catch {
      // 身份模式接口不可用（后端未就绪）时按 guip + 兜底身份继续，不阻塞页面
      setState({ ready: true, mode: "guip", login: getLoginName(), isAdmin: false, error: "" });
      return;
    }
    if (mode === "single") {
      setState({ ready: true, mode, login: "local", isAdmin: true, error: "" });
      return;
    }
    try {
      const me = await loadMe();
      setState({ ready: true, mode, login: me.login, isAdmin: Boolean(me.is_admin), error: "" });
    } catch {
      // 校验失败也不阻塞：沿用本地（或兜底）身份，权限按非管理员处理
      setState({ ready: true, mode, login: getLoginName(), isAdmin: false, error: "" });
    }
  }, []);

  useEffect(() => {
    // 延后到微任务执行：effect 体内同步 setState 会触发级联渲染（react-hooks/set-state-in-effect）
    queueMicrotask(() => void refresh());
  }, [refresh]);

  const setLogin = useCallback((login: string) => {
    setStoredLogin(login);
    void refresh();
  }, [refresh]);

  const logout = useCallback(() => {
    setStoredLogin("");
    void apiFetch("/api/identity/logout", { method: "POST" }).catch(() => undefined);
    refresh();
  }, [refresh]);

  const value = useMemo(
    () => ({ ...state, setLogin, logout }),
    [state, setLogin, logout],
  );

  return <IdentityContext.Provider value={value}>{children}</IdentityContext.Provider>;
}

export function useIdentity(): IdentityContextValue {
  const value = useContext(IdentityContext);
  if (!value) throw new Error("useIdentity must be used inside IdentityProvider");
  return value;
}
