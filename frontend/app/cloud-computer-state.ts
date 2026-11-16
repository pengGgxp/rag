export type Computer = {
  id: string; computer_id: string; name: string; conversation_id: string;
  status: string; summary: string; terminal: boolean;
  credits: string; credits_per_minute: string; seconds: number;
  idle_ends_at: string | null; max_ends_at: string | null;
  stop_reason?: string | null; stop_reason_text?: string | null; renewal_required_credits?: string | null;
};
export type Preview = {
  id: string; computer_id: string; name: string; status: string; summary: string; terminal: boolean; url: string | null;
};
export type PreviewDetail = { preview: Preview; computer: Computer; conversation_id: string };
export type CloudCommand = {
  id: string; computer_id: string; status: string; terminal: boolean; cancel_requested: boolean;
  result: { error?: string; generated_files?: { id: string; path: string }[] };
};
export type CloudCatalog = {
  available: boolean; credits_per_minute?: number; idle_seconds?: number; max_seconds?: number;
};
export type CloudScope = { userId: string; workspaceId: string; conversationId: string | null; previewId?: string };
export type CloudSnapshot = {
  catalog: CloudCatalog | null; computers: Record<string, Computer>; computerIds: string[]; commands: CloudCommand[];
  details: Record<string, PreviewDetail>; previewErrors: Record<string, string>; instanceErrors: Record<string, string>;
  error: string; actionErrors: Record<string, string>; ending: Record<string, boolean>; cancelling: Record<string, boolean>;
  revoked: boolean; loaded: boolean;
};
type Requester = <T>(path: string, options?: RequestInit) => Promise<T>;
const post = { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" };
const empty = (): CloudSnapshot => ({
  catalog: null, computers: {}, computerIds: [], commands: [], details: {}, previewErrors: {}, instanceErrors: {},
  error: "", actionErrors: {}, ending: {}, cancelling: {}, revoked: false, loaded: false,
});
const status = (error: unknown) => typeof error === "object" && error && "status" in error ? Number(error.status) : null;

export function previewReady(snapshot: CloudSnapshot, id: string) {
  const detail = snapshot.details[id], computer = detail && snapshot.computers[detail.computer.id];
  return !!detail && !!computer && !snapshot.revoked && !snapshot.ending[computer.id]
    && !snapshot.previewErrors[id] && !snapshot.instanceErrors[computer.id]
    && detail.preview.status === "ready" && !detail.preview.terminal
    && !computer.terminal && !["stopping", "uncertain"].includes(computer.status);
}

// Workspace-scoped observation. Viewing a row never changes a model execution target.
export class CloudComputerStore {
  private value = empty();
  private listeners = new Set<() => void>();
  private epoch = 0;
  private disposed = false;
  private poll: Promise<void> | null = null;
  private controller: AbortController | null = null;
  private selected: string | null = null;
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
    this.disposed = true; this.epoch++; this.controller?.abort(); this.poll = null; this.value = empty();
  }
  revoke = () => {
    this.epoch++; this.controller?.abort(); this.poll = null;
    this.set({ ...empty(), revoked: true, error: "云电脑暂不可访问，请重新登录或切换工作区" });
  };
  selectPreview(id: string | null) { this.selected = id; if (id) void this.refresh(true); }
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
    this.controller?.abort();
    const controller = new AbortController(), epoch = ++this.epoch;
    this.controller = controller;
    const read = <T>(path: string) => this.request<T>(path, { signal: controller.signal });
    const valid = () => !this.disposed && epoch === this.epoch;
    const run = async () => {
      try {
        const [catalog, recent] = await Promise.all([
          read<CloudCatalog>("/cloud-computer/catalog"), read<{ items: Computer[] }>("/cloud-computers?limit=20"),
        ]);
        const all = new Map(recent.items.map(item => [item.id, item]));
        let offset: number | null = 0;
        while (offset !== null) {
          const page: { items: Computer[]; next_offset: number | null } =
            await read(`/cloud-computers?active_only=true&offset=${offset}`);
          page.items.forEach(item => all.set(item.id, item)); offset = page.next_offset;
        }
        const computers: Record<string, Computer> = {};
        all.forEach(item => { computers[item.id] = this.mergeComputer(item); });
        const commands: CloudCommand[] = [], previews = new Set<string>(this.selected ? [this.selected] : []);
        const instanceErrors: Record<string, string> = {};
        await Promise.all([...all.keys()].map(async id => {
          try {
            const [jobs, current] = await Promise.all([
              read<{ items: CloudCommand[] }>(`/cloud-computers/${id}/commands`),
              read<{ preview: Preview | null }>(`/cloud-computers/${id}/preview`),
            ]);
            commands.push(...jobs.items);
            if (current.preview) previews.add(current.preview.id);
          } catch (error) {
            if ([401, 403].includes(Number(status(error)))) throw error;
            instanceErrors[id] = "此台状态更新失败，请刷新后重试";
            commands.push(...this.value.commands.filter(command => command.computer_id === id));
          }
        }));
        const details: Record<string, PreviewDetail> = {}, previewErrors: Record<string, string> = {};
        await Promise.all([...previews].map(async id => {
          try {
            const detail = await read<PreviewDetail>(`/previews/${id}`);
            details[id] = detail;
            if (!computers[detail.computer.id]) computers[detail.computer.id] = this.mergeComputer(detail.computer);
          } catch (error) {
            if ([401, 403].includes(Number(status(error)))) throw error;
            previewErrors[id] = "预览已结束或暂不可访问";
          }
        }));
        if (!valid()) return;
        Object.keys(computers).forEach(id => { computers[id] = this.mergeComputer(computers[id]); });
        const ending = { ...this.value.ending };
        Object.values(computers).forEach(item => { if (item.terminal) ending[item.id] = false; });
        this.set({ catalog, computers, computerIds: [...all.keys()], commands, details, previewErrors,
          instanceErrors, ending, loaded: true, error: "" });
      } catch (error) {
        if (!valid()) return;
        if ([401, 403].includes(Number(status(error)))) this.revoke();
        else this.set({ error: "状态更新失败，当前显示上次结果", loaded: true });
      } finally { if (valid()) this.poll = null; }
    };
    this.poll = run(); return this.poll;
  };
  end = async (id: string) => {
    const computer = this.value.computers[id];
    if (!computer || computer.terminal || this.value.ending[id] || computer.status === "stopping" || this.value.revoked) return;
    this.set({ ending: { ...this.value.ending, [id]: true }, actionErrors: { ...this.value.actionErrors, [id]: "" } });
    try {
      const result = await this.request<{ computer: Computer }>(`/cloud-computers/${id}/end`, post);
      if (this.disposed || this.value.revoked) return;
      this.set({ computers: { ...this.value.computers, [id]: this.mergeComputer(result.computer) } });
      await this.refresh(true);
    } catch (error) {
      if (this.disposed || this.value.revoked) return;
      if ([401, 403].includes(Number(status(error)))) this.revoke();
      else this.set({ ending: { ...this.value.ending, [id]: false },
        actionErrors: { ...this.value.actionErrors, [id]: "结束结果尚未确认，请刷新状态后重试" } });
    }
  };
  cancel = async (id: string) => {
    const command = this.value.commands.find(item => item.id === id);
    if (this.disposed || this.value.revoked || this.value.cancelling[id] || !command || command.terminal || command.cancel_requested) return;
    this.set({ cancelling: { ...this.value.cancelling, [id]: true } });
    try {
      await this.request(`/cloud-computer/commands/${id}/cancel`, post);
      if (!this.disposed) await this.refresh(true);
    } catch (error) {
      if ([401, 403].includes(Number(status(error)))) this.revoke();
      else this.set({ actionErrors: { ...this.value.actionErrors, [command.computer_id]: "停止命令失败，请重试" } });
    } finally { this.set({ cancelling: { ...this.value.cancelling, [id]: false } }); }
  };
  renew = async (id: string) => {
    if (!previewReady(this.value, id) || this.renewing.has(id) || this.disposed) return;
    this.renewing.add(id);
    try {
      await this.request(`/previews/${id}/heartbeat`, post);
      if (!this.disposed && !this.value.revoked) await this.refresh();
    } catch (error) {
      if ([401, 403, 404, 410].includes(Number(status(error)))) {
        this.set({ previewErrors: { ...this.value.previewErrors, [id]: "预览已结束或暂不可访问" } });
        await this.refresh();
      } else this.set({ error: "预览连接更新失败，请重试" });
    } finally { this.renewing.delete(id); }
  };
}
