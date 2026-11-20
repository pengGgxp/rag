"use client";

import { useEffect, useState } from "react";
import { useAuth } from "./auth";

export function ResourceNotice() {
  const { session } = useAuth();
  const key = `resource-notice:${session.user.id}`;
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const update = () => setVisible(localStorage.getItem(key) !== "seen");
    update();
    window.addEventListener("resource-notice-dismissed", update);
    window.addEventListener("storage", update);
    return () => {
      window.removeEventListener("resource-notice-dismissed", update);
      window.removeEventListener("storage", update);
    };
  }, [key]);
  if (!visible) return null;
  return <div className="resource-sharing-notice" role="note">
    <span>文件保存在你的资源中并占用存储空间。预览不消耗下载额度，主动下载和发送图片沿用现有额度规则；云电脑按运行时间计费。</span>
    <button type="button" className="secondary" onClick={() => {
      localStorage.setItem(key, "seen"); setVisible(false);
      window.dispatchEvent(new Event("resource-notice-dismissed"));
    }}>知道了</button>
  </div>;
}
