"use client";

import { useParams } from "next/navigation";
import { ModelProviderDetail } from "../../../model-provider-panel";

export default function ModelDetailPage() {
  const { id } = useParams<{ id: string }>();
  return <ModelProviderDetail key={id} id={id} />;
}
