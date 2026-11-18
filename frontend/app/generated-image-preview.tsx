"use client";

import { useId, useState } from "react";
import { useAuth } from "./auth";
import { FilePreview, InlineFileImage } from "./resource-preview";

export function GeneratedImagePreview({ id, name }: { id: string; name: string }) {
  const { session } = useAuth();
  const [opened, setOpened] = useState(false);
  const trigger = useId(), scope = `${session.user.id}:${session.workspace_id}`;
  const file = { type: "resource" as const, id, name };
  return <div data-generated-preview={trigger}>
    <InlineFileImage file={file} scope={scope} onOpen={() => setOpened(true)} />
    {opened && <FilePreview file={file} scope={scope} onClose={() => setOpened(false)} restoreFocusSelector={`[data-generated-preview="${trigger}"] button`} />}
  </div>;
}
