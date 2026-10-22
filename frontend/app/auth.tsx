"use client";

import {
  createContext,
  FormEvent,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import {
  ArrowRight,
  Check,
  ChevronLeft,
  ChevronRight,
  Layers3,
  Loader2,
  LogOut,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UserRound,
} from "lucide-react";
import { request, setCsrfToken } from "./api";
import { Modal } from "./modal";
import { BillingPanel, creditKind, type CreditUsage } from "./billing-panel";
import { UsagePolicyPanel } from "./usage-policy-panel";
import * as Tabs from "../components/ui/tabs";
import "./auth.css";

type Role = "admin" | "editor" | "viewer";
type AuthOptions = {
  oauth_name: string | null;
  password_reset: boolean;
  setup_available: boolean;
};
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
type Session = {
  user: {
    id: string;
    name: string;
    email: string;
    has_password: boolean;
    oauth_linked: boolean;
  };
  workspaces: { id: string; name: string; role: Role }[];
  workspace_id: string;
  csrf_token: string;
};
type Auth = { session: Session; role?: Role; openAccount: () => void };
const AuthContext = createContext<Auth | null>(null);
export function useAuth() {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("Missing authenticated workspace");
  return auth;
}
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

export function AccountButton() {
  const { session, openAccount } = useAuth();
  const workspace = session.workspaces.find(
    (item) => item.id === session.workspace_id,
  );
  return (
    <button
      className="account-button"
      onClick={openAccount}
      title="账号与工作区"
    >
      <span className="account-avatar">
        <UserRound size={17} />
      </span>
      <span>
        <strong>{session.user.name}</strong>
        <small>{workspace?.name ?? "选择工作区"}</small>
      </span>
    </button>
  );
}

export function AuthBoundary({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [options, setOptions] = useState<AuthOptions | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [account, setAccount] = useState(false);
  const [mode, setMode] = useState<
    "login" | "register" | "bootstrap" | "forgot" | "reset"
  >("login");
  const [resetToken, setResetToken] = useState("");
  const accept = useCallback((value: Session | null) => {
    setSession(value);
    setCsrfToken(value?.csrf_token ?? "");
  }, []);
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setOptions(await request<AuthOptions>("/auth/options"));
      try {
        accept(await request<Session>("/auth/me"));
      } catch {
        accept(null);
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [accept]);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.has("reset")) {
      setResetToken(params.get("reset") ?? "");
      setMode("reset");
      // Do not leave reset tokens in browser history or outgoing referrers.
      window.history.replaceState(null, "", "/");
    }
    if (params.has("account")) setAccount(true);
    if (params.has("auth_error")) {
      setError(
        params.get("auth_error") === "link_required"
          ? "该邮箱已有账号，请先用邮箱登录，再到账号设置中关联外部账号"
          : "外部登录未完成，请重试",
      );
      window.history.replaceState(null, "", "/");
    }
    void reload();
    const expired = () => {
      accept(null);
      setAccount(false);
      setMode("login");
      setError("登录已过期，请重新登录");
    };
    window.addEventListener("rag-session-expired", expired);
    return () => window.removeEventListener("rag-session-expired", expired);
  }, [accept, reload]);
  const workspace = session?.workspaces.find(
    (item) => item.id === session.workspace_id,
  );
  if (loading)
    return (
      <main className="auth-screen">
        <Loader2 className="spin" aria-label="正在加载账号" />
      </main>
    );
  if (!options)
    return (
      <main className="auth-screen">
        <p role="alert">{error}</p>
        <button className="secondary" onClick={() => void reload()}>
          重试
        </button>
      </main>
    );
  if (!session || mode === "reset")
    return (
      <LoginForm
        {...{ options, mode, setMode, resetToken, error, setError }}
        onSession={accept}
      />
    );
  return (
    <AuthContext.Provider
      value={{
        session,
        role: workspace?.role,
        openAccount: () => setAccount(true),
      }}
    >
      <div
        key={`${session.user.id}:${session.workspace_id}`}
        className="authenticated-app"
      >
        {children}
      </div>
      {account && (
        <AccountModal
          key={session.workspace_id}
          session={session}
          options={options}
          onClose={() => setAccount(false)}
          onSession={accept}
          onLogout={() => {
            accept(null);
            setAccount(false);
            setMode("login");
          }}
        />
      )}
      {!workspace && !account && (
        <Modal title="工作区访问已变更" onClose={() => setAccount(true)}>
          <button className="primary" onClick={() => setAccount(true)}>
            选择工作区
          </button>
        </Modal>
      )}
    </AuthContext.Provider>
  );
}

