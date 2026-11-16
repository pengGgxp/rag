/** SSE and status reads share one task; delayed snapshots must not undo progress. */
export function latestImageTask<T extends { updated_at?: string; terminal: boolean }>(
  current: T, incoming: T | undefined,
): T {
  if (!incoming) return current;
  const before = Date.parse(current.updated_at || "");
  const after = Date.parse(incoming.updated_at || "");
  if (Number.isFinite(before) && Number.isFinite(after) && before !== after) {
    return after > before ? incoming : current;
  }
  if (current.terminal !== incoming.terminal) return incoming.terminal ? incoming : current;
  return incoming;
}
