import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AEGIS — Autonomous Enterprise Compliance Intelligence System",
  description: "Enterprise-grade multi-agent compliance automation platform powered by AI. Real-time regulatory intelligence, causal DAG execution, and autonomous policy resolution.",
  keywords: ["compliance", "RegTech", "AI", "LangGraph", "RBI", "DPDP", "SEBI"],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      </head>
      <body>
        {children}
      </body>
    </html>
  );
}
