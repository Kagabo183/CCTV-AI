import type { Metadata } from "next";
import { Fira_Code, Fira_Sans } from "next/font/google";
import "./globals.css";

// Dashboard pairing (ui-ux-pro-max "dashboard, data, analytics"): Fira Sans for text, Fira Code for numbers and times.
const sans = Fira_Sans({ variable: "--font-sans-family", subsets: ["latin"], weight: ["400", "500", "600", "700"] });
const mono = Fira_Code({ variable: "--font-mono-family", subsets: ["latin"], weight: ["400", "500"] });

export const metadata: Metadata = {
  title: "Visionary · Talk to your cameras",
  description: "Ask your CCTV cameras questions in Kinyarwanda.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="rw" className={`${sans.variable} ${mono.variable} h-full antialiased`}>
      {/* browser extensions (e.g. Grammarly) add attributes to <body> before React loads */}
      <body className="h-full" suppressHydrationWarning>
        {children}
      </body>
    </html>
  );
}
