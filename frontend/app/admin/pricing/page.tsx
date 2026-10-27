"use client";

import { AdminHeading } from "../admin-ui";
import { WorkspaceTarget } from "../workspace-target";
import { UsagePolicyPanel } from "../../usage-policy-panel";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { FileSliders } from "lucide-react";

export default function PricingPage() {
  const workspace = useSearchParams().get("workspace");
  return <><AdminHeading title="计费规则"><Link className="secondary" href={`/admin/pricing/template${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ""}`}><FileSliders size={16} />新工作区价格模板</Link></AdminHeading><WorkspaceTarget>{id => <UsagePolicyPanel key={id} workspaceId={id} />}</WorkspaceTarget></>;
}
