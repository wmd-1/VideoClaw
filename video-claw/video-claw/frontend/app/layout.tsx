import type { Metadata } from "next";
import Script from "next/script";
import AppShell from "@/components/AppShell";
import { BASE_PATH } from "@/lib/basePathConfig";
import { IdentityProvider } from "@/lib/identityGuard";
import "./globals.css";

export const metadata: Metadata = {
  title: "交心-Video",
  description: "AI视频生成工具",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <head>
        <Script
          src={`${BASE_PATH}/GuipAPI.xc2.js`}
          strategy="beforeInteractive"
        />
      </head>
      <body
        className="antialiased"
      >
        <IdentityProvider>
          <AppShell>{children}</AppShell>
        </IdentityProvider>
      </body>
    </html>
  );
}
