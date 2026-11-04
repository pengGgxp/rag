export const DOCUMENT_LIMIT = 200 * 1024 * 1024;
export const BATCH_LIMIT = 500 * 1024 * 1024;
export const BATCH_COUNT = 200;
const ENTRY_LIMIT = 2000;
const extensions = new Set(["pdf", "docx", "md", "txt", "html", "htm", "csv", "xlsx"]);
export const DOCUMENT_ACCEPT = ".pdf,.docx,.md,.txt,.html,.htm,.csv,.xlsx";
export type ImportFile = { file: File; path: string };
export type ImportIssue = { path: string; reason: string };
export type ImportResult = { files: ImportFile[]; issues: ImportIssue[] };

function safePath(path: string) {
  const parts = path.split("/");
  return path.length <= 1024 && parts.length <= 32 && !/[\\:\x00-\x1f\x7f]/.test(path)
    && parts.every(part => part.length > 0 && part.length <= 255 && part !== "." && part !== "..");
}
function ignored(path: string) {
  return path.split("/").some(part => ["__MACOSX", ".DS_Store", "Thumbs.db", ".git"].includes(part));
}
function documentIssue(path: string, bytes: number): string | undefined {
  if (!safePath(path)) return "文件路径不安全或过长";
  if (ignored(path)) return "系统或隐藏辅助文件";
  if (!extensions.has(path.split(".").pop()!.toLowerCase())) return "不支持的文档格式";
  if (bytes === 0) return "空文件";
  if (bytes > DOCUMENT_LIMIT) return "单文件超过 200 MiB";
}

async function unpack(archive: File, signal: AbortSignal): Promise<ImportResult> {
  if (!archive.size || archive.size > DOCUMENT_LIMIT) throw new Error("ZIP 不能为空且不能超过 200 MiB");
  const { BlobReader, ZipReader } = await import("@zip.js/zip.js");
  signal.throwIfAborted();
  const reader = new ZipReader(new BlobReader(archive), { useWebWorkers: false });
  const result: ImportResult = { files: [], issues: [] };
  let entries = 0, declared = 0, actual = 0;
  const paths = new Set<string>();
  try {
    for await (const entry of reader.getEntriesGenerator()) {
      signal.throwIfAborted();
      if (++entries > ENTRY_LIMIT) throw new Error("ZIP 条目超过 2000 个，请拆分压缩包");
      const path = entry.filename.replace(/\/$/, "");
      if (!safePath(path)) throw new Error("ZIP 包含不安全或过长的路径");
      const kind = (entry.externalFileAttributes >>> 16) & 0o170000;
      if (kind && kind !== 0o100000 && kind !== 0o040000) throw new Error("ZIP 包含链接或特殊文件，请移除后重试");
      if (entry.encrypted) throw new Error("不支持加密 ZIP，请解密后重试");
      if (paths.has(path)) throw new Error("ZIP 包含重复路径，请整理后重试");
      paths.add(path);
      if (entry.directory) continue;
      declared += entry.uncompressedSize;
      if (!Number.isSafeInteger(declared) || declared > BATCH_LIMIT
        || entry.uncompressedSize > DOCUMENT_LIMIT
        || entry.uncompressedSize > Math.max(1024 * 1024, entry.compressedSize * 1000))
        throw new Error("ZIP 解压大小或压缩比例超限，请拆分后重试");
      const issue = documentIssue(path, entry.uncompressedSize);
      if (issue) { result.issues.push({ path: `${archive.name}/${path}`, reason: issue }); continue; }
      if (result.files.length >= BATCH_COUNT) throw new Error("ZIP 文档超过 200 个，请拆分后重试");
      const chunks: BlobPart[] = [];
      let written = 0;
      const sink = new WritableStream<Uint8Array>({ write(chunk) {
        signal.throwIfAborted();
        written += chunk.byteLength; actual += chunk.byteLength;
        if (written > entry.uncompressedSize || written > DOCUMENT_LIMIT || actual > BATCH_LIMIT)
          throw new Error("ZIP 实际解压大小超限");
        chunks.push(new Uint8Array(chunk));
      } });
      await entry.getData(sink, { checkSignature: true, signal });
      if (written !== entry.uncompressedSize) throw new Error("ZIP 文件大小校验失败");
      result.files.push({ file: new File(chunks, path.split("/").pop()!, {
        lastModified: entry.lastModDate?.getTime() ?? archive.lastModified,
        type: "application/octet-stream",
      }), path: `${archive.name}/${path}` });
    }
    return result;
  } finally { await reader.close(); }
}

export async function prepareDocuments(input: readonly File[], signal: AbortSignal, single = false): Promise<ImportResult> {
  const result: ImportResult = { files: [], issues: [] };
  let bytes = 0;
  for (const file of input) {
    signal.throwIfAborted();
    const path = file.webkitRelativePath || file.name;
    if (!safePath(path)) { result.issues.push({ path, reason: "文件路径不安全或过长" }); continue; }
    if (/\.zip$/i.test(file.name) && !single) {
      try {
        const unpacked = await unpack(file, signal);
        const unpackedBytes = unpacked.files.reduce((sum, item) => sum + item.file.size, 0);
        if (bytes + unpackedBytes > BATCH_LIMIT || result.files.length + unpacked.files.length > BATCH_COUNT)
          throw new Error("本批超过 200 个文档或 500 MiB，请分批上传");
        bytes += unpackedBytes;
        result.files.push(...unpacked.files); result.issues.push(...unpacked.issues);
      } catch (error) {
        signal.throwIfAborted();
        result.issues.push({ path, reason: error instanceof Error && /ZIP|本批/.test(error.message)
          ? error.message : "ZIP 已损坏或无法读取，请重新打包" });
      }
      continue;
    }
    const issue = documentIssue(path, file.size);
    if (issue) { result.issues.push({ path, reason: issue }); continue; }
    if (result.files.length >= (single ? 1 : BATCH_COUNT) || bytes + file.size > BATCH_LIMIT) {
      result.issues.push({ path, reason: single ? "新版本只能上传一个文档" : "本批超过 200 个文档或 500 MiB，请分批上传" }); continue;
    }
    result.files.push({ file, path }); bytes += file.size;
  }
  return result;
}

export async function droppedDocuments(transfer: DataTransfer, signal: AbortSignal): Promise<File[]> {
  const entries = Array.from(transfer.items).map(item => item.webkitGetAsEntry?.()).filter(entry => entry !== null && entry !== undefined);
  if (!entries.length) return Array.from(transfer.files);
  const files: File[] = [];
  let count = 0;
  async function visit(entry: FileSystemEntry, prefix = "") {
    signal.throwIfAborted();
    if (++count > ENTRY_LIMIT) throw new Error("文件夹条目超过 2000 个，请分批选择");
    const path = `${prefix}${entry.name}`;
    if (!safePath(path)) throw new Error("文件夹包含不安全或过长的路径");
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
      Object.defineProperty(file, "webkitRelativePath", { value: path });
      files.push(file);
    } else if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      while (true) {
        const children = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject));
        if (!children.length) break;
        for (const child of children) await visit(child, `${path}/`);
      }
    }
  }
  for (const entry of entries) await visit(entry);
  return files;
}
