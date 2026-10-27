"use client";

import { AdminHeading } from "../admin-ui";
import { WorkspaceTarget } from "../workspace-target";
import { ModelProviderPanel } from "../../model-provider-panel";

export default function ModelsPage() {
  return <><AdminHeading title="模型供应商" /><WorkspaceTarget>{id => <ModelProviderPanel key={id} workspaceId={id} />}</WorkspaceTarget></>;
}
