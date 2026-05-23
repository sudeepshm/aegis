"use client";

import { useCallback, useMemo, useEffect } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  BackgroundVariant,
  MarkerType,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";
import { useAegisStore } from "@/lib/store";

// ── Risk color map ────────────────────────────────────────────────────────────
const RISK_COLORS: Record<string, { text: string; bg: string; border: string }> = {
  CRITICAL: { text: "#EF4444", bg: "#FEF2F2", border: "#FECACA" },
  HIGH:     { text: "#F97316", bg: "#FFF7ED", border: "#FED7AA" },
  MEDIUM:   { text: "#2563EB", bg: "#EFF6FF", border: "#BFDBFE" },
  LOW:      { text: "#14B8A6", bg: "#F0FDF4", border: "#99F6E4" },
};

// ── Custom Node ───────────────────────────────────────────────────────────────
function DagTaskNode({ data, selected }: NodeProps) {
  const isBreached = data.blastBreached;
  const rc = RISK_COLORS[data.riskLevel] || RISK_COLORS.LOW;

  return (
    <div style={{
      padding: "0.75rem 1rem",
      borderRadius: "10px",
      background: isBreached ? "#FEF2F2" : "#FFFFFF",
      border: `1.5px solid ${isBreached ? "#FECACA" : selected ? "#F97316" : "#E5E7EB"}`,
      boxShadow: isBreached ? "0 4px 16px rgba(239,68,68,0.15)" : selected ? "0 0 0 3px rgba(249,115,22,0.12)" : "0 2px 8px rgba(0,0,0,0.06)",
      minWidth: "165px",
      transition: "all 0.35s ease",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "0.4rem" }}>
        <div style={{ width: "7px", height: "7px", borderRadius: "50%", background: isBreached ? "#EF4444" : "#14B8A6" }} />
        <span style={{
          fontSize: "0.6rem", fontWeight: 700,
          color: rc.text, background: rc.bg, border: `1px solid ${rc.border}`,
          padding: "0.1rem 0.4rem", borderRadius: "100px",
        }}>{data.riskLevel}</span>
      </div>
      <div style={{ fontSize: "0.78rem", fontWeight: 700, color: isBreached ? "#991B1B" : "#1F2937", marginBottom: "0.3rem", lineHeight: 1.3 }}>
        {data.label}
      </div>
      <div style={{ fontSize: "0.65rem", color: "#9CA3AF", marginBottom: "0.4rem" }}>{data.dept}</div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: "0.62rem", color: "#6B7280" }}>SLA: {data.sla}</span>
        <span style={{
          fontSize: "0.6rem", padding: "0.1rem 0.4rem", borderRadius: "100px", fontWeight: 700,
          background: data.status === "complete" ? "#F0FDF4" : data.status === "in-progress" ? "#EFF6FF" : data.status === "blocked" ? "#FEF2F2" : "#F9FAFB",
          color: data.status === "complete" ? "#14B8A6" : data.status === "in-progress" ? "#2563EB" : data.status === "blocked" ? "#EF4444" : "#9CA3AF",
        }}>{data.status}</span>
      </div>
    </div>
  );
}

const nodeTypes = { dagNode: DagTaskNode };

// ── DAG Canvas ────────────────────────────────────────────────────────────────
export default function DagCanvas() {
  const { dagNodes: rawNodes, dagEdges: rawEdges, blastRadiusActive, blastSourceId } = useAegisStore();

  const downstreamIds = useMemo(() => {
    if (!blastRadiusActive || !blastSourceId) return new Set<string>();
    const edges = rawEdges as Array<{ source: string; target: string }>;
    const visited = new Set<string>([blastSourceId]);
    const queue = [blastSourceId];
    while (queue.length) {
      const curr = queue.shift()!;
      edges.filter(e => e.source === curr).forEach(e => {
        if (!visited.has(e.target)) { visited.add(e.target); queue.push(e.target); }
      });
    }
    return visited;
  }, [blastRadiusActive, blastSourceId, rawEdges]);

  const annotatedNodes = useMemo(() =>
    (rawNodes as Array<{ id: string; type: string; position: { x: number; y: number }; data: Record<string, unknown> }>).map(n => ({
      ...n,
      data: { ...n.data, blastBreached: downstreamIds.has(n.id) },
    })), [rawNodes, downstreamIds]);

  const edgeOptions = useCallback((edgeId: string) => {
    const edge = (rawEdges as Array<{ id: string; source: string; target: string }>).find(e => e.id === edgeId);
    const isBreached = edge && (downstreamIds.has(edge.source) || downstreamIds.has(edge.target));
    return {
      style: { stroke: isBreached ? "#EF4444" : "#F97316", strokeWidth: isBreached ? 2 : 1.5, opacity: isBreached ? 1 : 0.4 },
      markerEnd: { type: MarkerType.ArrowClosed, color: isBreached ? "#EF4444" : "#F97316" },
    };
  }, [rawEdges, downstreamIds]);

  const builtEdges = useMemo(() =>
    (rawEdges as Array<{ id: string; source: string; target: string; animated?: boolean }>).map(e => ({
      ...e,
      ...edgeOptions(e.id),
    })), [rawEdges, edgeOptions]);

  const [nodes, setNodes, onNodesChange] = useNodesState(annotatedNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(builtEdges);

  useEffect(() => { setNodes(annotatedNodes as Parameters<typeof setNodes>[0]); }, [annotatedNodes, setNodes]);
  useEffect(() => { setEdges(builtEdges as Parameters<typeof setEdges>[0]); }, [builtEdges, setEdges]);

  return (
    <div style={{ width: "100%", height: "100%", background: "#FFFFFF" }}>
      <ReactFlow
        nodes={nodes} edges={edges}
        onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        fitView fitViewOptions={{ padding: 0.2 }}
      >
        <Background variant={BackgroundVariant.Dots} color="#F3F4F6" gap={20} size={1} />
        <Controls style={{ background: "#FFFFFF", border: "1px solid #E5E7EB", boxShadow: "0 2px 6px rgba(0,0,0,0.06)" }} />
        <MiniMap
          style={{ background: "#FFFFFF", border: "1px solid #E5E7EB" }}
          nodeColor={n => n.data.blastBreached ? "#EF4444" : "#14B8A6"}
        />
      </ReactFlow>
    </div>
  );
}
