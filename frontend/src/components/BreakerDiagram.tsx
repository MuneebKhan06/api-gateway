import { useId } from "react";
import { FAILURE_WINDOW_SECONDS, SUCCESS_THRESHOLD } from "../utils/breaker";

// The breaker's state machine, drawn from the same rules as breaker.py. The
// live state is filled in, so the diagram doubles as a status indicator.
// State carries a status colour here, always next to its name.

type State = "closed" | "open" | "half_open";

interface BreakerDiagramProps {
  state: string;
  failureThreshold: number;
  recoveryTimeoutSeconds: number;
}

const NODES: Record<State, { x: number; y: number; label: string; tone: string }> = {
  closed: { x: 90, y: 64, label: "CLOSED", tone: "ok" },
  open: { x: 430, y: 64, label: "OPEN", tone: "bad" },
  half_open: { x: 260, y: 206, label: "HALF OPEN", tone: "warn" },
};

interface Edge {
  id: string;
  d: string;
  label: string;
  lx: number;
  ly: number;
  anchor?: "start" | "middle" | "end";
}

const NODE_W = 124;
const NODE_H = 42;

export default function BreakerDiagram({
  state,
  failureThreshold,
  recoveryTimeoutSeconds,
}: BreakerDiagramProps) {
  // One page shows a diagram per upstream, so the marker id must be unique.
  // useId returns colons, which are not safe inside url(#...).
  const arrowId = `arrow${useId().replace(/:/g, "")}`;
  const edges: Edge[] = [
    {
      id: "trip",
      d: "M154 64 L366 64",
      label: `${failureThreshold} failures in a row within ${FAILURE_WINDOW_SECONDS}s`,
      lx: 260,
      ly: 52,
    },
    {
      id: "timeout",
      d: "M470 87 Q476 196 324 210",
      label: `${recoveryTimeoutSeconds}s elapse`,
      lx: 432,
      ly: 176,
      anchor: "start",
    },
    {
      id: "reopen",
      d: "M290 184 Q330 112 410 87",
      label: "any failure",
      lx: 334,
      ly: 132,
      anchor: "start",
    },
    {
      id: "recover",
      d: "M196 210 Q60 200 76 87",
      label: `${SUCCESS_THRESHOLD} successes`,
      lx: 92,
      ly: 176,
      anchor: "end",
    },
  ];

  return (
    <svg
      className="breaker-diagram"
      viewBox="0 0 520 250"
      role="img"
      aria-label={`Circuit breaker state machine, currently ${state.replace("_", " ")}`}
    >
      <defs>
        <marker id={arrowId} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0 L10 5 L0 10 z" className="arrow-head" />
        </marker>
      </defs>

      {edges.map((edge) => (
        <g key={edge.id}>
          <path className="edge" d={edge.d} markerEnd={`url(#${arrowId})`} />
          <text className="edge-label" x={edge.lx} y={edge.ly} textAnchor={edge.anchor ?? "middle"}>
            {edge.label}
          </text>
        </g>
      ))}

      {(Object.keys(NODES) as State[]).map((key) => {
        const node = NODES[key];
        const active = key === state;
        return (
          <g key={key} className={`node ${node.tone}${active ? " active" : ""}`}>
            <rect
              x={node.x - NODE_W / 2}
              y={node.y - NODE_H / 2}
              width={NODE_W}
              height={NODE_H}
              rx={10}
            />
            <text x={node.x} y={node.y} textAnchor="middle" dominantBaseline="central">
              {node.label}
            </text>
            {active && (
              <text className="now" x={node.x} y={node.y - NODE_H / 2 - 8} textAnchor="middle">
                now
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}
