import type { Metadata } from "next";
import "./styles.css";
import { AuthBoundary, AuthBootstrapScreen } from "./auth";
import { TooltipProvider } from "../components/ui/tooltip";
import { MotionProvider } from "../components/ui/motion-provider";
import { Suspense, type ReactNode } from "react";
import { SettingsNavigation } from "./settings/settings-navigation";

export const metadata: Metadata = {
  title: "知库 · 把资料变成有据可查的工作成果",
  description: "把你的资料，变成有据可查的工作成果。",
  referrer: "no-referrer",
};

export default function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <MotionProvider>
          <TooltipProvider>
            <Suspense fallback={<AuthBootstrapScreen />}><AuthBoundary>
              <SettingsNavigation>{children}</SettingsNavigation>
            </AuthBoundary></Suspense>
          </TooltipProvider>
        </MotionProvider>
      </body>
    </html>
  );
}
