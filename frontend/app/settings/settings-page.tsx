"use client";

import { useCallback, useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UserRound,
} from "lucide-react";
import { request } from "../api";
import {
  AuthOptions,
  Role,
  Session,
  useAuth,
} from "../auth";
import { BillingPanel } from "../billing-panel";
import { ModelSelectionPanel } from "../model-selection-panel";
import { sectionFromPath } from "./settings-routes";
import { useSettingsForm } from "./standalone-settings";
type Member = { id: string; name: string; email: string; role: Role };
type AuditLog = {
  id: string;
  action: string;
  resource_type: string;
  resource_id?: string | null;
  metadata?: Record<string, unknown> | null;
  actor?: { id?: string; name?: string; email?: string } | null;
  actor_user?: { id?: string; name?: string; email?: string } | null;
  created_at: string;
};
type AuditLogPage = {
  items: AuditLog[];
  total: number;
  page: number;
  page_size: number;
};

const ROLE_LABELS: Record<Role, string> = {
  admin: "管理员",
  editor: "编辑者",
  viewer: "只读成员",
};
const json = (body: unknown, method = "POST") => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "请求失败，请稍后重试";
export default function SettingsPage() {
  const { session, options, refreshSession } = useAuth();
  return (
    <AccountPage
      session={session}
      options={options}
      onSession={refreshSession}
    />
  );
}

