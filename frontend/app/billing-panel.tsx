"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUpRight, ChevronLeft, ChevronRight, CreditCard, Info, Loader2, RefreshCw } from "lucide-react";
import { request } from "./api";
import "./billing.css";

export type CreditUsage = {
  balance: number;
  pending_credits?: number;
  lifetime_granted: number;
  lifetime_spent: number;
  usage?: {
    events: number;
    units: Record<string, { units: number; credits: number }>;
  };
  entries: {
    id: string;
    amount: number;
    balance_after: number;
    kind: string;
    metadata: Record<string, unknown>;
    created_at: string;
  }[];
};
type Package = { id: string; name: string; amount_fen: number; credits: number; quote: string };
type Catalog = { enabled: boolean; packages: Package[] };
type Order = {
  id: string;
  order_no: string;
  package_name: string;
  amount_fen: number;
  credits: number;
  status: "creating" | "confirming" | "pending" | "expired" | "failed" | "paid";
  pay_url: string | null;
  created_at: string;
  expires_at: string | null;
  paid_at: string | null;
};
type OrderPage = { items: Order[]; total: number; page: number; page_size: number };

const count = (value: number) => value.toLocaleString("zh-CN");
const money = (fen: number) =>
  new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" }).format(fen / 100);
const time = (value: string) =>
  new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
const errorText = (error: unknown) => error instanceof Error ? error.message : "请求失败，请稍后重试";
const pending = (order: Order) => ["pending", "creating", "confirming"].includes(order.status);
const STATUS = {
  creating: "创建中", confirming: "确认中", pending: "待支付",
  expired: "已过期", failed: "创建失败", paid: "已到账",
};
export function creditKind(kind: string) {
  return ({
    paid_recharge: "充值到账", admin_grant: "历史赠送", initial_grant: "初始赠送",
    chat_reservation: "问答消耗", chat_release: "失败返还",
    usage_settlement: "能力使用",
    manual_credit: "人工入账", manual_debit: "人工扣减",
  } as Record<string, string>)[kind] ?? "积分变动";
}
const usageLabel: Record<string, string> = {
  llm_input_tokens: "模型输入量",
  llm_cached_input_tokens: "模型缓存读取量",
  llm_output_tokens: "模型输出量",
  embedding_tokens: "知识库处理量",
  ocr_pages: "文档解析页数",
};

