import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import "./rusa-control-center.css";

export const metadata: Metadata = {
  title: "RUSA AI — Agentic Control Center",
  description: "Architecture-aligned enterprise control center for RUSA AI agents, AICode tasks, governance, and observability."
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="id">
      <body>{children}</body>
    </html>
  );
}