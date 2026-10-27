"use client";

import { AdminHeading } from "../admin-ui";
import { WorkspaceTarget } from "../workspace-target";
import { UsagePolicyPanel } from "../../usage-policy-panel";

export default function PricingPage() {
  return <><AdminHeading title="计费规则" /><WorkspaceTarget>{id => <UsagePolicyPanel key={id} workspaceId={id} />}</WorkspaceTarget></>;
}
