"use client";

import { AdminHeading } from "../admin-ui";
import { WorkspaceTarget } from "../workspace-target";
import { UsagePolicyPanel } from "../../usage-policy-panel";

export default function PricingPage() {
  return <><AdminHeading title="工作区倍率" /><WorkspaceTarget>{id => <UsagePolicyPanel key={id} workspaceId={id} />}</WorkspaceTarget></>;
}
