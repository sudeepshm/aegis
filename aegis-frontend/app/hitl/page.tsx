"use client";

import { useState } from "react";
import { useAegisStore } from "@/lib/store";
import type { Obligation } from "@/lib/store";
import { Activity, History } from "lucide-react";
import CorrectionModal from "@/components/hitl/CorrectionModal";
import PageLayout from "@/components/shared/PageLayout";

export default function HITLPage() {
  const { obligations, overrideLog } = useAegisStore();
  const [selectedObl, setSelectedObl] = useState<Obligation | null>(null);
  const pending = obligations.filter(o => o.status === "PENDING");

  return (
    <PageLayout>
      <div style={{ maxWidth: "1000px" }}>
        {/* Header */}
        <div style={{ marginBottom: "1.5rem" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.5rem" }}>
            <div style={{ width: "34px", height: "34px", borderRadius: "8px", background: "#F97316", display: "flex", alignItems: "center", justifyContent: "center" }}>
              <Activity size={16} color="#FFFFFF" />
            </div>
            <h1 style={{ fontSize: "1.3rem", fontWeight: 700, color: "#1F2937", fontFamily: "Poppins, sans-serif" }}>Human-in-the-Loop Corrections</h1>
          </div>
          <p style={{ color: "#6B7280", fontSize: "0.875rem" }}>
            Override AI routing decisions. All overrides are cryptographically signed and recorded in the immutable WORM audit ledger.
          </p>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1.5rem" }}>
          {/* Pending */}
          <div>
            <div style={{ fontSize: "0.72rem", color: "#9CA3AF", letterSpacing: "0.08em", fontWeight: 700, marginBottom: "0.75rem" }}>
              OBLIGATIONS AWAITING REVIEW ({pending.length})
            </div>
            {pending.length === 0 ? (
              <div style={{ textAlign: "center", padding: "2rem", color: "#9CA3AF", fontSize: "0.875rem", background: "#F9FAFB", border: "1px solid #E5E7EB", borderRadius: "10px" }}>
                No pending obligations. Upload a document to begin.
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                {pending.map(obl => (
                  <div key={obl.id} style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "10px", padding: "1rem", boxShadow: "0 1px 4px rgba(0,0,0,0.04)" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "0.5rem" }}>
                      <div>
                        <span style={{ fontFamily: "JetBrains Mono", fontSize: "0.72rem", color: "#F97316", fontWeight: 700 }}>{obl.id}</span>
                        <div style={{ fontSize: "0.85rem", fontWeight: 600, color: "#1F2937", marginTop: "0.2rem" }}>{obl.regulation}</div>
                      </div>
                      <span style={{ padding: "0.2rem 0.6rem", borderRadius: "100px", fontSize: "0.65rem", fontWeight: 700, background: "#FFF7ED", color: "#F97316" }}>PENDING</span>
                    </div>
                    <div style={{ fontSize: "0.78rem", color: "#6B7280", marginBottom: "0.75rem" }}>
                      Routed to: <span style={{ color: "#1F2937", fontWeight: 500 }}>{obl.department}</span>
                    </div>
                    <button
                      onClick={() => setSelectedObl(obl)}
                      style={{
                        width: "100%", padding: "0.5rem",
                        background: "#F97316", border: "none",
                        borderRadius: "7px", color: "#FFFFFF", cursor: "pointer",
                        fontSize: "0.8rem", fontWeight: 700,
                        boxShadow: "0 2px 8px rgba(249,115,22,0.25)",
                      }}
                    >
                      ↩ Override AI Decision
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* WORM Ledger */}
          <div>
            <div style={{ fontSize: "0.72rem", color: "#9CA3AF", letterSpacing: "0.08em", fontWeight: 700, marginBottom: "0.75rem", display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <History size={12} /> WORM AUDIT LEDGER ({overrideLog.length} ENTRIES)
            </div>
            <div style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "12px", overflow: "hidden", boxShadow: "0 2px 8px rgba(0,0,0,0.04)" }}>
              <div style={{ padding: "0.875rem 1.25rem", borderBottom: "1px solid #F3F4F6", background: "#F9FAFB", display: "flex", alignItems: "center", gap: "0.5rem" }}>
                <div style={{ width: "8px", height: "8px", borderRadius: "50%", background: overrideLog.length > 0 ? "#14B8A6" : "#D1D5DB" }} />
                <span style={{ fontSize: "0.72rem", color: "#6B7280", fontWeight: 600 }}>
                  {overrideLog.length > 0 ? `${overrideLog.length} override(s) committed` : "No overrides recorded"}
                </span>
              </div>
              <div style={{ height: "380px", overflowY: "auto", padding: "1rem" }}>
                {overrideLog.length === 0 ? (
                  <div style={{ textAlign: "center", color: "#9CA3AF", marginTop: "4rem" }}>
                    <History size={28} color="#E5E7EB" style={{ margin: "0 auto 0.75rem" }} />
                    <div style={{ fontSize: "0.82rem" }}>No HITL overrides recorded yet.</div>
                  </div>
                ) : overrideLog.map((entry, i) => (
                  <div key={i} style={{ marginBottom: "1rem", padding: "0.875rem", background: "#F9FAFB", border: "1px solid #E5E7EB", borderRadius: "8px" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.5rem" }}>
                      <span style={{ fontFamily: "JetBrains Mono", fontSize: "0.7rem", color: "#F97316", fontWeight: 700 }}>{entry.obligationId}</span>
                      <span style={{ fontSize: "0.68rem", color: "#9CA3AF" }}>{entry.timestamp.slice(0, 19).replace("T", " ")}</span>
                    </div>
                    <div style={{ fontSize: "0.78rem", color: "#4B5563", marginBottom: "0.25rem" }}>
                      <span style={{ color: "#9CA3AF" }}>From:</span> {entry.originalDepartment} → <span style={{ color: "#0F172A", fontWeight: 600 }}>{entry.newDepartment}</span>
                    </div>
                    <div style={{ fontSize: "0.75rem", color: "#6B7280", marginBottom: "0.4rem" }}>{entry.reason}</div>
                    <div style={{ fontFamily: "JetBrains Mono", fontSize: "0.6rem", color: "#14B8A6", wordBreak: "break-all" }}>{entry.wormHash.slice(0, 40)}...</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
      {selectedObl && <CorrectionModal obligation={selectedObl} onClose={() => setSelectedObl(null)} />}
    </PageLayout>
  );
}
