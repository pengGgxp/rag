"use client";

import { createContext, useCallback, useContext, useId, useState, type ReactNode } from "react";
import { Download, FileText, Loader2 } from "lucide-react";
import { downloadFile } from "./api";
import { useAuth } from "./auth";
import { InlineFileImage } from "./resource-preview";
import { Tooltip } from "../components/ui/tooltip";
import { activityFiles, activityItem, type ToolEvent } from "./conversation-display";
import { latestImageTask } from "./image-task-state";
import type { ImageTaskResult } from "./image-task-result";
import { plainLinkClick, useResourceFilePreview } from "./resource-file-link";
import "./conversation-files.css";

type FileContextValue = ReturnType<typeof useResourceFilePreview> & {
  images: Record<string, ImageTaskResult>; updateImage: (value: ImageTaskResult) => void;
};
const FileContext = createContext<FileContextValue | null>(null);
export const useMessageFiles = () => useContext(FileContext);

export function MessageFileProvider({ scope, children }: { scope: string; children: ReactNode }) {
  const preview = useResourceFilePreview(scope);
  const [images, setImages] = useState<Record<string, ImageTaskResult>>({});
  const updateImage = useCallback((value: ImageTaskResult) => setImages(old => ({
    ...old, [value.task_id]: old[value.task_id] ? latestImageTask(old[value.task_id], value) : value,
  })), []);
  return <FileContext.Provider value={{ ...preview, images, updateImage }}>
    {children}{preview.preview}
  </FileContext.Provider>;
}

export function MessageFiles({ events }: { events: ToolEvent[] }) {
  const context = useMessageFiles()!;
  const { session } = useAuth();
  const [downloadError, setDownloadError] = useState("");
  const listId = useId();
  const files = activityFiles(events.map(event => activityItem({
    ...event, image_task: event.image_task
      ? latestImageTask(event.image_task, context.images[event.image_task.task_id]) : undefined,
  })), session.workspace_id);
  return <>{files.length > 0 && <div className="message-files" role="group" aria-label="本轮文件">
    {files.map(file => {
      const name = file.path.split("/").at(-1) || file.path;
      return file.availability === "missing" ? <span key={file.id} className="message-file unavailable" title={file.path}>
        <FileText size={17} aria-hidden="true" /><span className="message-file-name">{name}</span><span>已不可用</span>
      </span> : <div key={file.id} className={/\.(png|jpe?g|webp|gif|avif|bmp)$/i.test(name) ? "message-file-item message-file-image" : "message-file-item"}>
        {/\.(png|jpe?g|webp|gif|avif|bmp)$/i.test(name) && <InlineFileImage file={{ type: "resource", id: file.id, name }}
          scope={`${session.user.id}:${session.workspace_id}`} onOpen={element => {
            const trigger = element.closest(".message-file-item")?.querySelector<HTMLElement>(".message-file");
            void context.open(file.id, trigger || element);
          }} />}
        <div className="message-file-actions"><a className="message-file" href={file.url || `/resources?file=${encodeURIComponent(file.id)}`}
        data-file-preview-trigger={`${listId}-${file.id}`}
        title={file.path} aria-label={`查看文件：${file.path}`} aria-busy={context.busyId === file.id}
        onClick={event => { if (plainLinkClick(event)) { event.preventDefault(); void context.open(file.id, event.currentTarget); } }}>
        {context.busyId === file.id ? <Loader2 size={17} className="spin" aria-hidden="true" /> : <FileText size={17} aria-hidden="true" />}
        <span className="message-file-name">{name}</span>
      </a><Tooltip content={`下载 ${name}`}><button type="button" className="icon-button" aria-label={`下载 ${name}`} onClick={() => void downloadFile(`/resources/${file.id}/download`, name).catch(() => setDownloadError("下载失败，请重试"))}><Download size={17} /></button></Tooltip></div></div>;
    })}
  </div>}
    {context.error && <p className="message-file-error" role="alert">{context.error}</p>}
    {downloadError && <p className="message-file-error" role="alert">{downloadError}</p>}
  </>;
}
