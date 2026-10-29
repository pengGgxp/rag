"use client";

import { useState } from "react";
import { Check, Eye, FlaskConical, Loader2 } from "lucide-react";
import { request } from "./api";
import { json } from "./admin/admin-ui";
import { thinkingConfigError } from "./thinking-config-fields";
import type { ThinkingConfig } from "./model-types";

type TestResult = {
  version: number; thinking_version: number; completed: boolean; error: string | null;
  received_thinking?: boolean; reasoning_tokens?: number | null; credits?: number;
  parameters?: Record<string, unknown>;
};

export function ThinkingDiagnostics({ id, version, thinkingVersion, thinking, maxOutput, dirty }: {
  id: string; version: number; thinkingVersion: number; thinking: ThinkingConfig;
  maxOutput: number | null; dirty: boolean;
}) {
  const [option, setOption] = useState<string | null>(null);
  const optionId = thinking.options.some(item => item.id === option) ? option : thinking.default_option_id;
  const fingerprint = JSON.stringify({ id, version, thinkingVersion, thinking, maxOutput, optionId });
  const [preview, setPreview] = useState<{ fingerprint: string; parameters: Record<string, unknown> } | null>(null);
  const [result, setResult] = useState<{ fingerprint: string; value: TestResult } | null>(null);
  const [busy, setBusy] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState("");
  const [testRequest, setTestRequest] = useState<{ fingerprint: string; id: string } | null>(null);
  const stale = result !== null && (dirty || result.fingerprint !== fingerprint);
  const base = `/site/admin/llm-providers/${encodeURIComponent(id)}/thinking`;
  async function showPreview() {
    setBusy("preview"); setError(""); setConfirm(false);
    try {
      const value = await request<{ parameters: Record<string, unknown> }>(`${base}/preview`,
        json({ version, thinking, option_id: optionId, max_output_tokens: maxOutput }));
      setPreview({ fingerprint, parameters: value.parameters });
    } catch (e) { setError(e instanceof Error ? e.message : "暂时无法预览，请重试"); }
    finally { setBusy(""); }
  }
  async function test() {
    setBusy("test"); setError(""); setConfirm(false);
    const requestId = testRequest?.fingerprint === fingerprint ? testRequest.id : crypto.randomUUID();
    setTestRequest({ fingerprint, id: requestId });
    try {
      const value = await request<TestResult>(`${base}/test`, json({ request_id: requestId, version,
        thinking_config_version: thinkingVersion, option_id: optionId }));
      setResult({ fingerprint, value });
      setTestRequest(null);
    } catch (e) { setError(e instanceof Error ? e.message : "测试未完成，请重试"); }
    finally { setBusy(""); }
  }
  return <section className="model-form-section thinking-diagnostics">
    <h2>参数验证</h2>
    {thinking.options.length > 0 && <div className="provider-form-grid"><label className="thinking-test-option">验证档位
      <select value={optionId ?? ""} disabled={!!busy} onChange={e => { setOption(e.target.value); setConfirm(false); }}>
        {thinking.options.map(item => <option key={item.id} value={item.id}>{item.label} · {item.value}</option>)}
      </select>
    </label></div>}
    <div className="thinking-diagnostic-actions">
      <button type="button" className="secondary" disabled={!!busy || !!thinkingConfigError(thinking)}
        onClick={() => void showPreview()}>{busy === "preview" ? <Loader2 size={15} className="spin" /> : <Eye size={15} />}预览发送参数</button>
      <button type="button" className="secondary" disabled={!!busy || dirty}
        onClick={() => setConfirm(true)}>{busy === "test" ? <Loader2 size={15} className="spin" /> : <FlaskConical size={15} />}测试已保存配置</button>
    </div>
    {dirty && <p className="billing-muted">保存修改后可测试。</p>}
    {confirm && !dirty && <div className="admin-inline-confirm" role="alert">
      <span>将发送一次最多输出 1024 Token 的测试，按当前工作区计费。</span>
      <button className="secondary" type="button" onClick={() => setConfirm(false)}>取消</button>
      <button className="primary" type="button" onClick={() => void test()}>确认测试</button>
    </div>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {preview && preview.fingerprint === fingerprint && <dl className="thinking-parameter-list" aria-label="实际发送参数">
      {Object.entries(preview.parameters).map(([key, value]) => <div key={key}><dt>{key}</dt>
        <dd><code>{JSON.stringify(value)}</code></dd></div>)}
    </dl>}
    {result && <div className="thinking-test-result" aria-live="polite">
      {stale ? <p>配置或档位已变化，此测试结果已过期。</p> : <>
        <p className="thinking-test-heading">{result.value.completed && <Check size={15} />}
          {result.value.completed ? "模型已完整返回" : "测试未完整完成"}</p>
        <dl className="thinking-parameter-list">
          <div><dt>思考内容</dt><dd>{result.value.received_thinking ? "已收到" : "未返回"}</dd></div>
          <div><dt>推理用量</dt><dd>{result.value.reasoning_tokens == null ? "未报告" : `${result.value.reasoning_tokens} Token`}</dd></div>
          {result.value.credits !== undefined && <div><dt>本次消费</dt><dd>{result.value.credits} 积分</dd></div>}
        </dl>
        {result.value.error && <p className="form-error">{result.value.error}</p>}
        {result.value.parameters && <details className="thinking-test-parameters"><summary>本次测试参数</summary>
          <dl className="thinking-parameter-list">
            {Object.entries(result.value.parameters).map(([key, value]) => <div key={key}>
              <dt>{key}</dt><dd><code>{JSON.stringify(value)}</code></dd>
            </div>)}
          </dl>
        </details>}
        <p className="billing-muted">仅验证请求与返回，不代表档位效果评测。</p>
      </>}
    </div>}
  </section>;
}
