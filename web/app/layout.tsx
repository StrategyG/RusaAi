import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RUSA AI — Control Center",
  description: "Tenant-scoped control center for RUSA AI inference and autonomous coding tasks."
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="id">
      <body>{children}</body>
    </html>
  );
}