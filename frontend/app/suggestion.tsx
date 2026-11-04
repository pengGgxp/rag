"use client";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Loader2, MessageSquare, Send, X } from "lucide-react";
import { Dialog } from "../components/ui/dialog";
import { request } from "./api";
import { useAuth } from "./auth";
import "./admin/platform-content.css";

const Context = createContext({ open() {} });

export function SuggestionProvider({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  const [open, setOpen] = useState(false);
  const [category, setCategory] = useState("feature");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState(false);
  const [discard, setDiscard] = useState(false);
  const [contact, setContact] = useState("");
  const [contactError, setContactError] = useState(false);
  const [contactReload, setContactReload] = useState(0);
  useEffect(() => {
    if (!open) return;
    let active = true;
    setContact(""); setContactError(false);
    request<{ administrator_contact: string }>("/site/contact")
      .then(value => { if (active) setContact(value.administrator_contact); })
      .catch(() => { if (active) setContactError(true); });
    return () => { active = false; };
  }, [open, session.user.id, session.workspace_id, contactReload]);
  const pending = useRef<{ key: string; content: string; category: string } | null>(null);
  const submitting = useRef(false);
  const scope = useRef(0);
  const close = () => { if (content.trim()) setDiscard(true); else setOpen(false); };
  useEffect(() => { scope.current += 1; setOpen(false); setContent(""); setCategory("feature"); setError(""); setNotice(false); setBusy(false); setDiscard(false); submitting.current = false; pending.current = null; }, [session.user.id, session.workspace_id]);
  return <Context.Provider value={{ open() { setOpen(true); setNotice(false); setDiscard(false); } }}>
    {children}
    {notice && <div className="suggestion-notice" role="status"><Check size={16} />已收到，谢谢你的反馈<button className="icon-button" aria-label="关闭反馈提示" onClick={() => setNotice(false)}><X size={16} /></button></div>}
    {open && <Dialog title="意见反馈" onClose={close} busy={busy} restoreFocusSelector="[data-suggestion-entry]">
      <form className="suggestion-form managed-dialog-body" onSubmit={async event => {
        event.preventDefault(); if (submitting.current || !content.trim()) return;
        submitting.current = true; setBusy(true); setError(""); setDiscard(false);
        const currentScope = scope.current;
        const body = { category, content: content.trim() };
        if (!pending.current || pending.current.content !== body.content || pending.current.category !== category)
          pending.current = { ...body, key: crypto.randomUUID() };
        try {
          await request("/suggestions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, request_id: pending.current.key }) });
          if (currentScope !== scope.current) return;
          setOpen(false); setContent(""); pending.current = null; setNotice(true);
        } catch (reason) { if (currentScope === scope.current) setError(reason instanceof Error ? reason.message : "提交失败，请重试"); }
        finally { if (currentScope === scope.current) { setBusy(false); submitting.current = false; } }
      }}>
        <label>反馈类型<select disabled={busy} value={category} onChange={event => setCategory(event.target.value)}><option value="feature">功能建议</option><option value="problem">使用问题</option><option value="other">其他</option></select></label>
        <label>反馈内容<textarea autoFocus required disabled={busy} rows={6} maxLength={2000} value={content} onChange={event => setContent(event.target.value)} /></label>
        {contact && <section className="suggestion-contact" aria-label="管理员联系方式"><h3>联系管理员</h3><p>{contact}</p></section>}
        {contactError && <div className="suggestion-contact-error"><span>联系方式暂时无法加载</span><button className="text-button" type="button" onClick={() => setContactReload(value => value + 1)}>重试</button></div>}
        {error && <p className="form-error" role="alert">{error}</p>}
        {discard && <div className="admin-inline-confirm" role="alert"><span>放弃尚未提交的内容？</span><button className="secondary" type="button" onClick={() => setDiscard(false)}>继续填写</button><button className="danger" type="button" onClick={() => { setContent(""); setOpen(false); setDiscard(false); pending.current = null; }}>放弃反馈</button></div>}
        <div className="managed-actions"><button className="secondary" type="button" disabled={busy} onClick={close}>取消</button><button className="primary" disabled={busy || !content.trim()} type="submit">{busy ? <Loader2 size={16} className="spin" /> : <Send size={16} />}提交反馈</button></div>
      </form>
    </Dialog>}
  </Context.Provider>;
}

export function SuggestionEntry({ onOpen }: { onOpen: () => void }) {
  const context = useContext(Context);
  return <button className="navigation-item" data-suggestion-entry type="button" onClick={() => { onOpen(); context.open(); }}><MessageSquare size={18} />意见反馈</button>;
}
