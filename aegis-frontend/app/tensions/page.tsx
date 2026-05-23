"use client";

import { useState, useEffect } from "react";
import { useAegisStore } from "@/lib/store";
import type { PolicyTension } from "@/lib/store";
import { AlertTriangle, ChevronDown, ChevronUp, CheckCircle, Shield, BookOpen } from "lucide-react";
import PageLayout from "@/components/shared/PageLayout";
import { updateTension } from "@/lib/api";

const SEVERITY_CONFIG: Record<string, { text: string; bg: string; border: string }> = {
  CRITICAL: { text: "#EF4444", bg: "#FEF2F2", border: "#FECACA" },
  HIGH:     { text: "#F97316", bg: "#FFF7ED", border: "#FED7AA" },
  MEDIUM:   { text: "#2563EB", bg: "#EFF6FF", border: "#BFDBFE" },
  LOW:      { text: "#14B8A6", bg: "#F0FDF4", border: "#99F6E4" },
};

const STATUS_CONFIG: Record<string, { label: string; text: string; bg: string }> = {
  UNRESOLVED:      { label: "UNRESOLVED",      text: "#EF4444", bg: "#FEF2F2" },
  ACKNOWLEDGED:    { label: "ACKNOWLEDGED",    text: "#F97316", bg: "#FFF7ED" },
  CONTROL_APPLIED: { label: "CONTROL APPLIED", text: "#14B8A6", bg: "#F0FDF4" },
  REVIEWED:        { label: "ACKNOWLEDGED",    text: "#F97316", bg: "#FFF7ED" },
  DISMISSED:       { label: "CONTROL APPLIED", text: "#14B8A6", bg: "#F0FDF4" },
};

