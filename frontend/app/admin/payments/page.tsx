"use client";

import { useCallback, useEffect, useState } from "react";
import { RechargeSettings } from "./recharge-settings";
import { Check, ExternalLink, Save } from "lucide-react";
import { ApiError, request } from "../../api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "../admin-ui";
import { useAdminChanges } from "../admin-changes";
import { Dialog } from "../../../components/ui/dialog";
import * as Tabs from "../../../components/ui/tabs";
import "../../billing.css";

type Package = { id: string; name: string; amount_fen: number; credits: number };
type PaymentMethod = { code: string; label: string };
type Settings = {
  version: number;
  source: "database" | "environment";
  enabled: boolean;
  provider_key: string;
  provider_name: string;
  website_url: string;
  api_base_url: string;
  merchant_num: string;
  pay_type: string;
  payment_methods: PaymentMethod[];
  custom_credits_per_yuan: number;
  has_secret: boolean;
  packages: Package[];
  custom_recharge_enabled: boolean;
  callback_url: string;
  return_url: string;
  encryption_ready: boolean;
};
type Form = Pick<Settings, "enabled" | "api_base_url" | "merchant_num" | "pay_type" | "payment_methods" | "custom_credits_per_yuan" | "packages" | "custom_recharge_enabled">;

const EMPTY_FORM: Form = {
  enabled: false,
  api_base_url: "",
  merchant_num: "",
  pay_type: "",
  payment_methods: [],
  custom_credits_per_yuan: 100,
  packages: [],
  custom_recharge_enabled: false,
};

function formOf(settings: Settings): Form {
  return {
    enabled: settings.enabled,
    api_base_url: settings.api_base_url,
    merchant_num: settings.merchant_num,
    pay_type: settings.pay_type,
    payment_methods: settings.payment_methods ?? [{ code: settings.pay_type, label: settings.pay_type }],
    custom_credits_per_yuan: settings.custom_credits_per_yuan ?? 100,
    packages: settings.packages.map((item) => ({ ...item })),
    custom_recharge_enabled: settings.custom_recharge_enabled,
  };
}

