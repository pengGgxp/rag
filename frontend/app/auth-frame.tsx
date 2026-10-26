import Link from "next/link";
import { Layers3 } from "lucide-react";
import type { ReactNode } from "react";

export function AuthFrame({ children }: { children: ReactNode }) {
  return <main className="auth-screen">
    <aside className="auth-intro">
      <Link className="auth-brand" href="/"><span className="brand-mark"><Layers3 size={22} /></span><span>知库</span></Link>
      <div className="auth-intro-copy"><p className="auth-eyebrow">KNOWLEDGE WORKSPACE</p><h1>让知识成为可执行的工作流。</h1><p>把资料、文档和对话放在同一个工作空间里，让每个答案都能回到它的来源。</p></div>
      <div className="auth-intro-meta"><span>可追溯引用</span><span>工作区协作</span><span>智能检索</span></div>
    </aside>
    <section className="auth-form-region">{children}</section>
  </main>;
}
