"use client";

import { useEffect, useRef, useState } from "react";
import { useAegisStore } from "@/lib/store";
import { Terminal, Cpu, Eye, Zap, RefreshCw, ChevronDown, ChevronRight } from "lucide-react";
import type { AgentMessage } from "@/lib/store";
import PageLayout from "@/components/shared/PageLayout";
import PipelineTerminal from "@/components/PipelineTerminal";

const AGENT_CONFIG = {
  VERIFIER: { color: "#0F172A", label: "Verifier", icon: Eye, borderColor: "rgba(15,23,42,0.2)", bg: "rgba(15,23,42,0.04)" },
  CRITIC:   { color: "#14B8A6", label: "Critic",   icon: Zap, borderColor: "rgba(20,184,166,0.25)", bg: "rgba(20,184,166,0.04)" },
  PLANNER:  { color: "#F97316", label: "Planner",  icon: Cpu, borderColor: "rgba(249,115,22,0.25)", bg: "rgba(249,115,22,0.04)" },
};

export default function SwarmPage() {
  const { agentMessages, isScanning, uploadedDoc, clearMessages } = useAegisStore();
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const feedRef = useRef<HTMLDivElement>(null);

  // Auto-scroll on new messages streaming in
  useEffect(() => {
    if (feedRef.current) {
      feedRef.current.scrollTop = feedRef.current.scrollHeight;
    }
  }, [agentMessages.length]);

  const toggle = (id: string) =>
    setExpandedIds(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  const disagreements = agentMessages.filter(m => m.type === "disagreement").length;
  const resolutions = agentMessages.filter(m => m.type === "resolution").length;

  const getStatus = () => {
    if (isScanning) return "STREAMING";
    if (agentMessages.length > 0) return "COMPLETE";
    return "IDLE";
  };

  const status = getStatus();

  return (
    <PageLayout>
      <div style={{ maxWidth: "1200px" }}>
        {/* Header */}
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: "1.5rem" }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.5rem" }}>
              <div style={{ width: "34px", height: "34px", borderRadius: "8px", background: "#0F172A", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <Terminal size={16} color="#FFFFFF" />
              </div>
              <h1 style={{ fontSize: "1.3rem", fontWeight: 700, color: "#1F2937", fontFamily: "Poppins, sans-serif" }}>
                Multi-Agent Swarm Console
              </h1>
            </div>
            <p style={{ color: "#6B7280", fontSize: "0.875rem" }}>Live internal deliberation between Verifier, Critic, and Planner agents.</p>
          </div>
          {agentMessages.length > 0 && (
            <button onClick={clearMessages} style={{
              display: "flex", alignItems: "center", gap: "0.5rem",
              padding: "0.5rem 1rem", background: "#F9FAFB", border: "1px solid #E5E7EB",
              borderRadius: "8px", color: "#6B7280", cursor: "pointer", fontSize: "0.8rem", fontWeight: 600,
            }}>
              <RefreshCw size={13} /> Clear Console
            </button>
          )}
        </div>

        {/* Stats */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "1rem", marginBottom: "1.5rem" }}>
          {[
            { label: "Swarm Messages", value: agentMessages.length, color: "#0F172A" },
            { label: "Disagreements", value: disagreements, color: "#F97316" },
            { label: "Resolutions", value: resolutions, color: "#14B8A6" },
            { label: "System Status", value: status, color: status === "STREAMING" ? "#F97316" : status === "COMPLETE" ? "#14B8A6" : "#9CA3AF" },
          ].map(({ label, value, color }) => (
            <div key={label} style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "12px", padding: "1.25rem", boxShadow: "0 1px 4px rgba(0,0,0,0.04)" }}>
              <div style={{ fontSize: "0.72rem", color: "#9CA3AF", marginBottom: "0.5rem", fontWeight: 600 }}>{label}</div>
              <div style={{ fontSize: "1.3rem", fontWeight: 800, color, fontFamily: "Poppins, sans-serif" }}>{value}</div>
            </div>
          ))}
        </div>

        {/* Two Column Layout */}
        <div style={{
          display: "grid",
          gridTemplateColumns: uploadedDoc ? "1fr 400px" : "1fr",
          gap: "1.5rem",
          alignItems: "start"
        }}>
          {/* Left Panel: Deliberation Messages */}
          <div style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "14px", overflow: "hidden", boxShadow: "0 2px 12px rgba(0,0,0,0.05)" }}>
            {/* Top bar */}
            <div style={{
              padding: "0.875rem 1.25rem", borderBottom: "1px solid #F3F4F6",
              display: "flex", alignItems: "center", gap: "0.75rem",
              background: "#F9FAFB",
            }}>
              <div style={{ display: "flex", gap: "0.5rem" }}>
                {Object.values(AGENT_CONFIG).map(({ color, label }) => (
                  <div key={label} style={{
                    display: "flex", alignItems: "center", gap: "0.35rem",
                    padding: "0.2rem 0.6rem", borderRadius: "100px",
                    background: `${color}0f`, border: `1px solid ${color}22`,
                  }}>
                    <div style={{ width: "5px", height: "5px", borderRadius: "50%", background: color }} />
                    <span style={{ fontSize: "0.68rem", color, fontWeight: 700 }}>{label}</span>
                  </div>
                ))}
              </div>
              {isScanning && (
                <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: "0.4rem" }}>
                  <div style={{ width: "6px", height: "6px", borderRadius: "50%", background: "#F97316", animation: "pulse 1.5s infinite" }} />
                  <span style={{ fontSize: "0.68rem", color: "#F97316", fontWeight: 700 }}>SWARM ACTIVE</span>
                </div>
              )}
            </div>

            {/* Messages Feed */}
            <div ref={feedRef} style={{ height: "520px", overflowY: "auto", padding: "1.25rem", display: "flex", flexDirection: "column", gap: "0.75rem" }}>
              {agentMessages.length === 0 ? (
                <div style={{ textAlign: "center", color: "#9CA3AF", padding: "6rem 2rem" }}>
                  <Terminal size={36} color="#E5E7EB" style={{ margin: "0 auto 1.25rem" }} />
                  <div style={{ fontSize: "0.95rem", fontWeight: 700, color: "#4B5563", marginBottom: "0.5rem" }}>Swarm Console Offline</div>
                  <p style={{ fontSize: "0.82rem", color: "#9CA3AF", maxWidth: "340px", margin: "0 auto" }}>
                    No active ingestion job detected. Go to the Upload section and scan a regulatory circular to boot the agent swarm.
                  </p>
                </div>
              ) : (
                agentMessages.map(msg => (
                  <MessageBubble key={msg.id} msg={msg} expanded={expandedIds.has(msg.id)} onToggle={() => toggle(msg.id)} />
                ))
              )}
              {isScanning && (
                <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", color: "#9CA3AF", fontSize: "0.78rem", paddingLeft: "1rem" }}>
                  <div style={{ width: "6px", height: "6px", borderRadius: "50%", background: "#F97316", animation: "pulse 1.2s infinite" }} />
                  <span>Swarm agents are deliberating...</span>
                </div>
              )}
            </div>
          </div>

          {/* Right Panel: Raw WebSockets Log (Visible only during/after upload) */}
          {uploadedDoc && (
            <div>
              <div style={{ fontSize: "0.68rem", color: "#9CA3AF", letterSpacing: "0.08em", fontWeight: 700, marginBottom: "0.75rem" }}>
                RAW PIPELINE EVENT STREAM
              </div>
              <PipelineTerminal
                jobId={uploadedDoc.id}
                wsBaseUrl={process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}
                autoStart={false} // ws was already auto-triggered in store.ts
              />
            </div>
          )}
        </div>
      </div>
      <style>{`
        @keyframes pulse {
          0% { opacity: 0.3; }
          50% { opacity: 1; }
          100% { opacity: 0.3; }
        }
      `}</style>
    </PageLayout>
  );
}

