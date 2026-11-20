import Link from "next/link";
import type { ReactNode } from "react";

export function AuthFrame({ children }: { children: ReactNode }) {
  return <main className="auth-screen">
    <aside className="auth-intro">
      <Link className="auth-brand" href="/"><span className="brand-mark"><img src="/icon.svg" alt="" /></span><span>知库</span></Link>
      <div className="auth-intro-copy"><p className="auth-eyebrow">知库</p><h1>把你的资料，变成有据可查的工作成果。</h1><p>从知识库中查找依据，整理内容，并生成可以继续使用和交付的成果。</p></div>
      <div className="auth-intro-meta"><span>可追溯引用</span><span>个人知识管理</span><span>智能检索</span></div>
    </aside>
    <section className="auth-form-region">{children}</section>
  </main>;
}
