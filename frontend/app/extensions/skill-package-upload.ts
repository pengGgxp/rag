import { BlobReader, BlobWriter, ZipWriter } from "@zip.js/zip.js";
import { request } from "../api";

const json = (body: unknown) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

function put(url: string, blob: Blob): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.timeout = 10 * 60_000;
    xhr.onload = () => xhr.status >= 200 && xhr.status < 300 && xhr.getResponseHeader("ETag")
      ? resolve()
      : reject(new Error("上传失败，请重试"));
    xhr.onerror = () => reject(new Error("上传连接中断，请检查网络后重试"));
    xhr.ontimeout = () => reject(new Error("上传超时，请重试"));
    xhr.send(blob);
  });
}

export async function zipSkillFolder(files: File[]) {
  const writer = new ZipWriter(new BlobWriter("application/zip"));
  for (const file of files) {
    const path = file.webkitRelativePath || file.name;
    await writer.add(path, new BlobReader(file));
  }
  return new File([await writer.close()], "skills.zip", { type: "application/zip" });
}

export async function uploadSkillPackage(file: File, scope: "agent" | "site") {
  const root = scope === "site" ? "/site/admin/skills" : "/agent/skills";
  const upload = await request<{ id: string; part_size: number }>(`${root}/uploads`, json({
    filename: file.name,
    size_bytes: file.size,
  }));
  try {
    const count = Math.ceil(file.size / upload.part_size);
    let next = 0;
    let failure: unknown;
    const worker = async () => {
      while (next < count && !failure) {
        const number = ++next;
        const start = (number - 1) * upload.part_size;
        const part = file.slice(start, Math.min(file.size, start + upload.part_size));
        for (let attempt = 0; ; attempt++) {
          try {
            const signed = await request<{ url: string }>(
              `${root}/uploads/${upload.id}/parts/${number}`,
              json({}),
            );
            await put(signed.url, part);
            break;
          } catch (error) {
            if (attempt >= 2) throw error;
          }
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(3, count) }, async () => {
      try { await worker(); } catch (error) { failure ??= error; }
    }));
    if (failure) throw failure;
    await request(`${root}/uploads/${upload.id}/complete`, json({}));
    const preview = await request<{ items: SkillPackagePreview[] }>(
      `${root}/uploads/${upload.id}/preview`,
    );
    return { uploadId: upload.id, items: preview.items };
  } catch (error) {
    await request(`${root}/uploads/${upload.id}`, { method: "DELETE" }).catch(() => undefined);
    throw error;
  }
}

export type SkillPackagePreview = {
  id: number;
  folder: string;
  name: string;
  description: string;
  markdown: string;
  resources: { path: string; size: number }[];
  package_review: {
    manifest: { path: string; size: number; text: boolean; kind: string }[];
    issues: string[];
    can_enable: boolean;
  };
};
