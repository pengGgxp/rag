"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { Check, Mail, MessageCircle, RefreshCw, ShieldCheck, X } from "lucide-react";
import * as Tabs from "../../../components/ui/tabs";
import { request } from "../../api";
import { useAuth } from "../../auth";
import "./site-auth.css";

type SMTP = { enabled: boolean; host: string; port: number; tls_mode: "ssl" | "starttls" | "plain"; username: string; from_address: string; from_name: string; has_password: boolean };
type Wechat = { enabled: boolean; app_id: string; has_app_key: boolean; callback_url: string };
type Policy = { require_verified_email: boolean; hourly_email_limit: number };
type Configuration = { smtp: SMTP; wechat: Wechat; policy: Policy; encryption_ready: boolean };
const json = (body: unknown, method = "PUT") => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export function SiteAuthConsole() {
  const { session } = useAuth();
  const [config, setConfig] = useState<Configuration | null>(null);
  const [password, setPassword] = useState("");
  const [appKey, setAppKey] = useState("");
  const [clearPassword, setClearPassword] = useState(false);
  const [clearAppKey, setClearAppKey] = useState(false);
  const [email, setEmail] = useState(session.user.email);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState("smtp");
  const load = useCallback(async () => {
    setError("");
    try { setConfig(await request<Configuration>("/site/settings/auth")); }
    catch (e) { setError(e instanceof Error ? e.message : "配置加载失败"); }
  }, []);
  useEffect(() => { if (session.user.is_site_admin) void load(); }, [load, session.user.is_site_admin]);
  async function action(run: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await run(); } catch (e) { setError(e instanceof Error ? e.message : "操作失败，请重试"); } finally { setBusy(false); }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!config) return;
    await action(async () => {
      const { has_password: _hasPassword, ...smtp } = config.smtp;
      const { has_app_key: _hasAppKey, callback_url: _callback, ...wechat } = config.wechat;
      // Select known fields so environment fallback metadata is never submitted.
      const body = tab === "smtp" ? { smtp: { enabled: smtp.enabled, host: smtp.host, port: smtp.port, tls_mode: smtp.tls_mode, username: smtp.username, from_address: smtp.from_address, from_name: smtp.from_name, ...(password ? { password } : {}), clear_password: clearPassword } } : tab === "wechat" ? { wechat: { ...wechat, ...(appKey ? { app_key: appKey } : {}), clear_app_key: clearAppKey } } : { policy: config.policy };
      setConfig(await request<Configuration>("/site/settings/auth", json(body)));
      setPassword(""); setAppKey(""); setClearPassword(false); setClearAppKey(false); setNotice("设置已保存");
    });
  }
  if (!session.user.is_site_admin) return <main className="site-auth-denied"><ShieldCheck size={28} /><h1>无权访问站点管理</h1><Link className="secondary" href="/">关闭</Link></main>;
  return <main className="site-auth-console">
    <header className="site-auth-header"><div><span>知库 / 站点管理</span><h1>认证与登录</h1></div><Link href="/" className="icon-button" aria-label="关闭站点管理" title="关闭"><X size={20} /></Link></header>
    {error && <div role="alert" className="form-error">{error}{!config && <button className="text-button" onClick={() => void load()}><RefreshCw size={14} />重试</button>}</div>}
    {notice && <p role="status" className="auth-success">{notice}</p>}
    {!config ? !error && <div className="auth-flow-skeleton" aria-label="加载配置" role="status"><span className="auth-skeleton" /><span className="auth-skeleton" /><span className="auth-skeleton" /></div> : <Tabs.Root value={tab} onValueChange={(value) => { setTab(value); setNotice(""); setError(""); }}>
      <Tabs.List className="site-auth-tabs tab-list" aria-label="认证配置"><Tabs.Trigger value="smtp"><Mail size={16} />邮箱服务</Tabs.Trigger><Tabs.Trigger value="wechat"><MessageCircle size={16} />微信登录</Tabs.Trigger><Tabs.Trigger value="policy"><ShieldCheck size={16} />认证策略</Tabs.Trigger></Tabs.List>
      <form className="auth-form site-auth-form" onSubmit={save}><fieldset disabled={busy}>
        <Tabs.Content value="smtp"><h2>邮箱服务</h2><p className="site-auth-help">用于邮箱验证、验证码登录和找回密码。保存后可发送测试邮件。</p>
          <label className="site-auth-toggle"><input type="checkbox" checked={config.smtp.enabled} onChange={(e) => setConfig({ ...config, smtp: { ...config.smtp, enabled: e.target.checked } })} />启用邮箱服务</label>
          <div className="site-auth-grid"><label>SMTP 主机<input required={config.smtp.enabled} value={config.smtp.host} onChange={(e) => setConfig({ ...config, smtp: { ...config.smtp, host: e.target.value } })} placeholder="smtp.example.com" /></label><label>端口<input type="number" required min={1} max={65535} value={config.smtp.port} onChange={(e) => setConfig({ ...config, smtp: { ...config.smtp, port: Number(e.target.value) } })} /></label></div>
          <label className="account-field">加密方式<select value={config.smtp.tls_mode} onChange={(e) => setConfig({ ...config, smtp: { ...config.smtp, tls_mode: e.target.value as SMTP["tls_mode"] } })}><option value="ssl">SSL / TLS（通常 465）</option><option value="starttls">STARTTLS（通常 587）</option><option value="plain">无加密（仅本地测试）</option></select></label>
          <label>SMTP 账号<input autoComplete="off" value={config.smtp.username} onChange={(e) => setConfig({ ...config, smtp: { ...config.smtp, username: e.target.value } })} /></label>
          <label>SMTP 密码{config.smtp.has_password ? "（已配置，留空保留）" : ""}<input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label><label className="site-auth-toggle"><input type="checkbox" checked={clearPassword} onChange={(e) => setClearPassword(e.target.checked)} />清除已保存的 SMTP 密码</label>
          <div className="site-auth-grid"><label>发件邮箱<input type="email" required={config.smtp.enabled} value={config.smtp.from_address} onChange={(e) => setConfig({ ...config, smtp: { ...config.smtp, from_address: e.target.value } })} /></label><label>发件显示名<input maxLength={100} value={config.smtp.from_name} onChange={(e) => setConfig({ ...config, smtp: { ...config.smtp, from_name: e.target.value } })} /></label></div>
        </Tabs.Content>
        <Tabs.Content value="wechat"><h2>微信登录</h2><p className="site-auth-help">通过森遇授权页扫码。首次登录需验证邮箱后创建账号。</p><label className="site-auth-toggle"><input type="checkbox" checked={config.wechat.enabled} onChange={(e) => setConfig({ ...config, wechat: { ...config.wechat, enabled: e.target.checked } })} />启用微信登录</label><label>森遇应用 ID<input required={config.wechat.enabled} value={config.wechat.app_id} onChange={(e) => setConfig({ ...config, wechat: { ...config.wechat, app_id: e.target.value } })} /></label><label>AppKey{config.wechat.has_app_key ? "（已配置，留空保留）" : ""}<input type="password" autoComplete="new-password" value={appKey} onChange={(e) => setAppKey(e.target.value)} /></label><label className="site-auth-toggle"><input type="checkbox" checked={clearAppKey} onChange={(e) => setClearAppKey(e.target.checked)} />清除已保存的 AppKey</label><label>回调地址格式<input readOnly value={config.wechat.callback_url} /></label><p className="site-auth-help">每次授权生成独立回调地址。森遇侧需允许该站点的回调路径。</p></Tabs.Content>
        <Tabs.Content value="policy"><h2>认证策略</h2><p className="site-auth-help">开启前，请先确认邮箱实际收信及微信授权流程正常。</p><label className="site-auth-toggle"><input type="checkbox" checked={config.policy.require_verified_email} onChange={(e) => setConfig({ ...config, policy: { ...config.policy, require_verified_email: e.target.checked } })} />新旧账号登录时必须验证邮箱</label>{!session.user.email_verified && <p className="site-auth-help">开启前需要验证你的管理员邮箱。<Link href="/auth/verify-email?returnTo=%2Fadmin%2Fauth">验证邮箱</Link></p>}<label>站点每小时发信上限<input type="number" min={1} max={10000} required value={config.policy.hourly_email_limit} onChange={(e) => setConfig({ ...config, policy: { ...config.policy, hourly_email_limit: Number(e.target.value) } })} /></label></Tabs.Content>
        <button className="primary" type="submit" disabled={busy}><Check size={16} />{busy ? "保存中" : "保存设置"}</button>
      </fieldset></form>
      {tab === "smtp" && <form className="auth-form site-auth-test" onSubmit={(e) => { e.preventDefault(); void action(async () => { const result = await request<{ message: string }>("/site/settings/auth/test-email", json({ email }, "POST")); setNotice(result.message); }); }}><h2>测试投递</h2><label>收件邮箱<input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} /></label><button className="secondary" disabled={busy}><Mail size={16} />发送测试邮件</button></form>}
    </Tabs.Root>}
  </main>;
}
