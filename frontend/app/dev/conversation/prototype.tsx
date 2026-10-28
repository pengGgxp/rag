"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Copy, Info, Layers3, Play, Plus, RefreshCw, Square, ThumbsDown, ThumbsUp } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { WorkspaceShell, WorkspaceNavigation } from "../../workspace-shell";
import { WorkspaceHeader } from "../../workspace-header";
import { ConversationActivity, type ConversationActivityItem } from "../../conversation-activity";
import { ConversationContextControl, type ConversationContextState } from "../../conversation-context-control";
import { RequestModelPicker, type RequestModelChoice } from "../../request-model-picker";
import { Tooltip } from "../../../components/ui/tooltip";
import "../../chat.css";
import "../../conversation-experience.css";
import "./prototype.css";

type Scene = "completed" | "running" | "failed" | "cancelled" | "unconfigured" | "empty";
const activities: ConversationActivityItem[] = [
  { id: "retrieve", label: "检索项目知识库", kind: "retrieval", status: "completed", summary: "找到 6 条相关内容" },
  { id: "tool", label: "读取发布检查清单", kind: "tool", status: "completed", summary: "已读取 1 份检查清单" },
];
const models: RequestModelChoice[] = [
  { id: "deepseek", name: "DeepSeek", model: "deepseek-chat", available: true, thinking: {
    kind: "effort", version: 1, default_option_id: "high",
    options: [{ id: "high", label: "深入", detail: "high" }, { id: "low", label: "快速", detail: "low" }],
  } },
  { id: "qwen", name: "Qwen", model: "qwen-plus", available: true },
];
const answer = "### 先验证关键链路，再发布\n\n根据检索到的发布流程，本次可以按以下顺序执行：\n\n1. **备份与核对**：保存数据库及当前镜像，确认本次只涉及 API 和 Web。\n2. **验证与发布**：完成权限、流式响应和取消操作回归，再更新应用。\n3. **线上复查**：用隔离会话核对工具顺序与实际用量，保留异常时的恢复入口。\n\n数据库、Redis 和存储服务不需要重建。";
const initialContext: ConversationContextState = {
  estimatedInputTokens: 38120, capacity: 128000, outputReserve: 8192, eligible: true, status: "idle",
};

