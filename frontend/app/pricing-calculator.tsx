"use client";

import { useEffect, useState } from "react";
import { ArrowDownToLine, CircleAlert, Loader2, RefreshCw } from "lucide-react";
import { Dialog } from "../components/ui/dialog";
import { request } from "./api";
import { json, useAdminResource } from "./admin/admin-ui";
import { policyLabels, type Policy } from "./model-types";
import { formatCredits } from "./credit-format";

type Package = { id: string; name: string; amount_fen: number; credits: number; credits_per_yuan: string };
type Context = { ready: boolean; reference_package: Package | null; reason: string | null };
type Quote = {
  unit_size: number; credits_per_unit: number; reference_package: Package;
  target_price_yuan: string; base_price_yuan: string; rounding_extra_yuan: string;
  effective_unit_credits: string; effective_revenue_yuan: string; effective_fee_yuan: string;
  effective_profit_yuan: string; target_met: boolean; loss: boolean; unit_rounding: boolean;
};
const root = "/site/admin/pricing-calculator";
const amountPattern = /^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,8})?$/;
const feePattern = /^(?:0|[1-9][0-9]?)(?:\.[0-9]{1,4})?$/;
const multiplierPattern = /^(?:0\.(?:0[1-9]|[1-9][0-9]?)|[1-9][0-9]?(?:\.[0-9]{1,2})?|100(?:\.0{1,2})?)$/;
function display(value: string) {
  return value.replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1");
}
const yuan = (value: string) => `¥${display(value)}`;

