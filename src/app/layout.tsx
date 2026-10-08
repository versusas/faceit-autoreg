import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Faceit Autoreg Bot Status",
  description: "Dashboard for Faceit Autoreg Bot",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
