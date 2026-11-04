"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowUpRight, CreditCard, Loader2, RefreshCw } from "lucide-react";
import { ApiError, request } from "../../api";
import { useAuth } from "../../auth";
import { formatCredits } from "../../credit-format";
import { WorkspaceHeader } from "../../workspace-header";
import { WorkspaceShell } from "../../workspace-shell";
import "../../billing.css";

type Preview = { id: string; name: string; amount_fen: number; credits: number; quote: string;
  payment_methods: { id: string; label: string }[] };
type Order = { id: string; amount_fen: number; credits: number; package_name: string;
  payment_method_label: string | null; status: string; pay_url: string | null };
const money = (fen: number) => new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" }).format(fen / 100);
const errorText = (reason: unknown) => reason instanceof TypeError ? "连接中断，请检查支付状态后再继续"
  : reason instanceof Error ? reason.message : "请求未完成，请稍后重试";
const statuses: Record<string, string> = { creating: "正在创建支付订单", confirming: "正在确认支付结果，请勿重复付款",
  pending: "等待支付", paid: "充值成功，积分已到账", failed: "支付订单创建失败，请返回重新选择",
  expired: "订单已过期，请返回重新选择" };

export default function CheckoutPage() {
  const query = useSearchParams();
  const router = useRouter();
  const { session } = useAuth();
  const packageId = query.get("package") ?? "";
  const quote = query.get("quote") ?? "";
  const amount = query.get("amount");
  const orderId = query.get("order");
  const scope = `${session.user.id}:${session.workspace_id}`;
  const [preview, setPreview] = useState<Preview | null>(null);
  const [order, setOrder] = useState<Order | null>(null);
  const [method, setMethod] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const attempt = useRef<{ key: string; method: string } | null>(null);
  const activeScope = useRef(scope);
  activeScope.current = scope;
  useEffect(() => {
    activeScope.current = scope;
    return () => { activeScope.current = ""; };
  }, [scope]);
  const selection = useCallback(() => ({ package_id: packageId, quote,
    ...(amount !== null ? { custom_amount_fen: /^\d{1,5}$/.test(amount) ? Number(amount) * 100 : -1 } : {}) }), [packageId, quote, amount]);
  const back = new URLSearchParams();
  if (packageId) back.set("package", packageId);
  if (amount !== null) back.set("amount", amount);
  const backHref = `/settings/billing${back.size ? `?${back}` : ""}`;

  const acceptOrder = useCallback((value: Order) => {
    setOrder(value);
    setUncertain(false);
    router.replace(`/payments/checkout?order=${encodeURIComponent(value.id)}`, { scroll: false });
  }, [router]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true); setBusy(false); setError(""); setOrder(null); setPreview(null); setStale(false); setUncertain(false);
    attempt.current = null;
    async function load() {
      try {
        if (orderId) {
          const value = await request<Order>(`/payments/orders/${encodeURIComponent(orderId)}`);
          if (!cancelled) setOrder(value);
          return;
        }
        const params = new URLSearchParams(window.location.search);
        const key = params.get("attempt");
        if (key) {
          attempt.current = { key, method: params.get("method") ?? "" };
          const existing = await request<Order | null>(`/payments/order-attempts/${encodeURIComponent(key)}`);
          if (cancelled) return;
          if (existing) { acceptOrder(existing); return; }
        }
        const value = await request<Preview>("/payments/checkout-preview", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(selection()) });
        if (cancelled) return;
        setPreview(value);
        setMethod(attempt.current?.method ?? (value.payment_methods.length === 1 ? value.payment_methods[0].id : ""));
      } catch (reason) {
        if (!cancelled) {
          setError(errorText(reason));
          setStale(reason instanceof ApiError && reason.status === 409);
          setUncertain(Boolean(attempt.current) && !(reason instanceof ApiError));
        }
      } finally { if (!cancelled) setLoading(false); }
    }
    void load();
    return () => { cancelled = true; };
  }, [orderId, selection, scope, acceptOrder]);

  const refreshOrder = useCallback(async () => {
    const startingScope = activeScope.current;
    try {
      const value = order ? await request<Order>(`/payments/orders/${order.id}`)
        : attempt.current ? await request<Order | null>(`/payments/order-attempts/${encodeURIComponent(attempt.current.key)}`) : null;
      if (activeScope.current !== startingScope) return;
      if (value) acceptOrder(value);
      else {
        setUncertain(false);
        if (!preview) window.location.reload();
      }
      setError("");
    } catch (reason) { if (activeScope.current === startingScope) setError(errorText(reason)); }
  }, [order, preview, acceptOrder]);

  useEffect(() => {
    if (!order || !["creating", "confirming", "pending"].includes(order.status)) return;
    const refresh = () => { if (document.visibilityState === "visible") void refreshOrder(); };
    const timer = window.setInterval(refresh, 5000);
    window.addEventListener("focus", refresh);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", refresh); };
  }, [order, refreshOrder]);

  async function pay() {
    if (!preview || !method || busy || stale || uncertain) return;
    const startingScope = scope;
    setBusy(true); setError("");
    // Persist the attempt before sending so refresh/response loss can recover the same order.
    if (!attempt.current || attempt.current.method !== method) attempt.current = { key: crypto.randomUUID(), method };
    const params = new URLSearchParams(window.location.search);
    params.set("attempt", attempt.current.key); params.set("method", method);
    window.history.replaceState(null, "", `/payments/checkout?${params}`);
    try {
      const value = await request<Order>("/payments/orders", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...selection(), payment_method_id: method, idempotency_key: attempt.current.key }) });
      if (activeScope.current !== startingScope) return;
      acceptOrder(value);
      if (value.status === "pending" && value.pay_url) window.location.assign(value.pay_url);
    } catch (reason) {
      if (activeScope.current !== startingScope) return;
      setError(errorText(reason));
      setStale(reason instanceof ApiError && reason.status === 409);
      setUncertain(!(reason instanceof ApiError && reason.status < 500));
    } finally { if (activeScope.current === startingScope) setBusy(false); }
  }

  return <WorkspaceShell screen="billing">
    <WorkspaceHeader title="充值收银台" />
    <div className="checkout-scroll"><section className="checkout-content" aria-label="充值收银台">
      <Link className="checkout-back" href={backHref}><ArrowLeft size={16} />{order ? "返回积分与充值" : "修改充值金额"}</Link>
      {loading ? <p role="status"><Loader2 size={16} className="spin" />正在核对充值信息</p> : <>
        {error && <p className="form-error" role="alert">{error}</p>}
        {(preview || order) && <dl className="checkout-summary">
          <div><dt>应付金额</dt><dd className="checkout-amount">{money((order ?? preview)!.amount_fen)}</dd></div>
          <div><dt>到账积分</dt><dd>{formatCredits((order ?? preview)!.credits)} 积分</dd></div>
          {order?.payment_method_label && <div><dt>付款方式</dt><dd>{order.payment_method_label}</dd></div>}
        </dl>}
        {order ? <div className="checkout-result" role="status">
          <p>{statuses[order.status] ?? "请刷新查询支付结果"}</p>
          <div className="checkout-actions">
            {order.status === "pending" && order.pay_url && <a href={order.pay_url} className="primary checkout-link">继续支付<ArrowUpRight size={16} /></a>}
            {["creating", "confirming", "pending"].includes(order.status) && <button className="secondary" onClick={() => void refreshOrder()}><RefreshCw size={16} />查询支付结果</button>}
          </div>
        </div> : preview && <form onSubmit={event => { event.preventDefault(); void pay(); }}>
          <fieldset className="payment-provider-options checkout-methods" disabled={busy || uncertain || stale}>
            <legend>付款方式</legend>
            {preview.payment_methods.map(item => <label key={item.id} data-selected={method === item.id}>
              <input type="radio" name="cashier-method" value={item.id} checked={method === item.id} onChange={() => setMethod(item.id)} />
              <span>{item.label}</span>
            </label>)}
          </fieldset>
          <button className="primary checkout-pay" disabled={busy || uncertain || stale || !preview.payment_methods.some(m => m.id === method)}>
            {busy ? <Loader2 size={16} className="spin" /> : <CreditCard size={16} />}{busy ? "正在创建订单" : "立即支付"}
          </button>
        </form>}
        {uncertain && <button className="secondary" onClick={() => void refreshOrder()}><RefreshCw size={16} />检查支付状态</button>}
        {stale && <p><Link className="checkout-back" href={backHref}>返回核对最新报价及付款方式</Link></p>}
      </>}
    </section></div>
  </WorkspaceShell>;
}
