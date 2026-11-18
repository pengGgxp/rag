import DOMPurify from "dompurify";

export type OfficePages = { pages: string[]; width: number; height: number; dispose: () => void };
const MAX_EXPANDED = 150 * 1024 * 1024;

// Check actual ZIP sizes before allowing document renderers to decode resources.
async function safeArchive(blob: Blob, signal: AbortSignal) {
  const magic = new Uint8Array(await blob.slice(0, 8).arrayBuffer());
  if (magic.join(",") === "208,207,17,224,161,177,26,225")
    throw new Error("文档已加密或格式不受支持，请下载后查看");
  const { BlobReader, BlobWriter, ZipReader, ZipWriter } = await import("@zip.js/zip.js");
  const reader = new ZipReader(new BlobReader(blob), { useWebWorkers: false });
  const writer = new ZipWriter(new BlobWriter("application/zip"), { useWebWorkers: false });
  const paths = new Set<string>();
  let total = 0, declared = 0;
  try {
    for await (const entry of reader.getEntriesGenerator()) {
      signal.throwIfAborted();
      if (entry.encrypted) throw new Error("文档已加密，请解密后重试或下载查看");
      if (paths.size >= 4000 || paths.has(entry.filename) || /(^|\/)\.\.($|\/)|[\\\x00]/.test(entry.filename))
        throw new Error("文档无法安全读取，请下载后查看");
      paths.add(entry.filename);
      if (entry.directory) continue;
      declared += entry.uncompressedSize;
      if (declared > MAX_EXPANDED || entry.uncompressedSize > 50 * 1024 * 1024
        || entry.uncompressedSize > Math.max(1024 * 1024, entry.compressedSize * 1000))
        throw new Error("文档内容较大，请下载后查看");
      const chunks: Uint8Array<ArrayBuffer>[] = [];
      let count = 0;
      await entry.getData(new WritableStream<Uint8Array>({ write(chunk) {
        signal.throwIfAborted(); count += chunk.length; total += chunk.length;
        if (count > entry.uncompressedSize || total > MAX_EXPANDED) throw new Error("文档内容较大，请下载后查看");
        chunks.push(new Uint8Array(chunk));
      } }), { signal, checkSignature: true });
      let data = new Blob(chunks);
      if (entry.filename.endsWith(".rels")) {
        const xml = new DOMParser().parseFromString(await data.text(), "application/xml");
        if (xml.querySelector("parsererror")) throw new Error("文档内容损坏，请重新导出");
        xml.querySelectorAll('[TargetMode="External"]').forEach(node => node.remove());
        data = new Blob([new XMLSerializer().serializeToString(xml)]);
      }
      await writer.add(entry.filename, new BlobReader(data), { level: 0, signal });
    }
    return await writer.close();
  } finally { await reader.close(); }
}

export async function renderOffice(blob: Blob, kind: "docx" | "pptx", signal: AbortSignal): Promise<OfficePages> {
  const input = await safeArchive(blob, signal);
  signal.throwIfAborted();
  let pages: string[], style = "", width = 960, height = 540;
  if (kind === "docx") {
    const docx = await import("docx-preview");
    const isolated = document.implementation.createHTMLDocument("");
    const body = isolated.createElement("div"), styles = isolated.createElement("div");
    await docx.renderAsync(input, body, styles, { renderAltChunks: false, renderComments: false,
      useBase64URL: true, ignoreLastRenderedPageBreak: false, breakPages: true });
    pages = Array.from(body.querySelectorAll("section.docx")).map(node => node.outerHTML);
    const first = body.querySelector<HTMLElement>("section.docx");
    const points = (value: string | undefined, fallback: number) => value?.endsWith("pt") ? parseFloat(value) * 96 / 72 : fallback;
    width = points(first?.style.width, 794); height = points(first?.style.minHeight, 1123);
    style = Array.from(styles.querySelectorAll("style")).map(node => node.textContent).join("\n");
  } else {
    const { pptxToHtml } = await import("@jvmr/pptx-to-html");
    pages = await pptxToHtml(await input.arrayBuffer(), { width, height, scaleToFit: true, letterbox: true });
  }
  signal.throwIfAborted();
  if (!pages.length) throw new Error("未能读取文档内容，请下载后查看");
  const csp = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data: blob:; font-src data: blob:; connect-src 'none'; media-src 'none'; base-uri 'none'; form-action 'none'";
  return { width, height, dispose() {}, pages: pages.map(page => {
    const clean = DOMPurify.sanitize(page, { FORBID_TAGS: ["script", "iframe", "object", "embed", "form", "input", "link", "base"],
      FORBID_ATTR: ["href", "target", "action", "srcset"] });
    return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}"><style>${style.replace(/<\/style/gi, "")}
      html,body{margin:0;padding:0;background:white}body{overflow:auto;font-family:'Segoe UI','Microsoft YaHei',sans-serif}
      section.docx{margin:0!important;box-shadow:none!important}*{box-sizing:border-box}</style></head><body>${clean}</body></html>`;
  }) };
}
