"use client";

import { useParams } from "next/navigation";
import { PreviewSurface } from "../../cloud-preview";

export default function PreviewPage() {
  const { id } = useParams<{ id: string }>();
  return <PreviewSurface id={id} standalone />;
}
