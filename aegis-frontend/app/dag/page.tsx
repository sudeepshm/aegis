"use client";

import dynamic from "next/dynamic";
import { useAegisStore } from "@/lib/store";
import { GitBranch, Zap, RotateCcw } from "lucide-react";
import PageLayout from "@/components/shared/PageLayout";

const DagCanvas = dynamic(() => import("@/components/dag/DagCanvas"), {
  ssr: false,
  loading: () => (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#9CA3AF", fontSize: "0.8rem" }}>
      <div style={{ textAlign: "center" }}>
        <div style={{ width: "28px", height: "28px", borderRadius: "50%", border: "2px solid #E5E7EB", borderTopColor: "#F97316", animation: "spin 0.8s linear infinite", margin: "0 auto 0.75rem" }} />
        Initializing DAG renderer...
      </div>
    </div>
  ),
});

export default function DagPage() {
  const { blastRadiusActive, blastSourceId, triggerBlastRadius, resetBlastRadius, dagNodes } = useAegisStore();

  const criticalNodes = (dagNodes as Array<{ id: string; data: { riskLevel: string; label: string } }>)
    .filter(n => n.data.riskLevel === "CRITICAL" || n.data.riskLevel === "HIGH");

  return (
    <PageLayout>
      <div style={{ display: "flex", flexDirection: "column", height: "calc(100vh - 4rem)" }}>
        {/* Header */}
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: "1rem", flexShrink: 0 }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.5rem" }}>
              <div style={{ width: "34px", height: "34px", borderRadius: "8px", background: "#0F172A", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <GitBranch size={16} color="#FFFFFF" />
              </div>
              <h1 style={{ fontSize: "1.3rem", fontWeight: 700, color: "#1F2937", fontFamily: "Poppins, sans-serif" }}>Causal DAG Execution Viewer</h1>
            </div>
            <p style={{ color: "#6B7280", fontSize: "0.875rem" }}>
              Parent-child task dependencies. Use the Blast Radius Simulator to visualize cascading SLA breaches.
            </p>
          </div>
          <div style={{ display: "flex", gap: "0.75rem", alignItems: "center" }}>
            {blastRadiusActive && (
              <button onClick={resetBlastRadius} style={{ display: "flex", alignItems: "center", gap: "0.4rem", padding: "0.5rem 1rem", background: "#F9FAFB", border: "1px solid #E5E7EB", borderRadius: "8px", color: "#6B7280", cursor: "pointer", fontSize: "0.8rem" }}>
                <RotateCcw size={12} /> Reset
              </button>
            )}
            <div style={{
              padding: "0.4rem 0.875rem",
              background: blastRadiusActive ? "#FEF2F2" : "#F9FAFB",
              border: `1px solid ${blastRadiusActive ? "#FECACA" : "#E5E7EB"}`,
              borderRadius: "8px", display: "flex", alignItems: "center", gap: "0.5rem",
            }}>
              <Zap size={13} color={blastRadiusActive ? "#EF4444" : "#9CA3AF"} />
              <span style={{ fontSize: "0.75rem", color: blastRadiusActive ? "#EF4444" : "#9CA3AF", fontWeight: 700 }}>
                {blastRadiusActive ? `BLAST RADIUS ACTIVE` : "BLAST SIMULATOR OFF"}
              </span>
            </div>
          </div>
        </div>

        {/* Main layout */}
        <div style={{ display: "grid", gridTemplateColumns: "200px 1fr", gap: "1rem", flex: 1, minHeight: 0 }}>
          {/* Left panel */}
          <div style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "12px", padding: "1rem", overflow: "auto", boxShadow: "0 2px 6px rgba(0,0,0,0.04)" }}>
            <div style={{ fontSize: "0.65rem", color: "#9CA3AF", letterSpacing: "0.08em", fontWeight: 700, marginBottom: "0.75rem" }}>
              BLAST RADIUS SIMULATOR
            </div>
            <p style={{ fontSize: "0.72rem", color: "#6B7280", marginBottom: "0.75rem", lineHeight: 1.5 }}>
              Select a node to simulate a delay and see cascading SLA breaches:
            </p>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
              {criticalNodes.map(n => (
                <button
                  key={n.id}
                  onClick={() => blastSourceId === n.id ? resetBlastRadius() : triggerBlastRadius(n.id)}
                  style={{
                    padding: "0.5rem 0.6rem", borderRadius: "7px", cursor: "pointer",
                    textAlign: "left", fontSize: "0.72rem",
                    background: blastSourceId === n.id ? "#FEF2F2" : "#F9FAFB",
                    border: `1px solid ${blastSourceId === n.id ? "#FECACA" : "#E5E7EB"}`,
                    color: blastSourceId === n.id ? "#EF4444" : "#4B5563",
                    fontWeight: blastSourceId === n.id ? 700 : 400,
                  }}
                >
                  <Zap size={10} style={{ marginRight: "0.4rem", display: "inline", opacity: 0.6 }} />
                  {n.data.label}
                </button>
              ))}
            </div>
            {blastRadiusActive && (
              <div style={{ marginTop: "1rem", padding: "0.75rem", background: "#FEF2F2", border: "1px solid #FECACA", borderRadius: "8px" }}>
                <div style={{ fontSize: "0.65rem", color: "#EF4444", fontWeight: 700, marginBottom: "0.4rem" }}>CASCADE IMPACT</div>
                <div style={{ fontSize: "0.72rem", color: "#B91C1C" }}>
                  Multiple downstream tasks breached.<br />Estimated delay: 14+ days
                </div>
              </div>
            )}
          </div>

          {/* DAG canvas */}
          <div style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "12px", overflow: "hidden", position: "relative", boxShadow: "0 2px 8px rgba(0,0,0,0.04)" }}>
            <DagCanvas />
          </div>
        </div>
      </div>
      <style>{`@keyframes spin { to { transform: rotate(360deg); }}`}</style>
    </PageLayout>
  );
}
