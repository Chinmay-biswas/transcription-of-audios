import type { Metadata } from "next";

import { SiteHeader } from "@/components/site-header";
import "./globals.css";

export const metadata: Metadata = {
  title: "Meeting Intelligence",
  description: "Turn meeting recordings into searchable decisions and action items."
};

export default function RootLayout({
  children
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>
        <SiteHeader />
        <main className="shell page-content">{children}</main>
      </body>
    </html>
  );
}