function LoginForm({
  options,
  mode,
  setMode,
  resetToken,
  error,
  setError,
  onSession,
}: {
  options: AuthOptions;
  mode: "login" | "register" | "bootstrap" | "forgot" | "reset";
  setMode: (
    value: "login" | "register" | "bootstrap" | "forgot" | "reset",
  ) => void;
  resetToken: string;
  error: string;
  setError: (value: string) => void;
  onSession: (session: Session) => void;
}) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const isSignup = mode === "register" || mode === "bootstrap";
  const titles = {
    login: "登录知库",
    register: "创建账号",
    bootstrap: "初始化工作区",
    forgot: "找回密码",
    reset: "设置新密码",
  };
  const changeMode = (value: typeof mode) => {
    setMode(value);
    setError("");
    setNotice("");
    setPassword("");
  };
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (mode === "forgot") {
        const data = await request<{ message: string }>(
          "/auth/forgot-password",
          json({ email }),
        );
        setNotice(data.message);
      } else if (mode === "reset") {
        const data = await request<{ message: string }>(
          "/auth/reset-password",
          json({ token: resetToken, password }),
        );
        setMode("login");
        setNotice(data.message);
        setPassword("");
      } else {
        onSession(
          await request<Session>(
            `/auth/${mode}`,
            json({
              email,
              password,
              ...(isSignup ? { name } : {}),
              ...(mode === "bootstrap" ? { token } : {}),
            }),
          ),
        );
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  async function oauth() {
    setBusy(true);
    setError("");
    try {
      const result = await request<{ url: string }>(
        "/auth/oauth/start",
        json({}),
      );
      window.location.assign(result.url);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  return (
    <main className="auth-screen">
      <section className="auth-form-region">
        <a className="auth-brand" href="/">
          <span className="brand-mark">
            <Layers3 size={24} />
          </span>
          知库
        </a>
        <h1>{titles[mode]}</h1>
        <form className="auth-form" onSubmit={submit} key={mode}>
          {isSignup && (
            <label>
              姓名
              <input
                required
                autoComplete="name"
                value={name}
                maxLength={100}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
          )}
          {mode !== "reset" && (
            <label>
              邮箱
              <input
                required
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
          )}
          {mode !== "forgot" && (
            <label>
              {mode === "reset" ? "新密码" : "密码"}
              <input
                required
                type="password"
                autoComplete={
                  isSignup || mode === "reset"
                    ? "new-password"
                    : "current-password"
                }
                minLength={isSignup || mode === "reset" ? 12 : 1}
                maxLength={128}
                placeholder={
                  isSignup || mode === "reset" ? "至少 12 个字符" : ""
                }
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
          )}
          {mode === "bootstrap" && (
            <label>
              初始化口令
              <input
                required
                autoComplete="off"
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
            </label>
          )}
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
          <button className="primary auth-submit" disabled={busy} type="submit">
            {busy ? (
              <Loader2 className="spin" size={16} />
            ) : (
              <ArrowRight size={16} />
            )}
            {mode === "forgot"
              ? "发送重置邮件"
              : mode === "reset"
                ? "更新密码"
                : mode === "bootstrap"
                  ? "接管工作区"
                  : titles[mode]}
          </button>
        </form>
        {mode === "login" && options.oauth_name && (
          <button
            className="secondary oauth-button"
            disabled={busy}
            onClick={() => void oauth()}
          >
            <ShieldCheck size={16} />
            通过{options.oauth_name}登录
          </button>
        )}
        <div className="auth-links">
          <button
            className="text-button"
            onClick={() => changeMode(mode === "login" ? "register" : "login")}
          >
            {mode === "login" ? "创建账号" : "返回登录"}
          </button>
          {mode === "login" && options.password_reset && (
            <button
              className="text-button"
              onClick={() => changeMode("forgot")}
            >
              忘记密码
            </button>
          )}
        </div>
        {mode === "login" && options.setup_available && (
          <button
            className="auth-setup-link"
            onClick={() => changeMode("bootstrap")}
          >
            管理员初始化
          </button>
        )}
      </section>
    </main>
  );
}

function AccountModal({
  session,
  options,
  onClose,
  onSession,
  onLogout,
}: {
  session: Session;
  options: AuthOptions;
  onClose: () => void;
  onSession: (session: Session) => void;
  onLogout: () => void;
}) {
  const role = session.workspaces.find(
    (item) => item.id === session.workspace_id,
  )?.role;
  const [tab, setTab] = useState<"account" | "billing" | "policies" | "members" | "audit">(() =>
    typeof window !== "undefined" && new URLSearchParams(window.location.search).has("billing") ? "billing" : "account",
  );
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
    const url = new URL(window.location.href);
    url.searchParams.delete("account");
    url.searchParams.delete("billing");
    window.history.replaceState(null, "", url);
  }, []);
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
    if (tab === "account") void loadUsage().catch((e) => setError(errorMessage(e)));
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
  async function member(email: string, role: Role) {
    await request("/auth/members", json({ email, role }, "PUT"));
    await load();
    onSession(await request<Session>("/auth/me"));
  }
  return (
    <Modal title="账号与工作区" onClose={onClose} busy={busy} wide>
      <Tabs.Root
        className="account-settings"
        value={tab}
        onValueChange={(value) =>
          setTab(value as "account" | "billing" | "policies" | "members" | "audit")
        }
      >
        <Tabs.List className="account-tabs" aria-label="账号设置">
          <Tabs.Trigger value="account" disabled={busy}>我的账号</Tabs.Trigger>
          <Tabs.Trigger value="billing" disabled={busy}>积分与充值</Tabs.Trigger>
          {role === "admin" && (
            <Tabs.Trigger value="policies" disabled={busy}>计费规则</Tabs.Trigger>
          )}
          {role === "admin" && (
            <Tabs.Trigger value="members" disabled={busy}>工作区成员</Tabs.Trigger>
          )}
          {role === "admin" && (
            <Tabs.Trigger value="audit" disabled={busy}>审计日志</Tabs.Trigger>
          )}
        </Tabs.List>
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
            {usage && (
              <section className="credit-usage" aria-label="积分用量">
                <div className="credit-usage-heading">
                  <div>
                    <span className="credit-label">工作区积分</span>
                    <strong>{usage.balance.toLocaleString("zh-CN")}</strong>
                  </div>
                  <small>
                    已使用 {usage.lifetime_spent.toLocaleString("zh-CN")} ·
                    累计获得 {usage.lifetime_granted.toLocaleString("zh-CN")}
                  </small>
                </div>
                {usage.entries.length > 0 && (
                  <div className="credit-entry-list" aria-label="最近积分流水">
                    {usage.entries.slice(0, 5).map((entry) => (
                      <div className="credit-entry" key={entry.id}>
                        <span>
                          {creditKind(entry.kind)}
                        </span>
                        <strong className={entry.amount < 0 ? "negative" : ""}>
                          {entry.amount > 0 ? "+" : ""}
                          {entry.amount}
                        </strong>
                      </div>
                    ))}
                  </div>
                )}
                <button className="text-button credit-billing-link" onClick={() => setTab("billing")}>
                  积分与充值<ArrowRight size={14} />
                </button>
              </section>
            )}
            <label className="account-field">
              当前工作区
              <select
                value={session.workspace_id}
                disabled={busy}
                onChange={(e) =>
                  void action(async () => {
                    onSession(
                      await request<Session>(
                        "/auth/workspace",
                        json({ workspace_id: e.target.value }),
                      ),
                    );
                    onClose();
                  })
                }
              >
                {!role && (
                  <option value={session.workspace_id}>无权访问</option>
                )}
                {session.workspaces.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} · {ROLE_LABELS[item.role]}
                  </option>
                ))}
              </select>
            </label>
            <form
              className="account-password auth-form"
              onSubmit={(e) => {
                e.preventDefault();
                void action(async () => {
                  const result = await request<{ message: string }>(
                    "/auth/password",
                    json({ current_password: currentPassword, password }),
                  );
                  setNotice(result.message);
                  setPassword("");
                  setCurrentPassword("");
                  onSession(await request<Session>("/auth/me"));
                });
              }}
            >
              <input type="hidden" autoComplete="username" value={session.user.email} readOnly />
              <h3>{session.user.has_password ? "修改密码" : "设置站内密码"}</h3>
              {session.user.has_password && (
                <label>
                  当前密码
                  <input
                    type="password"
                    autoComplete="current-password"
                    required
                    value={currentPassword}
                    onChange={(e) => setCurrentPassword(e.target.value)}
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
                  onChange={(e) => setPassword(e.target.value)}
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
            <button
              className="secondary account-logout"
              disabled={busy}
              onClick={() =>
                void action(async () => {
                  await request("/auth/logout", { method: "POST" });
                  onLogout();
                })
              }
            >
              <LogOut size={16} />
              退出登录
            </button>
          </>
        ) : tab === "billing" ? (
          <BillingPanel
            workspaceName={session.workspaces.find((item) => item.id === session.workspace_id)?.name ?? ""}
            onBusyChange={setBusy}
          />
        ) : tab === "policies" ? (
          <UsagePolicyPanel onBusyChange={setBusy} />
        ) : tab === "members" ? (
          <>
            <form
              className="member-add"
              onSubmit={(e) => {
                e.preventDefault();
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
                  onChange={(e) => setEmail(e.target.value)}
                />
              </label>
              <label>
                角色
                <select
                  value={memberRole}
                  onChange={(e) => setMemberRole(e.target.value as Role)}
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
                    onChange={(e) =>
                      void action(() =>
                        member(item.email, e.target.value as Role),
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
      </Tabs.Root>
    </Modal>
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

export function LibraryAccess({
  id,
  onChange,
}: {
  id: string;
  onChange: () => void;
}) {
  const [members, setMembers] = useState<Member[]>([]);
  const [restricted, setRestricted] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [users, permissions] = await Promise.all([
        request<Member[]>("/auth/members"),
        request<{ restricted: boolean; user_ids: string[] }>(
          `/knowledge-bases/${id}/permissions`,
        ),
      ]);
      setMembers(users);
      setRestricted(permissions.restricted);
      setSelected(permissions.user_ids);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [id]);
  useEffect(() => {
    void load();
  }, [load]);
  if (loading) return <p>正在加载访问权限…</p>;
  return (
    <form
      className="library-permissions"
      onSubmit={(e) => {
        e.preventDefault();
        setBusy(true);
        setError("");
        setSaved(false);
        request(
          `/knowledge-bases/${id}/permissions`,
          json({ restricted, user_ids: selected }, "PUT"),
        )
          .then(() => {
            setSaved(true);
            onChange();
          })
          .catch((e) => setError(errorMessage(e)))
          .finally(() => setBusy(false));
      }}
    >
      <h2>访问权限</h2>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <label className="account-field">
        可见范围
        <select
          value={restricted ? "restricted" : "workspace"}
          onChange={(e) => {
            setRestricted(e.target.value === "restricted");
            setSaved(false);
          }}
        >
          <option value="workspace">工作区所有成员</option>
          <option value="restricted">仅管理员和指定成员</option>
        </select>
      </label>
      {restricted && (
        <fieldset className="permission-members">
          <legend>指定成员</legend>
          {members
            .filter((item) => item.role !== "admin")
            .map((item) => (
              <label key={item.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(item.id)}
                  onChange={(e) => {
                    setSelected(
                      e.target.checked
                        ? [...selected, item.id]
                        : selected.filter((value) => value !== item.id),
                    );
                    setSaved(false);
                  }}
                />
                <span>
                  {item.name}
                  <small>{item.email}</small>
                </span>
              </label>
            ))}
        </fieldset>
      )}
      <button className="secondary" disabled={busy}>
        <Check size={15} />
        保存权限
      </button>
      {saved && (
        <span className="auth-success" role="status">
          已保存
        </span>
      )}
    </form>
  );
}
