"use client";
import Link from "next/link";
import { ArrowUpRight } from "lucide-react";
import { AdminHeading, AdminLoad, number, useAdminResource } from "./admin-ui";
type Overview = {
  users_total: number;
  users_registered_30d: number;
  users_unverified: number;
  users_suspended: number;
  workspaces_total: number;
};
export default function AdminOverview() {
  const { data, error, loading, reload } = useAdminResource<Overview>(
    "/site/admin/overview",
  );
  return (
    <>
      <AdminHeading title="站点概览" />
      <AdminLoad error={error} loading={loading} retry={reload}>
        {data && (
          <>
            <dl className="admin-metrics">
              <div>
                <dt>用户总数</dt>
                <dd>{number(data.users_total)}</dd>
              </div>
              <div>
                <dt>近 30 天注册</dt>
                <dd>{number(data.users_registered_30d)}</dd>
              </div>
              <div>
                <dt>工作区总数</dt>
                <dd>{number(data.workspaces_total)}</dd>
              </div>
            </dl>
            <section className="admin-section">
              <h2>账号状态</h2>
              <Link
                className="admin-summary-link"
                href="/admin/users?verified=no"
              >
                <span>未验证邮箱</span>
                <strong>{number(data.users_unverified)}</strong>
                <ArrowUpRight size={17} />
              </Link>
              <Link
                className="admin-summary-link"
                href="/admin/users?status=suspended"
              >
                <span>已停用账号</span>
                <strong>{number(data.users_suspended)}</strong>
                <ArrowUpRight size={17} />
              </Link>
            </section>
            <section className="admin-section">
              <h2>管理入口</h2>
              <div className="admin-actions">
                <Link className="secondary" href="/admin/users">
                  用户管理
                  <ArrowUpRight size={16} />
                </Link>
                <Link className="secondary" href="/admin/workspaces">
                  工作区
                  <ArrowUpRight size={16} />
                </Link>
                <Link className="secondary" href="/admin/audit">
                  站点审计
                  <ArrowUpRight size={16} />
                </Link>
              </div>
            </section>
          </>
        )}
      </AdminLoad>
    </>
  );
}
