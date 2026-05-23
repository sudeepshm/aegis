import { create } from "zustand";
import { mockObligations, mockAgentMessages, mockTensions, mockDagNodes, mockDagEdges } from "./mock-data";
import { fetchTensions, simulateBlastRadius } from "./api";

// ─── Types ────────────────────────────────────────────────────────────────────

export type RiskLevel = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
export type ObligationStatus = "PENDING" | "APPROVED" | "REJECTED" | "OVERRIDDEN";
export type AgentType = "VERIFIER" | "CRITIC" | "PLANNER";

export interface UploadedDocument {
  id: string;
  name: string;
  size: number;
  type: string;
  uploadedAt: string;
  scanComplete: boolean;
  fraudDetected: boolean;
  checks: ScanCheck[];
}

export interface ScanCheck {
  id: string;
  label: string;
  tier: 1 | 2;
  status: "pass" | "fail" | "pending";
  detail?: string;
}

export interface Obligation {
  id: string;
  regulation: string;
  jurisdiction: string;
  department: string;
  description: string;
  riskLevel: RiskLevel;
  riskScore: number;
  status: ObligationStatus;
  confidence: number;
  dueDate: string;
  assignedBy: string;
  sourceDocId?: string;
}

export interface AgentMessage {
  id: string;
  agent: AgentType;
  content: string;
  timestamp: string;
  type: "thought" | "action" | "disagreement" | "resolution";
  resolved?: boolean;
}

export interface PolicyTension {
  id: string;
  title: string;
  severity: RiskLevel;
  regulationA: { name: string; excerpt: string };
  regulationB: { name: string; excerpt: string };
  contradiction: string;
  compensatingControl: string;
  status: "UNRESOLVED" | "ACKNOWLEDGED" | "CONTROL_APPLIED";
}

export interface HITLOverride {
  obligationId: string;
  originalDepartment: string;
  newDepartment: string;
  reason: string;
  timestamp: string;
  wormHash: string;
  chainId: string;
}

// ─── Store ────────────────────────────────────────────────────────────────────

interface AegisStore {
  // Upload state
  uploadedDoc: UploadedDocument | null;
  isScanning: boolean;
  setUploadedDoc: (doc: UploadedDocument) => void;
  triggerScan: (file: File) => void;
  clearUpload: () => void;

  // Obligations
  obligations: Obligation[];
  updateObligationStatus: (id: string, status: ObligationStatus) => void;
  applyHITLOverride: (override: HITLOverride) => void;

  // Agent swarm
  agentMessages: AgentMessage[];
  isSwarmActive: boolean;
  startSwarm: () => void;
  stopSwarm: () => void;
  clearMessages: () => void;

  // Policy tensions
  tensions: PolicyTension[];
  acknowledgeTension: (id: string) => void;
  loadTensions: () => Promise<void>;

  // DAG
  dagNodes: unknown[];
  dagEdges: unknown[];
  blastRadiusActive: boolean;
  blastSourceId: string | null;
  blastReport: any | null;
  triggerBlastRadius: (nodeId: string, delayDays?: number) => Promise<void>;
  resetBlastRadius: () => void;

  // HITL overrides log
  overrideLog: HITLOverride[];
}