function AccountPage({
  session,
  options,
  onSession,
}: {
  session: Session;
  options: AuthOptions;
  onSession: (session: Session) => void;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const { setDirty, setBusy: setFrameBusy } = useSettingsForm();
  const role = session.workspaces.find(
    (item) => item.id === session.workspace_id,
  )?.role;
  const tab = sectionFromPath(pathname)?.key;
  const [members, setMembers] = useState<Member[]>([]);
  const [auditLogs, setAuditLogs] = useState<AuditLogPage>({
    items: [],
    total: 0,
    page: 1,
    page_size: 10,
  });
  const [auditPage, setAuditPage] = useState(1);
  const [auditAction, setAuditAction] = useState("");
  const [auditResourceType, setAuditResourceType] = useState("");
  const [auditLoading, setAuditLoading] = useState(false);
  const [email, setEmail] = useState("");
  const [memberRole, setMemberRole] = useState<Role>("viewer");
  const [password, setPassword] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [membersLoading, setMembersLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => { setDirty(Boolean(password || currentPassword || email)); }, [password, currentPassword, email, setDirty]);
  useEffect(() => { setFrameBusy(busy); }, [busy, setFrameBusy]);
  useEffect(() => () => { setDirty(false); setFrameBusy(false); }, [setDirty, setFrameBusy]);
  useEffect(() => {
    setError(""); setNotice(""); setPassword(""); setCurrentPassword(""); setEmail("");
  }, [pathname]);
  useEffect(() => {
    if (pathname === "/settings/policies" && session.user.is_site_admin)
      router.replace(`/admin/pricing?workspace=${session.workspace_id}`);
  }, [pathname, session.user.is_site_admin, session.workspace_id, router]);

  const load = useCallback(
    async () => setMembers(await request<Member[]>("/auth/members")),
    [],
  );

  useEffect(() => {
    if (tab !== "members" || role !== "admin") return;
    let active = true;
    setMembersLoading(true);
    setError("");
    load().catch(e => { if (active) setError(errorMessage(e)); })
      .finally(() => { if (active) setMembersLoading(false); });
    return () => { active = false; };
  }, [tab, role, load, retry]);

  const loadAuditLogs = useCallback(async () => {
    setAuditLoading(true);
    try {
      const query = new URLSearchParams({
        page: String(auditPage),
        page_size: "10",
      });
      if (auditAction) query.set("action", auditAction);
      if (auditResourceType) query.set("resource_type", auditResourceType);
      const result = await request<
        AuditLogPage | { items: AuditLog[]; total: number } | AuditLog[]
      >(`/audit-logs?${query.toString()}`);
      const data = Array.isArray(result)
        ? { items: result, total: result.length, page: auditPage, page_size: 10 }
        : {
            items: result.items ?? [],
            total: result.total ?? result.items?.length ?? 0,
            page: "page" in result ? result.page ?? auditPage : auditPage,
            page_size:
              "page_size" in result ? result.page_size ?? 10 : 10,
          };
      setAuditLogs(data);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setAuditLoading(false);
    }
  }, [auditAction, auditPage, auditResourceType]);

  useEffect(() => {
    if (tab === "audit" && role === "admin") void loadAuditLogs();
  }, [tab, role, loadAuditLogs]);

  async function action(run: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await run();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function member(email: string, memberRole: Role) {
    await request("/auth/members", json({ email, role: memberRole }, "PUT"));
    await load();
    onSession(await request<Session>("/auth/me"));
  }

  if (tab === "models") return <ModelSelectionPanel key={session.workspace_id} editable={role === "admin"} onDirty={setDirty} onBusy={setFrameBusy} />;
  if (!tab) return <div className="settings-empty" role="status">
    {pathname === "/settings/policies"
      ? session.user.is_site_admin ? "正在打开站点管理…" : "计费规则由站点管理员管理，你无权访问。"
      : "找不到此设置页面，请从导航中选择。"}
  </div>;
  if ((tab === "members" || tab === "audit") && role !== "admin")
    return <p className="form-error" role="alert">此页面仅对工作区管理员开放。</p>;
  return (
          <section className="settings-content" key={tab}>
            {error && (
              <p className="form-error" role="alert">
                {error}
              </p>
            )}
            {notice && (
              <p className="auth-success" role="status">
                {notice}
              </p>
            )}
            {tab === "account" ? (
              <>
                <div className="account-identity">
                  <UserRound size={22} />
                  <div>
                    <strong>{session.user.name}</strong>
                    <p>{session.user.email}</p>
                  </div>
                </div>
                <dl className="settings-details">
                  <div><dt>姓名</dt><dd>{session.user.name}</dd></div>
                  <div><dt>邮箱</dt><dd>{session.user.email}</dd></div>
                </dl>
              </>
            ) : tab === "security" ? (
              <>
                <section className="settings-section">
                  <h2>邮箱</h2>
                  <div className="account-oauth"><div><strong>{session.user.email}</strong><p>{session.user.email_verified ? "已验证，可用于登录" : "尚未验证"}</p></div>{!session.user.email_verified && <button className="secondary" onClick={() => router.push("/auth/verify-email?returnTo=%2Fsettings%2Fsecurity")}><ShieldCheck size={15} />验证邮箱</button>}</div>
                </section>
                <section className="settings-section">
                  <h2>关联登录</h2>
                {(options.wechat || session.user.wechat_linked) && <div className="account-oauth"><div><strong>微信</strong><p>{session.user.wechat_linked ? "已绑定" : "未绑定"}</p></div><button className="secondary" disabled={busy} onClick={() => void action(async () => {
                  if (session.user.wechat_linked) {
                    const result = await request<{ message: string }>("/auth/wechat/unlink", json({}));
                    setNotice(result.message); onSession(await request<Session>("/auth/me"));
                  } else {
                    const result = await request<{ url: string }>("/auth/wechat/start", json({ link: true, return_to: "/settings/security" }));
                    window.location.assign(result.url);
                  }
                })}><ShieldCheck size={15} />{session.user.wechat_linked ? "解除微信绑定" : "绑定微信"}</button></div>}
                {!options.wechat && !session.user.wechat_linked && !options.oauth_name && <p className="settings-muted">暂无其他登录方式</p>}
                {options.oauth_name && <div className="account-oauth">
                  <div><strong>{options.oauth_name}</strong><p>{session.user.oauth_linked ? "已关联" : "未关联"}</p></div>
                  {!session.user.oauth_linked && <button className="secondary" disabled={busy} onClick={() => void action(async () => {
                    const result = await request<{ url: string }>("/auth/oauth/start", json({ link: true }));
                    window.location.assign(result.url);
                  })}><ShieldCheck size={15} />关联账号</button>}
                </div>}
                </section>
                <form
                  className="account-password auth-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void action(async () => {
                      const result = await request<{ message: string }>(
                        "/auth/password",
                        json({
                          current_password: currentPassword,
                          password,
                        }),
                      );
                      setNotice(result.message);
                      setPassword("");
                      setCurrentPassword("");
                      onSession(await request<Session>("/auth/me"));
                    });
                  }}
                >
                  <input
                    type="hidden"
                    autoComplete="username"
                    value={session.user.email}
                    readOnly
                  />
                  <h3>{session.user.has_password ? "修改密码" : "设置站内密码"}</h3>
                  {session.user.has_password && (
                    <label>
                      当前密码
                      <input
                        type="password"
                        autoComplete="current-password"
                        required
                        value={currentPassword}
                        onChange={(event) => setCurrentPassword(event.target.value)}
                      />
                    </label>
                  )}
                  <label>
                    新密码
                    <input
                      type="password"
                      autoComplete="new-password"
                      required
                      minLength={12}
                      maxLength={128}
                      placeholder="至少 12 个字符"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                    />
                  </label>
                  <button className="secondary" disabled={busy}>
                    <Check size={15} />
                    保存密码
                  </button>
                </form>
              </>
            ) : tab === "workspace" ? (
              <dl className="settings-details">
                <div><dt>工作区名称</dt><dd>{session.workspaces.find(item => item.id === session.workspace_id)?.name}</dd></div>
                <div><dt>我的角色</dt><dd>{role ? ROLE_LABELS[role] : "—"}</dd></div>
              </dl>
            ) : tab === "billing" ? (
              <BillingPanel
                workspaceName={
                  session.workspaces.find(
                    (item) => item.id === session.workspace_id,
                  )?.name ?? ""
                }
                onBusyChange={setBusy}
              />
            ) : tab === "members" ? (
              <>
                <form
                  className="member-add"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void action(async () => {
                      await member(email, memberRole);
                      setEmail("");
                    });
                  }}
                >
                  <label>
                    成员邮箱
                    <input
                      type="email"
                      required
                      placeholder="已注册的邮箱"
                      value={email}
                      onChange={(event) => setEmail(event.target.value)}
                    />
                  </label>
                  <label>
                    角色
                    <select
                      value={memberRole}
                      onChange={(event) =>
                        setMemberRole(event.target.value as Role)
                      }
                    >
                      {Object.entries(ROLE_LABELS).map(([key, label]) => (
                        <option key={key} value={key}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="primary"
                    disabled={busy}
                    title="添加成员"
                    aria-label="添加成员"
                  >
                    <Plus size={17} />
                  </button>
                </form>
                {membersLoading ? <SettingsSkeleton /> : error ? <button className="secondary" onClick={() => setRetry(value => value + 1)}>重试加载成员</button> : <div className="member-list">
                  {members.map((item) => (
                    <div className="member-row" key={item.id}>
                      <div>
                        <strong>
                          {item.name}
                          {item.id === session.user.id ? "（你）" : ""}
                        </strong>
                        <small>{item.email}</small>
                      </div>
                      <select
                        aria-label={`${item.name}的角色`}
                        value={item.role}
                        disabled={busy}
                        onChange={(event) =>
                          void action(() =>
                            member(item.email, event.target.value as Role),
                          )
                        }
                      >
                        {Object.entries(ROLE_LABELS).map(([key, label]) => (
                          <option key={key} value={key}>
                            {label}
                          </option>
                        ))}
                      </select>
                      <button
                        className="icon-button danger"
                        aria-label={`移除 ${item.name}`}
                        title="移除成员"
                        disabled={busy}
                        onClick={() => {
                          if (
                            window.confirm(`移除 ${item.name} 的工作区访问权限？`)
                          )
                            void action(async () => {
                              await request(`/auth/members/${item.id}`, {
                                method: "DELETE",
                              });
                              await load();
                              onSession(await request<Session>("/auth/me"));
                            });
                        }}
                      >
                        <Trash2 size={16} />
                      </button>
                    </div>
                  ))}
                </div>}
              </>
            ) : (
              <AuditLogPanel
                logs={auditLogs}
                action={auditAction}
                resourceType={auditResourceType}
                loading={auditLoading}
                onActionChange={(value) => {
                  setAuditAction(value);
                  setAuditPage(1);
                }}
                onResourceTypeChange={(value) => {
                  setAuditResourceType(value);
                  setAuditPage(1);
                }}
                onRefresh={() => void loadAuditLogs()}
                onPageChange={setAuditPage}
              />
            )}
          </section>
  );
}

