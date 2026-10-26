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
  type Credits,
  useAdminResource,
} from "../admin-ui";
export type AdminUser = {
  id: string;
  email: string;
  name: string;
  email_verified: boolean;
  email_verified_at: string | null;
  is_suspended: boolean;
  is_site_admin: boolean;
  created_at: string;
  active_sessions: number;
  login_methods: { password: boolean; wechat: boolean; other_oauth: boolean };
  workspaces: { id: string; name: string; role: string; credits: Credits }[];
};
export default function UsersPage() {
  const params = useSearchParams();
  const router = useRouter();
  const query = new URLSearchParams(params.toString());
  const { data, loading, error, reload } = useAdminResource<Page<AdminUser>>(
    `/site/admin/users?${query}`,
  );
  function changePage(page: number) {
    const next = new URLSearchParams(query);
    next.set("page", String(page));
    router.push(`/admin/users?${next}`);
  }
  return (
    <>
      <AdminHeading title="用户" />
      <form
        key={params.toString()}
        className="admin-filters"
        onSubmit={(event) => {
          event.preventDefault();
          const form = new FormData(event.currentTarget);
          const next = new URLSearchParams();
          for (const [key, value] of form.entries())
            if (value && value !== "all") next.set(key, String(value));
          router.push(`/admin/users?${next}`);
        }}
      >
        <label>
          搜索
          <input
            name="q"
            aria-label="搜索用户"
            placeholder="邮箱、姓名或用户 ID"
            defaultValue={params.get("q") || ""}
            maxLength={200}
          />
        </label>
        <label>
          邮箱验证
          <select
            name="verified"
            defaultValue={params.get("verified") || "all"}
          >
            <option value="all">全部</option>
            <option value="yes">已验证</option>
            <option value="no">未验证</option>
          </select>
        </label>
        <label>
          账号状态
          <select name="status" defaultValue={params.get("status") || "all"}>
            <option value="all">全部</option>
            <option value="active">正常</option>
            <option value="suspended">已停用</option>
          </select>
        </label>
        <button className="secondary" type="submit">
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
                    <th>用户</th>
                    <th>邮箱</th>
                    <th>账号状态</th>
                    <th>工作区</th>
                    <th>注册时间</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((user) => (
                    <tr key={user.id}>
                      <td>
                        <Link href={`/admin/users/${user.id}`}>
                          {user.name}
                        </Link>
                        {user.is_site_admin && <small>站点管理员</small>}
                        <small>{user.id}</small>
                      </td>
                      <td>
                        {user.email}
                        <small>
                          {user.email_verified ? "已验证" : "未验证"}
                        </small>
                      </td>
                      <td>
                        <span
                          className="admin-status"
                          data-danger={user.is_suspended}
                        >
                          {user.is_suspended ? "已停用" : "正常"}
                        </span>
                      </td>
                      <td>{user.workspaces.length}</td>
                      <td>
                        <time>{date(user.created_at)}</time>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!data.items.length && (
              <p className="admin-empty">没有符合条件的用户</p>
            )}
            <Pagination
              total={data.total}
              page={data.page}
              pageSize={data.page_size}
              change={changePage}
            />
          </>
        )}
      </AdminLoad>
    </>
  );
}
