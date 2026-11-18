/** PDF.js errors cross a worker boundary, so match their public names. */
export function pdfPreviewError(cause: unknown): string {
  const error = cause && typeof cause === "object" ? cause as { name?: unknown; message?: unknown } : {};
  if (error.name === "PasswordException")
    return "此 PDF 需要打开密码，请下载原文件后输入密码查看；如需入库，请移除打开密码后重新上传";
  if (error.name === "InvalidPDFException") {
    if (typeof error.message === "string" && /empty|no pages/i.test(error.message))
      return "PDF 文件为空或没有页面，请重新导出后上传";
    return "PDF 文件已损坏或格式无效，请重新导出后上传";
  }
  return "PDF 无法打开，请重新打开或下载原文件查看";
}
