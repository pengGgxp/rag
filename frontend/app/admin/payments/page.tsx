"use client";

import { useEffect, useState } from "react";
import { Check, Plus, Save, Trash2 } from "lucide-react";
import { ApiError, request } from "../../api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "../admin-ui";
import { useAdminChanges } from "../admin-changes";
import { Dialog } from "../../../components/ui/dialog";
import "../../billing.css";

type Package = { id: string; name: string; amount_fen: number; credits: number };
type Settings = {
  version: number;
  source: "database" | "environment";
  enabled: boolean;
  provider_key: string;
  api_base_url: string;
  merchant_num: string;
  pay_type: string;
  has_secret: boolean;
  packages: Package[];
  callback_url: string;
  return_url: string;
  encryption_ready: boolean;
};
type Form = Pick<Settings, "enabled" | "api_base_url" | "merchant_num" | "pay_type" | "packages">;

const EMPTY_FORM: Form = {
  enabled: false,
  api_base_url: "",
  merchant_num: "",
  pay_type: "",
  packages: [],
};

function formOf(settings: Settings): Form {
  return {
    enabled: settings.enabled,
    api_base_url: settings.api_base_url,
    merchant_num: settings.merchant_num,
    pay_type: settings.pay_type,
    packages: settings.packages.map((item) => ({ ...item })),
  };
}

function newPackage(): Package {
  return { id: `package_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`, name: "", amount_fen: 0, credits: 0 };
}

export default function PaymentsPage() {
  const resource = useAdminResource<Settings>("/site/admin/payments");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [form, setForm] = useState<Form>(EMPTY_FORM);
  const [original, setOriginal] = useState<Form>(EMPTY_FORM);
  const [secret, setSecret] = useState("");
  const [clearSecret, setClearSecret] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const dirty = JSON.stringify(form) !== JSON.stringify(original) || !!secret || clearSecret;
  useAdminChanges(dirty, busy);

  function accept(value: Settings) {
    const next = formOf(value);
    setSettings(value);
    setForm(next);
    setOriginal(next);
    setSecret("");
    setClearSecret(false);
  }

  useEffect(() => {
    if (resource.data) accept(resource.data);
  }, [resource.data]);

  function updatePackage(index: number, patch: Partial<Package>) {
    setForm((current) => ({
      ...current,
      packages: current.packages.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item),
    }));
  }

  async function save() {
    if (!settings) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const saved = await request<Settings>("/site/admin/payments", json({
        version: settings.version,
        ...form,
        secret: secret || null,
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

  return <>
    <AdminHeading title="支付设置" />
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {settings && <>
        {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
        {error && <div className="admin-error" role="alert">{error}</div>}
        {conflict && <p className="admin-inline-confirm" role="alert">
          <span>设置已被其他管理员更新。重新加载会放弃本页修改。</span>
          <button className="secondary" disabled={busy} onClick={() => { setConflict(false); setError(""); void resource.reload(); }}>放弃修改并重新加载</button>
        </p>}
        {!settings.encryption_ready && <div className="admin-error" role="status">服务端尚未配置凭据加密密钥。完成服务端加密配置后，才能安全保存商户密钥并启用支付。</div>}
        {settings.source === "environment" && <p className="site-setting-note">当前显示的是服务器环境变量中的现有配置草稿。首次保存后，数据库设置将用于新订单；待支付订单仍按创建时的配置处理。</p>}
        <form className="provider-form model-detail-form payment-settings-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <fieldset className="provider-form-fields" disabled={busy}>
            <section className="model-form-section">
              <h2>支付渠道</h2>
              <div className="provider-form-grid">
                <label className="provider-enabled provider-form-wide">
                  <input type="checkbox" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} />
                  启用积分充值
                </label>
                <label>支付网关地址<input required={form.enabled} type="url" inputMode="url" value={form.api_base_url} onChange={(event) => setForm({ ...form, api_base_url: event.target.value })} placeholder="https://..." /></label>
                <label>商户号<input required={form.enabled} autoComplete="off" value={form.merchant_num} onChange={(event) => setForm({ ...form, merchant_num: event.target.value })} /></label>
                <label>支付产品编码<input required={form.enabled} autoComplete="off" value={form.pay_type} onChange={(event) => setForm({ ...form, pay_type: event.target.value })} /></label>
                <label>商户密钥<input type="password" autoComplete="new-password" value={secret} disabled={clearSecret} onChange={(event) => setSecret(event.target.value)} placeholder={settings.has_secret ? "已配置，留空保留" : "填写商户密钥"} /></label>
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
            <section className="model-form-section">
              <div className="payment-packages-heading">
                <div><h2>充值档位</h2><p className="model-budget-description">金额与到账积分共同决定用户的充值报价；修改后只影响新订单和新的报价。</p></div>
                <button type="button" className="secondary" disabled={busy || form.packages.length >= 20} onClick={() => setForm({ ...form, packages: [...form.packages, newPackage()] })}><Plus size={16} />添加档位</button>
              </div>
              {form.packages.length === 0 ? <p className="admin-empty payment-package-empty">尚未配置充值档位</p> : <div className="payment-package-list">
                {form.packages.map((item, index) => <div className="payment-package-row" key={`${index}-${item.id}`}>
                  <label>档位 ID<input required maxLength={64} value={item.id} onChange={(event) => updatePackage(index, { id: event.target.value })} /></label>
                  <label>显示名称<input required maxLength={100} value={item.name} onChange={(event) => updatePackage(index, { name: event.target.value })} placeholder="例如：标准档" /></label>
                  <label>金额（元）<input required type="number" min="0.01" max="10000" step="0.01" value={item.amount_fen ? (item.amount_fen / 100).toFixed(2) : ""} onChange={(event) => updatePackage(index, { amount_fen: Math.round(Number(event.target.value) * 100) || 0 })} /></label>
                  <label>到账积分<input required type="number" min="1" max="1000000" step="1" value={item.credits || ""} onChange={(event) => updatePackage(index, { credits: Number(event.target.value) || 0 })} /></label>
                  <button type="button" className="icon-button payment-package-remove" aria-label={`移除${item.name || "此"}充值档位`} title="移除档位" disabled={busy} onClick={() => setForm({ ...form, packages: form.packages.filter((_, itemIndex) => itemIndex !== index) })}><Trash2 size={17} /></button>
                </div>)}
              </div>}
            </section>
            <div className="payment-footer-note">{!form.enabled ? "充值目前关闭；保存后用户端不会开放新充值。" : !settings.encryption_ready ? "加密配置未就绪，暂不能启用充值。" : "启用前请确认商户参数、密钥和至少一个充值档位完整。"}</div>
            <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : ""}</span>
              <button className="primary" disabled={busy || !dirty || conflict || (!settings.encryption_ready && (settings.has_secret || !!secret))}>{busy ? "保存中…" : <><Save size={16} />保存设置</>}</button>
            </div>
          </fieldset>
        </form>
      </>}
    </AdminLoad>
    {confirmClear && <Dialog title="清除支付密钥" onClose={() => setConfirmClear(false)}>
      <p>保存后会移除当前商户密钥。若充值仍处于启用状态，服务端将拒绝此设置。</p>
      <div className="modal-actions"><button className="secondary" onClick={() => setConfirmClear(false)}>取消</button><button className="primary" onClick={() => { setClearSecret(true); setSecret(""); setConfirmClear(false); }}>确认清除</button></div>
    </Dialog>}
  </>;
}
