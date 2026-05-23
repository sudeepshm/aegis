/**
 * aegis-frontend/lib/api.ts
 * =========================
 * Phase 9 — Enterprise Intelligence Upgrade.
 * Typed fetch wrappers for the FastAPI backend.
 *
 * Backend base URL is configured via process.env.NEXT_PUBLIC_API_URL
 * with a fallback to http://localhost:8000.
 */

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const API_KEY = process.env.NEXT_PUBLIC_CORRECTIONS_API_KEY || "";

// ─── Interfaces ──────────────────────────────────────────────────────────────

export interface CorrectionPayload {
  chain_id: string;
  corrected_by: string;
  stage: string;
  regulation_ref: string;
  domain: string;
  original_value: any;
  corrected_value: any;
  comment?: string;
}

export interface CorrectionResponse {
  status: string;
  correction_id: string;
  chain_id: string;
  message: string;
  timestamp: string;
}

export interface PolicyTension {
  id: string;
  title: string;
  severity: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
  regulationA: { name: string; excerpt: string };
  regulationB: { name: string; excerpt: string };
  contradiction: string;
  compensatingControl: string;
  status: "UNRESOLVED" | "ACKNOWLEDGED" | "CONTROL_APPLIED" | "REVIEWED" | "DISMISSED";
}

export interface BlastRadiusRequest {
  dag_nodes: any[];
  delayed_task: string;
  delay_days: number;
  regulation_deadlines?: Record<string, string>;
  task_duration_days?: number;
  base_risk_score?: number;
}

export interface BlastRadiusResponse {
  delayed_task: string;
  delay_days: number;
  cascade_impact: any[];
  deadline_breaches: any[];
  risk_score_delta: number;
  narrative: string;
  // Moderate tier keys
  affected_tasks: string[];
  risk_delta: number;
}

// ─── Request Helper ──────────────────────────────────────────────────────────

async function apiFetch<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const url = `${BASE_URL}${endpoint}`;
  const headers = new Headers(options.headers);

  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (API_KEY) {
    headers.set("X-API-Key", API_KEY);
  }

  try {
    const response = await fetch(url, {
      ...options,
      headers,
    });

    if (!response.ok) {
      let errorDetail = response.statusText;
      try {
        const errJson = await response.json();
        errorDetail = errJson.detail || JSON.stringify(errJson);
      } catch {
        // Fall back if response is not JSON
      }
      throw new Error(`API Error [${response.status}] ${endpoint}: ${errorDetail}`);
    }

    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof Error) {
      // Robust logging/handling for network and standard errors
      console.error(`[API Network/Request Failure] URL: ${url}`, error);
      throw new Error(`Network failure or API server unreachable: ${error.message}`);
    }
    throw new Error("An unexpected network error occurred.");
  }
}

// ─── API Methods ─────────────────────────────────────────────────────────────

/**
 * Log a Chief Compliance Officer correction to the backend's WORM audit chain.
 * POST /api/v1/corrections
 */
export async function submitCorrection(payload: CorrectionPayload): Promise<CorrectionResponse> {
  return apiFetch<CorrectionResponse>("/api/v1/corrections", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/**
 * Fetch all policy tension records from Redis.
 * GET /api/tensions
 */
export async function fetchTensions(regulator?: string, status?: string): Promise<any[]> {
  const params = new URLSearchParams();
  if (regulator) params.append("regulator", regulator);
  if (status) params.append("status", status);

  const queryStr = params.toString() ? `?${params.toString()}` : "";
  return apiFetch<any[]>(`/api/tensions${queryStr}`);
}

/**
 * Update the status of a tension record (e.g. REVIEWED or DISMISSED).
 * PATCH /api/tensions/{id}
 */
export async function updateTension(id: string, status: string): Promise<any> {
  return apiFetch<any>(`/api/tensions/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
}

/**
 * Simulate the blast radius of a task delay.
 * POST /api/v1/simulate_delay
 */
export async function simulateBlastRadius(
  nodeId: string,
  delayDays: number,
  dagNodes: any[] = []
): Promise<BlastRadiusResponse> {
  const payload: BlastRadiusRequest = {
    dag_nodes: dagNodes,
    delayed_task: nodeId,
    delay_days: delayDays,
    task_duration_days: 5,
    base_risk_score: 5.0,
  };

  return apiFetch<BlastRadiusResponse>("/api/v1/simulate_delay", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
