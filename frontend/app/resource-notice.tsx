"use client";

import { useEffect, useState } from "react";
import { useAuth } from "./auth";

export function ResourceNotice() {
  const { session } = useAuth();
  const key = `resource-notice:${session.user.id}:${session.workspace_id}`;
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
    <span>上传和生成的文件与当前工作区成员共享，长期保留并占用存储空间。查看和下载使用下载额度；模型实际下载文件时计量，缓存命中不计量。</span>
    <button type="button" className="secondary" onClick={() => {
      localStorage.setItem(key, "seen"); setVisible(false);
      window.dispatchEvent(new Event("resource-notice-dismissed"));
    }}>知道了</button>
  </div>;
}
