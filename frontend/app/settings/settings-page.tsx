"use client";

import { useCallback, useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  ArrowRight,
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
import { BillingPanel, creditKind, type CreditUsage } from "../billing-panel";
import { UsagePolicyPanel } from "../usage-policy-panel";
import {
  SettingsSidebar,
  type SettingsSection,
} from "./settings-sidebar";

type AccountTab = SettingsSection;
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
const ACCOUNT_TABS: AccountTab[] = [
  "account",
  "workspace",
  "billing",
  "policies",
  "members",
  "audit",
];
const json = (body: unknown, method = "POST") => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "请求失败，请稍后重试";
export function settingsPath(tab: AccountTab) {
  return tab === "account" ? "/settings" : `/settings/${tab}`;
}
function settingsTabFromPath(pathname: string): AccountTab {
  const segment = pathname.split("/").filter(Boolean)[1];
  return ACCOUNT_TABS.includes(segment as AccountTab)
    ? (segment as AccountTab)
    : "account";
}

export default function SettingsPage({ standalone = true }: { standalone?: boolean }) {
  const { session, options, refreshSession } = useAuth();
  return (
    <AccountPage
      session={session}
      options={options}
      onSession={refreshSession}
      standalone={standalone}
    />
  );
}

function AccountPage({
  session,
  options,
  onSession,
  standalone,
}: {
  session: Session;
  options: AuthOptions;
  onSession: (session: Session) => void;
  standalone: boolean;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const role = session.workspaces.find(
    (item) => item.id === session.workspace_id,
  )?.role;
  const [tab, setTab] = useState<AccountTab>(() => settingsTabFromPath(pathname));
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
  const [usage, setUsage] = useState<CreditUsage | null>(null);

  useEffect(() => {
    setTab(settingsTabFromPath(pathname));
  }, [pathname]);

  function navigateTab(next: AccountTab) {
    const returnTo = searchParams.get("returnTo");
    const validReturnTo = returnTo && returnTo.startsWith("/") &&
      !returnTo.startsWith("//") && !returnTo.includes("\\") &&
      !returnTo.startsWith("/settings");
    const query = validReturnTo ? `?returnTo=${encodeURIComponent(returnTo)}` : "";
    router.push(`${settingsPath(next)}${query}`);
  }

  const load = useCallback(
    async () => setMembers(await request<Member[]>("/auth/members")),
    [],
  );

  useEffect(() => {
    if (tab === "members" && role === "admin")
      load().catch((e) => setError(errorMessage(e)));
  }, [tab, role, load]);

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

  const loadUsage = useCallback(async () => {
    setUsage(await request<CreditUsage>("/usage"));
  }, []);

  useEffect(() => {
    if (tab === "workspace") void loadUsage().catch((e) => setError(errorMessage(e)));
  }, [tab, loadUsage]);

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

  const tabMeta = {
    account: {
      title: "我的账号",
      description: "管理个人资料、登录方式和账号安全。",
    },
    workspace: {
      title: "工作区概览",
      description: "查看当前工作区，并进入积分、成员和管理设置。",
    },
    billing: {
      title: "积分与充值",
      description: "查看工作区余额、充值记录和使用明细。",
    },
    policies: {
      title: "计费规则",
      description: "为不同模型和能力配置工作区积分消耗规则。",
    },
    members: {
      title: "工作区成员",
      description: "管理成员访问权限和工作区角色。",
    },
    audit: {
      title: "审计日志",
      description: "查看工作区内关键账号、内容和权限操作记录。",
    },
  } as const;

  return (
    <div className="settings-shell">
      <SettingsSidebar session={session} role={role} activeSection={tab} />
      <div className={`account-page ${standalone ? "" : "account-page-layer"}`}>
        <div className="account-settings">
          <header className="account-page-header">
            <div className="account-page-heading">
              <p>{tab === "account" ? "账号" : "工作区"}</p>
              <h1>{tabMeta[tab].title}</h1>
            </div>
          </header>
          <section className="account-page-content">
            <div className="account-content-heading">
              <h2>{tabMeta[tab].title}</h2>
              <p>{tabMeta[tab].description}</p>
            </div>
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
                {options.oauth_name && (
                  <div className="account-oauth">
                    <span>{options.oauth_name}</span>
                    {session.user.oauth_linked ? (
                      <span className="auth-success">已关联</span>
                    ) : (
                      <button
                        className="secondary"
                        disabled={busy}
                        onClick={() =>
                          void action(async () => {
                            const result = await request<{ url: string }>(
                              "/auth/oauth/start",
                              json({ link: true }),
                            );
                            window.location.assign(result.url);
                          })
                        }
                      >
                        <ShieldCheck size={15} />
                        关联账号
                      </button>
                    )}
                  </div>
                )}
              </>
            ) : tab === "workspace" ? (
              <div className="workspace-settings-overview">
                <label className="account-field">
                  当前工作区
                  <select
                    value={session.workspace_id}
                    disabled={busy}
                    onChange={(event) => void action(async () => onSession(await request<Session>("/auth/workspace", json({ workspace_id: event.target.value }))))}
                  >
                    {session.workspaces.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name} · {ROLE_LABELS[item.role]}
                      </option>
                    ))}
                  </select>
                </label>
                {usage && (
                  <section className="credit-usage" aria-label="积分用量">
                    <div className="credit-usage-heading">
                      <div>
                        <span className="credit-label">可用积分</span>
                        <strong>{usage.balance.toLocaleString("zh-CN")}</strong>
                      </div>
                      <small>已使用 {usage.lifetime_spent.toLocaleString("zh-CN")} · 累计获得 {usage.lifetime_granted.toLocaleString("zh-CN")}</small>
                    </div>
                    {usage.entries.length > 0 && (
                      <div className="credit-entry-list" aria-label="最近积分流水">
                        {usage.entries.slice(0, 5).map((entry) => (
                          <div className="credit-entry" key={entry.id}>
                            <span>{creditKind(entry.kind)}</span>
                            <strong className={entry.amount < 0 ? "negative" : ""}>
                              {entry.amount > 0 ? "+" : ""}{entry.amount}
                            </strong>
                          </div>
                        ))}
                      </div>
                    )}
                    <button className="text-button credit-billing-link" onClick={() => navigateTab("billing")}>
                      积分与充值 <ArrowRight size={14} />
                    </button>
                  </section>
                )}
                <div className="workspace-settings-links">
                  {(["billing", ...(role === "admin" ? ["policies", "members", "audit"] : [])] as AccountTab[]).map((section) => (
                    <button className="secondary" key={section} onClick={() => navigateTab(section)}>
                      {tabMeta[section].title}<ArrowRight size={14} />
                    </button>
                  ))}
                </div>
              </div>
            ) : tab === "billing" ? (
              <BillingPanel
                workspaceName={
                  session.workspaces.find(
                    (item) => item.id === session.workspace_id,
                  )?.name ?? ""
                }
                onBusyChange={setBusy}
              />
            ) : tab === "policies" ? (
              <UsagePolicyPanel onBusyChange={setBusy} />
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
                <div className="member-list">
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
                </div>
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
        </div>
      </div>
    </div>
  );
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
        <div className="audit-empty">正在加载审计日志…</div>
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
