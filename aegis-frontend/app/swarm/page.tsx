"use client";

import { useEffect, useRef, useState } from "react";
import { useAegisStore } from "@/lib/store";
import { Terminal, Cpu, Eye, Zap, RefreshCw, ChevronDown, ChevronRight } from "lucide-react";
import type { AgentMessage } from "@/lib/store";
import PageLayout from "@/components/shared/PageLayout";
import { mockAgentMessages } from "@/lib/mock-data";

const AGENT_CONFIG = {
  VERIFIER: { color: "#0F172A", label: "Verifier", icon: Eye, borderColor: "rgba(15,23,42,0.2)", bg: "rgba(15,23,42,0.04)" },
  CRITIC:   { color: "#14B8A6", label: "Critic",   icon: Zap, borderColor: "rgba(20,184,166,0.25)", bg: "rgba(20,184,166,0.04)" },
  PLANNER:  { color: "#F97316", label: "Planner",  icon: Cpu, borderColor: "rgba(249,115,22,0.25)", bg: "rgba(249,115,22,0.04)" },
};

export default function SwarmPage() {
  const { agentMessages, clearMessages } = useAegisStore();
  const [visibleCount, setVisibleCount] = useState(0);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const feedRef = useRef<HTMLDivElement>(null);

  const messages = agentMessages.length > 0 ? agentMessages : mockAgentMessages;

  useEffect(() => {
    setVisibleCount(0);
    let count = 0;
    const timer = setInterval(() => {
      count++;
      setVisibleCount(count);
      if (count >= messages.length) clearInterval(timer);
    }, 900);
    return () => clearInterval(timer);
  }, [messages]);

  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [visibleCount]);

  const toggle = (id: string) =>
    setExpandedIds(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  const handleReplay = () => {
    clearMessages();
    setTimeout(() => {
      useAegisStore.setState(s => ({ ...s, agentMessages: mockAgentMessages }));
    }, 50);
  };

  const visible = messages.slice(0, visibleCount);
  const disagreements = visible.filter(m => m.type === "disagreement").length;
  const resolutions = visible.filter(m => m.type === "resolution").length;

  return (
    <PageLayout>
      <div style={{ maxWidth: "1100px" }}>
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
          <button onClick={handleReplay} style={{
            display: "flex", alignItems: "center", gap: "0.5rem",
            padding: "0.5rem 1rem", background: "#F9FAFB", border: "1px solid #E5E7EB",
            borderRadius: "8px", color: "#0F172A", cursor: "pointer", fontSize: "0.8rem", fontWeight: 600,
          }}>
            <RefreshCw size={13} /> Replay
          </button>
        </div>

        {/* Stats */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "1rem", marginBottom: "1.5rem" }}>
          {[
            { label: "Messages", value: visible.length, color: "#0F172A" },
            { label: "Disagreements", value: disagreements, color: "#F97316" },
            { label: "Resolutions", value: resolutions, color: "#14B8A6" },
            { label: "Status", value: visibleCount < messages.length ? "THINKING" : "COMPLETE", color: visibleCount < messages.length ? "#F97316" : "#14B8A6" },
          ].map(({ label, value, color }) => (
            <div key={label} style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", borderRadius: "12px", padding: "1.25rem", boxShadow: "0 1px 4px rgba(0,0,0,0.04)" }}>
              <div style={{ fontSize: "0.72rem", color: "#9CA3AF", marginBottom: "0.5rem", fontWeight: 600 }}>{label}</div>
              <div style={{ fontSize: "1.3rem", fontWeight: 800, color, fontFamily: "Poppins, sans-serif" }}>{value}</div>
            </div>
          ))}
        </div>

        {/* Message feed — corporate chat style */}
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
            {visibleCount < messages.length && (
              <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: "0.4rem" }}>
                <div style={{ width: "6px", height: "6px", borderRadius: "50%", background: "#F97316" }} />
                <span style={{ fontSize: "0.68rem", color: "#F97316", fontWeight: 700 }}>LIVE</span>
              </div>
            )}
          </div>

          {/* Messages */}
          <div ref={feedRef} style={{ height: "520px", overflowY: "auto", padding: "1.25rem", display: "flex", flexDirection: "column", gap: "0.75rem" }}>
            {visible.length === 0 ? (
              <div style={{ textAlign: "center", color: "#9CA3AF", marginTop: "6rem" }}>
                <Terminal size={32} color="#E5E7EB" style={{ margin: "0 auto 1rem" }} />
                <div style={{ fontSize: "0.875rem" }}>Upload a document to activate the agent swarm.</div>
              </div>
            ) : visible.map(msg => (
              <MessageBubble key={msg.id} msg={msg} expanded={expandedIds.has(msg.id)} onToggle={() => toggle(msg.id)} />
            ))}
            {visibleCount < messages.length && messages.length > 0 && (
              <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", color: "#D1D5DB", fontSize: "0.8rem" }}>
                <div style={{ width: "5px", height: "5px", borderRadius: "50%", background: "#F97316" }} />
                Processing...
              </div>
            )}
          </div>
        </div>
      </div>
    </PageLayout>
  );
}

function MessageBubble({ msg, expanded, onToggle }: { msg: AgentMessage; expanded: boolean; onToggle: () => void }) {
  const config = AGENT_CONFIG[msg.agent];
  const Icon = config.icon;
  const isCollapsible = msg.type === "disagreement" || msg.type === "resolution";

  return (
    <div className="animate-stream-in" style={{
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
