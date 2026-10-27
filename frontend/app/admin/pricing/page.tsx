"use client";

import { AdminHeading } from "../admin-ui";
import { WorkspaceTarget } from "../workspace-target";
import { UsagePolicyPanel } from "../../usage-policy-panel";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { FileSliders } from "lucide-react";

export default function PricingPage() {
  const workspace = useSearchParams().get("workspace");
  return <><AdminHeading title="工作区消费倍率"><Link className="secondary" href={`/admin/pricing/template${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ""}`}><FileSliders size={16} />平台基准价</Link></AdminHeading><WorkspaceTarget>{id => <UsagePolicyPanel key={id} workspaceId={id} />}</WorkspaceTarget></>;
}
