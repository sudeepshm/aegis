"use client";

import Navbar from "./Navbar";

export default function PageLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="page-with-sidebar">
      <Navbar />
      <main className="main-content">
        {children}
      </main>
    </div>
  );
}
