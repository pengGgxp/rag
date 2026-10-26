import type { ReactNode } from "react";
import { AdminShell } from "./admin-shell";
import "./admin.css";

export default function AdminLayout({ children }: { children: ReactNode }) {
  return <AdminShell>{children}</AdminShell>;
}
