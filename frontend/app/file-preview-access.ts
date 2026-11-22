"use client";

import { API_URL, ApiError, request } from "./api";
import { cachedPreview, clearPreviewCache } from "./resource-preview-cache";

export type FileReference = { type: "resource" | "attachment" | "document"; id: string; name: string };
export type FileAccess = {
  id: string; filename: string; content_type: string; size_bytes: number; revision: string;
  kind: "image" | "pdf" | "docx" | "pptx" | "text" | "unsupported";
  etag: string; headers: Record<string, string>; url: string; expires_at: number;
};
export function fileEndpoint(file: FileReference) {
  return `${file.type === "resource" ? "/resources" : file.type === "document" ? "/documents" : "/chat/attachments"}/${encodeURIComponent(file.id)}`;
}
export function fileDownload(file: FileReference) {
  return `${fileEndpoint(file)}/${file.type === "attachment" ? "content" : "download"}`;
}

export class FileReaderSession {
  access!: FileAccess;
  private renewing?: Promise<void>;
  constructor(readonly file: FileReference, readonly scope: string, readonly signal: AbortSignal) {}
  async validate() {
    this.signal.throwIfAborted();
    try {
      const next = await request<FileAccess>(`${fileEndpoint(this.file)}/preview-access`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision: this.access?.revision }), signal: this.signal,
      });
      this.signal.throwIfAborted();
      this.access = next;
    } catch (error) {
      if (error instanceof ApiError && [401, 403, 404, 409, 410].includes(error.status)) {
        await clearPreviewCache(this.scope.split(":")[0]);
        throw new Error(error.status === 409 ? "文件已更新，请重新打开预览" : "文件已删除或无权查看，请重新选择");
      }
      this.signal.throwIfAborted();
      throw new Error("文件暂时无法打开，请重试或下载查看");
    }
  }
  private async renew() {
    if (!this.renewing) this.renewing = this.validate().finally(() => { this.renewing = undefined; });
    await this.renewing;
  }
  async read(range?: [number, number]): Promise<Blob> {
    this.signal.throwIfAborted();
    if (!this.access || this.access.expires_at * 1000 <= Date.now() + 10_000) await this.renew();
    const access = this.access;
    if (range && (range[0] < 0 || range[1] > access.size_bytes || range[0] >= range[1]))
      throw new Error("文件读取范围无效，请重新打开");
    if (!range && access.size_bytes > (access.kind === "docx" || access.kind === "pptx" ? 50 : 200) * 1024 * 1024)
      throw new Error("文件较大，请下载后查看");
    const blob = await cachedPreview(this.scope, access.revision, fileEndpoint(this.file), async () => {
      for (let attempt = 0; attempt < 2; attempt++) {
        const headers = new Headers(this.access.headers);
        if (range) headers.set("Range", `bytes=${range[0]}-${range[1] - 1}`);
        let response: Response;
        try {
          const document = this.file.type === "document";
          const url = document ? `${API_URL.replace(/\/$/, "")}${this.access.url}` : this.access.url;
          response = await fetch(url, { credentials: document ? "include" : "omit", referrerPolicy: "no-referrer",
            headers, signal: this.signal, cache: "no-store" });
        } catch (error) {
          this.signal.throwIfAborted();
          throw new Error("文件未能加载，请重试或下载查看");
        }
        if ([401, 403].includes(response.status) && !attempt) { await this.renew(); continue; }
        if ([404, 409, 412].includes(response.status)) {
          await clearPreviewCache(this.scope.split(":")[0]);
          throw new Error("文件已更新或删除，请重新打开");
        }
        if (!response.ok) throw new Error("文件未能加载，请重试或下载查看");
        const actualEtag = response.headers.get("ETag");
        if (!actualEtag || actualEtag.replaceAll('"', "") !== access.etag.replaceAll('"', "")) {
          await clearPreviewCache(this.scope.split(":")[0]);
          throw new Error("文件已更新，请重新打开预览");
        }
        if (range && (response.status !== 206 || response.headers.get("Content-Range") !== `bytes ${range[0]}-${range[1]-1}/${access.size_bytes}`))
          throw new Error("文件未完整接收，请重试");
        const result = await response.blob();
        if (result.size !== (range ? range[1] - range[0] : access.size_bytes)) throw new Error("文件未完整接收，请重试");
        return result;
      }
      throw new Error("文件链接已失效，请重试");
    }, range);
    this.signal.throwIfAborted();
    return blob;
  }
}
