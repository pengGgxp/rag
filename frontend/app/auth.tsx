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
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronLeft,
  ChevronRight,
  CreditCard,
  Eye,
  EyeOff,
  Layers3,
  Loader2,
  LogOut,
  MessageSquare,
  Plus,
  Puzzle,
  RefreshCw,
  ScrollText,
  ShieldCheck,
  Settings2,
  SlidersHorizontal,
  Trash2,
  UserRound,
  UsersRound,
} from "lucide-react";
import { request, setCsrfToken } from "./api";
import { Modal } from "./modal";
import { BillingPanel, creditKind, type CreditUsage } from "./billing-panel";
import { UsagePolicyPanel } from "./usage-policy-panel";
import * as Tabs from "../components/ui/tabs";
import "./auth.css";

type Role = "admin" | "editor" | "viewer";
type AccountTab = "account" | "billing" | "policies" | "members" | "audit";
type AuthOptions = {
  oauth_name: string | null;
  password_reset: boolean;
  setup_available: boolean;
  first_run: boolean;
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
const ACCOUNT_TABS: AccountTab[] = [
  "account",
  "billing",
  "policies",
  "members",
  "audit",
];
function settingsPath(tab: AccountTab) {
  return tab === "account" ? "/settings" : `/settings/${tab}`;
}
function settingsTabFromLocation(): AccountTab {
  if (typeof window === "undefined") return "account";
  const segment = window.location.pathname.split("/").filter(Boolean)[1];
  return ACCOUNT_TABS.includes(segment as AccountTab)
    ? (segment as AccountTab)
    : "account";
}

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
  const [returnPath, setReturnPath] = useState("/");
  const [mode, setMode] = useState<
    "login" | "register" | "setup" | "forgot" | "reset"
  >("login");
  const [resetToken, setResetToken] = useState("");
  const [setupToken, setSetupToken] = useState("");
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
    if (params.has("setup")) {
      setSetupToken(params.get("setup") ?? "");
      setMode("setup");
      // Keep the one-time setup token out of browser history and referrers.
      window.history.replaceState(null, "", "/");
    }
    if (params.has("reset")) {
      setResetToken(params.get("reset") ?? "");
      setMode("reset");
      // Do not leave reset tokens in browser history or outgoing referrers.
      window.history.replaceState(null, "", "/");
    }
    if (window.location.pathname.startsWith("/settings")) {
      setAccount(true);
    } else if (params.has("account")) {
      const legacyTab = params.has("billing") ? "billing" : "account";
      window.history.replaceState(null, "", settingsPath(legacyTab));
      setAccount(true);
    }
    if (params.has("auth_error")) {
      setError(
        params.get("auth_error") === "link_required"
          ? "该邮箱已有账号，请先用邮箱登录，再到账号设置中关联外部账号"
          : "外部登录未完成，请重试",
      );
      window.history.replaceState(null, "", "/");
    }
    void reload();
    const syncRoute = () => setAccount(window.location.pathname.startsWith("/settings"));
    window.addEventListener("popstate", syncRoute);
    const expired = () => {
      accept(null);
      setAccount(false);
      setMode("login");
      setError("登录已过期，请重新登录");
    };
    window.addEventListener("rag-session-expired", expired);
    return () => {
      window.removeEventListener("popstate", syncRoute);
      window.removeEventListener("rag-session-expired", expired);
    };
  }, [accept, reload]);
  const openAccount = useCallback(() => {
    setReturnPath(`${window.location.pathname}${window.location.search}`);
    window.history.pushState(null, "", settingsPath("account"));
    setAccount(true);
  }, []);
  const closeAccount = useCallback(() => {
    window.history.pushState(null, "", returnPath || "/");
    setAccount(false);
  }, [returnPath]);
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
  if (!session || mode === "reset" || mode === "setup")
    return (
      <LoginForm
        {...{ options, mode, setMode, resetToken, setupToken, error, setError }}
        onSession={accept}
      />
    );
  return (
    <AuthContext.Provider
      value={{
        session,
        role: workspace?.role,
        openAccount,
      }}
    >
      <div key={`${session.user.id}:${session.workspace_id}`} className="authenticated-app">
        <div className={account ? "app-view app-view-hidden" : "app-view"} aria-hidden={account}>
          {children}
        </div>
        {account && (
          <AccountPage
            key={session.workspace_id}
            session={session}
            options={options}
            onClose={closeAccount}
            onSession={accept}
            onLogout={() => {
              accept(null);
              setAccount(false);
              setMode("login");
            }}
          />
        )}
      </div>
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
  setupToken,
  error,
  setError,
  onSession,
}: {
  options: AuthOptions;
  mode: "login" | "register" | "setup" | "forgot" | "reset";
  setMode: (
    value: "login" | "register" | "setup" | "forgot" | "reset",
  ) => void;
  resetToken: string;
  setupToken: string;
  error: string;
  setError: (value: string) => void;
  onSession: (session: Session) => void;
}) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const isSignup = mode === "register" || mode === "setup";
  const titles = {
    login: "登录知库",
    register: "创建账号",
    setup: "设置工作区",
    forgot: "找回密码",
    reset: "设置新密码",
  };
  const changeMode = (value: typeof mode) => {
    setMode(value);
    setError("");
    setNotice("");
    setPassword("");
    setShowPassword(false);
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
            mode === "setup" ? "/auth/bootstrap" : `/auth/${mode}`,
            json({
              email,
              password,
              ...(isSignup ? { name } : {}),
              ...(mode === "setup" ? { token: setupToken || token } : {}),
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
      <aside className="auth-intro">
        <a className="auth-brand" href="/">
          <span className="brand-mark">
            <Layers3 size={22} />
          </span>
          <span>知库</span>
        </a>
        <div className="auth-intro-copy">
          <p className="auth-eyebrow">KNOWLEDGE WORKSPACE</p>
          <h1>让知识成为可执行的工作流。</h1>
          <p>
            把资料、文档和对话放在同一个工作空间里，让每个答案都能回到它的来源。
          </p>
        </div>
        <div className="auth-intro-meta">
          <span>可追溯引用</span>
          <span>工作区协作</span>
          <span>智能检索</span>
        </div>
      </aside>
      <section className="auth-form-region">
        {["login", "register"].includes(mode) && (
          <div className="auth-mode-tabs" role="tablist" aria-label="账号操作">
            <button
              className={mode === "login" ? "active" : ""}
              onClick={() => changeMode("login")}
              role="tab"
              aria-selected={mode === "login"}
              type="button"
            >
              登录
            </button>
            <button
              className={mode === "register" ? "active" : ""}
              onClick={() => changeMode("register")}
              role="tab"
              aria-selected={mode === "register"}
              type="button"
            >
              创建账号
            </button>
          </div>
        )}
        <div className="auth-heading">
          <h2>{titles[mode]}</h2>
          <p>
            {mode === "login" && "登录你的知识工作空间"}
            {mode === "register" &&
              (options.first_run
                ? "创建第一个账号，这个账号将成为工作区所有者"
                : "创建账号并开始使用知库")}
            {mode === "setup" && "完成首次部署，创建工作区所有者账号"}
            {mode === "forgot" && "我们会向你的邮箱发送重置链接"}
            {mode === "reset" && "设置一个新的登录密码"}
          </p>
        </div>
        {mode === "login" && options.first_run && (
          <div className="auth-first-run-note" role="status">
            这是知库的首次使用。还没有管理员，创建账号即可完成工作区设置。
          </div>
        )}
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
              <span className="auth-password-field">
                <input
                  required
                  type={showPassword ? "text" : "password"}
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
                <button
                  type="button"
                  className="auth-password-toggle"
                  onClick={() => setShowPassword((value) => !value)}
                  aria-label={showPassword ? "隐藏密码" : "显示密码"}
                  title={showPassword ? "隐藏密码" : "显示密码"}
                >
                  {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </span>
            </label>
          )}
          {mode === "setup" && !setupToken && (
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
                : mode === "setup"
                  ? "完成设置"
                  : titles[mode]}
          </button>
        </form>
        {mode === "login" && options.oauth_name && (
          <div className="auth-divider"><span>或</span></div>
        )}
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
          {mode !== "login" && mode !== "register" && (
            <button
              className="text-button"
              onClick={() => changeMode("login")}
              type="button"
            >
              返回登录
            </button>
          )}
          {mode === "login" && options.password_reset && (
            <button
              className="text-button"
              onClick={() => changeMode("forgot")}
              type="button"
            >
              忘记密码
            </button>
          )}
        </div>
        {mode === "setup" && (
          <p className="auth-private-note">
            此页面仅适用于部署者提供的一次性设置链接。
          </p>
        )}
      </section>
    </main>
  );
}

function AccountPage({
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
  const [tab, setTab] = useState<AccountTab>(() => settingsTabFromLocation());
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
    const syncTab = () => setTab(settingsTabFromLocation());
    window.addEventListener("popstate", syncTab);
    return () => window.removeEventListener("popstate", syncTab);
  }, []);
  function navigateTab(next: AccountTab) {
    setTab(next);
    window.history.pushState(null, "", settingsPath(next));
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
  const tabMeta = {
    account: {
      title: "我的账号",
      description: "管理登录方式、当前工作区和账号安全设置。",
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
      <aside className="settings-global-sidebar" aria-label="产品导航">
        <a className="settings-brand" href="/">
          <span className="brand-mark">
            <Layers3 size={21} />
          </span>
          <span>知库</span>
        </a>
        <nav className="settings-global-nav">
          <a href="/">
            <Layers3 size={17} />
            知识库
          </a>
          <a href="/?chat=1">
            <MessageSquare size={17} />
            知识问答
          </a>
          <a href="/?extensions=1">
            <Puzzle size={17} />
            Agent 扩展
          </a>
          <a href="/settings">
            <Settings2 size={17} />
            账号与工作区
          </a>
        </nav>
        <div className="settings-global-footer">
          <div className="settings-user-summary">
            <span className="account-avatar">
              <UserRound size={16} />
            </span>
            <span>
              <strong>{session.user.name}</strong>
              <small>{session.workspaces.find((item) => item.id === session.workspace_id)?.name ?? "当前工作区"}</small>
            </span>
          </div>
          <div className="settings-current-label">当前页面</div>
          <strong className="settings-current-page">账号与工作区</strong>
        </div>
      </aside>
      <main className="account-page">
      <Tabs.Root
        className="account-settings"
        value={tab}
        onValueChange={(value) => navigateTab(value as AccountTab)}
      >
        <header className="account-page-header">
          <div className="account-page-heading">
            <p>工作区 / 设置与管理</p>
            <h1>账号与工作区</h1>
          </div>
          <button className="secondary account-back" onClick={onClose} disabled={busy}>
            <ArrowLeft size={16} />
            返回工作台
          </button>
        </header>
        <div className="account-page-layout">
          <aside className="account-page-nav" aria-label="账号与工作区设置">
            <div className="account-page-nav-label">工作区设置</div>
            <Tabs.List className="account-tabs" aria-label="账号设置">
              <Tabs.Trigger value="account" disabled={busy}>
                <UserRound size={16} />
                我的账号
              </Tabs.Trigger>
              <Tabs.Trigger value="billing" disabled={busy}>
                <CreditCard size={16} />
                积分与充值
              </Tabs.Trigger>
              {role === "admin" && (
                <Tabs.Trigger value="policies" disabled={busy}>
                  <SlidersHorizontal size={16} />
                  计费规则
                </Tabs.Trigger>
              )}
              {role === "admin" && (
                <Tabs.Trigger value="members" disabled={busy}>
                  <UsersRound size={16} />
                  工作区成员
                </Tabs.Trigger>
              )}
              {role === "admin" && (
                <Tabs.Trigger value="audit" disabled={busy}>
                  <ScrollText size={16} />
                  审计日志
                </Tabs.Trigger>
              )}
            </Tabs.List>
          </aside>
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
                <button className="text-button credit-billing-link" onClick={() => navigateTab("billing")}>
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
          </section>
        </div>
      </Tabs.Root>
      </main>
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
