"use client";

import { useState } from "react";
import { useAegisStore } from "@/lib/store";
import { generateWormHash } from "@/lib/utils";
import type { Obligation } from "@/lib/store";
import { X, AlertTriangle, CheckCircle, Database, Loader2 } from "lucide-react";

const DEPARTMENTS = [
  "Treasury & Finance", "Data Privacy & IT", "Legal & Compliance",
  "Information Security", "AML & Sanctions", "Risk Management",
  "Board Secretariat", "Operations & KYC", "Human Resources",
];

export default function CorrectionModal({ obligation, onClose }: { obligation: Obligation; onClose: () => void }) {
  const { applyHITLOverride } = useAegisStore();
  const [reason, setReason] = useState("");
  const [newDept, setNewDept] = useState(obligation.department);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [success, setSuccess] = useState(false);
  const [wormHash, setWormHash] = useState("");

  const handleSubmit = async () => {
    if (!reason.trim()) return;
    setIsSubmitting(true);
    await new Promise(r => setTimeout(r, 1800));
    const hash = generateWormHash();
    setWormHash(hash);
    applyHITLOverride({
      obligationId: obligation.id,
      originalDepartment: obligation.department,
      newDepartment: newDept,
      reason,
      timestamp: new Date().toISOString(),
      wormHash: hash,
    });
    setIsSubmitting(false);
    setSuccess(true);
  };

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 9999,
      background: "rgba(15,23,42,0.4)", backdropFilter: "blur(6px)",
      display: "flex", alignItems: "center", justifyContent: "center", padding: "1rem",
    }} onClick={e => e.target === e.currentTarget && onClose()}>
      <div style={{
        width: "100%", maxWidth: "540px",
        background: "#FFFFFF", borderRadius: "16px",
        border: "1px solid #E5E7EB",
        boxShadow: "0 25px 50px rgba(0,0,0,0.12)",
        overflow: "hidden",
      }}>
        {/* Header */}
        <div style={{
          padding: "1.25rem 1.5rem", borderBottom: "1px solid #F3F4F6",
          display: "flex", alignItems: "center", gap: "0.75rem",
          background: "#FFFBF5",
        }}>
          <div style={{ width: "32px", height: "32px", borderRadius: "8px", background: "#FFF7ED", border: "1px solid #FED7AA", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
            <AlertTriangle size={16} color="#F97316" />
          </div>
          <div>
            <div style={{ fontWeight: 700, color: "#1F2937", fontSize: "1rem", fontFamily: "Poppins, sans-serif" }}>Human-in-the-Loop Override</div>
            <div style={{ fontSize: "0.75rem", color: "#9CA3AF" }}>This action is recorded in the immutable WORM audit ledger</div>
          </div>
          <button onClick={onClose} style={{ marginLeft: "auto", background: "none", border: "none", cursor: "pointer", color: "#9CA3AF", padding: "0.25rem" }}>
            <X size={18} />
          </button>
        </div>

        {!success ? (
          <div style={{ padding: "1.5rem" }}>
            {/* Current AI decision */}
            <div style={{ background: "#F9FAFB", border: "1px solid #E5E7EB", borderRadius: "10px", padding: "1rem", marginBottom: "1.25rem" }}>
              <div style={{ fontSize: "0.68rem", color: "#9CA3AF", letterSpacing: "0.08em", fontWeight: 700, marginBottom: "0.75rem" }}>CURRENT AI DECISION</div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.75rem" }}>
                {[
                  { label: "Obligation ID", value: obligation.id },
                  { label: "Confidence", value: `${(obligation.confidence * 100).toFixed(0)}%` },
                  { label: "AI Routing", value: obligation.department },
                  { label: "Risk Level", value: obligation.riskLevel },
                ].map(({ label, value }) => (
                  <div key={label}>
                    <div style={{ fontSize: "0.65rem", color: "#9CA3AF", marginBottom: "0.2rem" }}>{label}</div>
                    <div style={{ fontSize: "0.82rem", color: "#1F2937", fontWeight: 600 }}>{value}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* New department */}
            <div style={{ marginBottom: "1.25rem" }}>
              <label style={{ display: "block", fontSize: "0.82rem", fontWeight: 600, color: "#1F2937", marginBottom: "0.5rem" }}>
                Override Department Assignment
              </label>
              <select
                value={newDept} onChange={e => setNewDept(e.target.value)}
                style={{
                  width: "100%", padding: "0.65rem 0.875rem",
                  background: "#FFFFFF", border: "1px solid #E5E7EB",
                  borderRadius: "8px", color: "#1F2937", fontSize: "0.875rem",
                  outline: "none", cursor: "pointer",
                }}
              >
                {DEPARTMENTS.map(d => <option key={d} value={d}>{d}</option>)}
              </select>
            </div>

            {/* Reason */}
            <div style={{ marginBottom: "1.5rem" }}>
              <label style={{ display: "block", fontSize: "0.82rem", fontWeight: 600, color: "#1F2937", marginBottom: "0.5rem" }}>
                Reason for Override <span style={{ color: "#EF4444" }}>*</span>
              </label>
              <textarea
                value={reason} onChange={e => setReason(e.target.value)}
                placeholder="Provide a clear justification for overriding the AI routing decision..."
                rows={4}
                style={{
                  width: "100%", padding: "0.75rem 0.875rem",
                  background: "#FFFFFF",
                  border: `1px solid ${reason ? "#F97316" : "#E5E7EB"}`,
                  borderRadius: "8px", color: "#1F2937", fontSize: "0.875rem",
                  lineHeight: 1.6, resize: "vertical", outline: "none", fontFamily: "inherit",
                  transition: "border-color 0.15s",
                }}
              />
              {!reason && <div style={{ fontSize: "0.72rem", color: "#EF4444", marginTop: "0.35rem" }}>Reason is required to proceed</div>}
            </div>

            {/* Actions */}
            <div style={{ display: "flex", gap: "0.75rem", justifyContent: "flex-end" }}>
              <button onClick={onClose} style={{ padding: "0.65rem 1.25rem", background: "#F9FAFB", border: "1px solid #E5E7EB", borderRadius: "8px", color: "#6B7280", cursor: "pointer", fontSize: "0.875rem" }}>
                Cancel
              </button>
              <button
                onClick={handleSubmit} disabled={!reason.trim() || isSubmitting}
                style={{
                  display: "flex", alignItems: "center", gap: "0.5rem",
                  padding: "0.65rem 1.5rem",
                  background: reason ? "#F97316" : "#F3F4F6",
                  border: "none", borderRadius: "8px",
                  color: reason ? "#FFFFFF" : "#9CA3AF",
                  cursor: reason ? "pointer" : "not-allowed",
                  fontSize: "0.875rem", fontWeight: 700,
                  boxShadow: reason ? "0 4px 14px rgba(249,115,22,0.3)" : "none",
                }}
              >
                {isSubmitting ? <><Loader2 size={14} style={{ animation: "spin 1s linear infinite" }} /> Recording...</> : <><Database size={14} /> Submit Override</>}
              </button>
            </div>
          </div>
        ) : (
          <div style={{ padding: "2rem 1.5rem", textAlign: "center" }}>
            <div style={{ width: "60px", height: "60px", borderRadius: "50%", background: "#F0FDF4", border: "2px solid #BBF7D0", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 1.25rem" }}>
              <CheckCircle size={28} color="#14B8A6" />
            </div>
            <div style={{ fontSize: "1.1rem", fontWeight: 700, color: "#1F2937", fontFamily: "Poppins, sans-serif", marginBottom: "0.5rem" }}>Override Recorded</div>
            <div style={{ fontSize: "0.875rem", color: "#6B7280", marginBottom: "1.5rem" }}>
              The AI routing decision has been overridden and permanently logged in the WORM audit ledger.
            </div>
            <div style={{ background: "#F9FAFB", border: "1px solid #E5E7EB", borderRadius: "8px", padding: "0.875rem", marginBottom: "1.5rem" }}>
              <div style={{ fontSize: "0.65rem", color: "#9CA3AF", letterSpacing: "0.1em", marginBottom: "0.35rem" }}>WORM LEDGER ENTRY HASH</div>
              <div style={{ fontFamily: "JetBrains Mono", fontSize: "0.65rem", color: "#14B8A6", wordBreak: "break-all" }}>{wormHash}</div>
            </div>
            <button onClick={onClose} style={{ padding: "0.65rem 1.5rem", background: "#0F172A", border: "none", borderRadius: "8px", color: "#FFFFFF", cursor: "pointer", fontSize: "0.875rem", fontWeight: 700 }}>
              Close
            </button>
          </div>
        )}
        <style>{`@keyframes spin { to { transform: rotate(360deg); }}`}</style>
      </div>
    </div>
  );
}
