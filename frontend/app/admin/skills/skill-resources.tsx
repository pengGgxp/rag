"use client";
import { useMemo, useState } from "react";
import { Archive, FileCode2, FileText, FileWarning, Image as ImageIcon, Trash2 } from "lucide-react";
import type { PackageReview, SkillFile } from "./page";

function decode(file: SkillFile): string | null {
  try {
    const bytes = Uint8Array.from(atob(file.content), char => char.charCodeAt(0));
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    return /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(text) ? null : text;
  } catch { return null; }
}

export function SkillResources({ files, review, path, onSelect, onRemove, personal = false }: {
  files: SkillFile[]; review?: PackageReview; path: string; onSelect: (path: string) => void; onRemove?: (path: string) => void; personal?: boolean;
}) {
  const [removing, setRemoving] = useState("");
  const selected = files.find(file => file.path === path) ?? files[0];
  const metadata = review?.manifest.find(item => item.path === selected?.path);
  const notice = metadata?.personal_notice;
  const text = useMemo(() => selected ? decode(selected) : null, [selected]);
  const [unavailableImage, setUnavailableImage] = useState("");
  const image = metadata?.kind === "image" && selected
    ? `data:${metadata.media_type};base64,${selected.content}` : null;
  if (!files.length) return <p className="admin-empty">没有补充资料。</p>;
  return <div className="skill-resource-layout">
    <nav className="skill-file-list" aria-label="包内文件">
      {files.map(file => {
        const item = review?.manifest.find(entry => entry.path === file.path);
        return <button key={file.path} type="button" aria-current={file.path === selected?.path ? "true" : undefined}
          onClick={() => { onSelect(file.path); setRemoving(""); }}>
          {item?.kind === "image" ? <ImageIcon size={16} /> : item?.kind === "archive" ? <Archive size={16} />
            : item?.kind === "script" ? <FileCode2 size={16} />
            : item?.readable === false || item?.blocking ? <FileWarning size={16} /> : <FileText size={16} />}
          <span>{file.path}</span>
        </button>;
      })}
    </nav>
    {selected && <section className="skill-file-preview" aria-label="文件预览">
      <div className="skill-file-heading"><strong>{selected.path}</strong>
        {onRemove && <button className="secondary" type="button" onClick={() => setRemoving(selected.path)}><Trash2 size={16} />移除文件</button>}
      </div>
      {removing === selected.path && <div className="admin-inline-confirm"><span>移除此文件后，请一并调整引用它的指令。</span>
        <button className="secondary" type="button" onClick={() => {
          setRemoving("");
          requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(".skill-file-heading button")?.focus());
        }}>保留</button>
        <button className="danger" type="button" onClick={() => {
          onRemove?.(selected.path); setRemoving("");
          requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(".skill-file-list button")?.focus());
        }}>确认移除</button>
      </div>}
      {notice && <p className="extension-warning">{notice}</p>}
      {metadata?.blocking && <p className="admin-error" role="alert">{metadata.archive_error}</p>}
      {image && unavailableImage !== selected.path ? <img src={image} alt={selected.path} onError={() => setUnavailableImage(selected.path)} />
        : metadata?.kind === "archive" && metadata.archive_entries ? <div className="skill-archive">
          <p>包内条目仅用于查看和导出，不会自动解压。</p>
          <ul>{metadata.archive_entries.map(item => <li key={item.path}><span>{item.path}</span><small>{item.size.toLocaleString("zh-CN")} B</small></li>)}</ul>
        </div>
        : text === null ? <p>{personal ? "此文件无法预览，可导出后查看。" : "此文件保存在 Skill 中，模型可在云电脑中按路径使用。"}</p>
        : <pre tabIndex={0}>{text.slice(0, 50000)}{text.length > 50000 ? "\n\n……仅显示前 50000 字，可导出后查看完整文件。" : ""}</pre>}
    </section>}
  </div>;
}
