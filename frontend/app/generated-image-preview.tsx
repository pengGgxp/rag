"use client";

import { useEffect, useState } from "react";
import { ImageOff, Loader2, RotateCcw } from "lucide-react";
import { request } from "./api";
import { json } from "./admin/admin-ui";
import { Modal } from "./modal";

type ImageLink = { status: string; url: string | null; revision: string; error?: string };

export function GeneratedImagePreview({ id, name }: { id: string; name: string }) {
  const [thumbnail, setThumbnail] = useState<ImageLink | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [opened, setOpened] = useState(false);
  const [original, setOriginal] = useState<string | null>(null);
  const [originalLoaded, setOriginalLoaded] = useState(false);
  const [originalError, setOriginalError] = useState("");
  const [originalAttempt, setOriginalAttempt] = useState(0);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let polls = 0;
    setThumbnail(null); setError("");
    async function load() {
      try {
        const next = await request<ImageLink>(`/resources/${id}/image-preview-link`, json({ retry: attempt > 0 && polls === 0 }));
        if (disposed) return;
        if (next.status === "failed") throw new Error(next.error || "预览图准备失败");
        setThumbnail(next);
        if (next.status !== "ready") {
          if (++polls >= 30) throw new Error("预览图仍在准备，请稍后重试");
          timer = setTimeout(load, 2000);
        }
      } catch (cause) { if (!disposed) setError(cause instanceof Error ? cause.message : "预览图加载失败"); }
    }
    void load();
    return () => { disposed = true; clearTimeout(timer); };
  }, [id, attempt]);

  useEffect(() => {
    if (!opened) return;
    let disposed = false;
    setOriginal(null); setOriginalLoaded(false); setOriginalError("");
    void request<ImageLink>(`/resources/${id}/image-preview-link`, json({ original: true, revision: thumbnail?.revision }))
      .then(next => { if (!disposed) setOriginal(next.url); })
      .catch(cause => { if (!disposed) setOriginalError(cause instanceof Error ? cause.message : "原图加载失败"); });
    return () => { disposed = true; };
  }, [id, opened, originalAttempt, thumbnail?.revision]);

  return <>
    {error ? <div className="generated-image-placeholder" role="status"><ImageOff size={24} /><span>{error}</span><button type="button" className="secondary" onClick={() => setAttempt(value => value + 1)}><RotateCcw size={15} />重试预览</button></div>
      : thumbnail?.url ? <button type="button" className="generated-image-open" aria-label={`查看原图 ${name}`} onClick={() => setOpened(true)}><img src={thumbnail.url} alt={name} referrerPolicy="no-referrer" onError={() => setError("预览链接已失效，请重试")} /></button>
      : <div className="generated-image-placeholder" role="status"><Loader2 size={22} className="spin" /><span>正在准备预览图</span></div>}
    {opened && <Modal title={name} wide onClose={() => setOpened(false)}>
      {originalError ? <div className="generated-image-placeholder" role="alert"><span>{originalError}</span><button type="button" className="secondary" onClick={() => setOriginalAttempt(value => value + 1)}>重新加载</button></div>
        : <>
          {!originalLoaded && <div className="generated-image-placeholder" role="status">{thumbnail?.url && <img className="image-task-preview" src={thumbnail.url} alt="" />}<Loader2 size={22} className="spin" /><span>正在加载原图</span></div>}
          {original && <img className="image-task-preview" style={{ display: originalLoaded ? "block" : "none" }} src={original} alt={name} referrerPolicy="no-referrer" onLoad={() => setOriginalLoaded(true)} onError={() => setOriginalError("图片未能加载，请重试")} />}
        </>}
    </Modal>}
  </>;
}
