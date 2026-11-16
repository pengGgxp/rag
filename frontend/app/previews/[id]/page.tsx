"use client";

import { useParams } from "next/navigation";
import { PreviewSurface } from "../../cloud-preview";
import { CloudComputerProvider } from "../../cloud-computer-context";
import { useAuth } from "../../auth";

export default function PreviewPage() {
  const { id } = useParams<{ id: string }>();
  const { session } = useAuth();
  return <CloudComputerProvider key={`${session.user.id}:${session.workspace_id}:${id}`}
    scope={{ userId: session.user.id, workspaceId: session.workspace_id, conversationId: null, previewId: id }}>
    <PreviewSurface id={id} standalone />
  </CloudComputerProvider>;
}
