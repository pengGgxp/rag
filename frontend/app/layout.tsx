import type { Metadata } from "next";
import "./styles.css";
import { AuthBoundary, AuthBootstrapScreen } from "./auth";
import { TooltipProvider } from "../components/ui/tooltip";
import { MotionProvider } from "../components/ui/motion-provider";
import { Suspense, type ReactNode } from "react";

export const metadata: Metadata = {
  title: "知库 · 知识工作空间",
  description: "知识库、文档与可追溯的知识问答。",
  referrer: "no-referrer",
};

export default function RootLayout({
  children,
  settings,
}: Readonly<{ children: ReactNode; settings: ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <MotionProvider>
          <TooltipProvider>
            <Suspense fallback={<AuthBootstrapScreen />}><AuthBoundary>
              {children}
              {settings}
            </AuthBoundary></Suspense>
          </TooltipProvider>
        </MotionProvider>
      </body>
    </html>
  );
}
