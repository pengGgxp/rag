"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { request, ApiError } from "./api";
import { formatCredits } from "./credit-format";
import type { Computer } from "./cloud-computer-state";

type UsagePage = { items: Computer[]; next_offset: number | null };
const PAGE_SIZE = 10;
const time = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
}) : "尚未开始";
const duration = (seconds: number) => `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;

export function CloudComputerUsage() {
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<UsagePage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revoked, setRevoked] = useState(false);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const clear = useCallback(() => {
    generation.current++; controller.current?.abort(); setPage(null); setRevoked(true);
    setLoading(false); setError("用量暂不可查看，请重新登录后重试");
  }, []);
  const load = useCallback(async () => {
    if (revoked) return;
    const current = ++generation.current;
    controller.current?.abort(); controller.current = new AbortController();
    setLoading(true);
    try {
      const result = await request<UsagePage>(`/cloud-computers?offset=${offset}&limit=${PAGE_SIZE}`,
        { signal: controller.current.signal });
      if (current !== generation.current) return;
      setPage(result); setError("");
    } catch (e) {
      if (current !== generation.current) return;
      if (e instanceof ApiError && [401, 403].includes(e.status)) clear();
      else setError("用量更新失败，请重试");
    } finally { if (current === generation.current) setLoading(false); }
  }, [offset, revoked, clear]);
  useEffect(() => {
    void load();
    return () => { generation.current++; controller.current?.abort(); };
  }, [load]);
  useEffect(() => {
    window.addEventListener("rag-session-expired", clear);
    window.addEventListener("rag-session-ended", clear);
    return () => {
      window.removeEventListener("rag-session-expired", clear);
      window.removeEventListener("rag-session-ended", clear);
    };
  }, [clear]);
  const turn = (next: number) => { setPage(null); setOffset(next); };
  return <section className="billing-section cloud-usage" aria-label="我的云电脑用量">
    <div className="cloud-usage-heading"><h3>我的云电脑用量</h3>
      <button type="button" className="icon-button" aria-label="刷新云电脑用量" disabled={loading || revoked}
        onClick={() => void load()}><RefreshCw size={16} className={loading ? "spin" : ""} /></button>
    </div>
    {error && <p className="form-error" role="alert">{error}</p>}
    {loading && !page ? <p className="billing-muted" role="status">正在加载用量…</p>
      : !page?.items.length ? <p className="billing-muted">{revoked ? "暂无可查看的记录" : "暂无云电脑用量"}</p>
      : <div className="cloud-usage-list">{page.items.map(computer => <details className="cloud-usage-record" key={computer.id}>
        <summary>
          <span className="cloud-usage-name"><strong>{/^云电脑 [0-9a-f]{8}$/.test(computer.name) ? "临时任务" : computer.name}</strong>
            <span><time>{time(computer.started_at)}</time><span>{duration(computer.seconds)}</span></span>
          </span>
          <span className="cloud-usage-charge"><strong>{formatCredits(Number(computer.credits))}<small> 积分</small></strong>
            <span>{computer.terminal ? "已结算" : "未结算"}</span></span>
          <ChevronDown size={16} className="cloud-usage-chevron" aria-hidden="true" />
        </summary>
        <dl>
          <div><dt>{computer.terminal ? "最终消费" : "当前累计"}</dt><dd>{formatCredits(Number(computer.credits))} 积分</dd></div>
          <div><dt>本次单价</dt><dd>{formatCredits(Number(computer.credits_per_minute))} 积分/分钟</dd></div>
          <div><dt>运行时长</dt><dd>{duration(computer.seconds)}</dd></div>
          {computer.ended_at && <div><dt>结束时间</dt><dd>{time(computer.ended_at)}</dd></div>}
          {computer.stop_reason_text && <div><dt>结束原因</dt><dd>{computer.stop_reason_text}</dd></div>}
          {computer.stop_reason === "insufficient_balance" && computer.renewal_required_credits != null
            && <div><dt>续期所需预留</dt><dd>{formatCredits(Number(computer.renewal_required_credits))} 积分</dd></div>}
        </dl>
      </details>)}</div>}
    {(offset > 0 || page?.next_offset != null) && <div className="billing-pagination">
      <span>第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span><div>
        <button type="button" className="icon-button" aria-label="上一页云电脑用量" disabled={loading || offset === 0 || revoked}
          onClick={() => turn(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft size={16} /></button>
        <button type="button" className="icon-button" aria-label="下一页云电脑用量" disabled={loading || page?.next_offset == null || revoked}
          onClick={() => turn(page!.next_offset!)}><ChevronRight size={16} /></button>
      </div>
    </div>}
  </section>;
}
