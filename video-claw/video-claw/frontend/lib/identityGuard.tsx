"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import IdentityMissing from "@/components/IdentityMissing";
import { withBasePath } from "@/lib/basePath";
import {
  getLoginName,
  getUrlLogin,
  setResolvedIdentity,
} from "@/lib/identityState";

export { getLoginName };

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

export function identityHeaders(headers?: HeadersInit): Headers {
  const merged = new Headers(headers);
  const login = getLoginName();
  if (login) merged.set("X-User-Login-Name", login);
  return merged;
}

function resolveRequestTarget(input: RequestInfo | URL): RequestInfo | URL {
  if (typeof input === "string") return withBasePath(input);
  return input;
}

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  return fetch(resolveRequestTarget(input), { ...init, headers: identityHeaders(init?.headers) });
}

type GuipGlobalInfo = { userInfo?: { loginName?: string } };

type GuipWindow = Window & {
  globalInfo?: () => GuipGlobalInfo;
  promise?: { globalInfo?: () => Promise<GuipGlobalInfo> };
};

/** SDK 解析总预算：短轮询与 promise.globalInfo() 共用，不叠加。 */
const GUIP_RESOLVE_BUDGET_MS = 1500;
const GUIP_POLL_INTERVAL_MS = 100;

function readGuipLogin(info: GuipGlobalInfo | null | undefined): string {
  const login = info?.userInfo?.loginName;
  return typeof login === "string" ? login.trim() : "";
}

/** 等待 SDK 挂载全局方法；耗时计入总预算。 */
async function waitForGuipApi(deadline: number): Promise<GuipWindow> {
  let guip = window as GuipWindow;
  while (Date.now() < deadline) {
    if (typeof guip.promise?.globalInfo === "function" || typeof guip.globalInfo === "function") {
      return guip;
    }
    await new Promise<void>((resolve) => window.setTimeout(resolve, GUIP_POLL_INTERVAL_MS));
    guip = window as GuipWindow;
  }
  return guip;
}

/** GUIP 内网环境：异步 globalInfo 优先，同步兜底；总预算 1.5s。 */
async function detectGuipLogin(): Promise<string> {
  if (typeof window === "undefined") return "";
  const deadline = Date.now() + GUIP_RESOLVE_BUDGET_MS;
  try {
    const guip = await waitForGuipApi(deadline);
    if (typeof guip.promise?.globalInfo === "function") {
      const remaining = Math.max(0, deadline - Date.now());
      const info = await Promise.race([
        guip.promise.globalInfo(),
        new Promise<null>((resolve) => window.setTimeout(() => resolve(null), remaining)),
      ]);
      const login = readGuipLogin(info);
      if (login) return login;
    }
    if (typeof guip.globalInfo === "function") {
      const login = readGuipLogin(guip.globalInfo());
      if (login) return login;
    }
  } catch {
    // 无 GUIP SDK / SDK 异常时按无身份处理
  }
  return "";
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
  const [state, setState] = useState<IdentityState>({
    ready: false,
    mode: "guip",
    login: "",
    isAdmin: false,
    error: "",
  });

  const refresh = useCallback(async () => {
    setState((prev) => ({ ...prev, ready: false, error: "" }));
    try {
      const modeResp = await apiFetch("/api/identity/mode", { cache: "no-store" });
      if (!modeResp.ok) throw new Error(`身份模式读取失败（${modeResp.status}）`);
      const modeBody = await modeResp.json();
      const mode: "guip" | "single" = modeBody.mode === "single" ? "single" : "guip";
      if (mode === "single") {
        // single 模式固定 local：API 头与媒体 URL 共用同一有效身份
        setResolvedIdentity("single", "local");
        setState({ ready: true, mode, login: "local", isAdmin: true, error: "" });
        return;
      }

      // 身份来源优先级：?User（受 allow_url_user 约束） > GUIP SDK。
      const allowUrlUser = modeBody.allow_url_user !== false;
      const urlLogin = allowUrlUser ? getUrlLogin() : "";
      const login = urlLogin || (await detectGuipLogin());
      if (!login) {
        setResolvedIdentity("guip", "");
        setState((prev) => ({
          ...prev,
          ready: true,
          mode,
          login: "",
          isAdmin: false,
          error: "未获取到用户身份",
        }));
        return;
      }

      // 必须先写入内存有效身份，/me 才能带上 X-User-Login-Name。
      setResolvedIdentity("guip", login);
      const me = await loadMe();
      if (me.login !== login) {
        console.warn(
          `[identity] /me 返回身份 ${me.login} 与解析值 ${login} 不一致，以后端为准`,
        );
      }
      setResolvedIdentity(mode, me.login);
      setState({
        ready: true,
        mode,
        login: me.login,
        isAdmin: Boolean(me.is_admin),
        error: "",
      });
    } catch (err) {
      console.warn("[identity] 未获取到用户身份", err);
      setResolvedIdentity("guip", "");
      setState((prev) => ({
        ...prev,
        ready: true,
        login: "",
        isAdmin: false,
        error: (err as Error)?.message || "身份校验失败",
      }));
    }
  }, []);

  useEffect(() => {
    queueMicrotask(() => void refresh());
  }, [refresh]);

  const setLogin = useCallback((login: string) => {
    setResolvedIdentity("guip", login);
    void refresh();
  }, [refresh]);

  const logout = useCallback(() => {
    setResolvedIdentity("guip", "");
    void apiFetch("/api/identity/logout", { method: "POST" }).catch(() => undefined);
    setState({ ready: true, mode: "guip", login: "", isAdmin: false, error: "" });
  }, []);

  const value = useMemo(
    () => ({ ...state, setLogin, logout }),
    [state, setLogin, logout],
  );

  if (!state.ready) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-white text-sm text-slate-500">
        正在校验身份…
      </div>
    );
  }

  if (state.mode === "guip" && !state.login) {
    return <IdentityMissing />;
  }

  return <IdentityContext.Provider value={value}>{children}</IdentityContext.Provider>;
}

export function useIdentity(): IdentityContextValue {
  const value = useContext(IdentityContext);
  if (!value) throw new Error("useIdentity must be used inside IdentityProvider");
  return value;
}
