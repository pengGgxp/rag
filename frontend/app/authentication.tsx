"use client";

import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ArrowRight, Loader2, Mail, MessageCircle, ShieldCheck } from "lucide-react";
import * as Tabs from "../components/ui/tabs";
import { request } from "./api";
import type { AuthOptions, Session } from "./auth";
import { AuthFrame } from "./auth-frame";

type Flow = { purpose: "register" | "login" | "verify" | "wechat"; stage: string; email: string; retry_after: number; return_to: string };
type AuthResult = Session | { status: "pending"; flow: Flow };
const json = (body: unknown) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
function safeReturn(value: string | null) {
  return value?.startsWith("/") && !value.startsWith("//") && !value.includes("\\") && !value.startsWith("/auth") && !/[\x00-\x1f]/.test(value) ? value : "/";
}

export function Authentication({ options, session, onSession }: { options: AuthOptions; session: Session | null; onSession: (session: Session | null) => void }) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const step = pathname?.startsWith("/auth/") ? pathname.split("/")[2] || "login" : "login";
  const verifying = step === "verify-email" || step === "complete";
  const [flow, setFlow] = useState<Flow | null>(null);
  const [loading, setLoading] = useState(verifying);
  const [email, setEmail] = useState(session?.user.email ?? "");
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [method, setMethod] = useState(options.email_code ? "code" : "password");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmationToken, setConfirmationToken] = useState("");
  useEffect(() => {
    if (step !== "confirm-email") return;
    const token = new URLSearchParams(window.location.hash.slice(1)).get("token") || "";
    if (token) {
      setConfirmationToken(token);
      window.history.replaceState(window.history.state, "", window.location.pathname);
    }
  }, [step]);
  const [countdown, setCountdown] = useState(0);
  const returnTo = safeReturn(params.get("returnTo"));
  const purpose = verifying ? flow?.purpose || "verify" : step === "register" ? "register" : "login";
  const profile = verifying && flow?.stage === "profile";
  useEffect(() => {
    if (!verifying) { setLoading(false); return; }
    let active = true;
    setLoading(true);
    request<{ flow: Flow | null }>("/auth/flow").then(({ flow: next }) => {
      if (!active) return;
      setFlow(next);
      if (next?.email) setEmail(next.email);
      setCountdown(next?.retry_after ?? 0);
    }).catch((e) => active && setError(e.message)).finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [verifying]);
  useEffect(() => {
    if (!countdown) return;
    const id = setTimeout(() => setCountdown((value) => Math.max(0, value - 1)), 1000);
    return () => clearTimeout(id);
  }, [countdown]);
  useEffect(() => {
    if (params.has("error")) setError("微信授权未完成或已过期，请重新授权。");
  }, [params]);

  function accept(result: AuthResult) {
    if ("status" in result) {
      setFlow(result.flow);
      setCountdown(result.flow.retry_after);
      if (result.flow.email) setEmail(result.flow.email);
      router.replace(`/auth/${result.flow.stage === "profile" ? "complete" : "verify-email"}?returnTo=${encodeURIComponent(returnTo)}`);
    } else {
      onSession(result);
      router.replace(safeReturn(flow?.return_to || returnTo));
    }
  }
  async function action(run: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await run(); } catch (e) { setError(e instanceof Error ? e.message : "操作失败，请重试"); } finally { setBusy(false); }
  }
  async function send() {
    const result = await request<AuthResult>("/auth/email/send-code", json({ email, purpose, return_to: returnTo }));
    accept(result);
      setNotice(purpose === "login" ? "若该邮箱已注册，验证码将发送至收件箱。" : "验证码已提交发送，请检查收件箱及垃圾邮件。");
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    await action(async () => {
      if (step === "forgot") {
        const result = await request<{ message: string }>("/auth/forgot-password", json({ email }));
        setNotice(result.message);
      } else if (profile) {
        accept(await request<AuthResult>("/auth/complete-registration", json({ name, password: password || null })));
      } else if (verifying && flow?.stage === "code") {
        accept(await request<AuthResult>("/auth/email/verify", json({ code })));
        setCode("");
      } else if (verifying || (step === "register" && options.email_code) || (step === "login" && method === "code")) {
        await send();
      } else {
        accept(await request<AuthResult>(step === "register" ? "/auth/register" : "/auth/login", json({ email, password, ...(step === "register" ? { name } : {}) })));
      }
    });
  }
  const title = profile ? "完成账号设置" : verifying ? "验证你的邮箱" : step === "register" ? "创建知库账号" : step === "forgot" ? "找回密码" : "登录知库";
  const passwordInput = !verifying && step !== "forgot" && (method === "password" || (step === "register" && !options.email_code));
  if (step === "confirm-email") return <AuthFrame>
    <div className="auth-heading"><h2>确认邮箱所有权</h2><p>确认后可通过邮箱验证码登录，并重新设置密码。原有设备将退出登录。</p></div>
    {error && <p role="alert" className="form-error">{error}</p>}
    {notice ? <p role="status" className="auth-success">{notice}</p> : <button className="primary auth-submit" disabled={busy || !confirmationToken} onClick={() => void action(async () => {
      const result = await request<{ message: string }>("/auth/email/confirm-link", json({ token: confirmationToken }));
      setConfirmationToken(""); setNotice(result.message);
    })}><Mail size={16} />确认验证邮箱</button>}
    {!confirmationToken && !notice && <p className="form-error">请从验证邮件中重新打开链接。</p>}
    <div className="auth-links"><a href="/auth/login">前往登录</a></div>
  </AuthFrame>;
  return <AuthFrame>
    <div className="auth-heading"><h2>{title}</h2><p>{profile ? "邮箱已验证，完善资料即可开始使用。" : verifying ? "验证邮箱所有权后继续。" : "进入你的知识工作空间。"}</p></div>
    {loading ? <div className="auth-flow-skeleton" aria-label="加载中" role="status"><span className="auth-skeleton" /><span className="auth-skeleton" /><span className="auth-skeleton" /></div> : <>
      {step === "login" && options.email_code && <Tabs.Root value={method} onValueChange={setMethod}><Tabs.List className="auth-mode-tabs" aria-label="登录方式"><Tabs.Trigger value="code">邮箱验证码</Tabs.Trigger><Tabs.Trigger value="password">密码登录</Tabs.Trigger></Tabs.List></Tabs.Root>}
      <form className="auth-form" onSubmit={submit}>
        {!profile && <label>邮箱<input type="email" autoComplete="email" required maxLength={320} value={email} readOnly={verifying && (purpose === "verify" || flow?.stage === "code")} onChange={(e) => setEmail(e.target.value)} /></label>}
        {(profile || (step === "register" && !options.email_code)) && <label>姓名<input required autoComplete="name" maxLength={100} value={name} onChange={(e) => setName(e.target.value)} /></label>}
        {(profile || passwordInput) && <label>{profile && purpose === "wechat" ? "密码（可稍后设置）" : "密码"}<input type="password" required={!(profile && purpose === "wechat")} minLength={step === "register" || profile ? 12 : 1} maxLength={128} autoComplete={profile || step === "register" ? "new-password" : "current-password"} value={password} onChange={(e) => setPassword(e.target.value)} /></label>}
        {verifying && flow?.stage === "code" && <><label>邮箱验证码<input autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]{6}" minLength={6} maxLength={6} required value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} /></label><button className="text-button" type="button" disabled={busy || countdown > 0} onClick={() => void action(send)}>{countdown > 0 ? `${countdown} 秒后重发` : "重新发送验证码"}</button></>}
        {error && <p className="form-error" role="alert">{error}</p>}{notice && <p className="auth-success" role="status">{notice}</p>}
        <button className="primary auth-submit" disabled={busy} type="submit">{busy ? <Loader2 size={16} className="spin" /> : <ArrowRight size={16} />}{profile ? "完成注册" : verifying && flow?.stage === "code" ? "验证并继续" : step === "forgot" ? "发送重置邮件" : verifying || (step === "register" && options.email_code) || (step === "login" && method === "code") ? "发送验证码" : step === "register" ? "创建账号" : "登录"}</button>
      </form>
      {step === "login" && (options.wechat || options.oauth_name) && <div className="auth-divider"><span>或</span></div>}
      {step === "login" && options.wechat && <button className="secondary oauth-button" disabled={busy} onClick={() => void action(async () => { const result = await request<{ url: string }>("/auth/wechat/start", json({ return_to: returnTo })); window.location.assign(result.url); })}><MessageCircle size={16} />微信登录</button>}
      {step === "login" && options.oauth_name && <button className="secondary oauth-button" disabled={busy} onClick={() => void action(async () => { const result = await request<{ url: string }>("/auth/oauth/start", json({})); window.location.assign(result.url); })}><ShieldCheck size={16} />通过{options.oauth_name}登录</button>}
      <div className="auth-links"><Link href={step === "register" || verifying || step === "forgot" ? "/auth/login" : "/auth/register"}>{step === "register" || verifying || step === "forgot" ? "返回登录" : "创建账号"}</Link>{step === "login" && options.password_reset && <Link href="/auth/forgot">忘记密码</Link>}{session && <Link href={returnTo}><Mail size={14} />稍后验证</Link>}</div>
    </>}
  </AuthFrame>;
}
