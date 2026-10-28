"use client";

/**
 * 兼容入口：身份门禁已迁移到 `lib/identityGuard.tsx`。
 *
 * 保留本文件是为了避免旧引用（组件/文档/构建缓存）失效；不要在此重新实现
 * `IdentityProvider` 或登录表单。门禁行为以 `identityGuard.tsx` 为准。
 */
export {
  apiFetch,
  getLoginName,
  identityHeaders,
  IdentityProvider,
  setStoredLogin,
  useIdentity,
} from "@/lib/identityGuard";
