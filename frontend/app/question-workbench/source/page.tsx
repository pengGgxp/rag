"use client";
import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { request } from "../../api";

export default function QuestionSource() {
  const params = useSearchParams(); const [data, setData] = useState<{ title: string; text: string } | null>(null); const [error, setError] = useState("");
  const query = params.toString();
  useEffect(() => { let live = true; void request<{ title: string; text: string }>("/question-workbench/source?" + query).then(value => { if (live) setData(value); }).catch(e => { if (live) setError(e.message); }); return () => { live = false; }; }, [query]);
  return <main className="workspace-body" style={{ maxWidth: 900, margin: "0 auto", padding: 28 }}><h1>{data?.title || "来源资料"}</h1>{error ? <p role="alert">{error}</p> : <p style={{ whiteSpace: "pre-wrap", lineHeight: 1.8, overflowWrap: "anywhere", marginTop: 24 }}>{data?.text || "正在读取…"}</p>}</main>;
}
