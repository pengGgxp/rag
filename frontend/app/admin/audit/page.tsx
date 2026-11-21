"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Search } from "lucide-react";
import {
  AdminHeading,
  AdminLoad,
  date,
  Pagination,
  type Page,
  useAdminResource,
} from "../admin-ui";
type Event = {
  id: string;
  actor_user_id: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  result: string;
  details: Record<string, unknown>;
  created_at: string;
};
const actions: Record<string, string> = {
  "search.updated": "更新联网搜索配置",
  "search.tested": "测试联网搜索",
  "auth.login": "登录成功",
  "auth.login_failed": "密码登录失败",
  "auth.login_blocked_suspended": "停用账号登录被阻止",
  "auth.register": "账号注册",
  "auth.logout": "退出登录",
  "auth.password_changed": "修改密码",
  "auth.password_reset": "重置密码",
  "auth.email_verified": "邮箱验证成功",
  "auth.email_verification_failed": "邮箱验证失败",
  "auth.email_delivery_failed": "验证邮件发送失败",
  "auth.oauth_linked": "关联第三方账号",
  "auth.oauth_failed": "第三方授权失败",
  "auth.site_admin_grant": "服务器授予站点权限",
  "auth.site_admin_revoke": "服务器撤销站点权限",
  "auth.wechat_failed": "微信授权失败",
  "auth.wechat_linked": "绑定微信",
  "auth.wechat_unlinked": "解除微信绑定",
  "auth.settings_updated": "更新认证配置",
  "auth.email_test": "测试邮件",
  "user.suspended": "停用用户",
  "user.reactivated": "恢复用户",
  "user.sessions_revoked": "撤销用户会话",
  "user.verification_resent": "重发验证邮件",
  "storage.updated": "更新文件存储配置",
  "storage.tested": "测试文件存储",
  "storage.migrated": "迁移原始文件",
  "site.settings_updated": "更新站点设置",
  "user.credits_adjusted": "调整账号积分",
  "user.model_policy_changed": "更新账号模型策略",
  "storage.granted": "增加存储额度",
  "workspace.credits_adjusted": "调整工作区积分",
};
export default function SiteAuditPage() {
  const params = useSearchParams();
  const router = useRouter();
  const { data, loading, error, reload } = useAdminResource<Page<Event>>(
    `/site/admin/audit?${params}`,
  );
  return (
    <>
      <AdminHeading title="站点审计">
        <span>保留最近一年记录</span>
      </AdminHeading>
      <form
        key={params.toString()}
        className="admin-filters"
        onSubmit={(e) => {
          e.preventDefault();
          const next = new URLSearchParams();
          for (const [key, value] of new FormData(e.currentTarget))
            if (value)
              next.set(
                key,
                key.endsWith("_date")
                  ? new Date(String(value)).toISOString()
                  : String(value),
              );
          router.push(`/admin/audit?${next}`);
        }}
      >
        <label>
          操作者 ID
          <input
            name="actor_id"
            placeholder="用户 ID"
            defaultValue={params.get("actor_id") || ""}
          />
        </label>
        <label>
          动作
          <select name="action" defaultValue={params.get("action") || ""}>
            <option value="">全部动作</option>
            {Object.entries(actions).map(([value, label]) => (
              <option value={value} key={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          对象
          <select
            name="target_type"
            defaultValue={params.get("target_type") || ""}
          >
            <option value="">全部</option>
            <option value="user">用户</option>
            <option value="workspace">工作区</option>
            <option value="auth_settings">认证配置</option>
            <option value="site_settings">站点设置</option>
          </select>
        </label>
        <label>
          结果
          <select name="result" defaultValue={params.get("result") || ""}>
            <option value="">全部</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
            <option value="blocked">已阻止</option>
          </select>
        </label>
        <label>
          开始日期
          <input
            name="from_date"
            type="date"
            defaultValue={params.get("from_date")?.slice(0, 10) || ""}
          />
        </label>
        <label>
          结束日期（不含）
          <input
            name="to_date"
            type="date"
            defaultValue={params.get("to_date")?.slice(0, 10) || ""}
          />
        </label>
        <button className="secondary">
          <Search size={16} />
          查询
        </button>
      </form>
      <AdminLoad loading={loading} error={error} retry={reload}>
        {data && (
          <>
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>动作</th>
                    <th>操作者</th>
                    <th>目标</th>
                    <th>结果</th>
                    <th>摘要</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((event) => (
                    <tr key={event.id}>
                      <td>
                        <time>{date(event.created_at)}</time>
                      </td>
                      <td>{actions[event.action] || event.action}</td>
                      <td>
                        {event.actor_user_id ? (
                          <Link href={`/admin/users/${event.actor_user_id}`}>
                            {event.actor_user_id}
                          </Link>
                        ) : (
                          "未认证"
                        )}
                      </td>
                      <td>
                        {event.target_type === "user" && event.target_id ? (
                          <Link href={`/admin/users/${event.target_id}`}>
                            {event.target_id}
                          </Link>
                        ) : event.target_type === "site_settings" ? (
                          <Link href="/admin/settings">站点设置</Link>
                        ) : event.target_type === "auth_settings" ? (
                          "认证配置"
                        ) : (
                          "用户"
                        )}
                      </td>
                      <td>
                        <span
                          className="admin-status"
                          data-danger={event.result !== "success"}
                        >
                          {(
                            {
                              success: "成功",
                              failed: "失败",
                              blocked: "已阻止",
                            } as Record<string, string>
                          )[event.result] || event.result}
                        </span>
                      </td>
                      <td className="admin-audit-details">
                        {Object.keys(event.details).length ? (
                          <details>
                            <summary>查看摘要</summary>
                            <pre>{JSON.stringify(event.details, null, 2)}</pre>
                          </details>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!data.items.length && (
              <p className="admin-empty">没有符合条件的审计记录</p>
            )}
            <Pagination
              total={data.total}
              page={data.page}
              pageSize={data.page_size}
              change={(page) => {
                const next = new URLSearchParams(params);
                next.set("page", String(page));
                router.push(`/admin/audit?${next}`);
              }}
            />
          </>
        )}
      </AdminLoad>
    </>
  );
}
