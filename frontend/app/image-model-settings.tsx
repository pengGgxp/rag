"use client";

import { useEffect, useState } from "react";
import { RefreshCw, Save } from "lucide-react";
import { ApiError, request } from "./api";
import { useAuth } from "./auth";
import { AdminLoad, json, useAdminResource } from "./admin/admin-ui";
import type { ModelProvider } from "./model-types";
import { ImageTaskActivity, type ImageTaskResult } from "./image-task-result";

type Settings = { version: number; generate_model_id: string | null; edit_model_id: string | null };

export function ImageModelSettings({ state }: { state: (dirty: boolean, busy: boolean) => void }) {
  const { session } = useAuth();
  const attemptKey = `rag-image-test:${session.user.id}:${session.workspace_id}`;
  const resource = useAdminResource<Settings>("/site/admin/image-models");
  const models = useAdminResource<ModelProvider[]>("/site/admin/llm-providers");
  const [form, setForm] = useState<Settings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const [discard, setDiscard] = useState(false);
  const [operation, setOperation] = useState<"generate" | "edit">("generate");
  const [prompt, setPrompt] = useState("");
  const [reference, setReference] = useState("");
  const [referenceVersion, setReferenceVersion] = useState(1);
  const [confirmTest, setConfirmTest] = useState(false);
  const [task, setTask] = useState<ImageTaskResult | null>(null);
  const [attempt, setAttempt] = useState<{ key: string; payload: string } | null>(null);
  const [restoring, setRestoring] = useState(true);
  const [reviewOpen, setReviewOpen] = useState(false);
  useEffect(() => {
    let disposed = false;
    setRestoring(true); setTask(null); setAttempt(null);
    async function restore() {
      try {
        const saved = sessionStorage.getItem(attemptKey);
        if (!saved) return;
        const pending = JSON.parse(saved) as { key: string; payload: string };
        const input = JSON.parse(pending.payload);
        if (disposed) return;
        setAttempt(pending); setPrompt(input.prompt); setOperation(input.operation);
        setReference(input.references?.[0]?.id ?? ""); setReferenceVersion(input.references?.[0]?.version ?? 1);
        const original = await request<ImageTaskResult>(`/image-tasks/by-request/${pending.key}`);
        if (!disposed) setTask(original);
      } catch (cause) {
        if (!disposed && !(cause instanceof ApiError && cause.status === 404)) setError("上次测试进度暂不可用，重试将查询原请求");
      } finally { if (!disposed) setRestoring(false); }
    }
    void restore();
    return () => { disposed = true; };
  }, [attemptKey]);
  const dirty = !!form && JSON.stringify(form) !== JSON.stringify(resource.data);
  useEffect(() => { if (resource.data) setForm(resource.data); }, [resource.data]);
  useEffect(() => state(dirty, busy), [dirty, busy, state]);
  async function save() {
    if (!form) return;
    setBusy(true); setError(""); setNotice("");
    try { await request<Settings>("/site/admin/image-models", json(form, "PUT")); await resource.reload(); setNotice("默认图片模型已保存"); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "保存失败，请重试"); setConflict(cause instanceof ApiError && cause.status === 409); }
    finally { setBusy(false); }
  }
  const selected = models.data?.find(model => model.id === resource.data?.[`${operation}_model_id`]);
  const spec = selected?.image_config.specs.find(spec => spec.operation === operation && spec.size === selected.image_config.default_size && spec.quality === selected.image_config.default_quality);
  async function test() {
    setBusy(true); setError(""); setConfirmTest(false);
    const payload = JSON.stringify({ operation, prompt, ...(operation === "edit" ? { references: [{ id: reference, version: referenceVersion }] } : {}) });
    const current = attempt?.payload === payload && !task ? attempt : { key: crypto.randomUUID(), payload };
    try {
      sessionStorage.setItem(attemptKey, JSON.stringify(current));
      setAttempt(current);
      setTask(await request<ImageTaskResult>("/image-tasks", json({ ...JSON.parse(payload), idempotency_key: current.key })));
    }
    catch (cause) { setError(cause instanceof Error ? cause.message : "测试未完成，可重试查询原请求"); }
    finally { setBusy(false); }
  }
  return <section className="model-form-section image-model-settings"><h2>默认图片模型</h2>
    <AdminLoad loading={resource.loading || models.loading} error={resource.error || models.error} retry={() => { void resource.reload(); void models.reload(); }}>
      {form && <form onSubmit={event => { event.preventDefault(); void save(); }}>
        <fieldset disabled={busy} className="provider-form-fields"><div className="provider-form-grid">
          {(["generate", "edit"] as const).map(operation => <label key={operation}>{operation === "generate" ? "图片生成" : "图片编辑"}
            <select value={form[`${operation}_model_id`] ?? ""} onChange={event => setForm({ ...form, [`${operation}_model_id`]: event.target.value || null })}>
              <option value="">不开放</option>
              {models.data?.filter(model => model.enabled && model.image_config?.[operation]).map(model => <option key={model.id} value={model.id}>{model.connection_name} / {model.name}</option>)}
            </select></label>)}
        </div></fieldset>
        {error && <p className="form-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
        {conflict && <button className="secondary" type="button" disabled={busy} onClick={() => setDiscard(true)}>重新加载</button>}
        {discard && <div className="admin-inline-confirm" role="alert"><span>放弃当前修改并重新加载？</span><button type="button" className="secondary" onClick={() => setDiscard(false)}>继续编辑</button><button type="button" className="danger" onClick={() => { setDiscard(false); setConflict(false); void resource.reload(); }}>放弃修改</button></div>}
        <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : ""}</span><button className="primary" disabled={busy || !dirty || conflict}><Save size={16} />保存默认模型</button></div>
      </form>}
      <details className="model-advanced"><summary>图片付费测试</summary>
        <div className="provider-form-grid"><label>操作<select value={operation} disabled={busy} onChange={event => { setOperation(event.target.value as "generate" | "edit"); setConfirmTest(false); }}><option value="generate">生成</option><option value="edit">编辑</option></select></label>
          <label>提示词<input value={prompt} maxLength={12000} disabled={busy} onChange={event => { setPrompt(event.target.value); setConfirmTest(false); }} /></label>
          {operation === "edit" && <><label>参考资源 ID<input value={reference} disabled={busy} onChange={event => { setReference(event.target.value); setConfirmTest(false); }} /></label><label>资源版本<input type="number" min={1} value={referenceVersion} onChange={event => { setReferenceVersion(Number(event.target.value)); setConfirmTest(false); }} /></label></>}
        </div>
        <button type="button" className="secondary" disabled={busy || restoring || dirty || !spec || !prompt.trim() || !!task && (!task.terminal || ["uncertain", "save_failed"].includes(task.status))} onClick={() => setConfirmTest(true)}>测试当前默认模型</button>
        {confirmTest && spec && <div className="admin-inline-confirm" role="alert"><span>将调用 {selected?.name}，按 {spec.credits_per_image} 积分/张及当前工作区倍率计费，生成 1 张，并使用存储与下载额度。</span>
          <button type="button" className="secondary" onClick={() => setConfirmTest(false)}>取消</button><button type="button" className="primary" disabled={busy} onClick={() => void test()}>确认付费测试</button></div>}
        {task && <ImageTaskActivity key={task.task_id} initial={task} onUpdate={setTask} />}
      </details>
      <details className="model-advanced" onToggle={event => setReviewOpen(event.currentTarget.open)}><summary>待核对图片任务</summary>
        {reviewOpen && <ImageTaskReview />}
      </details>
    </AdminLoad>
  </section>;
}

