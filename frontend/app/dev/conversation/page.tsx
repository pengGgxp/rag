import { notFound } from "next/navigation";
import { ConversationPrototype } from "./prototype";

export default function ConversationPrototypePage() {
  if (process.env.NODE_ENV !== "development") notFound();
  return <ConversationPrototype />;
}