export function SettingsSkeleton() {
  return <div className="settings-skeleton" role="status" aria-label="加载中">
    <span /><span /><span />
  </div>;
}

const AUDIT_ACTION_LABELS: Record<string, string> = {
  "auth.register": "注册账号",
  "auth.login": "登录",
  "auth.logout": "退出登录",
  "auth.password_changed": "修改密码",
  "auth.password_reset": "重置密码",
  "auth.workspace_switched": "切换工作区",
  "workspace.member_added": "添加成员",
  "workspace.member_role_changed": "修改成员角色",
  "workspace.member_removed": "移除成员",
  "knowledge_base.created": "创建知识库",
  "knowledge_base.permission_changed": "修改知识库权限",
  "document.uploaded": "上传文档",
  "document.deleted": "删除文档",
  "document.restored": "恢复文档",
  "document.reindexed": "重建文档索引",
  "conversation.created": "创建会话",
  "conversation.renamed": "重命名会话",
  "conversation.deleted": "删除会话",
  "feedback.created_or_updated": "提交反馈",
  "feedback.deleted": "撤销反馈",
  "recharge.created": "创建充值订单",
  "recharge.paid": "充值到账",
};

function auditActionLabel(action: string) {
  return AUDIT_ACTION_LABELS[action] ?? action;
}

