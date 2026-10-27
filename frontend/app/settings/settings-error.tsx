"use client";

import { RefreshCw } from "lucide-react";

export default function SettingsError({ reset }: { reset: () => void }) {
  return <div className="admin-error" role="alert">
    <span>设置内容加载失败，请重试。</span>
    <button className="secondary" onClick={reset}><RefreshCw size={16} />重试</button>
  </div>;
}