export default function TensionsPage() {
  const { tensions, loadTensions } = useAegisStore();
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const fetchAll = async () => {
      try {
        setErrorMsg(null);
        await loadTensions();
      } catch (err) {
        if (active) {
          setErrorMsg("Could not connect to the API. Make sure the FastAPI backend is running at http://localhost:8000.");
        }
      } finally {
        if (active) setLoading(false);
      }
    };
    fetchAll();
    return () => { active = false; };
  }, [loadTensions]);

  const toggle = (id: string) =>
    setExpandedIds(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  const handleAcknowledge = async (id: string) => {
    try {
      await updateTension(id, "REVIEWED");
      await loadTensions();
    } catch (err) {
      console.error("Failed to acknowledge tension:", err);
      alert(err instanceof Error ? err.message : "Failed to update tension status.");
    }
  };

  const handleApplyControl = async (id: string) => {
    try {
      await updateTension(id, "DISMISSED");
      await loadTensions();
    } catch (err) {
      console.error("Failed to apply compensating control:", err);
      alert(err instanceof Error ? err.message : "Failed to update tension status.");
    }
  };

  const counts = {
    total: tensions.length,
    critical: tensions.filter(t => t.severity === "CRITICAL").length,
    unresolved: tensions.filter(t => t.status === "UNRESOLVED" || t.status === "OPEN").length,
    controlled: tensions.filter(t => t.status === "CONTROL_APPLIED" || t.status === "DISMISSED").length,
  };

  return (
    <PageLayout>
      <div style={{ maxWidth: "1000px" }}>
        {/* Header */}
        <div style={{ marginBottom: "1.5rem" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.5rem" }}>
            <div style={{ width: "34px", height: "34px", borderRadius: "8px", background: "#EF4444", display: "flex", alignItems: "center", justifyContent: "center" }}>
              <AlertTriangle size={16} color="#FFFFFF" />
            </div>
            <h1 style={{ fontSize: "1.3rem", fontWeight: 700, color: "#1F2937", fontFamily: "Poppins, sans-serif" }}>
              Policy Tension & Compensating Controls
            </h1>
          </div>
          <p style={{ color: "#6B7280", fontSize: "0.875rem" }}>
            Semantic contradictions between conflicting regulations, with AI-generated compensating controls.
          </p>
        </div>

        {/* Stats */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "1rem", marginBottom: "1.5rem" }}>
          {[
            { label: "Total Tensions", value: counts.total, color: "#0F172A" },
            { label: "Critical", value: counts.critical, color: "#EF4444" },
            { label: "Unresolved", value: counts.unresolved, color: "#F97316" },
            { label: "Controls Applied", value: counts.controlled, color: "#14B8A6" },
          ].map(({ label, value, color }) => (
            <div key={label} style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "12px", padding: "1.25rem", boxShadow: "0 1px 4px rgba(0,0,0,0.04)", transition: "all 0.2s" }}>
              <div style={{ fontSize: "0.72rem", color: "#9CA3AF", marginBottom: "0.5rem", fontWeight: 600 }}>{label}</div>
              <div style={{ fontSize: "1.75rem", fontWeight: 800, color, fontFamily: "Poppins, sans-serif" }}>{value}</div>
            </div>
          ))}
        </div>

        {/* Tension Cards */}
        {loading ? (
          <div style={{ padding: "4rem 2rem", textAlign: "center", color: "#6B7280", background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "12px" }}>
            <div style={{ width: "32px", height: "32px", borderRadius: "50%", border: "2.5px solid #E5E7EB", borderTopColor: "#EF4444", animation: "spin 1s linear infinite", margin: "0 auto 1.25rem" }} />
            <div style={{ fontSize: "0.875rem", fontWeight: 600, color: "#4B5563" }}>Loading policy tensions from WORM ledger...</div>
          </div>
        ) : errorMsg ? (
          <div style={{ padding: "3rem 2rem", textAlign: "center", background: "#FFF5F5", border: "1px solid #FEE2E2", borderRadius: "14px", boxShadow: "0 4px 12px rgba(239,68,68,0.05)" }}>
            <AlertTriangle size={36} color="#EF4444" style={{ margin: "0 auto 1.25rem" }} />
            <div style={{ fontSize: "1rem", fontWeight: 700, color: "#991B1B", marginBottom: "0.5rem" }}>System Connection Error</div>
            <div style={{ fontSize: "0.85rem", color: "#B91C1C", marginBottom: "1.5rem", maxWidth: "480px", marginInline: "auto", lineHeight: 1.5 }}>{errorMsg}</div>
            <button
              onClick={async () => {
                setLoading(true);
                setErrorMsg(null);
                try {
                  await loadTensions();
                } catch {
                  setErrorMsg("Could not connect to the API. Make sure the FastAPI backend is running at http://localhost:8000.");
                } finally {
                  setLoading(false);
                }
              }}
              style={{ padding: "0.65rem 1.5rem", background: "#EF4444", border: "none", borderRadius: "8px", color: "#FFFFFF", cursor: "pointer", fontSize: "0.8rem", fontWeight: 700, boxShadow: "0 4px 12px rgba(239,68,68,0.2)" }}
            >
              Retry Connection
            </button>
          </div>
        ) : tensions.length === 0 ? (
          <div style={{ padding: "4rem 2rem", textAlign: "center", background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "12px", boxShadow: "0 2px 6px rgba(0,0,0,0.02)" }}>
            <CheckCircle size={36} color="#14B8A6" style={{ margin: "0 auto 1.25rem" }} />
            <div style={{ fontSize: "1rem", fontWeight: 700, color: "#1F2937", marginBottom: "0.5rem" }}>No Policy Tensions Detected</div>
            <div style={{ fontSize: "0.85rem", color: "#6B7280", maxWidth: "420px", marginInline: "auto", lineHeight: 1.5 }}>
              All regulatory requirements are fully aligned. No semantic contradictions exist in the current mapped corpus.
            </div>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
            {tensions.map(tension => (
              <TensionCard
                key={tension.id}
                tension={tension}
                expanded={expandedIds.has(tension.id)}
                onToggle={() => toggle(tension.id)}
                onAcknowledge={() => handleAcknowledge(tension.id)}
                onApplyControl={() => handleApplyControl(tension.id)}
              />
            ))}
          </div>
        )}
      </div>
      <style>{`@keyframes spin { to { transform: rotate(360deg); }}`}</style>
    </PageLayout>
  );
}

