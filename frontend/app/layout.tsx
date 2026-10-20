import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "RAG Knowledge Workspace",
  description: "Build and use trusted knowledge bases.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

