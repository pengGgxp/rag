"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { ArrowLeft, LogOut, Mail, ShieldBan, ShieldCheck } from "lucide-react";
import { Dialog } from "../../../../components/ui/dialog";
import { ApiError, request } from "../../../api";
import { useAuth } from "../../../auth";
import { StoragePanel } from "../../../storage-panel";
import type { AdminUser } from "../page";
import { useAdminChanges } from "../../admin-changes";
import { UserCredits } from "../../user-credits";
import {
  AdminHeading,
  AdminLoad,
  date,
  json,
  useAdminResource,
} from "../../admin-ui";
type ModelProvider = { id: string; name: string; enabled: boolean; supports_chat?: boolean };
export default function UserDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { session } = useAuth();
  const {
    data: user,
    error,
    loading,
    reload,
  } = useAdminResource<AdminUser>(`/site/admin/users/${id}`);
  const models = useAdminResource<ModelProvider[]>("/site/admin/llm-providers");
  const [operation, setOperation] = useState<
    "status" | "sessions" | "email" | null
  >(null);
  const [reason, setReason] = useState("support_review");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [accessBusy, setAccessBusy] = useState(false);
  const [accessError, setAccessError] = useState("");
  const [accessConflict, setAccessConflict] = useState(false);
  const [policyBusy, setPolicyBusy] = useState(false);
  const [policyError, setPolicyError] = useState("");
  const [policyModels, setPolicyModels] = useState<string[]>([]);
  const [policyDefault, setPolicyDefault] = useState("");
  const [policyMultiplier, setPolicyMultiplier] = useState("100");
  const [storageBusy, setStorageBusy] = useState(false);
  useAdminChanges(false, storageBusy || policyBusy || accessBusy || busy);
  useEffect(() => {
    if (!user) return;
    setPolicyModels(user.model_policy.available_models);
    setPolicyDefault(user.model_policy.default_model || "");
    setPolicyMultiplier(String(user.model_policy.billing_multiplier_percent));
  }, [user?.id, user?.model_policy.version]);
  async function savePolicy() {
    if (!user) return;
    setPolicyBusy(true); setPolicyError(""); setNotice("");
    try {
      await request(`/site/admin/users/${id}/model-policy`, json({
        available_models: policyModels,
        default_model: policyDefault || null,
        billing_multiplier_percent: Number(policyMultiplier),
        version: user.model_policy.version,
      }, "PUT"));
      setNotice("账号模型策略已更新");
      await reload();
    } catch (error) {
      setPolicyError(error instanceof Error ? error.message : "保存失败，请重试");
    } finally { setPolicyBusy(false); }
  }
  async function changeWorkbench(enabled: boolean) {
    if (!user) return;
    setAccessBusy(true); setAccessError(""); setNotice("");
    try {
      await request(`/site/admin/users/${id}/question-workbench`, json({ enabled, version: user.question_workbench_version }, "PUT"));
      setNotice(enabled ? "已开放题目工作台" : "已关闭题目工作台，已有题目保留");
      await reload();
    } catch (error) {
      setAccessError(error instanceof Error ? error.message : "保存失败，请重试");
      if (error instanceof ApiError && error.status === 409) setAccessConflict(true);
    } finally { setAccessBusy(false); }
  }
  async function confirm() {
    if (!user || !operation) return;
    setBusy(true);
    setActionError("");
    setNotice("");
    try {
      if (operation === "status") {
        await request(
          `/site/admin/users/${id}/status`,
          json(
            {
              suspended: !user.is_suspended,
              reason_code: user.is_suspended ? "resolved" : reason,
            },
            "PUT",
          ),
        );
        setNotice(
          user.is_suspended
            ? "账号已恢复，可重新登录"
            : "账号已停用，所有会话已撤销",
        );
      } else if (operation === "sessions") {
        const result = await request<{ revoked: number }>(
          `/site/admin/users/${id}/revoke-sessions`,
          { method: "POST" },
        );
        setNotice(`已撤销 ${result.revoked} 个会话`);
      } else {
        const result = await request<{ message: string }>(
          `/site/admin/users/${id}/resend-verification`,
          { method: "POST" },
        );
        setNotice(result.message);
      }
      setOperation(null);
      await reload();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Link className="admin-back" href="/admin/users">
        <ArrowLeft size={15} />
        用户
      </Link>
      <AdminHeading title={user?.name || "用户详情"} />
      {notice && (
        <p className="auth-success" role="status">
          {notice}
        </p>
      )}
      <AdminLoad loading={loading} error={error} retry={reload}>
        {user && (
          <>
            <dl className="admin-details">
              <div>
                <dt>邮箱</dt>
                <dd>
                  {user.email} · {user.email_verified ? "已验证" : "未验证"}
                </dd>
              </div>
              <div>
                <dt>账号状态</dt>
                <dd>
                  {user.is_suspended ? "已停用" : "正常"}
                  {user.is_site_admin ? " · 站点管理员" : ""}
                </dd>
              </div>
              <div>
                <dt>用户 ID</dt>
                <dd>{user.id}</dd>
              </div>
              <div>
                <dt>注册时间</dt>
                <dd>{date(user.created_at)}</dd>
              </div>
              <div>
                <dt>登录方式</dt>
                <dd>
                  {[
                    user.login_methods.password && "密码",
                    user.login_methods.wechat && "微信已绑定",
                    user.login_methods.other_oauth && "第三方账号已关联",
                    user.email_verified && "邮箱已验证",
                  ]
                    .filter(Boolean)
                    .join("、") || "待验证邮箱"}
                </dd>
              </div>
              <div>
                <dt>有效会话</dt>
                <dd>{user.active_sessions}</dd>
              </div>
            </dl>
            <section className="admin-section">
              <h2>存储与下载</h2>
              <StoragePanel userId={user.id} onBusyChange={setStorageBusy} />
            </section>
            <section className="admin-section admin-user-capability">
              <h2>功能权限</h2>
              <label className="provider-enabled">
                <input type="checkbox" checked={user.question_workbench_enabled}
                  disabled={accessBusy || accessConflict} onChange={event => void changeWorkbench(event.target.checked)} />
                题目工作台
              </label>
              <p>允许该用户访问题目工作台，并在对话中选择出题与组卷工具。</p>
              {accessError && <p className="form-error" role="alert">{accessError}</p>}
              {accessConflict && <button className="secondary" onClick={() => {
                setAccessConflict(false); setAccessError(""); void reload();
              }}>重新读取</button>}
            </section>
            <section className="admin-section">
              <h2>账号操作</h2>
              <div className="admin-actions">
                <button
                  id="admin-user-status"
                  className="secondary"
                  disabled={user.id === session.user.id}
                  onClick={() => {
                    setActionError("");
                    setOperation("status");
                  }}
                >
                  {user.is_suspended ? (
                    <ShieldCheck size={16} />
                  ) : (
                    <ShieldBan size={16} />
                  )}
                  {user.is_suspended ? "恢复账号" : "停用账号"}
                </button>
                <button
                  id="admin-user-sessions"
                  className="secondary"
                  onClick={() => {
                    setActionError("");
                    setOperation("sessions");
                  }}
                >
                  <LogOut size={16} />
                  撤销所有会话
                </button>
                <button
                  id="admin-user-email"
                  className="secondary"
                  disabled={user.email_verified || user.is_suspended}
                  onClick={() => {
                    setActionError("");
                    setOperation("email");
                  }}
                >
                  <Mail size={16} />
                  重发验证邮件
                </button>
              </div>
            </section>
            <section className="admin-section">
              <h2>账号额度与模型策略</h2>
              <dl className="admin-details">
                <div><dt>当前余额</dt><dd>{user.credits.balance}</dd></div>
                <div><dt>累计充值</dt><dd>{user.credits.lifetime_granted}</dd></div>
                <div><dt>累计消费</dt><dd>{user.credits.lifetime_spent}</dd></div>
                <div><dt>倍率</dt><dd>{(user.model_policy.billing_multiplier_percent / 100).toFixed(2)}x</dd></div>
                <div><dt>默认模型</dt><dd>{models.data?.find(model => model.id === user.model_policy.default_model)?.name || "未设置"}</dd></div>
                <div><dt>可用模型</dt><dd>{models.data?.filter(model => user.model_policy.available_models.includes(model.id))
                  .map(model => model.name).join("、") || "未设置"}</dd></div>
              </dl>
              <UserCredits id={id} name={user.name} changed={message => { setNotice(message); void reload(); }} />
              <div className="admin-form-grid">
                <label>
                  可用模型
                  <select multiple value={policyModels} disabled={policyBusy || models.loading}
                    onChange={event => setPolicyModels(Array.from(event.target.selectedOptions, option => option.value))}>
                    {models.data?.filter(model => model.enabled && model.supports_chat !== false).map(model =>
                      <option key={model.id} value={model.id}>{model.name}</option>)}
                  </select>
                </label>
                <label>
                  默认模型
                  <select value={policyDefault} disabled={policyBusy} onChange={event => setPolicyDefault(event.target.value)}>
                    <option value="">未设置</option>
                    {models.data?.filter(model => policyModels.includes(model.id)).map(model =>
                      <option key={model.id} value={model.id}>{model.name}</option>)}
                  </select>
                </label>
                <label>
                  消费倍率（百分比）
                  <input type="number" min={1} max={10000} value={policyMultiplier} disabled={policyBusy}
                    onChange={event => setPolicyMultiplier(event.target.value)} />
                </label>
              </div>
              {policyError && <p className="form-error" role="alert">{policyError}</p>}
              <button className="primary" disabled={policyBusy || models.loading} onClick={() => void savePolicy()}>
                {policyBusy ? "保存中" : "保存模型策略"}
              </button>
            </section>
          </>
        )}
      </AdminLoad>
      {operation && user && (
        <Dialog
          title={
            operation === "status"
              ? user.is_suspended
                ? "恢复账号"
                : "停用账号"
              : operation === "sessions"
                ? "撤销所有会话"
                : "重发验证邮件"
          }
          busy={busy}
          restoreFocusSelector={`#admin-user-${operation}`}
          onClose={() => setOperation(null)}
        >
          <div className="admin-confirm">
            <p>{user.email}</p>
            <p>
              {operation === "status"
                ? user.is_suspended
                  ? "恢复后，用户可重新登录。"
                  : "停用会立即撤销所有会话并阻止登录，账号数据仍会保留。"
                : operation === "sessions"
                  ? "所有已登录设备将退出登录，用户之后仍可重新登录。"
                  : "验证链接将在 30 分钟后过期。首次邮箱验证会使旧密码失效，并退出原有设备。每分钟最多发送一次。"}
            </p>
            {operation === "status" && !user.is_suspended && (
              <label>
                操作原因
                <select
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                >
                  <option value="support_review">用户支持核查</option>
                  <option value="abuse">滥用行为</option>
                  <option value="security">账号安全</option>
                </select>
              </label>
            )}
            {actionError && (
              <p className="form-error" role="alert">
                {actionError}
              </p>
            )}
            <div className="admin-actions">
              <button
                className="primary"
                disabled={busy}
                onClick={() => void confirm()}
              >
                {busy ? "处理中" : "确认"}
              </button>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => setOperation(null)}
              >
                取消
              </button>
            </div>
          </div>
        </Dialog>
      )}
    </>
  );
}
