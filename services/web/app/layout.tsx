import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "ResumeForge",
  description: "Tailor your resume to a job posting, without inventing anything.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="top">
            <h1>ResumeForge</h1>
            <nav>
              <Link href="/">New resume</Link>
              <Link href="/profile">Profile</Link>
            </nav>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
