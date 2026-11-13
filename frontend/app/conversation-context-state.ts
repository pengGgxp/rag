export type WindowVersion = { windowId?: string | null; contextRevision?: number };

// Context revisions are monotonic across replacement windows. Delayed events
// from the previous subscription must not overwrite an installed newer state.
export function acceptContextState<T extends WindowVersion>(current: T, incoming: T): T {
  if (current.contextRevision !== undefined && incoming.contextRevision !== undefined) {
    if (incoming.contextRevision < current.contextRevision) return current;
    if (incoming.contextRevision === current.contextRevision && current.windowId && incoming.windowId
      && current.windowId !== incoming.windowId) return current;
  }
  return incoming;
}
