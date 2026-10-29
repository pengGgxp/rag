"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ArrowLeft, LogOut, Mail, ShieldBan, ShieldCheck } from "lucide-react";
import { Dialog } from "../../../../components/ui/dialog";
import { request } from "../../../api";
import { useAuth } from "../../../auth";
import type { AdminUser } from "../page";
import { WorkspaceCredits } from "../../workspace-credits";
import {
  AdminHeading,
  AdminLoad,
  date,
  json,
  useAdminResource,
} from "../../admin-ui";
export default function UserDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { session } = useAuth();
  const {
    data: user,
    error,
    loading,
    reload,
  } = useAdminResource<AdminUser>(`/site/admin/users/${id}`);
  const [operation, setOperation] = useState<
    "status" | "sessions" | "email" | null
  >(null);
  const [reason, setReason] = useState("support_review");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
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
              <h2>所属工作区</h2>
              {!user.workspaces.length && (
                <p className="admin-empty">暂无工作区</p>
              )}
              {user.workspaces.map((workspace) => (
                <section key={workspace.id} className="admin-section">
                  <h2>
                    <Link href={`/admin/workspaces/${workspace.id}`}>
                      {workspace.name}
                    </Link>{" "}
                    ·{" "}
                    {(
                      {
                        admin: "管理员",
                        editor: "编辑者",
                        viewer: "只读成员",
                      } as Record<string, string>
                    )[workspace.role] || workspace.role}
                  </h2>
                  <WorkspaceCredits id={workspace.id} name={workspace.name} credits={workspace.credits}
                    changed={setNotice} />
                </section>
              ))}
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