// Development-only fixture. This route never runs model calls and is unavailable in production.
export function ConversationPrototype() {
  const [scene, setScene] = useState<Scene>("completed");
  const [stage, setStage] = useState(4);
  const [playing, setPlaying] = useState(false);
  const [modelId, setModelId] = useState("deepseek");
  const [thinkingId, setThinkingId] = useState("high");
  const [question, setQuestion] = useState("");
  const [context, setContext] = useState(initialContext);
  const [latest, setLatest] = useState(false);
  const [notice, setNotice] = useState("");
  const scroll = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const running = scene === "running";
  const busy = running || context.status === "running";
  useEffect(() => {
    if (!playing || stage >= 4) return;
    const timeout = setTimeout(() => {
      setStage(value => value + 1);
      if (stage === 3) { setScene("completed"); setPlaying(false); }
    }, 1800);
    return () => clearTimeout(timeout);
  }, [playing, stage]);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  useEffect(() => {
    if (!latest && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [stage, latest]);
  const items: ConversationActivityItem[] = scene === "cancelled"
    ? [{ ...activities[0] }, { ...activities[1], status: "cancelled", summary: "已停止，不再启动后续操作" }]
    : scene === "failed"
    ? [{ ...activities[0] }, { ...activities[1], status: "failed", summary: "服务暂时不可用，未自动重试" }]
    : !running ? activities : stage < 2
      ? [{ ...activities[0], status: stage === 0 ? "waiting" : "running", summary: stage === 0 ? "等待模型发起检索" : "正在查找相关内容" }]
      : [activities[0], { ...activities[1], status: stage === 2 ? "running" : "completed", summary: stage === 2 ? "正在读取已授权的检查清单" : activities[1].summary }];
  const compact = () => {
    setContext(value => ({ ...value, status: "running" }));
    timer.current = setTimeout(() => setContext(value => ({ ...value, estimatedInputTokens: 12140, status: "completed", eligible: false })), 1800);
  };
  const replay = () => { setScene("running"); setStage(0); setPlaying(true); setLatest(false); };
  const selectScene = (next: Scene) => {
    if (timer.current) clearTimeout(timer.current);
    setScene(next); setPlaying(false); setStage(next === "running" ? 2 : 4);
    setContext(next === "unconfigured" ? { ...initialContext, capacity: null, outputReserve: null }
      : next === "empty" ? { ...initialContext, estimatedInputTokens: 0, eligible: false } : initialContext);
  };
  return <WorkspaceShell screen="chat">
    <WorkspaceNavigation>
      <button type="button" className="secondary new-chat" onClick={() => selectScene("empty")}><Plus size={16} />新对话</button>
      <div className="history-heading">今天</div>
      <div className="conversation-list"><div className="conversation-item selected">
        <button type="button" onClick={() => selectScene("completed")}><Layers3 size={15} /><span>发布前需要检查什么？</span></button>
      </div></div>
    </WorkspaceNavigation>
    <div className="chat-main conversation-prototype">
      <WorkspaceHeader title={scene === "empty" ? "新对话" : "发布前需要检查什么？"}
        actions={<div className="prototype-controls">
          <label><span>交互样稿</span><select aria-label="样稿状态" value={scene} onChange={event => selectScene(event.target.value as Scene)}>
            <option value="completed">完成</option><option value="running">执行中</option>
            <option value="failed">工具失败</option><option value="cancelled">已停止</option><option value="unconfigured">容量未配置</option><option value="empty">空对话</option>
          </select></label>
          <Tooltip content="重播交互样稿"><button type="button" className="icon-button" aria-label="重播样稿" onClick={replay}><Play size={16} /></button></Tooltip>
        </div>} />
      <section className="chat-messages" ref={scroll} aria-label="对话内容" onScroll={() => {
        const area = scroll.current;
        if (area) setLatest(area.scrollHeight - area.scrollTop - area.clientHeight > 100);
      }}>
        {scene === "empty" ? <div className="answer-empty"><span className="answer-emblem"><Layers3 size={32} strokeWidth={1.4} /></span><h2>今天想了解什么？</h2></div>
          : <div className="chat-transcript">
            <article className="chat-message user"><span className="message-label">你</span><p className="chat-question">帮我查一下，发布前需要检查什么？</p></article>
            <article className="chat-message assistant">
              <span className="message-label"><Layers3 size={16} />知库</span>
              <div className="answer-text"><p>我先查找项目的发布规范，再核对检查清单。</p></div>
              {items[0] && <ConversationActivity key={`first-${scene}`} items={[items[0]]}
                finished={!["waiting", "running"].includes(items[0].status)} />}
              {items[1] && <>
                <div className="answer-text"><p>检索到的规范要求核对发布清单，我再读取清单中的具体项目。</p></div>
                <ConversationActivity key={`second-${scene}`} items={[items[1]]}
                  finished={!["waiting", "running"].includes(items[1].status)} />
              </>}
              {scene === "failed" || scene === "cancelled" ? <div className="chat-message-error" role="alert"><Info size={15} /><span>{scene === "cancelled" ? "已停止，保留已生成内容。" : "检查清单暂时无法读取，已保留当前内容。"}</span>
                <button className="text-button" onClick={replay}><RefreshCw size={14} />重试</button></div>
                : (!running || stage >= 3) && <div className="answer-text"><ReactMarkdown>{running ? answer.slice(0, 94) : answer}</ReactMarkdown></div>}
              {running ? <div className="prototype-live-status" role="status"><span />{stage < 3 ? "正在处理" : "正在生成回答"}</div>
                : <div className="answer-actions">
                  <Tooltip content="复制回答"><button className="icon-button" aria-label="复制回答" onClick={() => {
                    void navigator.clipboard.writeText(answer).then(() => setNotice("已复制回答"));
                  }}><Copy size={15} /></button></Tooltip>
                  <Tooltip content="有帮助"><button className="icon-button" aria-label="有帮助" onClick={() => setNotice("样稿：已记录反馈")}><ThumbsUp size={15} /></button></Tooltip>
                  <Tooltip content="无帮助"><button className="icon-button" aria-label="无帮助" onClick={() => setNotice("样稿：已记录反馈")}><ThumbsDown size={15} /></button></Tooltip>
                  <details className="answer-details prototype-answer-details"><summary>回答详情</summary>
                    <dl className="conversation-detail-metadata"><div><dt>模型</dt><dd>DeepSeek · deepseek-chat</dd></div>
                      <div><dt>请求思考设置</dt><dd>深入 · high</dd></div></dl>
                  </details>
                </div>}
            </article>
            {notice && <p className="prototype-notice" role="status">{notice}</p>}
          </div>}
      </section>
      {latest && <div className="conversation-latest"><button onClick={() => {
        scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: "instant" }); setLatest(false);
      }}><ArrowDown size={14} />回到最新</button></div>}
      <div className="composer-area">
        <form className="composer" onSubmit={event => { event.preventDefault(); if (question.trim()) { setQuestion(""); replay(); } }}>
          <textarea aria-label="向知识库提问" placeholder="继续提问…" rows={2} value={question} disabled={busy} onChange={event => setQuestion(event.target.value)} />
          <div className="composer-footer">
            <RequestModelPicker models={models} modelId={modelId} thinkingId={thinkingId} loading={false} disabled={busy}
              onModelChange={id => { setModelId(id); setThinkingId("high"); }} onThinkingChange={setThinkingId} />
            <ConversationContextControl state={context} disabled={busy} onCompact={compact} />
            {running ? <button className="send-button" type="button" aria-label="停止回答" onClick={() => { setPlaying(false); setScene("cancelled"); }}><Square size={14} /></button>
              : <button className="send-button" type="submit" disabled={busy || !question.trim()} aria-label="发送问题"><ArrowUp size={19} /></button>}
          </div>
        </form>
        <div className="composer-meta"><span>交互样稿 · 不发起真实调用，不产生费用</span></div>
      </div>
    </div>
  </WorkspaceShell>;
}
