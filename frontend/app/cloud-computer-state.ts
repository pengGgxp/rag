export type Computer = {
  id: string; status: string; summary: string; terminal: boolean;
  stop_reason: string | null; stop_reason_text: string | null; renewal_required_credits: string | null;
  credits: string; credits_per_minute: string; seconds: number;
  idle_ends_at: string | null; max_ends_at: string | null;
};
export type Preview = {
  id: string; name: string; status: string; summary: string; terminal: boolean; url: string | null;
};
export type PreviewDetail = { preview: Preview; computer: Computer; conversation_id: string };
export type CloudCommand = {
  id: string; status: string; terminal: boolean; cancel_requested: boolean;
  result: { error?: string; generated_files?: { id: string; path: string }[] };
};
export type CloudCatalog = {
  available: boolean; credits_per_minute?: number; idle_seconds?: number; max_seconds?: number;
};
export type CloudScope = { userId: string; workspaceId: string; conversationId: string | null; previewId?: string };
export type CloudSnapshot = {
  catalog: CloudCatalog | null; computer: Computer | null; commands: CloudCommand[];
  preview: Preview | null; details: Record<string, PreviewDetail>;
  previewErrors: Record<string, string>; error: string; actionError: string;
  ending: boolean; cancelling: string | null; revoked: boolean; loaded: boolean;
};
type Requester = <T>(path: string, options?: RequestInit) => Promise<T>;
const post = { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" };
const empty = (): CloudSnapshot => ({
  catalog: null, computer: null, commands: [], preview: null, details: {}, previewErrors: {},
  error: "", actionError: "", ending: false, cancelling: null, revoked: false, loaded: false,
});
const status = (error: unknown) => typeof error === "object" && error && "status" in error ? error.status : null;

export function previewReady(snapshot: CloudSnapshot, id: string) {
  const detail = snapshot.details[id], computer = snapshot.computer;
  return !!detail && !snapshot.revoked && !snapshot.ending && !snapshot.previewErrors[id]
    && detail.preview.status === "ready" && !detail.preview.terminal && computer?.id === detail.computer.id
    && !computer.terminal && !["stopping", "uncertain"].includes(computer.status);
}

// One store belongs to one authenticated scope; nothing is persisted across scopes or tabs.
export class CloudComputerStore {
  private value = empty();
  private listeners = new Set<() => void>();
  private epoch = 0;
  private disposed = false;
  private poll: Promise<void> | null = null;
  private controller: AbortController | null = null;
  private selected: string | null = null;
  private renewing = new Set<string>();
  private retired = new Set<string>();
  private conversationId: string | null;
  readonly scope: CloudScope;
  private readonly request: Requester;
  constructor(scope: CloudScope, request: Requester) {
    this.scope = scope;
    this.request = request;
    this.conversationId = scope.conversationId;
    this.selected = scope.previewId ?? null;
  }
  getSnapshot = () => this.value;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private set(patch: Partial<CloudSnapshot>) {
    if (this.disposed) return;
    this.value = { ...this.value, ...patch };
    this.listeners.forEach(listener => listener());
  }
  activate() { this.disposed = false; }
  dispose() {
    this.disposed = true; this.epoch++; this.controller?.abort(); this.poll = null;
    this.value = empty();
  }
  revoke = () => {
    this.epoch++; this.controller?.abort(); this.poll = null;
    this.set({ ...empty(), revoked: true, error: "云电脑暂不可访问，请重新登录或切换对话" });
  };
  selectPreview(id: string | null) { this.selected = id; if (id) void this.refresh(true); }
  private mergeComputer(next: Computer | null) {
    const old = this.value.computer;
    if (old && (!next || old.id === next.id)) {
      if (old.terminal) return old;
      if (old.status === "stopping" && !next?.terminal && next?.status !== "stopping")
        return { ...(next ?? old), status: "stopping", summary: old.summary,
          stop_reason: old.stop_reason, stop_reason_text: old.stop_reason_text,
          renewal_required_credits: old.renewal_required_credits };
    }
    return next;
  }
  refresh = (replace = false): Promise<void> => {
    if (this.disposed || this.value.revoked) return Promise.resolve();
    if (this.poll && !replace) return this.poll;
    this.controller?.abort();
    const controller = new AbortController(), epoch = ++this.epoch;
    this.controller = controller;
    const read = <T>(path: string) => this.request<T>(path, { signal: controller.signal });
    const valid = () => !this.disposed && epoch === this.epoch;
    const run = async () => {
      try {
        let conversationId = this.conversationId;
        if (!conversationId && this.scope.previewId) {
          const detail = await read<PreviewDetail>(`/previews/${this.scope.previewId}`);
          if (!valid()) return;
          conversationId = detail.conversation_id;
          this.conversationId = conversationId;
        }
        const [catalog, current, commands, latest] = await Promise.all([
          read<CloudCatalog>("/cloud-computer/catalog"),
          conversationId ? read<{ computer: Computer | null }>(`/conversations/${conversationId}/cloud-computer`) : { computer: null },
          conversationId ? read<{ items: CloudCommand[] }>(`/conversations/${conversationId}/cloud-computer/commands`) : { items: [] },
          conversationId ? read<{ preview: Preview | null }>(`/conversations/${conversationId}/preview`) : { preview: null },
        ]);
        const ids = [...new Set([latest.preview?.id, this.selected].filter((id): id is string => !!id))];
        const details: Record<string, PreviewDetail> = {}, previewErrors: Record<string, string> = {};
        await Promise.all(ids.map(async id => {
          try {
            const detail = await read<PreviewDetail>(`/previews/${id}`);
            if (detail.conversation_id === conversationId) details[id] = detail;
            else previewErrors[id] = "此预览不属于当前对话";
          } catch (error) {
            if ([401, 403, 404, 410].includes(Number(status(error)))) previewErrors[id] = "预览已结束或暂不可访问";
            else throw error;
          }
        }));
        if (!valid()) return;
        const previous = this.value.computer;
        if (current.computer && this.retired.has(current.computer.id)) return;
        if (previous && current.computer && previous.id !== current.computer.id) this.retired.add(previous.id);
        const computer = this.mergeComputer(current.computer);
        const changed = !!previous && !!computer && previous.id !== computer.id;
        this.set({ catalog, computer, commands: commands.items, preview: latest.preview, details, previewErrors,
          loaded: true, error: "", actionError: computer?.terminal || changed ? "" : this.value.actionError,
          ending: computer?.terminal || changed ? false : this.value.ending });
      } catch (error) {
        if (!valid()) return;
        if ([401, 403, 404].includes(Number(status(error)))) this.revoke();
        else this.set({ error: "状态更新失败，当前显示上次结果", loaded: true });
      } finally {
        if (valid()) this.poll = null;
      }
    };
    this.poll = run();
    return this.poll;
  };
  end = async () => {
    if (!this.conversationId || !this.value.computer || this.value.computer.terminal
      || this.value.ending || this.value.computer.status === "stopping" || this.value.revoked) return;
    const computerId = this.value.computer.id;
    const epoch = ++this.epoch;
    this.controller?.abort(); this.poll = null;
    this.set({ ending: true, actionError: "" });
    try {
      const result = await this.request<{ computer: Computer | null }>(
        `/conversations/${this.conversationId}/cloud-computer/end`, post);
      if (this.disposed || this.value.revoked) return;
      // A concurrent poll may finish first; terminal data must still win.
      if (this.value.computer?.id !== computerId) return;
      if (result.computer?.id === computerId) this.set({ computer: this.mergeComputer(result.computer) });
      await this.refresh(true);
    } catch (error) {
      if (this.disposed || this.value.revoked || this.value.computer?.id !== computerId) return;
      if ([401, 403, 404].includes(Number(status(error)))) this.revoke();
      else {
        this.set({ ending: false, actionError: "结束结果尚未确认，请刷新状态后重试" });
        if (epoch === this.epoch) await this.refresh();
      }
    }
  };
  cancel = async (id: string) => {
    if (this.disposed || this.value.revoked || this.value.cancelling
      || !this.value.commands.some(command => command.id === id && !command.terminal && !command.cancel_requested)) return;
    this.set({ cancelling: id, actionError: "" });
    try {
      await this.request(`/cloud-computer/commands/${id}/cancel`, post);
      if (!this.disposed) await this.refresh(true);
    } catch (error) {
      if ([401, 403, 404].includes(Number(status(error)))) this.revoke();
      else this.set({ actionError: "停止命令失败，请重试" });
    } finally { this.set({ cancelling: null }); }
  };
  renew = async (id: string) => {
    if (!previewReady(this.value, id) || this.renewing.has(id) || this.disposed) return;
    this.renewing.add(id);
    try {
      await this.request(`/previews/${id}/heartbeat`, post);
      // Never install heartbeat data: the canonical status query owns all fees and end state.
      if (!this.disposed && !this.value.revoked) await this.refresh();
    } catch (error) {
      if ([401, 403, 404, 410].includes(Number(status(error)))) {
        this.set({ previewErrors: { ...this.value.previewErrors, [id]: "预览已结束或暂不可访问" } });
        await this.refresh();
      } else this.set({ error: "预览连接更新失败，请重试" });
    } finally { this.renewing.delete(id); }
  };
}
