import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "知库 · 知识工作空间",
  description: "知识库、文档与可追溯的知识问答。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}

