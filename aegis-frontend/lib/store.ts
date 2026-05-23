import { create } from "zustand";
import { mockObligations, mockAgentMessages, mockTensions, mockDagNodes, mockDagEdges } from "./mock-data";

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

  // DAG
  dagNodes: unknown[];
  dagEdges: unknown[];
  blastRadiusActive: boolean;
  blastSourceId: string | null;
  triggerBlastRadius: (nodeId: string) => void;
  resetBlastRadius: () => void;

  // HITL overrides log
  overrideLog: HITLOverride[];
}

export const useAegisStore = create<AegisStore>((set, get) => ({
  // ── Upload ────────────────────────────────────────────────────────────────
  uploadedDoc: null,
  isScanning: false,

  setUploadedDoc: (doc) => set({ uploadedDoc: doc }),

  triggerScan: (file: File) => {
    const pendingDoc: UploadedDocument = {
      id: `doc-${Date.now()}`,
      name: file.name,
      size: file.size,
      type: file.type,
      uploadedAt: new Date().toISOString(),
      scanComplete: false,
      fraudDetected: false,
      checks: [
        { id: "hash", label: "File Hash Integrity", tier: 1, status: "pending" },
        { id: "exif", label: "EXIF Metadata Consistency", tier: 1, status: "pending" },
        { id: "watermark", label: "Temporal Watermark Validation", tier: 1, status: "pending" },
        { id: "adobe", label: "Adobe Metadata Forensics", tier: 2, status: "pending" },
        { id: "timestamp", label: "Creation Timestamp Anomaly", tier: 2, status: "pending" },
        { id: "ca", label: "Signatory CA Chain Verification", tier: 2, status: "pending" },
      ],
    };
    set({ uploadedDoc: pendingDoc, isScanning: true });

    // Simulate scan checks firing one by one
    const checks = ["hash", "exif", "watermark", "adobe", "timestamp", "ca"];
    const results: Record<string, "pass" | "fail"> = {
      hash: "pass", exif: "pass", watermark: "pass",
      adobe: "pass", timestamp: "fail", ca: "pass",
    };

    checks.forEach((checkId, i) => {
      setTimeout(() => {
        set((state) => {
          if (!state.uploadedDoc) return state;
          const newChecks = state.uploadedDoc.checks.map((c) =>
            c.id === checkId ? { ...c, status: results[checkId] } : c
          );
          const allDone = i === checks.length - 1;
          const fraudDetected = newChecks.some((c) => c.status === "fail");

          // When upload completes, load obligations into store
          if (allDone) {
            const newObligations = mockObligations.map((o) => ({
              ...o,
              sourceDocId: pendingDoc.id,
            }));
            return {
              uploadedDoc: { ...state.uploadedDoc!, checks: newChecks, scanComplete: allDone, fraudDetected },
              isScanning: !allDone,
              obligations: newObligations,
              agentMessages: mockAgentMessages,
              dagNodes: mockDagNodes,
              dagEdges: mockDagEdges,
            };
          }
          return { uploadedDoc: { ...state.uploadedDoc!, checks: newChecks } };
        });
      }, 800 + i * 700);
    });
  },

  clearUpload: () => set({ uploadedDoc: null, isScanning: false }),

  // ── Obligations ──────────────────────────────────────────────────────────
  obligations: mockObligations,

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
  agentMessages: mockAgentMessages,
  isSwarmActive: false,

  startSwarm: () => set({ isSwarmActive: true }),
  stopSwarm: () => set({ isSwarmActive: false }),
  clearMessages: () => set({ agentMessages: [] }),

  // ── Tensions ─────────────────────────────────────────────────────────────
  tensions: mockTensions,

  acknowledgeTension: (id) =>
    set((state) => ({
      tensions: state.tensions.map((t) =>
        t.id === id ? { ...t, status: "ACKNOWLEDGED" } : t
      ),
    })),

  // ── DAG / Blast Radius ───────────────────────────────────────────────────
  dagNodes: mockDagNodes,
  dagEdges: mockDagEdges,
  blastRadiusActive: false,
  blastSourceId: null,

  triggerBlastRadius: (nodeId) =>
    set({ blastRadiusActive: true, blastSourceId: nodeId }),

  resetBlastRadius: () =>
    set({ blastRadiusActive: false, blastSourceId: null }),

  // ── HITL Log ─────────────────────────────────────────────────────────────
  overrideLog: [],
}));
