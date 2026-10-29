"use client";

import { useParams } from "next/navigation";
import { ConnectionDetail } from "../../../../model-connections-panel";

export default function ConnectionPage() {
  const { id } = useParams<{ id: string }>();
  return <ConnectionDetail key={id} id={id} />;
}
