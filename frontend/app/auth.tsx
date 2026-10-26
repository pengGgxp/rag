"use client";

import {
  createContext,
  FormEvent,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  ArrowRight,
  Check,
  ChevronDown,
  Eye,
  EyeOff,
  Layers3,
  Loader2,
  LogOut,
  RefreshCw,
  ShieldCheck,
  Settings2,
  UserRound,
} from "lucide-react";
import { request, setCsrfToken } from "./api";
import { Modal } from "./modal";
import { Authentication } from "./authentication";
import { AuthFrame } from "./auth-frame";
import "./auth.css";

export type Role = "admin" | "editor" | "viewer";
export type AuthOptions = {
  oauth_name: string | null;
  password_reset: boolean;
  setup_available: boolean;
  first_run: boolean;
  email_code?: boolean;
  wechat?: boolean;
  require_verified_email?: boolean;
};
type Member = { id: string; name: string; email: string; role: Role };
export type Session = {
  user: {
    id: string;
    name: string;
    email: string;
    has_password: boolean;
    oauth_linked: boolean;
    email_verified?: boolean;
    wechat_linked?: boolean;
    is_site_admin?: boolean;
  };
  workspaces: { id: string; name: string; role: Role }[];
  workspace_id: string;
  csrf_token: string;
};
type Auth = {
  session: Session;
  role?: Role;
  options: AuthOptions;
  refreshSession: () => Promise<Session>;
  logout: () => void;
};
const AuthContext = createContext<Auth | null>(null);
export function useAuth() {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("Missing authenticated workspace");
  return auth;
}
const json = (body: unknown, method = "POST") => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "请求失败，请稍后重试";

export function AccountButton() {
  const { session, refreshSession, logout } = useAuth();
  const workspace = session.workspaces.find(
    (item) => item.id === session.workspace_id,
  );
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [switching, setSwitching] = useState(false);
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const returnTo = pathname?.startsWith("/settings")
    ? "/"
    : `${pathname || "/"}${searchParams.size ? `?${searchParams.toString()}` : ""}`;
  const personalSettingsHref = `/settings?returnTo=${encodeURIComponent(returnTo)}`;
  const workspaceSettingsHref = `/settings/workspace?returnTo=${encodeURIComponent(returnTo)}`;
  const menuItems = useRef<Array<HTMLAnchorElement | HTMLButtonElement>>([]);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        rootRef.current?.querySelector<HTMLButtonElement>("[aria-haspopup='menu']")?.focus();
      }
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  function focusMenuItem(direction: 1 | -1) {
    const items = menuItems.current.filter(
      (item) => !(item instanceof HTMLButtonElement && item.disabled),
    );
    if (!items.length) return;
    const current = document.activeElement;
    const index = items.indexOf(current as HTMLAnchorElement | HTMLButtonElement);
    items[(index + direction + items.length) % items.length]?.focus();
  }

  async function switchWorkspace(workspaceId: string) {
    if (workspaceId === session.workspace_id) {
      setOpen(false);
      return;
    }
    setSwitching(true);
    try {
      await request(
        "/auth/workspace",
        json({ workspace_id: workspaceId }),
      );
      await refreshSession();
      setOpen(false);
    } finally {
      setSwitching(false);
    }
  }

  return (
    <div className="account-menu" ref={rootRef}>
      <button
        className="account-button"
        type="button"
        aria-haspopup="menu"
        data-account-trigger="true"
        aria-expanded={open}
        title="账号与工作区"
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setOpen(true);
            requestAnimationFrame(() => menuItems.current[0]?.focus());
          }
        }}
      >
        <span className="account-avatar">
          <UserRound size={17} />
        </span>
        <span>
          <strong>{session.user.name}</strong>
          <small>{workspace?.name ?? "选择工作区"}</small>
        </span>
        <ChevronDown className="account-menu-chevron" size={15} aria-hidden="true" />
      </button>
      {open && (
        <div
          className="account-menu-panel"
          ref={menuRef}
          role="menu"
          aria-label="账号与工作区"
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              focusMenuItem(1);
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              focusMenuItem(-1);
            } else if (event.key === "Escape") {
              event.preventDefault();
              setOpen(false);
              rootRef.current?.querySelector<HTMLButtonElement>("[aria-haspopup='menu']")?.focus();
            }
          }}
        >
          <Link
            ref={(element) => {
              if (element) menuItems.current[0] = element;
            }}
            className="account-menu-item"
            href={personalSettingsHref}
            role="menuitem"
            onClick={() => setOpen(false)}
          >
            <UserRound size={16} />
            我的账号
          </Link>
          <Link
            ref={(element) => {
              if (element) menuItems.current[1] = element;
            }}
            className="account-menu-item"
            href={workspaceSettingsHref}
            role="menuitem"
            onClick={() => setOpen(false)}
          >
            <Settings2 size={16} />
            工作区设置
          </Link>
          {session.user.is_site_admin && <Link className="account-menu-item" href="/admin" role="menuitem" ref={(element) => { if (element) menuItems.current[session.workspaces.length + 2] = element; }} onClick={() => setOpen(false)}><ShieldCheck size={16} />站点管理</Link>}
          {session.workspaces.length > 1 && (
            <>
              <div className="account-menu-label">切换工作区</div>
              {session.workspaces.map((item, index) => (
                <button
                  key={item.id}
                  ref={(element) => {
                    if (element) menuItems.current[index + 2] = element;
                  }}
                  className="account-menu-item"
                  type="button"
                  role="menuitem"
                  disabled={switching}
                  onClick={() => void switchWorkspace(item.id)}
                >
                  <span className="account-menu-item-icon">
                    <Layers3 size={16} />
                  </span>
                  {item.name}
                  {item.id === session.workspace_id && (
                    <Check className="account-menu-check" size={15} />
                  )}
                </button>
              ))}
            </>
          )}
          <div className="account-menu-divider" />
          <button
            ref={(element) => {
              menuItems.current[session.workspaces.length + (session.user.is_site_admin ? 3 : 2)] = element!;
            }}
            className="account-menu-item danger"
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              logout();
            }}
          >
            <LogOut size={16} />
            退出登录
          </button>
        </div>
      )}
    </div>
  );
}