function auditActor(log: AuditLog) {
  const actor = log.actor ?? log.actor_user;
  return actor?.name || actor?.email || "系统";
}

function auditTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function auditSummary(log: AuditLog) {
  const metadata = log.metadata;
  if (!metadata || typeof metadata !== "object") return "—";
  const hidden = /password|token|cookie|secret|content|body|prompt/i;
  if (hidden.test(JSON.stringify(metadata))) return "—";
  const entries = Object.entries(metadata)
    .filter(([key, value]) => !hidden.test(key) && value !== null && value !== "")
    .slice(0, 2)
    .map(([key, value]) => {
      const rendered =
        typeof value === "string" ? value : JSON.stringify(value);
      return `${key}: ${rendered}`;
    });
  return entries.length ? entries.join(" · ") : "—";
}

function AuditLogPanel({
  logs,
  action,
  resourceType,
  loading,
  onActionChange,
  onResourceTypeChange,
  onRefresh,
  onPageChange,
}: {
  logs: AuditLogPage;
  action: string;
  resourceType: string;
  loading: boolean;
  onActionChange: (value: string) => void;
  onResourceTypeChange: (value: string) => void;
  onRefresh: () => void;
  onPageChange: (page: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(logs.total / logs.page_size));
  return (
    <section className="audit-panel" aria-label="审计日志">
      <div className="audit-toolbar">
        <label>
          动作
          <input
            value={action}
            onChange={(event) => onActionChange(event.target.value)}
            placeholder="例如 document.deleted"
          />
        </label>
        <label>
          资源类型
          <input
            value={resourceType}
            onChange={(event) => onResourceTypeChange(event.target.value)}
            placeholder="例如 document"
          />
        </label>
        <button
          className="icon-button"
          title="刷新日志"
          aria-label="刷新日志"
          disabled={loading}
          onClick={onRefresh}
        >
          <RefreshCw size={16} className={loading ? "spin" : ""} />
        </button>
      </div>
      {loading && logs.items.length === 0 ? (
        <SettingsSkeleton />
      ) : logs.items.length === 0 ? (
        <div className="audit-empty">暂无匹配的审计记录</div>
      ) : (
        <div className="audit-table-wrap">
          <table className="audit-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>操作者</th>
                <th>动作</th>
                <th>资源</th>
                <th>摘要</th>
              </tr>
            </thead>
            <tbody>
              {logs.items.map((log) => (
                <tr key={log.id}>
                  <td>{auditTime(log.created_at)}</td>
                  <td>{auditActor(log)}</td>
                  <td title={log.action}>{auditActionLabel(log.action)}</td>
                  <td>
                    <span>{log.resource_type}</span>
                    {log.resource_id && (
                      <small title={log.resource_id}>{log.resource_id}</small>
                    )}
                  </td>
                  <td className="audit-summary" title={auditSummary(log)}>
                    {auditSummary(log)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="audit-pagination">
        <span>
          共 {logs.total} 条 · 第 {Math.min(logs.page, totalPages)} /{" "}
          {totalPages} 页
        </span>
        <div>
          <button
            className="icon-button"
            title="上一页"
            aria-label="上一页"
            disabled={loading || logs.page <= 1}
            onClick={() => onPageChange(Math.max(1, logs.page - 1))}
          >
            <ChevronLeft size={16} />
          </button>
          <button
            className="icon-button"
            title="下一页"
            aria-label="下一页"
            disabled={loading || logs.page >= totalPages}
            onClick={() =>
              onPageChange(Math.min(totalPages, logs.page + 1))
            }
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>
    </section>
  );
}
