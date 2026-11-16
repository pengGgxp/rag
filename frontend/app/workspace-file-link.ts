export type WorkspaceFileReference = {
  id: string; path: string; size_bytes?: number; version?: number; content_type?: string;
  revision?: string; availability?: "available" | "missing" | "pending"; url?: string;
};

export function workspaceFileId(href: string | undefined, origin?: string): string | null {
  if (!href || (!href.startsWith("/") && !origin)) return null;
  try {
    const base = origin || "https://workspace.invalid";
    const url = new URL(href, base);
    if (url.origin !== new URL(base).origin || url.pathname !== "/resources"
      || url.searchParams.getAll("file").length !== 1) return null;
    const id = url.searchParams.get("file") || "";
    return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id) ? id : null;
  } catch { return null; }
}
