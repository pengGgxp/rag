"use client";
import { RefreshCw } from "lucide-react";
export default function AdminError({ reset }: { reset: () => void }) {
  return (
    <div className="admin-error" role="alert">
      <span>页面暂时无法加载，请重试。</span>
      <button className="secondary" onClick={reset}>
        <RefreshCw size={15} />
        重试
      </button>
    </div>
  );
}
