"use client";

import { useState } from "react";
import { useAegisStore } from "@/lib/store";
import { formatDate, getRiskColor } from "@/lib/utils";
import { ClipboardList, Search, TrendingUp, AlertTriangle, Clock, ThumbsUp, ChevronUp, ChevronDown } from "lucide-react";
import type { Obligation, RiskLevel, ObligationStatus } from "@/lib/store";
import HITLModal from "@/components/hitl/CorrectionModal";
import PageLayout from "@/components/shared/PageLayout";

const RISK_COLORS: Record<RiskLevel, { text: string; bg: string; border: string }> = {
  CRITICAL: { text: "#EF4444", bg: "#FEF2F2", border: "#FECACA" },
  HIGH:     { text: "#F97316", bg: "#FFF7ED", border: "#FED7AA" },
  MEDIUM:   { text: "#2563EB", bg: "#EFF6FF", border: "#BFDBFE" },
  LOW:      { text: "#14B8A6", bg: "#F0FDF4", border: "#99F6E4" },
};

const STATUS_COLORS: Record<ObligationStatus, { text: string; bg: string }> = {
  PENDING:    { text: "#F97316", bg: "#FFF7ED" },
  APPROVED:   { text: "#14B8A6", bg: "#F0FDF4" },
  REJECTED:   { text: "#EF4444", bg: "#FEF2F2" },
  OVERRIDDEN: { text: "#2563EB", bg: "#EFF6FF" },
};

