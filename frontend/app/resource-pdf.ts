import type { FileReaderSession } from "./file-preview-access";

export async function loadPdf(reader: FileReaderSession, onFailure?: () => void) {
  const pdf = await import("pdfjs-dist");
  pdf.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
  const size = reader.access.size_bytes;
  if (!size) throw new Error("PDF 内容为空，请下载查看");
  const first = new Uint8Array(await (await reader.read([0, Math.min(size, 65536)])).arrayBuffer());
  reader.signal.throwIfAborted();
  let loading: ReturnType<typeof pdf.getDocument>;
  class Transport extends pdf.PDFDataRangeTransport {
    requestDataRange(begin: number, end: number) {
      void reader.read([begin, Math.min(end, size)]).then(blob => blob.arrayBuffer()).then(data => {
        if (!reader.signal.aborted) this.onDataRange(begin, new Uint8Array(data));
      }).catch(() => { if (!reader.signal.aborted) onFailure?.(); void loading.destroy(); });
    }
  }
  loading = pdf.getDocument({ range: new Transport(size, first, true), length: size, rangeChunkSize: 65536,
    disableAutoFetch: true, disableStream: true, isEvalSupported: false });
  const abort = () => { void loading.destroy(); };
  reader.signal.addEventListener("abort", abort, { once: true });
  void loading.promise.catch(() => {}).finally(() => reader.signal.removeEventListener("abort", abort));
  return loading;
}
