import { useMemo, useRef, useState, type PointerEvent } from "react";

// Remaining allowance over time, one marker per request. Shared by the live
// burst runner and the simulations, so a real run and a replay look alike.
//
// One series, so no legend box for it; the legend covers the two marker
// states instead, and those carry a shape as well as a colour so allowed and
// refused never depend on colour alone.

export interface ChartPoint {
  /** Seconds since the start. */
  x: number;
  /** Remaining allowance after this request. */
  y: number;
  allowed: boolean;
  /** One line describing the request, shown in the tooltip and table. */
  label: string;
}

interface BurstChartProps {
  points: ChartPoint[];
  yMax: number;
  /** Fixed x extent, so a chart does not rescale while a burst is running. */
  xMax?: number;
  height?: number;
  title: string;
  /** Optional vertical markers, e.g. a window boundary. */
  markers?: { x: number; label: string }[];
  /**
   * For small multiples: no caption, legend or table toggle. The group
   * shows one shared ChartLegend instead, so cells do not repeat it.
   */
  compact?: boolean;
}

/** The two marker states, by shape as well as colour. */
export function ChartLegend({ children }: { children?: React.ReactNode }) {
  return (
    <span className="chart-legend">
      <span>
        <svg width="12" height="12" aria-hidden="true">
          <circle cx="6" cy="6" r="4.5" fill="var(--ok)" />
        </svg>
        allowed
      </span>
      <span>
        <svg width="12" height="12" aria-hidden="true">
          <path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="var(--bad)" strokeWidth="2.5" strokeLinecap="round" />
        </svg>
        429 refused
      </span>
      {children}
    </span>
  );
}

const WIDTH = 720;
const PAD = { top: 14, right: 16, bottom: 30, left: 44 };

function niceStep(max: number, target: number): number {
  const raw = max / target;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const normalized = raw / magnitude;
  const nice = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return nice * magnitude;
}

function ticks(max: number, target: number): number[] {
  if (max <= 0) return [0];
  const step = niceStep(max, target);
  const values: number[] = [];
  for (let value = 0; value <= max + step * 0.001; value += step) values.push(Number(value.toFixed(6)));
  return values;
}

function formatSecondsTick(value: number): string {
  return value >= 10 || Number.isInteger(value) ? `${Math.round(value)}s` : `${value.toFixed(1)}s`;
}

export default function BurstChart({
  points,
  yMax,
  xMax,
  height = 220,
  title,
  markers = [],
  compact = false,
}: BurstChartProps) {
  const [hover, setHover] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);

  const sorted = useMemo(() => [...points].sort((a, b) => a.x - b.x), [points]);
  const extentX = Math.max(xMax ?? 0, sorted.length ? sorted[sorted.length - 1].x : 0, 1);
  const extentY = Math.max(yMax, 1);

  const plotW = WIDTH - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const sx = (x: number) => PAD.left + (x / extentX) * plotW;
  const sy = (y: number) => PAD.top + plotH - (y / extentY) * plotH;

  const path = sorted
    .map((point, index) => `${index === 0 ? "M" : "L"}${sx(point.x).toFixed(1)},${sy(point.y).toFixed(1)}`)
    .join(" ");

  // Hover snaps to the nearest request on x, which is what a crosshair on a
  // dense run needs; hunting for an 8px dot among 100 is not usable.
  const onMove = (event: PointerEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg || sorted.length === 0) return;
    const rect = svg.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * WIDTH;
    let best = 0;
    for (let index = 1; index < sorted.length; index += 1) {
      if (Math.abs(sx(sorted[index].x) - x) < Math.abs(sx(sorted[best].x) - x)) best = index;
    }
    setHover(best);
  };

  const hovered = hover !== null ? sorted[hover] : null;
  const markerSize = sorted.length > 150 ? 3 : 4;

  return (
    <figure className="burst-chart">
      {!compact && (
        <figcaption className="row">
          <span className="hint">{title}</span>
          <ChartLegend>
            <button type="button" className="small" onClick={() => setShowTable((value) => !value)}>
              {showTable ? "Chart" : "Table"}
            </button>
          </ChartLegend>
        </figcaption>
      )}

      {showTable ? (
        <div className="table-wrap chart-table">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Result</th>
                <th>Remaining</th>
                <th>Request</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((point, index) => (
                <tr key={index}>
                  <td className="mono">{point.x.toFixed(2)}s</td>
                  <td>{point.allowed ? "allowed" : "refused"}</td>
                  <td className="mono">{point.y}</td>
                  <td>{point.label}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="chart-frame">
          <svg
            ref={svgRef}
            viewBox={`0 0 ${WIDTH} ${height}`}
            role="img"
            aria-label={title}
            onPointerMove={onMove}
            onPointerLeave={() => setHover(null)}
          >
            {ticks(extentY, 4).map((value) => (
              <g key={`y${value}`}>
                <line className="grid" x1={PAD.left} x2={WIDTH - PAD.right} y1={sy(value)} y2={sy(value)} />
                <text className="tick" x={PAD.left - 8} y={sy(value)} textAnchor="end" dominantBaseline="middle">
                  {value}
                </text>
              </g>
            ))}
            {ticks(extentX, 6).map((value) => (
              <text key={`x${value}`} className="tick" x={sx(value)} y={height - 10} textAnchor="middle">
                {formatSecondsTick(value)}
              </text>
            ))}

            {markers.map((marker) => (
              <g key={marker.label}>
                <line className="boundary" x1={sx(marker.x)} x2={sx(marker.x)} y1={PAD.top} y2={PAD.top + plotH} />
                <text className="tick" x={sx(marker.x) + 4} y={PAD.top + 10}>
                  {marker.label}
                </text>
              </g>
            ))}

            {sorted.length > 1 && <path className="series" d={path} />}

            {hovered && (
              <line className="crosshair" x1={sx(hovered.x)} x2={sx(hovered.x)} y1={PAD.top} y2={PAD.top + plotH} />
            )}

            {sorted.map((point, index) => {
              const cx = sx(point.x);
              const cy = sy(point.y);
              const active = index === hover;
              const r = active ? markerSize + 2 : markerSize;
              return point.allowed ? (
                <circle key={index} className="dot ok" cx={cx} cy={cy} r={r} />
              ) : (
                <path
                  key={index}
                  className="dot bad"
                  d={`M${cx - r} ${cy - r} L${cx + r} ${cy + r} M${cx + r} ${cy - r} L${cx - r} ${cy + r}`}
                />
              );
            })}
          </svg>

          {hovered && (
            <div
              className="chart-tooltip"
              style={{
                left: `${(sx(hovered.x) / WIDTH) * 100}%`,
                top: `${(sy(hovered.y) / height) * 100}%`,
              }}
            >
              <strong>{hovered.allowed ? "Allowed" : "Refused, 429"}</strong>
              <div>{hovered.label}</div>
              <div className="mono">
                {hovered.x.toFixed(2)}s, {hovered.y} left
              </div>
            </div>
          )}
        </div>
      )}
    </figure>
  );
}
