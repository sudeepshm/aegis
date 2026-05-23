"use client";

import React, { useEffect, useRef, useState, useCallback } from "react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface PipelineEvent {
  job_id: string;
  stage: string;
  status: "STARTED" | "COMPLETED" | "ERROR";
  log: string;
  payload?: Record<string, unknown>;
  timestamp: string;
  type?: string; // for ping events
}

interface PipelineTerminalProps {
  /** Unique job identifier for the pipeline run */
  jobId: string;
  /** WebSocket base URL (e.g., "ws://localhost:8000") */
  wsBaseUrl: string;
  /** Chunk text to send when triggering the pipeline */
  chunkText?: string;
  /** Regulator context ("rbi" | "sebi") */
  regulator?: string;
  /** Auto-trigger pipeline on mount */
  autoStart?: boolean;
}

// ---------------------------------------------------------------------------
// Status color mapping
// ---------------------------------------------------------------------------

const STATUS_COLORS: Record<string, string> = {
  STARTED: "text-cyan-400",
  COMPLETED: "text-emerald-400",
  ERROR: "text-red-400",
  PIPELINE_COMPLETE: "text-amber-300 font-bold",
  PIPELINE_ERROR: "text-red-500 font-bold",
};

const STAGE_ICONS: Record<string, string> = {
  obligation_extraction: "📋",
  action_decomposition: "🔧",
  sme_rule_injection: "📐",
  json_structuring: "🏗️",
  guardrails_validation: "🛡️",
  conflict_detector: "⚡",
  PIPELINE_COMPLETE: "✅",
  PIPELINE_ERROR: "❌",
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function PipelineTerminal({
  jobId,
  wsBaseUrl,
  chunkText = "",
  regulator = "rbi",
  autoStart = false,
}: PipelineTerminalProps) {
  const [logs, setLogs] = useState<PipelineEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isComplete, setIsComplete] = useState(false);
  const [isRunning, setIsRunning] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll on new logs
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [logs]);

  // WebSocket connection
  useEffect(() => {
    const wsUrl = `${wsBaseUrl}/ws/pipeline/${jobId}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      setError(null);
      setLogs((prev) => [
        ...prev,
        {
          job_id: jobId,
          stage: "SYSTEM",
          status: "COMPLETED",
          log: `Connected to pipeline WebSocket`,
          timestamp: new Date().toISOString(),
        },
      ]);

      // Auto-start if configured
      if (autoStart && chunkText) {
        ws.send(
          JSON.stringify({
            chunk_text: chunkText,
            regulator,
          })
        );
        setIsRunning(true);
      }
    };

    ws.onmessage = (event) => {
      try {
        const data: PipelineEvent = JSON.parse(event.data);

        // Ignore ping events
        if (data.type === "ping") return;

        setLogs((prev) => [...prev, data]);

        if (
          data.stage === "PIPELINE_COMPLETE" ||
          data.stage === "PIPELINE_ERROR"
        ) {
          setIsComplete(true);
          setIsRunning(false);
        }
      } catch {
        // Ignore malformed messages
      }
    };

    ws.onerror = () => {
      setError("WebSocket connection error");
      setConnected(false);
    };

    ws.onclose = () => {
      setConnected(false);
      setIsRunning(false);
    };

    return () => {
      ws.close();
    };
  }, [jobId, wsBaseUrl, autoStart, chunkText, regulator]);

  // Manual trigger
  const handleStart = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN && chunkText) {
      wsRef.current.send(
        JSON.stringify({
          chunk_text: chunkText,
          regulator,
        })
      );
      setIsRunning(true);
      setIsComplete(false);
      setLogs([]);
    }
  }, [chunkText, regulator]);

  return (
    <div
      className="rounded-xl overflow-hidden border border-white/10"
      style={{
        background: "linear-gradient(145deg, #0d1117 0%, #161b22 100%)",
      }}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/10">
        <div className="flex items-center gap-3">
          {/* Traffic light dots */}
          <div className="flex gap-1.5">
            <span className="w-3 h-3 rounded-full bg-red-500/80" />
            <span className="w-3 h-3 rounded-full bg-yellow-500/80" />
            <span className="w-3 h-3 rounded-full bg-green-500/80" />
          </div>
          <span className="text-xs text-gray-400 font-mono">
            pipeline/{jobId.slice(0, 8)}
          </span>
        </div>

        <div className="flex items-center gap-3">
          {/* Connection indicator */}
          <span className="flex items-center gap-1.5 text-xs">
            <span
              className={`w-2 h-2 rounded-full ${
                connected ? "bg-emerald-400 animate-pulse" : "bg-red-400"
              }`}
            />
            <span className="text-gray-400">
              {connected ? "Connected" : "Disconnected"}
            </span>
          </span>

          {/* Spinner while running */}
          {isRunning && (
            <svg
              className="w-4 h-4 text-cyan-400 animate-spin"
              viewBox="0 0 24 24"
              fill="none"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
              />
            </svg>
          )}

          {/* Manual start button */}
          {!autoStart && !isRunning && chunkText && (
            <button
              onClick={handleStart}
              disabled={!connected}
              className="px-3 py-1 text-xs font-medium rounded-md
                         bg-cyan-500/20 text-cyan-400 border border-cyan-500/30
                         hover:bg-cyan-500/30 transition-colors
                         disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Run Pipeline
            </button>
          )}
        </div>
      </div>

      {/* Terminal body */}
      <div
        ref={scrollRef}
        className="p-4 overflow-y-auto font-mono text-sm leading-relaxed"
        style={{ maxHeight: "480px", minHeight: "240px" }}
      >
        {error && (
          <div className="mb-3 px-3 py-2 rounded-md bg-red-500/10 border border-red-500/20 text-red-400 text-xs">
            {error}
          </div>
        )}

        {logs.length === 0 && !isRunning && (
          <p className="text-gray-500 italic text-xs">
            Awaiting pipeline execution…
          </p>
        )}

        {logs.map((event, i) => (
          <LogLine key={`${event.timestamp}-${i}`} event={event} />
        ))}

        {isComplete && (
          <div className="mt-3 pt-3 border-t border-white/5 text-xs text-gray-500">
            Pipeline execution finished. {logs.filter((l) => l.status === "COMPLETED").length}{" "}
            stage(s) completed.
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// LogLine sub-component
// ---------------------------------------------------------------------------

function LogLine({ event }: { event: PipelineEvent }) {
  const icon = STAGE_ICONS[event.stage] || "●";
  const colorClass =
    event.stage === "PIPELINE_COMPLETE" || event.stage === "PIPELINE_ERROR"
      ? STATUS_COLORS[event.stage]
      : STATUS_COLORS[event.status] || "text-gray-300";

  const time = event.timestamp
    ? new Date(event.timestamp).toLocaleTimeString("en-IN", {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "";

  return (
    <div className="flex gap-2 items-start py-0.5 group hover:bg-white/[0.02] rounded px-1 -mx-1">
      <span className="text-gray-600 text-xs w-16 shrink-0 pt-0.5 tabular-nums">
        {time}
      </span>
      <span className="w-5 shrink-0 text-center">{icon}</span>
      <span className={`${colorClass} break-words`}>{event.log}</span>
      {event.payload && Object.keys(event.payload).length > 0 && (
        <span className="text-gray-600 text-xs ml-auto shrink-0">
          {JSON.stringify(event.payload)}
        </span>
      )}
    </div>
  );
}
