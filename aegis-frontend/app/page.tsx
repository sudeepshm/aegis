"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { Upload, Terminal, ClipboardList, GitBranch, AlertTriangle, Activity, ArrowRight, Shield } from "lucide-react";

const AegisHero = dynamic(() => import("@/components/hero/AegisHero"), {
  ssr: false,
  loading: () => (
    <div style={{
      position: "fixed", inset: 0, background: "#FFFFFF",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 0,
    }}>
      <div style={{ textAlign: "center" }}>
        <div style={{
          width: "40px", height: "40px", borderRadius: "50%",
          border: "3px solid #F3F4F6", borderTopColor: "#F97316",
          animation: "spin 0.8s linear infinite", margin: "0 auto 1rem",
        }} />
        <p style={{ color: "#9CA3AF", fontFamily: "Inter", fontSize: "0.8rem", letterSpacing: "0.08em" }}>
          Initializing Studio Visualizer...
        </p>
      </div>
    </div>
  ),
});

const features = [
  { href: "/upload", icon: Upload, label: "Anti-Fraud Scanner", desc: "Document forensics", color: "#0F172A" },
  { href: "/swarm", icon: Terminal, label: "Agent Swarm", desc: "Live AI dialogue", color: "#14B8A6" },
  { href: "/review", icon: ClipboardList, label: "MAP Review", desc: "CCO obligation inbox", color: "#0F172A" },
  { href: "/hitl", icon: Activity, label: "HITL Corrections", desc: "AI routing overrides", color: "#F97316" },
  { href: "/dag", icon: GitBranch, label: "Causal DAG", desc: "Task dependencies", color: "#0F172A" },
  { href: "/tensions", icon: AlertTriangle, label: "Policy Tensions", desc: "Regulatory conflicts", color: "#EF4444" },
];

export default function HomePage() {
  return (
    <>
      {/* 3D Canvas — white studio background, behind all HTML (z:0) */}
      <AegisHero />

      {/* HTML Overlay — strictly outside Canvas (z:10+) */}
      <div style={{
        position: "fixed", inset: 0, zIndex: 10,
        pointerEvents: "none",
        display: "flex", flexDirection: "column",
        justifyContent: "space-between",
        padding: "2.5rem 3rem 2rem",
      }}>
        {/* Top-left hero text */}
        <div style={{ pointerEvents: "auto", maxWidth: "500px" }}>
          {/* Status pill */}
          <div style={{
            display: "inline-flex", alignItems: "center", gap: "0.5rem",
            background: "rgba(255,255,255,0.9)",
            border: "1px solid #E5E7EB",
            borderRadius: "100px", padding: "0.35rem 0.9rem",
            marginBottom: "1.5rem",
            boxShadow: "0 1px 4px rgba(0,0,0,0.06)",
          }}>
            <div style={{ width: "6px", height: "6px", borderRadius: "50%", background: "#14B8A6" }} />
            <span style={{ fontSize: "0.7rem", color: "#6B7280", fontWeight: 500, letterSpacing: "0.06em" }}>
              PHASE 9 ENTERPRISE · ALL SYSTEMS ACTIVE
            </span>
          </div>

          <h1 style={{
            fontFamily: "'Poppins', sans-serif",
            fontSize: "clamp(2.4rem, 5vw, 3.8rem)",
            fontWeight: 900, lineHeight: 1.05,
            letterSpacing: "-0.03em",
            color: "#FFFFFF",
            marginBottom: "0.4rem",
          }}>
            AEGIS
          </h1>
          <h2 style={{
            fontFamily: "'Poppins', sans-serif",
            fontSize: "clamp(0.85rem, 1.5vw, 1.1rem)",
            fontWeight: 400, color: "#6B7280",
            letterSpacing: "0.25em", textTransform: "uppercase", color: "rgba(255,255,255,0.75)",
            marginBottom: "1.5rem",
          }}>
            The Standalone Automation
          </h2>

          <p style={{
            fontSize: "0.95rem", color: "#4B5563", lineHeight: 1.75,
            maxWidth: "400px", marginBottom: "2rem",
          }}>
            Autonomous multi-agent compliance intelligence. Ingesting regulatory documents,
            resolving policy tensions, and routing obligations — without human intervention.
          </p>

          <div style={{ display: "flex", gap: "0.75rem" }}>
            <Link href="/upload" style={{
              display: "inline-flex", alignItems: "center", gap: "0.5rem",
              padding: "0.75rem 1.5rem",
              background: "#F97316",
              color: "#FFFFFF", borderRadius: "8px",
              textDecoration: "none", fontWeight: 700, fontSize: "0.875rem",
              boxShadow: "0 4px 14px rgba(249,115,22,0.35)",
              transition: "all 0.2s",
            }}>
              <Upload size={15} /> Begin Ingestion
            </Link>
            <Link href="/dag" style={{
              display: "inline-flex", alignItems: "center", gap: "0.5rem",
              padding: "0.75rem 1.5rem",
              background: "rgba(255,255,255,0.9)",
              color: "#0F172A", borderRadius: "8px",
              textDecoration: "none", fontWeight: 600, fontSize: "0.875rem",
              border: "1px solid #E5E7EB",
              boxShadow: "0 1px 4px rgba(0,0,0,0.06)",
            }}>
              View DAG <ArrowRight size={14} />
            </Link>
          </div>
        </div>

        {/* Bottom feature tiles */}
        <div style={{ pointerEvents: "auto" }}>
          <div style={{
            display: "grid", gridTemplateColumns: "repeat(6, 1fr)",
            gap: "0.75rem", maxWidth: "860px",
          }}>
            {features.map(({ href, icon: Icon, label, desc, color }) => (
              <Link key={href} href={href} style={{
                display: "flex", flexDirection: "column", gap: "0.45rem",
                padding: "0.875rem",
                background: "rgba(255,255,255,0.92)",
                backdropFilter: "blur(12px)",
                border: "1px solid #E5E7EB",
                borderRadius: "10px", textDecoration: "none",
                boxShadow: "0 2px 8px rgba(0,0,0,0.06)",
              }}>
                <Icon size={17} color={color} />
                <div style={{ fontSize: "0.72rem", fontWeight: 700, color: "#1F2937", lineHeight: 1.2 }}>{label}</div>
                <div style={{ fontSize: "0.63rem", color: "#9CA3AF" }}>{desc}</div>
              </Link>
            ))}
          </div>
        </div>
      </div>

      <style>{`
        @keyframes spin { to { transform: rotate(360deg); } }
      `}</style>
    </>
  );
}
