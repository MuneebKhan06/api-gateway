// A stat tile trend: one series in a recessive tone, the latest point in the
// accent. No axes; the tile's own value and label carry the numbers, and the
// title on the SVG reads out the range for anyone who cannot see the line.

interface SparklineProps {
  values: number[];
  label: string;
  format: (value: number) => string;
  width?: number;
  height?: number;
}

export default function Sparkline({ values, label, format, width = 160, height = 36 }: SparklineProps) {
  if (values.length < 2) {
    return <div className="sparkline-empty hint">collecting</div>;
  }
  const max = Math.max(...values, 1e-9);
  const pad = 4;
  const x = (index: number) => pad + (index / (values.length - 1)) * (width - pad * 2);
  const y = (value: number) => pad + (height - pad * 2) * (1 - value / max);
  const path = values.map((value, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ");
  const last = values[values.length - 1];
  const description = `${label}: latest ${format(last)}, peak ${format(max)} over the last ${values.length} scrapes`;

  return (
    <svg className="sparkline" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={description}>
      <title>{description}</title>
      <line className="baseline" x1={pad} x2={width - pad} y1={height - pad} y2={height - pad} />
      <path className="trend" d={path} />
      <circle className="end" cx={x(values.length - 1)} cy={y(last)} r={3.5} />
    </svg>
  );
}
