"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Search } from "lucide-react";
import {
  AdminHeading,
  AdminLoad,
  date,
  number,
  Pagination,
  type Page,
  type Credits,
  useAdminResource,
} from "../admin-ui";
export type AdminWorkspace = {
  id: string;
  name: string;
  created_at: string;
  member_count: number;
  credits: Credits;
  configuration: { enabled_model_providers: number; provider_names: string[] };
  members?: {
    id: string;
    email: string;
    name: string;
    role: string;
    is_suspended: boolean;
  }[];
};
export default function WorkspacesPage() {
  const params = useSearchParams();
  const router = useRouter();
  const { data, loading, error, reload } = useAdminResource<
    Page<AdminWorkspace>
  >(`/site/admin/workspaces?${params}`);
  return (
    <>
      <AdminHeading title="工作区" />
      <form
        key={params.toString()}
        className="admin-filters"
        onSubmit={(e) => {
          e.preventDefault();
          const q = String(new FormData(e.currentTarget).get("q") || "");
          router.push(`/admin/workspaces?q=${encodeURIComponent(q)}`);
        }}
      >
        <label>
          搜索
          <input
            name="q"
            aria-label="搜索工作区"
            placeholder="工作区名称或 ID"
            defaultValue={params.get("q") || ""}
            maxLength={200}
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
                    <th>工作区</th>
                    <th>成员</th>
                    <th>积分余额</th>
                    <th>累计入账</th>
                    <th>累计消耗</th>
                    <th>创建时间</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((workspace) => (
                    <tr key={workspace.id}>
                      <td>
                        <Link href={`/admin/workspaces/${workspace.id}`}>
                          {workspace.name}
                        </Link>
                        <small>{workspace.id}</small>
                      </td>
                      <td>{workspace.member_count}</td>
                      <td>{number(workspace.credits.balance)}</td>
                      <td>{number(workspace.credits.lifetime_granted)}</td>
                      <td>{number(workspace.credits.lifetime_spent)}</td>
                      <td>
                        <time>{date(workspace.created_at)}</time>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!data.items.length && (
              <p className="admin-empty">没有符合条件的工作区</p>
            )}
            <Pagination
              total={data.total}
              page={data.page}
              pageSize={data.page_size}
              change={(page) => {
                const next = new URLSearchParams(params);
                next.set("page", String(page));
                router.push(`/admin/workspaces?${next}`);
              }}
            />
          </>
        )}
      </AdminLoad>
    </>
  );
}
