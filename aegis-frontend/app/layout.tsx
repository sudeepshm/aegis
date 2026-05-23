import type { Metadata } from "next";
import "./globals.css";
import { auth } from "@/auth";

export const metadata: Metadata = {
  title: "AEGIS — Autonomous Enterprise Compliance Intelligence System",
  description: "Enterprise-grade multi-agent compliance automation platform powered by AI. Real-time regulatory intelligence, causal DAG execution, and autonomous policy resolution.",
  keywords: ["compliance", "RegTech", "AI", "LangGraph", "RBI", "DPDP", "SEBI"],
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const session = await auth();
  const role = session?.user?.role ?? "guest";

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      </head>
      <body data-user-role={role}>
        {children}
      </body>
    </html>
  );
}