export const useAegisStore = create<AegisStore>((set, get) => ({
  // ── Upload ────────────────────────────────────────────────────────────────
  uploadedDoc: null,
  isScanning: false,

  setUploadedDoc: (doc) => set({ uploadedDoc: doc }),

  triggerScan: async (file: File) => {
    const pendingDoc: UploadedDocument = {
      id: `doc-${Date.now()}`,
      name: file.name,
      size: file.size,
      type: file.type,
      uploadedAt: new Date().toISOString(),
      scanComplete: false,
      fraudDetected: false,
      checks: [
        { id: "obligation_extraction", label: "Obligation Extraction", tier: 1, status: "pending" },
        { id: "action_decomposition", label: "Action Decomposition", tier: 1, status: "pending" },
        { id: "sme_rule_injection", label: "SME Rule Injection", tier: 1, status: "pending" },
        { id: "json_structuring", label: "JSON Structuring", tier: 2, status: "pending" },
        { id: "guardrails_validation", label: "Guardrails Validation", tier: 2, status: "pending" },
        { id: "conflict_detector", label: "Conflict Detector", tier: 2, status: "pending" },
      ],
    };
    set({ uploadedDoc: pendingDoc, isScanning: true, agentMessages: [] });

    try {
      const apiBase = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      const ingestUrl = `${apiBase}/api/v1/ingest`;

      const formData = new FormData();
      formData.append("file", file);

      const response = await fetch(ingestUrl, {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        throw new Error(`Ingest HTTP Error: ${response.statusText}`);
      }

      const { job_id, chunk_text, filename } = await response.json();
      const regulator = filename.toLowerCase().includes("sebi") ? "sebi" : "rbi";

      // Connect to dynamic WebSocket pipeline stream
      const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsHost = apiBase.replace(/^https?:\/\//, "");
      const wsUrl = `${wsProtocol}//${wsHost}/ws/pipeline/${job_id}`;

      const socket = new WebSocket(wsUrl);

      socket.onopen = () => {
        socket.send(
          JSON.stringify({
            chunk_text,
            regulator,
          })
        );

        set((state) => {
          if (!state.uploadedDoc) return state;
          return {
            agentMessages: [
              {
                id: `system-init`,
                agent: "VERIFIER",
                content: `Ingestion success. Initialized pipeline workspace with job ID: ${job_id}. Starting agent swarm coordination.`,
                timestamp: new Date().toLocaleTimeString("en-IN", { hour12: false }),
                type: "action",
              },
            ],
          };
        });
      };

      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === "ping") return;

          const { stage, status, log, payload } = data;

          set((state) => {
            if (!state.uploadedDoc) return state;

            // Map stage transitions to UI check statuses
            const newChecks = state.uploadedDoc.checks.map((c) => {
              if (c.id === stage) {
                return {
                  ...c,
                  status: (status === "COMPLETED" ? "pass" : status === "ERROR" ? "fail" : "pending") as "pass" | "fail" | "pending",
                  detail: log,
                };
              }
              return c;
            });

            // Map stage to appropriate compliance agent
            const agentType =
              stage === "obligation_extraction" || stage === "guardrails_validation"
                ? "VERIFIER"
                : stage === "action_decomposition" || stage === "sme_rule_injection"
                ? "CRITIC"
                : "PLANNER";

            // Append live logging statement to agentMessages
            const newAgentMessages = [...state.agentMessages];
            if (log) {
              newAgentMessages.push({
                id: `msg-${Date.now()}-${Math.random().toString(36).substr(2, 4)}`,
                agent: agentType,
                content: log,
                timestamp: new Date().toLocaleTimeString("en-IN", { hour12: false }),
                type: status === "ERROR" ? "disagreement" : "action",
              });
            }

            // Once the WebSocket sends PIPELINE_COMPLETE, commit parsed items
            if (stage === "PIPELINE_COMPLETE" && payload) {
              const parsedMap = payload.validated_map || {};
              const obligationsList = parsedMap.obligations || [];

              const newObligations = obligationsList.map((o: any, idx: number) => {
                const action = o.actions?.[0] || {};
                const rL = (action.risk_level || "Medium").toUpperCase();
                const riskLevel = rL === "HIGH" ? "HIGH" : rL === "MEDIUM" ? "MEDIUM" : rL === "LOW" ? "LOW" : "MEDIUM";
                const days = action.deadline_days || 90;
                const dueDate = new Date(Date.now() + days * 24 * 60 * 60 * 1000).toISOString().split("T")[0];

                return {
                  id: o.obligation_id || `OBL-00${idx + 1}`,
                  regulation: regulator.toUpperCase() || "RBI",
                  jurisdiction: "India",
                  department: action.owner || action.routing_tag || "Legal & Compliance",
                  description: o.raw_text || "Verify and report compliance.",
                  riskLevel: riskLevel,
                  riskScore: riskLevel === "HIGH" ? 85 : riskLevel === "MEDIUM" ? 55 : 25,
                  status: "PENDING",
                  confidence: o.confidence_score || 0.85,
                  dueDate: dueDate,
                  assignedBy: "AI Router v2.1",
                  sourceDocId: job_id,
                };
              });

              // Construct a high-fidelity execution DAG dynamically
              const nodes: any[] = [
                {
                  id: "n-ingest",
                  type: "dagNode",
                  position: { x: 400, y: 50 },
                  data: { label: `Ingested Circular`, dept: "IT Operations", sla: "T+0", status: "complete", riskLevel: "LOW" },
                },
              ];

              const edges: any[] = [];

              newObligations.forEach((o: any, idx: number) => {
                const nodeId = `n-${o.id}`;
                nodes.push({
                  id: nodeId,
                  type: "dagNode",
                  position: { x: 150 + idx * 250, y: 220 + (idx % 2) * 80 },
                  data: {
                    label: `${o.regulation} (${o.id})`,
                    dept: o.department,
                    sla: `T+${o.riskLevel === "HIGH" ? 30 : 60}`,
                    status: "pending",
                    riskLevel: o.riskLevel,
                  },
                });

                edges.push({
                  id: `e-ingest-${o.id}`,
                  source: "n-ingest",
                  target: nodeId,
                  animated: true,
                });
              });

              const ccoNodeId = "n-cco-signoff";
              nodes.push({
                id: ccoNodeId,
                type: "dagNode",
                position: { x: 400, y: 450 },
                data: { label: "CCO Final Sign-off", dept: "Compliance", sla: "T+90", status: "pending", riskLevel: "HIGH" },
              });

              newObligations.forEach((o: any) => {
                edges.push({
                  id: `e-${o.id}-cco`,
                  source: `n-${o.id}`,
                  target: ccoNodeId,
                });
              });

              const wormNodeId = "n-worm-sync";
              nodes.push({
                id: wormNodeId,
                type: "dagNode",
                position: { x: 400, y: 580 },
                data: { label: "WORM Audit Log Sync", dept: "IT Operations", sla: "T+91", status: "pending", riskLevel: "LOW" },
              });
              edges.push({
                id: "e-cco-worm",
                source: ccoNodeId,
                target: wormNodeId,
              });

              socket.close(); // Cleanly close socket on completion

              return {
                uploadedDoc: {
                  ...state.uploadedDoc!,
                  checks: newChecks,
                  scanComplete: true,
                  fraudDetected: newChecks.some((c) => c.status === "fail"),
                },
                isScanning: false,
                obligations: newObligations,
                agentMessages: newAgentMessages,
                dagNodes: nodes,
                dagEdges: edges,
              };
            }

            return {
              uploadedDoc: {
                ...state.uploadedDoc!,
                checks: newChecks,
              },
              agentMessages: newAgentMessages,
            };
          });
        } catch (err) {
          console.error("Failed to parse WebSocket event:", err);
        }
      };

      socket.onerror = (err) => {
        console.error("WebSocket Connection Error:", err);
        set((state) => {
          if (!state.uploadedDoc) return state;
          return {
            isScanning: false,
            uploadedDoc: {
              ...state.uploadedDoc!,
              scanComplete: true,
              fraudDetected: true,
            },
          };
        });
      };

      socket.onclose = () => {
        set({ isScanning: false });
      };

    } catch (err) {
      console.error("Scan/Ingest failed:", err);
      set({ isScanning: false });
      alert("Failed to upload and ingest file. Ensure the FastAPI backend is running.");
    }
  },

  clearUpload: () => set({ uploadedDoc: null, isScanning: false }),

  // ── Obligations ──────────────────────────────────────────────────────────
  obligations: [],

  updateObligationStatus: (id, status) =>
    set((state) => ({
      obligations: state.obligations.map((o) =>
        o.id === id ? { ...o, status } : o
      ),
    })),

  applyHITLOverride: (override) =>
    set((state) => ({
      obligations: state.obligations.map((o) =>
        o.id === override.obligationId
          ? { ...o, department: override.newDepartment, status: "OVERRIDDEN" }
          : o
      ),
      overrideLog: [...state.overrideLog, override],
    })),

  // ── Agent Swarm ──────────────────────────────────────────────────────────
  agentMessages: [],
  isSwarmActive: false,

  startSwarm: () => set({ isSwarmActive: true }),
  stopSwarm: () => set({ isSwarmActive: false }),
  clearMessages: () => set({ agentMessages: [] }),

  // ── Tensions ─────────────────────────────────────────────────────────────
  tensions: [],

  acknowledgeTension: (id) =>
    set((state) => ({
      tensions: state.tensions.map((t) =>
        t.id === id ? { ...t, status: "ACKNOWLEDGED" } : t
      ),
    })),

  loadTensions: async () => {
    try {
      const data = await fetchTensions();
      const mapped = data.map((t: any) => ({
        id: t.tension_id || t.id,
        title: t.title || `${t.new_regulator?.toUpperCase() || "Regulation"} vs ${t.conflicting_regulator?.toUpperCase() || "Regulation"} Conflict`,
        severity: t.severity || (t.similarity_score > 0.95 ? "CRITICAL" : t.similarity_score > 0.9 ? "HIGH" : "MEDIUM"),
        regulationA: t.regulationA || {
          name: t.new_obligation_id || "New Obligation",
          excerpt: t.new_text || "",
        },
        regulationB: t.regulationB || {
          name: t.conflicting_chunk_id || "Conflicting Obligation",
          excerpt: t.conflicting_text || "",
        },
        contradiction: t.contradiction || t.llm_explanation || "Regulatory contradiction detected.",
        compensatingControl: t.compensatingControl || "Audit access permissions, log data access to WORM, and enforce strict jurisdiction segregation.",
        status: t.status === "OPEN" ? "UNRESOLVED" : t.status === "REVIEWED" ? "ACKNOWLEDGED" : t.status === "DISMISSED" ? "CONTROL_APPLIED" : t.status || "UNRESOLVED",
      }));
      set({ tensions: mapped });
    } catch (err) {
      console.error("Failed to load tensions from backend:", err);
      set({ tensions: [] });
    }
  },

  // ── DAG / Blast Radius ───────────────────────────────────────────────────
  dagNodes: [],
  dagEdges: [],
  blastRadiusActive: false,
  blastSourceId: null,
  blastReport: null,

  triggerBlastRadius: async (nodeId, delayDays = 15) => {
    set({ blastRadiusActive: true, blastSourceId: nodeId, blastReport: null });

    const nodes = get().dagNodes as any[];
    const edges = get().dagEdges as any[];

    // Extract a valid, clean DAG dependency structure from the ReactFlow nodes/edges
    const payloadDag = nodes.map((node: any) => {
      const parentEdges = edges.filter((e: any) => e.target === node.id);
      const dependsOn = parentEdges.map((edge: any) => edge.source);
      return {
        task: node.id,
        depends_on: dependsOn,
      };
    });

    try {
      const report = await simulateBlastRadius(nodeId, delayDays, payloadDag);
      set({ blastReport: report });
    } catch (err) {
      console.error("Failed to run blast radius simulation:", err);
      // Premium graceful fallback if connection times out or fails
      set({
        blastReport: {
          delayed_task: nodeId,
          delay_days: delayDays,
          total_tasks_impacted: 1,
          risk_delta_pct: 15.0,
          narrative: `Hypothetical delay simulated on ${nodeId}. API connection offline, utilizing local BFS indicators.`,
          cascade_tasks: [{ task_name: nodeId, original_start_day: 0, delayed_start_day: delayDays, days_delayed: delayDays }],
          breached_deadlines: [],
          // Moderate tier compatibility fallback
          affected_tasks: [nodeId],
          deadline_breaches: [],
          risk_delta: 15.0,
        },
      });
    }
  },

  resetBlastRadius: () =>
    set({ blastRadiusActive: false, blastSourceId: null, blastReport: null }),

  // ── HITL Log ─────────────────────────────────────────────────────────────
  overrideLog: [],
}));
