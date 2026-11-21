export type Computer = {
  id: string; computer_id: string; name: string; conversation_id: string;
  status: string; summary: string; terminal: boolean;
  credits: string; credits_per_minute: string; seconds: number;
  started_at: string | null; ended_at: string | null;
  idle_ends_at: string | null; max_ends_at: string | null;
  stop_reason?: string | null; stop_reason_text?: string | null; renewal_required_credits?: string | null;
};
export type Preview = {
  id: string; computer_id: string; name: string; status: string; summary: string; terminal: boolean; url: string | null;
};
export type PreviewDetail = { preview: Preview; computer: Computer; conversation_id: string };
export type CloudScope = { userId: string; conversationId: string | null; previewId?: string };
export type CloudSnapshot = {
  computers: Record<string, Computer>; details: Record<string, PreviewDetail>;
  previewErrors: Record<string, string>; error: string; actionErrors: Record<string, string>;
  ending: Record<string, boolean>; revoked: boolean;
};
type Requester = <T>(path: string, options?: RequestInit) => Promise<T>;
const post = { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" };
const empty = (): CloudSnapshot => ({
  computers: {}, details: {}, previewErrors: {}, error: "", actionErrors: {}, ending: {}, revoked: false,
});
const status = (error: unknown) => typeof error === "object" && error && "status" in error ? Number(error.status) : null;

export function previewReady(snapshot: CloudSnapshot, id: string) {
  const detail = snapshot.details[id], computer = detail && snapshot.computers[detail.computer.id];
  return !!detail && !!computer && !snapshot.revoked && !snapshot.ending[computer.id]
    && !snapshot.previewErrors[id] && detail.preview.status === "ready" && !detail.preview.terminal
    && !computer.terminal && !["stopping", "uncertain"].includes(computer.status);
}

// Message references are checked once; only an open preview is polled.
export class CloudComputerStore {
  private value = empty();
  private listeners = new Set<() => void>();
  private epoch = 0;
  private disposed = false;
  private poll: Promise<void> | null = null;
  private controller: AbortController | null = null;
  private selected: string | null = null;
  private references = new Map<string, number>();
  private pending = new Set<string>();
  private renewing = new Set<string>();
  readonly scope: CloudScope;
  private readonly request: Requester;
  constructor(scope: CloudScope, request: Requester) {
    this.scope = scope; this.request = request; this.selected = scope.previewId ?? null;
  }
  getSnapshot = () => this.value;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private set(patch: Partial<CloudSnapshot>) {
    if (this.disposed) return;
    this.value = { ...this.value, ...patch }; this.listeners.forEach(listener => listener());
  }
  activate() { this.disposed = false; }
  dispose() {
    this.disposed = true; this.epoch++; this.controller?.abort(); this.poll = null;
    this.value = empty(); this.pending.clear();
  }
  revoke = () => {
    this.epoch++; this.controller?.abort(); this.poll = null; this.pending.clear();
    this.set({ ...empty(), revoked: true, error: "预览暂不可访问，请重新登录后重试" });
  };
  watchPreview(id: string) {
    this.references.set(id, (this.references.get(id) ?? 0) + 1);
    if (!this.value.details[id]) {
      this.pending.add(id);
      queueMicrotask(() => { void this.refresh(true); });
    }
    return () => {
      const remaining = (this.references.get(id) ?? 1) - 1;
      if (remaining) this.references.set(id, remaining);
      else { this.references.delete(id); this.pending.delete(id); }
    };
  }
  selectPreview(id: string | null) {
    this.selected = id;
    this.epoch++; this.controller?.abort(); this.poll = null;
    if (id) void this.refresh(true);
  }
  private mergeComputer(next: Computer) {
    const old = this.value.computers[next.id];
    if (old?.terminal) return old;
    if (old?.status === "stopping" && !next.terminal && next.status !== "stopping")
      return { ...next, status: "stopping", summary: old.summary, stop_reason: old.stop_reason,
        stop_reason_text: old.stop_reason_text, renewal_required_credits: old.renewal_required_credits };
    return next;
  }
  refresh = (replace = false): Promise<void> => {
    if (this.disposed || this.value.revoked) return Promise.resolve();
    if (this.poll && !replace) return this.poll;
    const ids = [...new Set([...this.pending, ...(this.selected ? [this.selected] : [])])];
    if (!ids.length) return Promise.resolve();
    this.controller?.abort();
    const controller = new AbortController(), epoch = ++this.epoch;
    this.controller = controller;
    const valid = () => !this.disposed && epoch === this.epoch;
    const run = async () => {
      const results = await Promise.all(ids.map(async id => {
        try {
          return { id, detail: await this.request<PreviewDetail>(`/previews/${id}`, { signal: controller.signal }) };
        } catch (error) { return { id, error }; }
      }));
      if (!valid()) return;
      if (results.some(result => "error" in result && [401, 403].includes(Number(status(result.error))))) {
        this.revoke(); return;
      }
      const computers = { ...this.value.computers }, details = { ...this.value.details };
      const previewErrors = { ...this.value.previewErrors }, ending = { ...this.value.ending };
      const actionErrors = { ...this.value.actionErrors };
      results.forEach(result => {
        this.pending.delete(result.id);
        if (result.detail) {
          details[result.id] = result.detail;
          const computer = this.mergeComputer(result.detail.computer);
          computers[computer.id] = computer;
          if (computer.terminal) { ending[computer.id] = false; delete actionErrors[computer.id]; }
          delete previewErrors[result.id];
        } else previewErrors[result.id] = [404, 410].includes(Number(status(result.error)))
          ? "预览已结束" : "状态更新失败，当前显示上次结果，请刷新重试";
      });
      this.set({ computers, details, previewErrors, ending, actionErrors, error: "" });
      if (valid()) this.poll = null;
    };
    this.poll = run(); return this.poll;
  };
  end = async (id: string) => {
    const computer = this.value.computers[id];
    if (this.disposed || !computer || computer.terminal || this.value.ending[id]
      || computer.status === "stopping" || this.value.revoked) return;
    this.set({ ending: { ...this.value.ending, [id]: true }, actionErrors: { ...this.value.actionErrors, [id]: "" } });
    try {
      const result = await this.request<{ computer: Computer }>(`/cloud-computers/${id}/end`, post);
      if (this.disposed || this.value.revoked) return;
      const next = this.mergeComputer(result.computer);
      this.set({ computers: { ...this.value.computers, [id]: next },
        ending: { ...this.value.ending, [id]: !next.terminal } });
      await this.refresh(true);
    } catch (error) {
      if (this.disposed || this.value.revoked) return;
      if ([401, 403].includes(Number(status(error)))) this.revoke();
      else this.set({ ending: { ...this.value.ending, [id]: false },
        actionErrors: { ...this.value.actionErrors, [id]: "结束结果尚未确认，请刷新状态后重试" } });
    }
  };
  renew = async (id: string) => {
    if (this.selected !== id || !previewReady(this.value, id) || this.renewing.has(id) || this.disposed) return;
    this.renewing.add(id);
    try {
      await this.request(`/previews/${id}/heartbeat`, post);
      if (!this.disposed && !this.value.revoked) await this.refresh();
    } catch (error) {
      if (this.disposed || this.value.revoked || this.selected !== id) return;
      if ([401, 403].includes(Number(status(error)))) this.revoke();
      else this.set({ previewErrors: { ...this.value.previewErrors, [id]:
        [404, 410].includes(Number(status(error))) ? "预览已结束" : "预览连接更新失败，请刷新重试" } });
    } finally { this.renewing.delete(id); }
  };
}
