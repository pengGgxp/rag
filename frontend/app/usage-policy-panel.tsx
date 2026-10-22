"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, Save } from "lucide-react";
import { request } from "./api";

type Policy = {
  capability: string;
  unit: string;
  unit_size: number;
  credits_per_unit: number;
  enabled: boolean;
  version: number;
};

const labels: Record<string, string> = {
  llm_input_tokens: "模型输入 Token",
  llm_output_tokens: "模型输出 Token",
  embedding_tokens: "Embedding Token",
  ocr_pages: "OCR 页数",
};

export function UsagePolicyPanel({ onBusyChange }: { onBusyChange: (busy: boolean) => void }) {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPolicies(await request<Policy[]>("/usage/policies"));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载规则失败");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function save(policy: Policy) {
    setSaving(policy.capability);
    onBusyChange(true);
    setError("");
    setNotice("");
    try {
      const updated = await request<Policy>(`/usage/policies/${policy.capability}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(policy),
      });
      setPolicies((items) => items.map((item) => item.capability === updated.capability ? updated : item));
      setNotice("计费规则已保存，新规则只影响后续用量");
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存规则失败");
    } finally {
      setSaving("");
      onBusyChange(false);
    }
  }

  return (
    <section className="billing-panel" aria-label="计费规则">
      <div className="billing-heading">
        <div>
          <span className="credit-label">内部计费规则</span>
          <small>仅管理员可见，用户端不会展示单价或公式</small>
        </div>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {notice && <p className="billing-notice" role="status">{notice}</p>}
      {loading ? <div className="billing-empty"><Loader2 size={18} className="spin" />正在加载</div> : (
        <div className="recharge-form">
          {policies.map((policy) => (
            <div className="recharge-option" key={policy.capability}>
              <span>
                {labels[policy.capability] ?? policy.capability}
                <small>版本 {policy.version}</small>
              </span>
              <input aria-label={`${policy.capability}计量单位`} value={policy.unit}
                onChange={(e) => setPolicies((items) => items.map((item) =>
                  item.capability === policy.capability ? { ...item, unit: e.target.value } : item))}
                style={{ width: 100 }} />
              <input aria-label={`${policy.capability}单位大小`} type="number" min={1}
                value={policy.unit_size} onChange={(e) => setPolicies((items) => items.map((item) =>
                  item.capability === policy.capability ? { ...item, unit_size: Number(e.target.value) } : item))}
                style={{ width: 90 }} />
              <input aria-label={`${policy.capability}积分`} type="number" min={0}
                value={policy.credits_per_unit} onChange={(e) => setPolicies((items) => items.map((item) =>
                  item.capability === policy.capability ? { ...item, credits_per_unit: Number(e.target.value) } : item))}
                style={{ width: 90 }} />
              <button className="icon-button" title="保存规则" aria-label="保存规则"
                disabled={saving === policy.capability} onClick={() => void save(policy)}>
                {saving === policy.capability ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
              </button>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