export default function PaymentsPage() {
  const resource = useAdminResource<Settings>("/site/admin/payments");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [providerKey, setProviderKey] = useState("recharge");
  const [rechargeState, setRechargeState] = useState({ dirty: false, busy: false });
  const onRechargeState = useCallback((dirty: boolean, busy: boolean) => setRechargeState({ dirty, busy }), []);
  const [pendingProvider, setPendingProvider] = useState("");
  const [form, setForm] = useState<Form>(EMPTY_FORM);
  const [original, setOriginal] = useState<Form>(EMPTY_FORM);
  const [secret, setSecret] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const [publicKey, setPublicKey] = useState("");
  const [clearSecret, setClearSecret] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const dirty = JSON.stringify(form) !== JSON.stringify(original) || !!secret || !!privateKey || !!publicKey || clearSecret;
  useAdminChanges(dirty || rechargeState.dirty, busy || rechargeState.busy);

  function accept(value: Settings) {
    const next = formOf(value);
    setSettings(value);
    setForm(next);
    setOriginal(next);
    setSecret("");
    setPrivateKey("");
    setPublicKey("");
    setClearSecret(false);
  }

  useEffect(() => {
    if (resource.data) accept(resource.data);
  }, [resource.data]);

  async function save() {
    if (!settings) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const endpoint = providerKey === "zhifux" ? "/site/admin/payments" : `/site/admin/payments/${providerKey}`;
      const saved = await request<Settings>(endpoint, json({
        version: settings.version,
        provider_key: providerKey,
        ...form,
        secret: secret || null,
        private_key: privateKey || null,
        public_key: publicKey || null,
        clear_secret: clearSecret,
      }, "PUT"));
      accept(saved);
      setConflict(false);
      setNotice("支付设置已保存");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败，请重试");
      setConflict(reason instanceof ApiError && reason.status === 409);
    } finally {
      setBusy(false);
    }
  }

  async function selectProvider(key: string) {
    if (key === providerKey || busy) return;
    if (key === "recharge") { if (settings) accept(settings); setProviderKey(key); return; }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const value = await request<Settings>(`/site/admin/payments/${key}`);
      setProviderKey(key);
      accept(value);
      setConflict(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取渠道配置失败");
    } finally {
      setBusy(false);
    }
  }

  async function reloadCurrent() {
    const endpoint = providerKey === "zhifux" ? "/site/admin/payments" : `/site/admin/payments/${providerKey}`;
    const value = await request<Settings>(endpoint);
    accept(value);
  }

  return <>
    <AdminHeading title="支付设置" />
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {settings && <>
        <Tabs.Root value={providerKey} activationMode="manual" onValueChange={(key) => {
          if (dirty || rechargeState.dirty) setPendingProvider(key); else void selectProvider(key);
        }}>
        <Tabs.List className="payment-provider-tabs" aria-label="支付设置分组">
          <Tabs.Trigger value="recharge" disabled={busy || rechargeState.busy}>充值与付款方式</Tabs.Trigger>
          <Tabs.Trigger value="zhifux" disabled={busy || rechargeState.busy}>支付 FM</Tabs.Trigger>
          <Tabs.Trigger value="ezfp" disabled={busy || rechargeState.busy}>易支付</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="recharge"><RechargeSettings onStateChange={onRechargeState} /></Tabs.Content>
        {providerKey !== "recharge" && <Tabs.Content value={providerKey}>
        {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
        {error && <div className="admin-error" role="alert">{error}</div>}
        {conflict && <p className="admin-inline-confirm" role="alert">
          <span>设置已被其他管理员更新。重新加载会放弃本页修改。</span>
          <button className="secondary" disabled={busy} onClick={() => { setConflict(false); setError(""); void reloadCurrent().catch((reason) => setError(reason instanceof Error ? reason.message : "重新加载失败")); }}>放弃修改并重新加载</button>
        </p>}
        {!settings.encryption_ready && <div className="admin-error" role="status">服务端尚未配置凭据加密密钥。完成服务端加密配置后，才能安全保存商户密钥并启用支付。</div>}
        {settings.source === "environment" && <p className="site-setting-note">当前显示的是服务器环境变量中的现有配置草稿。首次保存后，数据库设置将用于新订单；待支付订单仍按创建时的配置处理。</p>}
        <form className="provider-form model-detail-form payment-settings-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <fieldset className="provider-form-fields" disabled={busy}>
            <section className="model-form-section">
              <h2>{settings.provider_name}</h2>
              <a className="payment-provider-link" href={settings.website_url} target="_blank" rel="noreferrer">访问官网<ExternalLink size={14} /></a>
              <div className="provider-form-grid">
                <label className="provider-enabled provider-form-wide">
                  <input type="checkbox" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} />
                  启用此支付渠道
                </label>
                <label>支付网关地址<input required={form.enabled} type="url" inputMode="url" readOnly={providerKey === "ezfp"} value={form.api_base_url} onChange={(event) => setForm({ ...form, api_base_url: event.target.value })} placeholder="https://..." /></label>
                <label>商户号<input required={form.enabled} autoComplete="off" value={form.merchant_num} onChange={(event) => setForm({ ...form, merchant_num: event.target.value })} /></label>
                {providerKey === "ezfp" ? <>
                  <label className="provider-form-wide">商户 RSA 私钥<textarea className="payment-key-input" autoComplete="new-password" value={privateKey} disabled={clearSecret} onChange={(event) => setPrivateKey(event.target.value)} placeholder={settings.has_secret ? "已配置，留空保留" : "粘贴商户后台生成的 RSA 私钥"} /></label>
                  <label className="provider-form-wide">平台 RSA 公钥<textarea className="payment-key-input" autoComplete="off" value={publicKey} disabled={clearSecret} onChange={(event) => setPublicKey(event.target.value)} placeholder={settings.has_secret ? "已配置，留空保留" : "粘贴易支付平台公钥"} /></label>
                </> : <label>商户密钥<input type="password" autoComplete="new-password" value={secret} disabled={clearSecret} onChange={(event) => setSecret(event.target.value)} placeholder={settings.has_secret ? "已配置，留空保留" : "填写商户密钥"} /></label>}
                {settings.has_secret && <div className="payment-secret-action">
                  <span>{clearSecret ? "保存后清除密钥" : "商户密钥已加密保存"}</span>
                  <button type="button" className="secondary" disabled={busy} onClick={() => clearSecret ? setClearSecret(false) : setConfirmClear(true)}>{clearSecret ? "撤销清除" : "清除密钥"}</button>
                </div>}
              </div>
              <dl className="payment-endpoints">
                <div><dt>支付回调地址</dt><dd><code>{settings.callback_url || "尚未配置"}</code></dd></div>
                <div><dt>支付完成返回地址</dt><dd><code>{settings.return_url || "尚未配置"}</code></dd></div>
              </dl>
            </section>
            <div className="payment-footer-note">{!form.enabled ? "此渠道已关闭。" : !settings.encryption_ready ? "加密配置未就绪，暂不能启用。" : "付款方式及充值报价在“充值与付款方式”中统一设置。"}</div>
            <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : ""}</span>
              <button className="primary" disabled={busy || !dirty || conflict || (!settings.encryption_ready && (settings.has_secret || !!secret || !!privateKey || !!publicKey))}>{busy ? "保存中…" : <><Save size={16} />保存设置</>}</button>
            </div>
          </fieldset>
        </form>
        </Tabs.Content>}
        </Tabs.Root>
      </>}
    </AdminLoad>
    {pendingProvider && <Dialog title="放弃未保存的修改？" onClose={() => setPendingProvider("")}>
      <p>切换页签会放弃当前修改。</p>
      <div className="modal-actions"><button className="secondary" onClick={() => setPendingProvider("")}>继续编辑</button><button className="danger" onClick={() => { const key = pendingProvider; setPendingProvider(""); void selectProvider(key); }}>放弃修改并切换</button></div>
    </Dialog>}
    {confirmClear && <Dialog title="清除支付密钥" onClose={() => setConfirmClear(false)}>
      <p>保存后会移除当前渠道的密钥。若充值仍处于启用状态，服务端将拒绝此设置。</p>
      <div className="modal-actions"><button className="secondary" onClick={() => setConfirmClear(false)}>取消</button><button className="primary" onClick={() => { setClearSecret(true); setSecret(""); setPrivateKey(""); setPublicKey(""); setConfirmClear(false); }}>确认清除</button></div>
    </Dialog>}
  </>;
}
