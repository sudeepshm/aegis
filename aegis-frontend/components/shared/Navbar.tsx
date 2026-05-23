"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Shield, Upload, Terminal, ClipboardList,
  GitBranch, AlertTriangle, Home, Activity
} from "lucide-react";

const navItems = [
  { href: "/", icon: Home, label: "AEGIS Home" },
  { href: "/upload", icon: Upload, label: "Document Upload" },
  { href: "/swarm", icon: Terminal, label: "Agent Swarm Console" },
  { href: "/review", icon: ClipboardList, label: "MAP Review Dashboard" },
  { href: "/hitl", icon: Activity, label: "HITL Corrections" },
  { href: "/dag", icon: GitBranch, label: "Causal DAG Viewer" },
  { href: "/tensions", icon: AlertTriangle, label: "Policy Tensions" },
];

export default function Navbar() {
  const pathname = usePathname();

  return (
    <nav style={{
      position: "fixed", left: 0, top: 0, bottom: 0, width: "240px",
      background: "#FFFFFF",
      borderRight: "1px solid #E5E7EB",
      display: "flex", flexDirection: "column",
      zIndex: 100,
      boxShadow: "2px 0 8px rgba(0,0,0,0.04)",
    }}>
      {/* Logo */}
      <div style={{
        padding: "1.5rem 1.25rem",
        borderBottom: "1px solid #F3F4F6",
        display: "flex", alignItems: "center", gap: "0.75rem",
      }}>
        <div style={{
          width: "36px", height: "36px", borderRadius: "10px",
          background: "#0F172A",
          display: "flex", alignItems: "center", justifyContent: "center",
          boxShadow: "0 2px 8px rgba(15,23,42,0.3)",
        }}>
          <Shield size={18} color="#FFFFFF" />
        </div>
        <div>
          <div style={{
            fontSize: "1rem", fontWeight: 800, letterSpacing: "0.08em",
            color: "#0F172A", fontFamily: "'Poppins', sans-serif", lineHeight: 1,
          }}>AEGIS</div>
          <div style={{ fontSize: "0.6rem", color: "#9CA3AF", letterSpacing: "0.12em", marginTop: "2px" }}>
            COMPLIANCE AI
          </div>
        </div>
      </div>

      {/* Nav Items */}
      <div style={{ flex: 1, padding: "0.75rem", display: "flex", flexDirection: "column", gap: "2px", overflowY: "auto" }}>
        {navItems.map(({ href, icon: Icon, label }) => {
          const isActive = pathname === href;
          return (
            <Link key={href} href={href} style={{
              display: "flex", alignItems: "center", gap: "0.75rem",
              padding: "0.6rem 0.875rem", borderRadius: "8px",
              textDecoration: "none", transition: "all 0.15s ease",
              background: isActive ? "rgba(249,115,22,0.08)" : "transparent",
              border: `1px solid ${isActive ? "rgba(249,115,22,0.2)" : "transparent"}`,
              color: isActive ? "#F97316" : "#6B7280",
            }}>
              <Icon size={16} strokeWidth={isActive ? 2.5 : 1.5} />
              <span style={{
                fontSize: "0.82rem", fontWeight: isActive ? 600 : 400,
                fontFamily: "'Inter', sans-serif",
              }}>{label}</span>
              {isActive && (
                <div style={{
                  marginLeft: "auto", width: "5px", height: "5px",
                  borderRadius: "50%", background: "#F97316",
                }} />
              )}
            </Link>
          );
        })}
      </div>

      {/* Footer */}
      <div style={{
        padding: "1rem 1.25rem",
        borderTop: "1px solid #F3F4F6",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.4rem" }}>
          <div style={{ width: "6px", height: "6px", borderRadius: "50%", background: "#14B8A6" }} />
          <span style={{ fontSize: "0.7rem", color: "#14B8A6", fontWeight: 600 }}>ALL SYSTEMS NOMINAL</span>
        </div>
        <div style={{ fontSize: "0.65rem", color: "#9CA3AF" }}>AEGIS v2.1.0 · Phase 9</div>
      </div>
    </nav>
  );
}
