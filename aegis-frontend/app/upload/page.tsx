"use client";

import { useCallback, useState } from "react";
import { useDropzone } from "react-dropzone";
import { useAegisStore } from "@/lib/store";
import { formatBytes } from "@/lib/utils";
import { Upload, AlertTriangle, CheckCircle, XCircle, Clock, Shield, FileText, Loader2 } from "lucide-react";
import PageLayout from "@/components/shared/PageLayout";

export default function UploadPage() {
  const { uploadedDoc, isScanning, triggerScan, clearUpload } = useAegisStore();
  const [isDragOver, setIsDragOver] = useState(false);

  const onDrop = useCallback((files: File[]) => {
    if (files[0]) triggerScan(files[0]);
  }, [triggerScan]);

  const { getRootProps, getInputProps } = useDropzone({
    onDrop,
    accept: { "application/pdf": [".pdf"], "image/*": [".png", ".jpg", ".jpeg"] },
    multiple: false, disabled: isScanning,
    onDragEnter: () => setIsDragOver(true),
    onDragLeave: () => setIsDragOver(false),
  });

  const fraudDetected = uploadedDoc?.fraudDetected;
  const scanProgress = uploadedDoc ? (uploadedDoc.checks.filter(c => c.status !== "pending").length / 6) * 100 : 0;

  return (
    <PageLayout>
      <div style={{ maxWidth: "900px" }}>
        {/* Page Header */}
        <div style={{ marginBottom: "2rem" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.5rem" }}>
            <div style={{ width: "34px", height: "34px", borderRadius: "8px", background: "#0F172A", display: "flex", alignItems: "center", justifyContent: "center" }}>
              <Shield size={16} color="#FFFFFF" />
            </div>
            <h1 style={{ fontSize: "1.3rem", fontWeight: 700, color: "#1F2937", fontFamily: "Poppins, sans-serif" }}>
              Document Upload & Anti-Fraud Scanner
            </h1>
          </div>
          <p style={{ color: "#6B7280", fontSize: "0.875rem" }}>
            Upload regulatory evidence for AI-powered forensic analysis. Tier 1 & Tier 2 validation with temporal anachronism detection.
          </p>
        </div>

        {/* Fraud alert banner */}
        {fraudDetected && uploadedDoc?.scanComplete && (
          <div style={{
            background: "#FEF2F2", border: "1px solid #FECACA",
            borderLeft: "4px solid #EF4444",
            borderRadius: "8px", padding: "1rem 1.25rem",
            display: "flex", alignItems: "flex-start", gap: "0.75rem",
            marginBottom: "1.5rem",
          }}>
            <AlertTriangle size={18} color="#EF4444" style={{ flexShrink: 0, marginTop: "1px" }} />
            <div>
              <div style={{ fontWeight: 700, color: "#991B1B", fontSize: "0.875rem", marginBottom: "0.25rem" }}>
                Forgery Detected — Temporal Anachronism in Document Metadata
              </div>
              <div style={{ color: "#B91C1C", fontSize: "0.8rem" }}>
                The document creation timestamp does not match the referenced regulatory standard publication date.
                This evidence has been quarantined and flagged for manual review.
              </div>
            </div>
          </div>
        )}

        {/* Success banner */}
        {!fraudDetected && uploadedDoc?.scanComplete && (
          <div style={{
            background: "#F0FDF4", border: "1px solid #BBF7D0",
            borderLeft: "4px solid #14B8A6",
            borderRadius: "8px", padding: "1rem 1.25rem",
            display: "flex", alignItems: "center", gap: "0.75rem",
            marginBottom: "1.5rem",
          }}>
            <CheckCircle size={18} color="#14B8A6" />
            <div style={{ color: "#065F46", fontWeight: 600, fontSize: "0.875rem" }}>
              Document authenticated. Obligations extracted and synced to MAP Review Dashboard.
            </div>
          </div>
        )}

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1.5rem" }}>
          {/* Drop zone */}
          <div>
            <div
              {...getRootProps()}
              style={{
                border: `2px dashed ${isDragOver ? "#F97316" : uploadedDoc ? "#14B8A6" : "#D1D5DB"}`,
                borderRadius: "14px", padding: "3rem 2rem", textAlign: "center",
                cursor: isScanning ? "not-allowed" : "pointer",
                transition: "all 0.25s ease",
                background: isDragOver ? "#FFF7ED" : "#FAFAFA",
                position: "relative", overflow: "hidden",
              }}
            >
              <input {...getInputProps()} />

              {/* Linear progress bar (replacing skeleton pulse) */}
              {isScanning && (
                <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: "3px", background: "#F3F4F6" }}>
                  <div style={{
                    height: "100%", background: "#F97316",
                    width: `${scanProgress}%`, transition: "width 0.6s ease",
                    borderRadius: "0 2px 2px 0",
                  }} />
                </div>
              )}

              <div style={{
                width: "64px", height: "64px", borderRadius: "50%",
                background: isScanning ? "#FFF7ED" : uploadedDoc ? "#F0FDF4" : "#F9FAFB",
                border: `1px solid ${isScanning ? "#FED7AA" : uploadedDoc ? "#BBF7D0" : "#E5E7EB"}`,
                display: "flex", alignItems: "center", justifyContent: "center",
                margin: "0 auto 1.5rem",
              }}>
                {isScanning ? (
                  <Loader2 size={28} color="#F97316" style={{ animation: "spin 1s linear infinite" }} />
                ) : uploadedDoc ? (
                  <FileText size={28} color="#14B8A6" />
                ) : (
                  <Upload size={28} color="#9CA3AF" />
                )}
              </div>

              {isScanning ? (
                <>
                  <div style={{ color: "#F97316", fontWeight: 600, marginBottom: "0.5rem", fontSize: "0.9rem" }}>
                    Scanning Document...
                  </div>
                  <div style={{ color: "#9CA3AF", fontSize: "0.8rem" }}>
                    Running forensic analysis pipeline
                  </div>
                </>
              ) : uploadedDoc ? (
                <>
                  <div style={{ color: "#1F2937", fontWeight: 600, marginBottom: "0.25rem" }}>{uploadedDoc.name}</div>
                  <div style={{ color: "#9CA3AF", fontSize: "0.8rem", marginBottom: "1rem" }}>{formatBytes(uploadedDoc.size)}</div>
                  <button onClick={(e) => { e.stopPropagation(); clearUpload(); }} style={{
                    background: "#F9FAFB", border: "1px solid #E5E7EB",
                    borderRadius: "6px", padding: "0.4rem 1rem",
                    color: "#6B7280", cursor: "pointer", fontSize: "0.8rem",
                  }}>Upload New Document</button>
                </>
              ) : (
                <>
                  <div style={{ color: "#1F2937", fontWeight: 600, marginBottom: "0.5rem" }}>Drop regulatory document here</div>
                  <div style={{ color: "#9CA3AF", fontSize: "0.8rem", marginBottom: "1rem" }}>PDF, PNG, or JPEG · Max 50MB</div>
                  <div style={{
                    display: "inline-block", padding: "0.5rem 1.25rem",
                    background: "#F97316", borderRadius: "6px", color: "#FFFFFF", fontSize: "0.8rem", fontWeight: 600,
                  }}>Browse Files</div>
                </>
              )}
            </div>

            {!uploadedDoc && !isScanning && (
              <button
                onClick={() => triggerScan(new File([""], "RBI_Master_Direction_Q3_2025.pdf", { type: "application/pdf" }))}
                style={{
                  marginTop: "0.75rem", width: "100%", padding: "0.6rem",
                  background: "#F9FAFB", border: "1px solid #E5E7EB",
                  borderRadius: "8px", color: "#0F172A", cursor: "pointer", fontSize: "0.8rem", fontWeight: 600,
                }}
              >
                ⚡ Demo: Upload Sample RBI Document
              </button>
            )}
          </div>

          {/* Scan results panel */}
          <div style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "14px", padding: "1.25rem", boxShadow: "0 2px 8px rgba(0,0,0,0.04)" }}>
            <div style={{ fontSize: "0.72rem", color: "#9CA3AF", letterSpacing: "0.1em", marginBottom: "1rem", fontWeight: 600 }}>
              FORENSIC VALIDATION PIPELINE
            </div>

            <div style={{ fontSize: "0.68rem", color: "#9CA3AF", letterSpacing: "0.08em", marginBottom: "0.5rem", fontWeight: 600 }}>
              TIER 1 — STRUCTURAL INTEGRITY
            </div>
            {(uploadedDoc?.checks || []).filter(c => c.tier === 1).map(c => <CheckItem key={c.id} check={c} />)}
            {!uploadedDoc && ["File Hash Integrity", "EXIF Metadata Consistency", "Temporal Watermark Validation"].map(l => (
              <CheckItem key={l} check={{ id: l, label: l, tier: 1, status: "pending" }} />
            ))}

            <div style={{ height: "1px", background: "#F3F4F6", margin: "1rem 0" }} />

            <div style={{ fontSize: "0.68rem", color: "#9CA3AF", letterSpacing: "0.08em", marginBottom: "0.5rem", fontWeight: 600 }}>
              TIER 2 — FORENSIC ANALYSIS
            </div>
            {(uploadedDoc?.checks || []).filter(c => c.tier === 2).map(c => <CheckItem key={c.id} check={c} />)}
            {!uploadedDoc && ["Adobe Metadata Forensics", "Creation Timestamp Anomaly", "Signatory CA Chain Verification"].map(l => (
              <CheckItem key={l} check={{ id: l, label: l, tier: 2, status: "pending" }} />
            ))}
          </div>
        </div>
        <style>{`@keyframes spin { to { transform: rotate(360deg); }}`}</style>
      </div>
    </PageLayout>
  );
}

function CheckItem({ check }: { check: { id: string; label: string; tier: number; status: string } }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: "0.75rem",
      padding: "0.55rem 0.75rem", borderRadius: "7px", marginBottom: "0.35rem",
      background: check.status === "fail" ? "#FEF2F2" : check.status === "pass" ? "#F0FDF4" : "#F9FAFB",
      border: `1px solid ${check.status === "fail" ? "#FECACA" : check.status === "pass" ? "#BBF7D0" : "#F3F4F6"}`,
    }}>
      {check.status === "pass" ? <CheckCircle size={14} color="#14B8A6" /> :
       check.status === "fail" ? <XCircle size={14} color="#EF4444" /> :
       <Clock size={14} color="#D1D5DB" />}
      <span style={{
        fontSize: "0.8rem",
        color: check.status === "fail" ? "#991B1B" : check.status === "pass" ? "#065F46" : "#9CA3AF",
      }}>{check.label}</span>
      {check.status === "fail" && (
        <span style={{ marginLeft: "auto", fontSize: "0.65rem", color: "#EF4444", background: "#FEE2E2", padding: "0.15rem 0.5rem", borderRadius: "100px", fontWeight: 700 }}>
          FLAGGED
        </span>
      )}
    </div>
  );
}