function MessageBubble({ msg, expanded, onToggle }: { msg: AgentMessage; expanded: boolean; onToggle: () => void }) {
  const config = AGENT_CONFIG[msg.agent] || AGENT_CONFIG.VERIFIER;
  const Icon = config.icon;
  const isCollapsible = msg.type === "disagreement" || msg.type === "resolution";

  return (
    <div style={{
      background: msg.type === "disagreement" ? "rgba(249,115,22,0.04)" :
        msg.type === "resolution" ? "rgba(20,184,166,0.04)" : config.bg,
      border: `1px solid ${msg.type === "disagreement" ? "rgba(249,115,22,0.2)" :
        msg.type === "resolution" ? "rgba(20,184,166,0.2)" : config.borderColor}`,
      borderRadius: "10px", overflow: "hidden",
    }}>
      <div
        style={{ display: "flex", alignItems: "center", gap: "0.6rem", padding: "0.65rem 1rem", cursor: isCollapsible ? "pointer" : "default" }}
        onClick={isCollapsible ? onToggle : undefined}
      >
        <div style={{
          width: "28px", height: "28px", borderRadius: "50%",
          background: `${config.color}12`, border: `1px solid ${config.color}22`,
          display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
        }}>
          <Icon size={13} color={config.color} />
        </div>
        <div>
          <span style={{ fontSize: "0.78rem", fontWeight: 700, color: config.color }}>{config.label}</span>
          <span style={{ fontSize: "0.68rem", color: "#9CA3AF", marginLeft: "0.5rem" }}>{msg.timestamp}</span>
        </div>
        {msg.type === "disagreement" && (
          <span style={{ marginLeft: "0.5rem", padding: "0.1rem 0.5rem", background: "#FFF7ED", border: "1px solid #FED7AA", borderRadius: "100px", fontSize: "0.6rem", color: "#F97316", fontWeight: 700 }}>
            OBJECTION
          </span>
        )}
        {msg.type === "resolution" && (
          <span style={{ marginLeft: "0.5rem", padding: "0.1rem 0.5rem", background: "#F0FDF4", border: "1px solid #BBF7D0", borderRadius: "100px", fontSize: "0.6rem", color: "#14B8A6", fontWeight: 700 }}>
            RESOLVED
          </span>
        )}
        {isCollapsible && (
          <span style={{ marginLeft: "auto", color: "#9CA3AF" }}>
            {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          </span>
        )}
      </div>
      {(!isCollapsible || expanded) && (
        <div style={{ padding: "0 1rem 0.75rem 3.5rem", fontSize: "0.82rem", color: "#4B5563", lineHeight: 1.7 }}>
          {msg.content}
        </div>
      )}
    </div>
  );
}
