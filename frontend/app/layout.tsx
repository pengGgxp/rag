import type { Metadata } from "next";
import "./styles.css";
import { AuthBoundary } from "./auth";
import { TooltipProvider } from "../components/ui/tooltip";
import { MotionProvider } from "../components/ui/motion-provider";

export const metadata: Metadata = {
  title: "知库 · 知识工作空间",
  description: "知识库、文档与可追溯的知识问答。",
  referrer: "no-referrer",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <MotionProvider>
          <TooltipProvider>
            <AuthBoundary>{children}</AuthBoundary>
          </TooltipProvider>
        </MotionProvider>
      </body>
    </html>
  );
}
