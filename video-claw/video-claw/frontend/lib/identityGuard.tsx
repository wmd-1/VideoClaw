"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import IdentityMissing from "@/components/IdentityMissing";
import { withBasePath } from "@/lib/basePath";
import {
  getLoginName,
  setResolvedIdentity,
  setStoredLogin,
} from "@/lib/identityState";

export { getLoginName, setStoredLogin };

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

type GuipWindow = Window & {
  globalInfo?: () => { userInfo?: { loginName?: string } };
  promise?: { globalInfo?: () => Promise<{ userInfo?: { loginName?: string } }> };
};

/** GUIP 内网环境：优先异步 globalInfo，再同步兜底。 */
async function detectGuipLogin(): Promise<string> {
  if (typeof window === "undefined") return "";
  const guip = window as GuipWindow;
  try {
    if (typeof guip.promise?.globalInfo === "function") {
      const info = await Promise.race([
        guip.promise.globalInfo(),
        new Promise<null>((resolve) => window.setTimeout(() => resolve(null), 1500)),
      ]);
      const login = info?.userInfo?.loginName;
      if (login?.trim()) return login.trim();
    }
    if (typeof guip.globalInfo === "function") {
      const login = guip.globalInfo()?.userInfo?.loginName;
      if (login?.trim()) return login.trim();
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

      // 身份来源优先级：URL ?User > 本机缓存 > GUIP SDK。
      if (!getLoginName()) {
        const guipLogin = await detectGuipLogin();
        if (guipLogin) setStoredLogin(guipLogin);
      }

      // 即使没有明文登录名也请求 /me：GUIP/反代可能已在请求上注入身份头。
      const me = await loadMe();
      // /me 的结论即为有效身份，此后不再被 URL 改写
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
