"use client";
import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { request } from "../api";

export default function WorkbenchLaunch() {
  const params = useSearchParams(); const state = params.get("state"); const [error, setError] = useState(""); const [attempt, retry] = useState(0);
  useEffect(() => { let live = true; setError(""); void (async () => {
    if (!state) {
      const status = await request<{ available: boolean; url: string | null }>("/question-workbench");
      if (!status.available || !status.url) throw new Error("题目工作台暂不可用，请稍后重试");
      if (live) window.location.replace(status.url + "/api/auth/start");
    } else {
      const result = await request<{ redirect: string }>("/question-workbench/authorize", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ state }) });
      if (live) window.location.replace(result.redirect);
    }
  })().catch(e => { if (live) setError(e.message); }); return () => { live = false; }; }, [state, attempt]);
  return <main className="auth-bootstrap-screen"><section className="auth-bootstrap-panel"><h1>题目工作台</h1>{error ? <><p role="alert">{error}</p><button onClick={() => retry(n => n + 1)}>重新连接</button></> : <p role="status">正在打开…</p>}</section></main>;
}