export function BillingPanel({ workspaceName, onBusyChange }: {
  workspaceName: string;
  onBusyChange: (busy: boolean) => void;
}) {
  const [usage, setUsage] = useState<CreditUsage | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [orders, setOrders] = useState<OrderPage | null>(null);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const attempt = useRef<{ packageId: string; key: string; quote: string } | null>(null);
  const previousStates = useRef(new Map<string, string>());
  const generation = useRef(0);

  const load = useCallback(async (quiet = false) => {
    const current = ++generation.current;
    if (!quiet) setLoading(true);
    try {
      const [options, history] = await Promise.all([
        request<Catalog>("/payments/catalog"),
        request<OrderPage>(`/payments/orders?page=${page}&page_size=10`),
      ]);
      const balance = await request<CreditUsage>("/usage");
      if (current !== generation.current) return;
      const newlyPaid = history.items.some((order) =>
        order.status === "paid" && previousStates.current.has(order.id)
        && previousStates.current.get(order.id) !== "paid",
      );
      history.items.forEach((order) => previousStates.current.set(order.id, order.status));
      setUsage(balance);
      setCatalog(options);
      setOrders(history);
      setSelected((value) => options.packages.some((item) => item.id === value) ? value : options.packages[0]?.id ?? "");
      setError("");
      if (newlyPaid) setNotice("充值成功，积分已到账");
    } catch (e) {
      if (current === generation.current) setError(errorText(e));
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }, [page]);

  useEffect(() => {
    void load();
    return () => { generation.current += 1; };
  }, [load]);
  const hasPending = orders?.items.some(pending) ?? false;
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible") void load(true);
    };
    window.addEventListener("focus", refresh);
    const timer = hasPending ? window.setInterval(refresh, 5000) : null;
    return () => {
      window.removeEventListener("focus", refresh);
      if (timer !== null) window.clearInterval(timer);
    };
  }, [load, hasPending]);

  async function checkout() {
    const option = catalog?.packages.find((item) => item.id === selected);
    if (!option || busy) return;
    setBusy(true);
    onBusyChange(true);
    setError("");
    setNotice("");
    if (attempt.current?.packageId !== selected || attempt.current.quote !== option.quote) {
      attempt.current = { packageId: selected, key: crypto.randomUUID(), quote: option.quote };
    }
    try {
      const order = await request<Order>("/payments/orders", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ package_id: selected, idempotency_key: attempt.current.key, quote: option.quote }),
      });
      attempt.current = null;
      previousStates.current.set(order.id, order.status);
      const quoteMatches = order.amount_fen === option.amount_fen && order.credits === option.credits;
      if (order.status === "pending" && order.pay_url && quoteMatches) {
        const back = new URL(window.location.href);
        back.searchParams.set("account", "1");
        back.searchParams.set("billing", "1");
        window.history.replaceState(null, "", back);
        window.location.assign(order.pay_url);
        return;
      }
      setNotice(!quoteMatches ? "充值档位已更新，请核对订单后再继续支付"
        : order.status === "paid" ? "充值成功，积分已到账"
        : order.status === "failed" ? "支付订单创建失败，请稍后重试"
        : "订单正在确认，请勿重复付款。已付款未到账请联系支持。");
      if (page === 1) await load(true);
      else setPage(1);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      onBusyChange(false);
    }
  }

  const selectedPackage = catalog?.packages.find((item) => item.id === selected);
  const totalPages = Math.max(1, Math.ceil((orders?.total ?? 0) / 10));
  return (
    <section className="billing-panel" aria-label="积分与充值">
      <div className="billing-heading">
        <div>
          <span className="credit-label">可用积分</span>
          <strong>{usage ? count(usage.balance) : "--"}</strong>
          <small>{workspaceName}</small>
        </div>
        <button className="icon-button" title="刷新充值记录" aria-label="刷新充值记录"
          disabled={loading || busy} onClick={() => void load()}>
          <RefreshCw size={16} className={loading ? "spin" : ""} />
        </button>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {Boolean(usage?.pending_credits) && <div className="billing-pending" role="status"><Info size={18} aria-hidden="true" /><div><strong>{count(usage!.pending_credits!)} 积分待结算</strong><p>请充值补足，下次使用模型前将优先结清。</p></div></div>}
      {notice && <p className="billing-notice" role="status"><Info size={16} />{notice}</p>}
      {loading && !catalog ? (
        <div className="settings-skeleton" role="status" aria-label="加载中"><span /><span /><span /></div>
      ) : catalog?.enabled ? (
        <form className="recharge-form" onSubmit={(event) => { event.preventDefault(); void checkout(); }}>
          <fieldset className="recharge-options" disabled={busy}>
            <legend>积分充值</legend>
            {catalog.packages.map((item) => (
              <label className="recharge-option" key={item.id} data-selected={selected === item.id}>
                <input type="radio" name="recharge-package" value={item.id}
                  checked={selected === item.id} onChange={() => setSelected(item.id)} />
                <span>{item.name}<strong>{count(item.credits)}<small> 积分</small></strong></span>
                <b>{money(item.amount_fen)}</b>
              </label>
            ))}
          </fieldset>
          <div className="recharge-checkout">
            <span>应付 <strong>{selectedPackage ? money(selectedPackage.amount_fen) : "--"}</strong></span>
            <button className="primary" type="submit" disabled={busy || !selectedPackage}>
              {busy ? <Loader2 size={16} className="spin" /> : <CreditCard size={16} />}
              {busy ? "创建订单" : "前往支付"}
            </button>
          </div>
        </form>
      ) : catalog && <div className="billing-empty">暂未开放充值</div>}
      {usage?.usage && Object.keys(usage.usage.units).length > 0 && (
        <section className="billing-section" aria-label="最近使用量">
          <h3>最近使用量</h3>
          <div className="billing-ledger">
            {Object.entries(usage.usage.units).map(([capability, value]) => (
              <div key={capability}>
                <span>{usageLabel[capability] ?? "其他能力"}<small>累计</small></span>
                <strong>{count(value.units)}</strong>
                <small>使用量</small>
              </div>
            ))}
          </div>
        </section>
      )}
      <section className="billing-section" aria-label="我的充值订单">
        <h3>我的充值订单</h3>
        {!orders?.items.length ? <p className="billing-muted">暂无充值订单</p> : (
          <div className="recharge-order-list">
            {orders.items.map((order) => (
              <article className="recharge-order" key={order.id}>
                <div className="recharge-order-title">
                  <strong>{order.package_name}</strong>
                  <span className="payment-status" data-status={order.status}>{STATUS[order.status]}</span>
                </div>
                <div className="recharge-order-amount">
                  <span>{money(order.amount_fen)}<small> · {count(order.credits)} 积分</small></span>
                  {order.status === "pending" && order.pay_url && (
                    <a href={order.pay_url} target="_blank" rel="noopener noreferrer" className="text-button">
                      继续支付<ArrowUpRight size={14} />
                    </a>
                  )}
                </div>
                <div className="recharge-order-detail">
                  <time>{time(order.created_at)}</time>
                  <code title={order.order_no}>{order.order_no}</code>
                </div>
              </article>
            ))}
          </div>
        )}
        {(orders?.total ?? 0) > 10 && (
          <div className="billing-pagination">
            <span>共 {orders?.total} 条 · {page} / {totalPages}</span>
            <div>
              <button className="icon-button" title="上一页订单" aria-label="上一页订单"
                disabled={loading || page <= 1} onClick={() => setPage(page - 1)}><ChevronLeft size={16} /></button>
              <button className="icon-button" title="下一页订单" aria-label="下一页订单"
                disabled={loading || page >= totalPages} onClick={() => setPage(page + 1)}><ChevronRight size={16} /></button>
            </div>
          </div>
        )}
      </section>
      <section className="billing-section" aria-label="最近积分流水">
        <h3>最近积分流水</h3>
        {!usage?.entries.length ? <p className="billing-muted">暂无积分流水</p> : (
          <div className="billing-ledger">
            {usage.entries.map((entry) => (
              <div key={entry.id}>
                <span>{creditKind(entry.kind)}<small>{time(entry.created_at)}</small></span>
                <strong data-negative={entry.amount < 0}>{entry.amount > 0 ? "+" : ""}{count(entry.amount)}</strong>
                <small>余额 {count(entry.balance_after)}</small>
              </div>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}