export default function ReviewPage() {
  const { obligations, updateObligationStatus } = useAegisStore();
  const [search, setSearch] = useState("");
  const [filterRisk, setFilterRisk] = useState("ALL");
  const [selected, setSelected] = useState<Obligation | null>(null);
  const [hitlObl, setHitlObl] = useState<Obligation | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const filtered = obligations
    .filter(o => {
      const q = search.toLowerCase();
      return (o.id.toLowerCase().includes(q) || o.regulation.toLowerCase().includes(q) || o.department.toLowerCase().includes(q))
        && (filterRisk === "ALL" || o.riskLevel === filterRisk);
    })
    .sort((a, b) => sortDir === "desc" ? b.riskScore - a.riskScore : a.riskScore - b.riskScore);

  const stats = {
    total: obligations.length,
    critical: obligations.filter(o => o.riskLevel === "CRITICAL").length,
    pending: obligations.filter(o => o.status === "PENDING").length,
    approved: obligations.filter(o => o.status === "APPROVED").length,
  };

  return (
    <PageLayout>
      <div style={{ maxWidth: "1300px" }}>
        {/* Header */}
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: "1.5rem" }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.5rem" }}>
              <div style={{ width: "34px", height: "34px", borderRadius: "8px", background: "#0F172A", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <ClipboardList size={16} color="#FFFFFF" />
              </div>
              <h1 style={{ fontSize: "1.3rem", fontWeight: 700, color: "#1F2937", fontFamily: "Poppins, sans-serif" }}>
                Pending MAP Review Dashboard
              </h1>
            </div>
            <p style={{ color: "#6B7280", fontSize: "0.875rem" }}>CCO inbox — review and approve AI-extracted obligations before ServiceNow sync.</p>
          </div>
          <div style={{ padding: "0.5rem 1rem", background: "#FFF7ED", border: "1px solid #FED7AA", borderRadius: "8px", color: "#F97316", fontSize: "0.8rem", fontWeight: 700 }}>
            {stats.pending} PENDING
          </div>
        </div>

        {/* Stats */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "1rem", marginBottom: "1.5rem" }}>
          {[
            { label: "Total Obligations", value: stats.total, icon: TrendingUp, color: "#0F172A" },
            { label: "Critical Risk", value: stats.critical, icon: AlertTriangle, color: "#EF4444" },
            { label: "Pending Review", value: stats.pending, icon: Clock, color: "#F97316" },
            { label: "Auto-Approved", value: stats.approved, icon: ThumbsUp, color: "#14B8A6" },
          ].map(({ label, value, icon: Icon, color }) => (
            <div key={label} style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "12px", padding: "1.25rem", boxShadow: "0 1px 4px rgba(0,0,0,0.04)" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
                <span style={{ fontSize: "0.75rem", color: "#6B7280" }}>{label}</span>
                <Icon size={16} color={color} />
              </div>
              <div style={{ fontSize: "1.75rem", fontWeight: 800, color, fontFamily: "Poppins, sans-serif" }}>{value}</div>
            </div>
          ))}
        </div>

        {/* Filters */}
        <div style={{ display: "flex", gap: "0.75rem", marginBottom: "1rem", alignItems: "center" }}>
          <div style={{ position: "relative", flex: 1 }}>
            <Search size={14} color="#9CA3AF" style={{ position: "absolute", left: "0.75rem", top: "50%", transform: "translateY(-50%)" }} />
            <input
              value={search} onChange={e => setSearch(e.target.value)}
              placeholder="Search obligations, regulations, departments..."
              style={{
                width: "100%", padding: "0.6rem 0.75rem 0.6rem 2.25rem",
                background: "#FFFFFF", border: "1px solid #E5E7EB",
                borderRadius: "8px", color: "#1F2937", fontSize: "0.85rem", outline: "none",
                boxShadow: "0 1px 2px rgba(0,0,0,0.04)",
              }}
            />
          </div>
          {["ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW"].map(r => (
            <button key={r} onClick={() => setFilterRisk(r)} style={{
              padding: "0.5rem 0.9rem", borderRadius: "6px", cursor: "pointer", fontSize: "0.75rem", fontWeight: 700,
              background: filterRisk === r ? (r === "CRITICAL" ? "#FEF2F2" : r === "HIGH" ? "#FFF7ED" : r === "MEDIUM" ? "#EFF6FF" : r === "LOW" ? "#F0FDF4" : "#0F172A") : "#F9FAFB",
              border: `1px solid ${filterRisk === r ? (r === "CRITICAL" ? "#FECACA" : r === "HIGH" ? "#FED7AA" : r === "MEDIUM" ? "#BFDBFE" : r === "LOW" ? "#99F6E4" : "#0F172A") : "#E5E7EB"}`,
              color: filterRisk === r ? (r === "ALL" ? "#FFFFFF" : r === "CRITICAL" ? "#EF4444" : r === "HIGH" ? "#F97316" : r === "MEDIUM" ? "#2563EB" : "#14B8A6") : "#6B7280",
            }}>{r}</button>
          ))}
        </div>

        {/* Main grid */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 360px", gap: "1.25rem" }}>
          {/* Table */}
          <div style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "12px", overflow: "hidden", boxShadow: "0 2px 8px rgba(0,0,0,0.04)" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ borderBottom: "1px solid #F3F4F6", background: "#F9FAFB" }}>
                  {["ID", "Regulation", "Department", "Risk", "Confidence", "Status", "Actions"].map(h => (
                    <th key={h} style={{ padding: "0.75rem 1rem", textAlign: "left", fontSize: "0.7rem", color: "#9CA3AF", letterSpacing: "0.06em", fontWeight: 700 }}>
                      {h === "Risk" ? (
                        <button onClick={() => setSortDir(d => d === "desc" ? "asc" : "desc")} style={{ display: "flex", alignItems: "center", gap: "0.25rem", background: "none", border: "none", cursor: "pointer", color: "#9CA3AF", fontSize: "0.7rem", fontWeight: 700 }}>
                          {h} {sortDir === "desc" ? <ChevronDown size={10} /> : <ChevronUp size={10} />}
                        </button>
                      ) : h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.map(obl => {
                  const rc = RISK_COLORS[obl.riskLevel];
                  const sc = STATUS_COLORS[obl.status];
                  return (
                    <tr key={obl.id} onClick={() => setSelected(obl)} style={{
                      borderBottom: "1px solid #F9FAFB", cursor: "pointer",
                      background: selected?.id === obl.id ? "#FFFBF5" : "#FFFFFF",
                      transition: "background 0.1s",
                    }}>
                      <td style={{ padding: "0.875rem 1rem" }}>
                        <span style={{ fontFamily: "JetBrains Mono", fontSize: "0.72rem", color: "#0F172A", fontWeight: 700 }}>{obl.id}</span>
                      </td>
                      <td style={{ padding: "0.875rem 1rem", maxWidth: "160px" }}>
                        <div style={{ fontSize: "0.8rem", color: "#1F2937", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{obl.regulation}</div>
                        <div style={{ fontSize: "0.7rem", color: "#9CA3AF" }}>{obl.jurisdiction}</div>
                      </td>
                      <td style={{ padding: "0.875rem 1rem" }}>
                        <span style={{ fontSize: "0.78rem", color: "#4B5563" }}>{obl.department}</span>
                      </td>
                      <td style={{ padding: "0.875rem 1rem" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                          <span style={{ padding: "0.2rem 0.6rem", borderRadius: "100px", fontSize: "0.65rem", fontWeight: 700, background: rc.bg, border: `1px solid ${rc.border}`, color: rc.text }}>
                            {obl.riskLevel}
                          </span>
                          <span style={{ fontSize: "0.72rem", color: "#9CA3AF", fontFamily: "JetBrains Mono" }}>{obl.riskScore}</span>
                        </div>
                      </td>
                      <td style={{ padding: "0.875rem 1rem" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                          <div style={{ flex: 1, height: "4px", background: "#F3F4F6", borderRadius: "2px", width: "52px" }}>
                            <div style={{ height: "100%", width: `${obl.confidence * 100}%`, background: "#F97316", borderRadius: "2px" }} />
                          </div>
                          <span style={{ fontSize: "0.72rem", color: "#6B7280", fontFamily: "JetBrains Mono" }}>{(obl.confidence * 100).toFixed(0)}%</span>
                        </div>
                      </td>
                      <td style={{ padding: "0.875rem 1rem" }}>
                        <span style={{ padding: "0.2rem 0.6rem", borderRadius: "100px", fontSize: "0.65rem", fontWeight: 700, background: sc.bg, color: sc.text }}>
                          {obl.status}
                        </span>
                      </td>
                      <td style={{ padding: "0.875rem 1rem" }}>
                        <div style={{ display: "flex", gap: "0.3rem" }}>
                          {obl.status === "PENDING" && (
                            <>
                              <Btn label="Approve" bg="#F0FDF4" border="#BBF7D0" color="#14B8A6" onClick={e => { e.stopPropagation(); updateObligationStatus(obl.id, "APPROVED"); }} />
                              <Btn label="Reject" bg="#FEF2F2" border="#FECACA" color="#EF4444" onClick={e => { e.stopPropagation(); updateObligationStatus(obl.id, "REJECTED"); }} />
                            </>
                          )}
                          <Btn label="Override" bg="#FFF7ED" border="#FED7AA" color="#F97316" onClick={e => { e.stopPropagation(); setHitlObl(obl); }} />
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Detail panel */}
          <div style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "12px", padding: "1.25rem", boxShadow: "0 2px 8px rgba(0,0,0,0.04)" }}>
            {selected ? (
              <>
                <div style={{ marginBottom: "1rem" }}>
                  <div style={{ fontFamily: "JetBrains Mono", fontSize: "0.72rem", color: "#F97316", fontWeight: 700, marginBottom: "0.5rem" }}>{selected.id}</div>
                  <div style={{ fontSize: "1rem", fontWeight: 700, color: "#1F2937", fontFamily: "Poppins, sans-serif", marginBottom: "0.75rem", lineHeight: 1.3 }}>{selected.regulation}</div>
                  {(() => { const rc = RISK_COLORS[selected.riskLevel]; return <span style={{ padding: "0.25rem 0.75rem", borderRadius: "100px", fontSize: "0.72rem", fontWeight: 700, background: rc.bg, border: `1px solid ${rc.border}`, color: rc.text }}>{selected.riskLevel} · {selected.riskScore}</span>; })()}
                </div>
                <div style={{ height: "1px", background: "#F3F4F6", margin: "1rem 0" }} />
                {[
                  { label: "Department", value: selected.department },
                  { label: "Jurisdiction", value: selected.jurisdiction },
                  { label: "Due Date", value: formatDate(selected.dueDate) },
                  { label: "Confidence", value: `${(selected.confidence * 100).toFixed(0)}%` },
                  { label: "Assigned By", value: selected.assignedBy },
                ].map(({ label, value }) => (
                  <div key={label} style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.6rem" }}>
                    <span style={{ fontSize: "0.75rem", color: "#9CA3AF" }}>{label}</span>
                    <span style={{ fontSize: "0.78rem", color: "#1F2937", fontWeight: 500, textAlign: "right", maxWidth: "60%" }}>{value}</span>
                  </div>
                ))}
                <div style={{ height: "1px", background: "#F3F4F6", margin: "1rem 0" }} />
                <div style={{ fontSize: "0.68rem", color: "#9CA3AF", letterSpacing: "0.06em", fontWeight: 700, marginBottom: "0.5rem" }}>OBLIGATION TEXT</div>
                <p style={{ fontSize: "0.82rem", color: "#4B5563", lineHeight: 1.7 }}>{selected.description}</p>
              </>
            ) : (
              <div style={{ textAlign: "center", color: "#D1D5DB", marginTop: "4rem" }}>
                <ClipboardList size={32} color="#E5E7EB" style={{ margin: "0 auto 1rem" }} />
                <div style={{ fontSize: "0.875rem", color: "#9CA3AF" }}>Select an obligation to view details</div>
              </div>
            )}
          </div>
        </div>
      </div>
      {hitlObl && <HITLModal obligation={hitlObl} onClose={() => setHitlObl(null)} />}
    </PageLayout>
  );
}

function Btn({ label, bg, border, color, onClick }: { label: string; bg: string; border: string; color: string; onClick: (e: React.MouseEvent) => void }) {
  return (
    <button onClick={onClick} style={{ padding: "0.3rem 0.6rem", borderRadius: "5px", cursor: "pointer", background: bg, border: `1px solid ${border}`, color, fontSize: "0.68rem", fontWeight: 700 }}>
      {label}
    </button>
  );
}