function TensionCard({ tension, expanded, onToggle, onAcknowledge, onApplyControl }: {
  tension: PolicyTension; expanded: boolean; onToggle: () => void; onAcknowledge: () => void; onApplyControl: () => void;
}) {
  const sc = SEVERITY_CONFIG[tension.severity] || SEVERITY_CONFIG.MEDIUM;
  const stc = STATUS_CONFIG[tension.status] || { label: tension.status, text: "#6B7280", bg: "#F3F4F6" };

  return (
    <div style={{
      background: "#FFFFFF", borderRadius: "14px", overflow: "hidden",
      border: `1px solid ${tension.status === "UNRESOLVED" || tension.status === "OPEN" ? sc.border : "#E5E7EB"}`,
      boxShadow: tension.status === "UNRESOLVED" || tension.status === "OPEN" ? `0 2px 12px ${sc.text}18` : "0 2px 8px rgba(0,0,0,0.04)",
      transition: "all 0.3s ease",
    }}>
      {/* Header */}
      <div style={{ padding: "1.25rem 1.5rem", cursor: "pointer", display: "flex", alignItems: "center", gap: "1rem" }} onClick={onToggle}>
        <div style={{ width: "36px", height: "36px", borderRadius: "9px", flexShrink: 0, background: sc.bg, border: `1px solid ${sc.border}`, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <AlertTriangle size={16} color={sc.text} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", marginBottom: "0.4rem", flexWrap: "wrap" }}>
            <span style={{ fontSize: "0.95rem", fontWeight: 700, color: "#1F2937", fontFamily: "Poppins, sans-serif" }}>{tension.title}</span>
            <span style={{ padding: "0.2rem 0.7rem", borderRadius: "100px", fontSize: "0.65rem", fontWeight: 700, background: sc.bg, border: `1px solid ${sc.border}`, color: sc.text }}>{tension.severity}</span>
            <span style={{ padding: "0.2rem 0.7rem", borderRadius: "100px", fontSize: "0.65rem", fontWeight: 700, background: stc.bg, color: stc.text }}>{stc.label}</span>
          </div>
          <div style={{ display: "flex", gap: "0.4rem" }}>
            <span style={{ fontSize: "0.73rem", color: "#6B7280", background: "#F9FAFB", border: "1px solid #E5E7EB", padding: "0.15rem 0.5rem", borderRadius: "4px" }}>
              {tension.regulationA.name.split(" ").slice(0, 3).join(" ")}...
            </span>
            <span style={{ color: "#9CA3AF", fontSize: "0.73rem" }}>vs</span>
            <span style={{ fontSize: "0.73rem", color: "#6B7280", background: "#F9FAFB", border: "1px solid #E5E7EB", padding: "0.15rem 0.5rem", borderRadius: "4px" }}>
              {tension.regulationB.name.split(" ").slice(0, 3).join(" ")}...
            </span>
          </div>
        </div>
        <span style={{ color: "#9CA3AF", flexShrink: 0 }}>
          {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </span>
      </div>

      {/* Expanded */}
      {expanded && (
        <div style={{ borderTop: "1px solid #F3F4F6", padding: "1.25rem 1.5rem" }}>
          {/* Regulation excerpts */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem", marginBottom: "1.25rem" }}>
            {[tension.regulationA, tension.regulationB].map((reg, i) => (
              <div key={i} style={{ background: "#F9FAFB", border: "1px solid #E5E7EB", borderRadius: "10px", padding: "1rem" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.6rem" }}>
                  <BookOpen size={13} color="#9CA3AF" />
                  <span style={{ fontSize: "0.72rem", color: "#6B7280", lineHeight: 1.3 }}>{reg.name}</span>
                </div>
                <p style={{ fontSize: "0.78rem", color: "#4B5563", lineHeight: 1.65, fontStyle: "italic" }}>"{reg.excerpt}"</p>
              </div>
            ))}
          </div>

          {/* Contradiction — frosted glass panel */}
          <div style={{
            background: `${sc.bg}`,
            border: `1px solid ${sc.border}`,
            borderLeft: `4px solid ${sc.text}`,
            borderRadius: "10px", padding: "1rem", marginBottom: "1.25rem",
          }}>
            <div style={{ fontSize: "0.7rem", color: sc.text, letterSpacing: "0.08em", fontWeight: 700, marginBottom: "0.5rem" }}>
              ⚡ Semantic Contradiction
            </div>
            <p style={{ fontSize: "0.82rem", color: "#374151", lineHeight: 1.7 }}>{tension.contradiction}</p>
          </div>

          {/* Compensating control */}
          <div style={{ background: "#F0FDF4", border: "1px solid #BBF7D0", borderLeft: "4px solid #14B8A6", borderRadius: "10px", padding: "1rem", marginBottom: "1.25rem" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.5rem" }}>
              <Shield size={13} color="#14B8A6" />
              <span style={{ fontSize: "0.7rem", color: "#14B8A6", letterSpacing: "0.08em", fontWeight: 700 }}>AI COMPENSATING CONTROL</span>
            </div>
            <p style={{ fontSize: "0.82rem", color: "#065F46", lineHeight: 1.7 }}>{tension.compensatingControl}</p>
          </div>

          {/* Actions */}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.75rem" }}>
            {(tension.status === "UNRESOLVED" || tension.status === "OPEN") && (
              <button onClick={onAcknowledge} style={{
                display: "flex", alignItems: "center", gap: "0.5rem",
                padding: "0.6rem 1.25rem", background: "#FFF7ED", border: "1px solid #FED7AA",
                borderRadius: "8px", color: "#F97316", cursor: "pointer", fontSize: "0.8rem", fontWeight: 700,
                transition: "all 0.2s",
              }}>
                <CheckCircle size={13} /> Acknowledge Tension
              </button>
            )}
            {tension.status !== "CONTROL_APPLIED" && tension.status !== "DISMISSED" && (
              <button onClick={onApplyControl} style={{
                display: "flex", alignItems: "center", gap: "0.5rem",
                padding: "0.6rem 1.25rem", background: "#0F172A", border: "none",
                borderRadius: "8px", color: "#FFFFFF", cursor: "pointer", fontSize: "0.8rem", fontWeight: 700,
                transition: "all 0.2s",
              }}>
                <Shield size={13} /> Apply Control
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