type ReviewTask = ImageTaskResult & { workspace_id: string; actor_user_id: string; upstream_request_id: string | null; held_credits: string; requested_images: number };

function ImageTaskReview() {
  const resource = useAdminResource<ReviewTask[]>("/site/admin/image-models/tasks");
  return <div className="image-task-review">
    <button type="button" className="secondary" onClick={() => void resource.reload()} disabled={resource.loading}><RefreshCw size={15} />刷新任务</button>
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {resource.data?.length === 0 && <p className="billing-muted">暂无待核对任务</p>}
      {resource.data?.map(task => <ImageTaskResolution key={task.task_id} task={task} reload={resource.reload} />)}
    </AdminLoad>
  </div>;
}

function ImageTaskResolution({ task, reload }: { task: ReviewTask; reload: () => Promise<void> }) {
  const [count, setCount] = useState(0);
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function resolve() {
    setBusy(true); setError("");
    try {
      await request(`/site/admin/image-models/tasks/${task.task_id}/resolve`, json({ confirmed_images: count, reason }));
      await reload();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "核对未完成，请重试"); }
    finally { setBusy(false); }
  }
  return <section className="image-review-row"><strong>{task.summary}</strong>
    <dl><dt>任务</dt><dd>{task.task_id}</dd><dt>工作区</dt><dd>{task.workspace_id}</dd>
      <dt>上游请求</dt><dd>{task.upstream_request_id || "未返回"}</dd><dt>预留积分</dt><dd>{task.held_credits}</dd></dl>
    {task.status === "save_failed" ? <p className="billing-muted">请由任务发起人恢复权限或空间后重试保存。</p> : <form onSubmit={event => { event.preventDefault(); setConfirm(true); }}>
      <fieldset className="provider-form-fields" disabled={busy}><div className="provider-form-grid">
        <label>确认已生成张数<input type="number" required min={0} max={task.requested_images} step={1} value={count} onChange={event => { setCount(Number(event.target.value)); setConfirm(false); }} /></label>
        <label>核对依据<input required minLength={5} maxLength={300} value={reason} onChange={event => { setReason(event.target.value); setConfirm(false); }} /></label>
      </div></fieldset>
      {!confirm && <button className="secondary" disabled={busy}>核对费用</button>}
      {confirm && <div className="admin-inline-confirm" role="alert"><span>按 {count} 张结算生成积分并释放差额；此操作不会再次生成图片。</span>
        <button type="button" className="secondary" disabled={busy} onClick={() => setConfirm(false)}>返回核对</button>
        <button type="button" className="primary" disabled={busy} onClick={() => void resolve()}>确认结算</button></div>}
    </form>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </section>;
}
