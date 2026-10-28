"use client";

/**
 * 身份缺失阻断页：URL ?User=、本机缓存与 GUIP SDK 都拿不到登录名时显示。
 * 不提供手工输入入口；身份必须由部署环境（GUIP/反代）提供。
 */
export default function IdentityMissing() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-white px-4 text-slate-900">
      <div className="w-full max-w-[520px] rounded-xl bg-white px-8 py-7 shadow-[0_8px_32px_rgba(15,23,42,0.12)]">
        <h1 className="m-0 text-xl font-semibold">未获取到用户身份</h1>
        <p className="mt-3 text-sm leading-7 text-slate-600">
          请从GUWP门户重新进入本平台。若反复出现此提示，请联系系统管理员处理。
        </p>
        <button
          type="button"
          onClick={() => {
            try {
              window.localStorage.removeItem("video-claw.login-name");
            } catch {
              // 存储不可用时无需清理
            }
            window.location.reload();
          }}
          className="mt-5 rounded-lg border border-slate-300 bg-slate-50 px-4 py-2 text-sm text-slate-700 hover:bg-slate-100"
        >
          重试
        </button>
      </div>
    </div>
  );
}