export function AuthBootstrapScreen({ error, onRetry }: { error?: string; onRetry?: () => void }) {
  if (!error) {
    return (
      <main className="auth-bootstrap-shell" aria-busy="true" aria-label="知库工作区">
        <aside className="auth-bootstrap-sidebar" aria-hidden="true">
          <div className="auth-bootstrap-brand">
            <span className="brand-mark">
              <Layers3 size={20} />
            </span>
            <span>知库</span>
          </div>
          <div className="auth-bootstrap-nav">
            <span className="auth-skeleton auth-skeleton-nav active" />
            <span className="auth-skeleton auth-skeleton-nav" />
            <span className="auth-skeleton auth-skeleton-nav" />
            <span className="auth-skeleton auth-skeleton-nav short" />
          </div>
          <div className="auth-bootstrap-sidebar-footer">
            <span className="auth-skeleton auth-skeleton-avatar" />
            <span className="auth-skeleton auth-skeleton-footer-line" />
          </div>
        </aside>
        <section className="auth-bootstrap-content" aria-hidden="true">
          <header className="auth-bootstrap-toolbar">
            <span className="auth-skeleton auth-skeleton-breadcrumb" />
            <span className="auth-skeleton auth-skeleton-toolbar-action" />
          </header>
          <div className="auth-bootstrap-main">
            <span className="auth-skeleton auth-skeleton-kicker" />
            <span className="auth-skeleton auth-skeleton-title" />
            <span className="auth-skeleton auth-skeleton-description" />
            <div className="auth-bootstrap-blocks">
              <span className="auth-skeleton auth-skeleton-block large" />
              <span className="auth-skeleton auth-skeleton-block" />
              <span className="auth-skeleton auth-skeleton-block" />
            </div>
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="auth-bootstrap-screen" role="alert" aria-live="polite">
      <section className="auth-bootstrap-panel">
        <div className="auth-bootstrap-brand">
          <span className="brand-mark">
            <Layers3 size={22} />
          </span>
          <span>知库</span>
        </div>
        {error ? (
          <>
            <h1>工作区暂时无法连接</h1>
            <p>{error}</p>
            <button className="primary auth-bootstrap-action" onClick={onRetry}>
              <RefreshCw size={16} />
              重试连接
            </button>
          </>
        ) : null}
      </section>
    </main>
  );
}

export function AuthBoundary({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [session, setSession] = useState<Session | null>(null);
  const [options, setOptions] = useState<AuthOptions | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
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
      setMode("login");
      setError("登录已过期，请重新登录");
    };
    window.addEventListener("rag-session-expired", expired);
    return () => {
      window.removeEventListener("rag-session-expired", expired);
    };
  }, [accept, reload]);
  const workspace = session?.workspaces.find(
    (item) => item.id === session.workspace_id,
  );
  const refreshSession = useCallback(async () => {
    const next = await request<Session>("/auth/me");
    accept(next);
    return next;
  }, [accept]);
  const logout = useCallback(() => {
    void request("/auth/logout", { method: "POST" }).then(() => {
      accept(null);
      setMode("login");
      router.replace("/auth/login");
    }).catch((e) => setError(errorMessage(e)));
  }, [accept, router]);
  useEffect(() => {
    if (!loading && options && !session && mode !== "reset" && mode !== "setup" && !pathname.startsWith("/auth/")) {
      const returnTo = `${pathname}${searchParams.size ? `?${searchParams.toString()}` : ""}`;
      router.replace(`/auth/login?returnTo=${encodeURIComponent(returnTo)}`);
    }
  }, [loading, options, session, mode, pathname, searchParams, router]);
  if (loading)
    return <AuthBootstrapScreen />;
  if (!options)
    return <AuthBootstrapScreen error={error || "服务暂时不可用"} onRetry={() => void reload()} />;
  if (mode !== "reset" && mode !== "setup" && (!session || (pathname.startsWith("/auth/") && pathname !== "/auth/login")))
    return <Authentication options={options} session={session} onSession={accept} />;
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
        options,
        refreshSession,
        logout,
      }}
    >
      <div key={`${session.user.id}:${session.workspace_id}`} className="authenticated-app">
        <div className="app-view">
          {children}
        </div>
      </div>
      {!workspace && (
        <Modal title="工作区访问已变更" onClose={() => undefined}>
          <button className="primary" onClick={() => window.location.assign("/settings")}>
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
    <AuthFrame>
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
    </AuthFrame>
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