export function PricingCalculator({ policy, busy, apply, close, restoreFocusSelector, initialCost = "" }: {
  policy: Policy; busy: boolean; apply: (patch: Partial<Policy>) => void;
  close: () => void; restoreFocusSelector: string;
  initialCost?: string;
}) {
  const label = policyLabels[policy.capability] ?? policy.capability;
  const unit = policy.unit === "packages" ? "套餐" : policy.unit === "pages" ? "页" : policy.unit === "calls" ? "次" : "Token";
  const context = useAdminResource<Context>(root);
  const [cost, setCost] = useState(initialCost ? Number(initialCost).toFixed(8).replace(/\.?0+$/, "") || "0" : "");
  const [profit, setProfit] = useState("");
  const [overhead, setOverhead] = useState("0");
  const [fee, setFee] = useState("0.1");
  const [units, setUnits] = useState(String(policy.unit_size));
  const [multiplier, setMultiplier] = useState("1.00");
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ key: string; quote?: Quote; error?: string }>({ key: "" });
  const validUnit = /^\d+$/.test(units) && Number(units) >= 1 && Number(units) <= 2147483647
    && (policy.unit !== "calls" || [1, 1000].includes(Number(units)))
    && (policy.unit !== "packages" || Number(units) === 1);
  const valid = [cost, profit, overhead].every(value => amountPattern.test(value))
    && feePattern.test(fee) && validUnit && multiplierPattern.test(multiplier);
  const payload = JSON.stringify({ capability: policy.capability, unit_size: Number(units),
    cost_yuan: cost, profit_yuan: profit, overhead_yuan: overhead, payment_fee_percent: fee, multiplier });
  const key = `${payload}:${attempt}:${context.data?.reference_package?.credits_per_yuan}`;
  const quote = valid && !context.loading && !context.error && context.data?.ready && state.key === key ? state.quote : undefined;
  const error = state.key === key ? state.error : undefined;
  const calculating = valid && !context.loading && !context.error && context.data?.ready && !quote && !error;
  useEffect(() => {
    if (!valid || context.loading || context.error || !context.data?.ready) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void request<Quote>(`${root}/quote`, { ...json(JSON.parse(payload)), signal: controller.signal })
        .then(value => { if (!controller.signal.aborted) setState({ key, quote: value }); })
        .catch(reason => { if (!controller.signal.aborted) setState({ key, error: reason instanceof Error ? reason.message : "计算失败，请重试" }); });
    }, 350);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [payload, valid, context.loading, context.error, context.data?.ready, key]);
  const reference = quote?.reference_package ?? context.data?.reference_package;
  return <Dialog title="按成本计算" onClose={close} busy={busy} wide restoreFocusSelector={restoreFocusSelector}>
    <section className="price-calculator" aria-label={`${label}人民币定价`} onKeyDown={event => {
      if (event.key === "Enter" && event.target instanceof HTMLInputElement) event.preventDefault();
    }}>
      <div className="calculator-object"><strong>{label}</strong><span>每 {validUnit ? Number(units).toLocaleString() : "—"} {unit}</span></div>
      <div className="calculator-fields">
        <label>计价用量<span className="policy-input">{policy.unit === "calls" ? <select aria-label="计价用量" value={units} onChange={e => setUnits(e.target.value)} disabled={busy}><option value="1">1</option><option value="1000">1,000</option></select> : <input aria-label="计价用量" inputMode="numeric" value={units} onChange={e => setUnits(e.target.value)} disabled={busy || policy.unit === "packages"} />}<span>{unit}</span></span></label>
        <label>人民币成本<span className="policy-input"><input aria-label="人民币成本" autoFocus inputMode="decimal" placeholder="0.00" value={cost} onChange={e => setCost(e.target.value)} disabled={busy} /><span>元</span></span></label>
        <label>目标利润<span className="policy-input"><input aria-label="目标利润" inputMode="decimal" placeholder="0.00" value={profit} onChange={e => setProfit(e.target.value)} disabled={busy} /><span>元</span></span></label>
        <label>其他费用分摊<span className="policy-input"><input aria-label="其他费用分摊" inputMode="decimal" value={overhead} onChange={e => setOverhead(e.target.value)} disabled={busy} /><span>元</span></span></label>
        <label>支付费率<span className="policy-input"><input aria-label="支付费率" inputMode="decimal" value={fee} onChange={e => setFee(e.target.value)} disabled={busy} /><span>%</span></span></label>
        {policy.unit !== "packages" && <label>工作区倍率试算<span className="policy-input"><input aria-label="工作区倍率试算" inputMode="decimal" value={multiplier} onChange={e => setMultiplier(e.target.value)} disabled={busy} /><span>×</span></span></label>}
      </div>
      {!valid && cost !== "" && profit !== "" && <p className="calculator-warning" role="alert"><CircleAlert size={16} />金额须非负且最多 8 位小数；费率为 0–99.9999%，用量为正整数，倍率为 0.01–100、最多两位小数。</p>}
      <div className="calculator-reference">
        {context.loading ? <span role="status"><Loader2 size={14} className="spin" />读取充值套餐</span>
          : context.error ? <span role="alert">{context.error}<button type="button" className="secondary" onClick={() => void context.reload()}><RefreshCw size={14} />重试</button></span>
          : reference ? <span>按最优惠套餐「{reference.name}」：¥{(reference.amount_fen / 100).toFixed(2)} / {reference.credits.toLocaleString()} 积分</span>
          : <span role="alert">{context.data?.reason}<button type="button" className="secondary" onClick={() => void context.reload()}><RefreshCw size={14} />重新读取</button></span>}
      </div>
      <div className="calculator-results" aria-live="polite" aria-busy={Boolean(calculating)}>
        {calculating ? <p className="calculator-empty"><Loader2 size={16} className="spin" />正在计算</p>
          : error ? <div className="calculator-warning" role="alert">{error}<button type="button" className="secondary" onClick={() => { void context.reload(); setAttempt(value => value + 1); }}><RefreshCw size={14} />重试</button></div>
          : quote ? <>
            <div className="calculator-total"><span>建议基准价</span><strong>{formatCredits(quote.credits_per_unit)} <small>积分 / {quote.unit_size.toLocaleString()} {unit}</small></strong></div>
            <dl className="calculator-breakdown">
              <div><dt>目标售价</dt><dd>{yuan(quote.target_price_yuan)}</dd></div>
              <div><dt>对应售价</dt><dd>{yuan(quote.base_price_yuan)}</dd></div>
              <div><dt>精度调整差额</dt><dd>{yuan(quote.rounding_extra_yuan)}</dd></div>
              <div><dt>倍率后单位积分</dt><dd>{display(quote.effective_unit_credits)}</dd></div>
              <div><dt>倍率后预计收入</dt><dd>{yuan(quote.effective_revenue_yuan)}</dd></div>
              <div><dt>预计支付手续费</dt><dd>{yuan(quote.effective_fee_yuan)}</dd></div>
              <div className="calculator-profit" data-loss={quote.loss}><dt>倍率后预计利润</dt><dd>{yuan(quote.effective_profit_yuan)}</dd></div>
            </dl>
            {!quote.target_met && <p className="calculator-warning" role="alert"><CircleAlert size={16} />{quote.loss ? "该倍率下预计亏损，请检查基准价或工作区倍率。" : "该倍率下未达到目标利润。"}</p>}
            {quote.unit_size !== policy.unit_size && <p className="calculator-warning"><CircleAlert size={16} />应用将同时把计价用量从 {policy.unit_size.toLocaleString()} 改为 {quote.unit_size.toLocaleString()} {unit}。</p>}
          </> : <p className="calculator-empty">填写成本与目标利润后查看结果</p>}
      </div>
      <details className="calculator-formula"><summary>计算依据</summary><p>售价 =（成本 + 费用分摊 + 目标利润）÷（1 − 支付费率）；积分 = 售价 × 每元兑换积分，保留四位小数。消费按实际用量比例计算。</p><p>收益按上述用量估算，未含未填写的税费或固定成本。倍率仅用于试算，支付费率不修改支付渠道设置。</p></details>
      <div className="calculator-footer"><button type="button" className="secondary" onClick={close}>取消</button><button type="button" className="primary" disabled={busy || !quote} onClick={() => {
        if (quote) { apply({ unit_size: quote.unit_size, credits_per_unit: quote.credits_per_unit }); close(); }
      }}><ArrowDownToLine size={16} />应用到价格</button></div>
    </section>
  </Dialog>;
}
