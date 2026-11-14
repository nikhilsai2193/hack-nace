import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Study Room",
  description: "AI group study room: DeepInfra writes the discussion, Nace Drex Max v1.1 supervises.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
