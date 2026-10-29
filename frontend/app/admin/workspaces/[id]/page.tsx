"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ArrowLeft } from "lucide-react";
import { useAdminChanges } from "../../admin-changes";
import { WorkspaceCredits } from "../../workspace-credits";
import type { AdminWorkspace } from "../page";
import {
  AdminHeading,
  AdminLoad,
  date,
  useAdminResource,
} from "../../admin-ui";
export default function WorkspaceDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [notice, setNotice] = useState("");
  useAdminChanges(false, false);
  const { data, loading, error, reload } = useAdminResource<AdminWorkspace>(
    `/site/admin/workspaces/${id}`,
  );
  return (
    <>
      <Link className="admin-back" href="/admin/workspaces">
        <ArrowLeft size={15} />
        工作区
      </Link>
      <AdminHeading title={data?.name || "工作区详情"}>
        {data && <div className="provider-form-actions">
          <Link className="secondary" href="/admin/models">模型管理</Link>
          <Link className="secondary" href={`/admin/pricing?workspace=${data.id}`}>消费倍率</Link>
        </div>}
      </AdminHeading>
      {notice && <p className="auth-success" role="status">{notice}</p>}
      <AdminLoad loading={loading} error={error} retry={reload}>
        {data && (
          <>
            <dl className="admin-details">
              <div>
                <dt>工作区 ID</dt>
                <dd>{data.id}</dd>
              </div>
              <div>
                <dt>创建时间</dt>
                <dd>{date(data.created_at)}</dd>
              </div>
              <div>
                <dt>已使用关联中的启用模型数</dt>
                <dd>{data.configuration.enabled_model_providers}</dd>
              </div>
              <div>
                <dt>消费倍率</dt>
                <dd>{data.billing_multiplier} ×</dd>
              </div>
              <div>
                <dt>已使用关联模型</dt>
                <dd>
                  {data.configuration.provider_names.join("、") || "未配置"}
                </dd>
              </div>
            </dl>
            <section className="admin-section">
              <h2>积分摘要</h2>
              <WorkspaceCredits key={data.id} id={data.id} name={data.name} credits={data.credits}
                changed={setNotice} />
            </section>
            <section className="admin-section">
              <h2>成员 · {data.member_count}</h2>
              <div className="admin-table-wrap">
                <table className="admin-table">
                  <thead>
                    <tr>
                      <th>用户</th>
                      <th>邮箱</th>
                      <th>工作区角色</th>
                      <th>账号状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.members?.map((member) => (
                      <tr key={member.id}>
                        <td>
                          <Link href={`/admin/users/${member.id}`}>
                            {member.name}
                          </Link>
                        </td>
                        <td>{member.email}</td>
                        <td>
                          {(
                            {
                              admin: "管理员",
                              editor: "编辑者",
                              viewer: "只读成员",
                            } as Record<string, string>
                          )[member.role] || member.role}
                        </td>
                        <td>{member.is_suspended ? "已停用" : "正常"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!data.members?.length && <p className="admin-empty">暂无成员</p>}
            </section>
          </>
        )}
      </AdminLoad>
    </>
  );
}
