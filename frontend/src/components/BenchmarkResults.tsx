import { algorithmLabel } from "../utils/format";

// Measured numbers from scripts/benchmark_algorithms.py, as published in the
// README: 10,000 requests, 50 concurrent, 200 clients, Redis 7 in Docker on
// one machine. Static because they describe a benchmark run, not live state.

interface Row {
  algorithm: string;
  throughput: number;
  avgMs: number;
  p95Ms: number;
  bytesPerClient: number;
  complexity: string;
}

const ROWS: Row[] = [
  { algorithm: "fixed_window", throughput: 2112, avgMs: 18.78, p95Ms: 25.56, bytesPerClient: 120, complexity: "O(1)" },
  { algorithm: "token_bucket", throughput: 1854, avgMs: 21.63, p95Ms: 30.45, bytesPerClient: 175, complexity: "O(1)" },
  { algorithm: "sliding_window", throughput: 1930, avgMs: 20.63, p95Ms: 28.26, bytesPerClient: 3192, complexity: "O(requests)" },
];

interface MetricProps {
  title: string;
  note: string;
  value: (row: Row) => number;
  format: (value: number) => string;
}

/** Horizontal bars from a shared zero baseline, one metric per chart. */
function Metric({ title, note, value, format }: MetricProps) {
  const max = Math.max(...ROWS.map(value));
  return (
    <div className="stack bench-metric">
      <div>
        <strong>{title}</strong>
        <div className="hint">{note}</div>
      </div>
      {ROWS.map((row) => (
        <div key={row.algorithm} className="bench-row" title={`${algorithmLabel(row.algorithm)}: ${format(value(row))}`}>
          <span className="bench-label">{algorithmLabel(row.algorithm)}</span>
          <span className="bench-track">
            <span className="bench-bar" style={{ width: `${(value(row) / max) * 100}%` }} />
          </span>
          <span className="bench-value mono">{format(value(row))}</span>
        </div>
      ))}
    </div>
  );
}

export default function BenchmarkResults() {
  return (
    <section className="card">
      <div className="card-header">
        <h2>Measured, not guessed</h2>
        <span>scripts/benchmark_algorithms.py, 10,000 requests, 50 concurrent</span>
      </div>

      <div className="grid grid-3">
        <Metric
          title="Throughput"
          note="requests per second, higher is better"
          value={(row) => row.throughput}
          format={(value) => value.toLocaleString()}
        />
        <Metric
          title="P95 latency"
          note="milliseconds, lower is better"
          value={(row) => row.p95Ms}
          format={(value) => `${value.toFixed(1)} ms`}
        />
        <Metric
          title="Memory per client"
          note="bytes at 50 requests each, lower is better"
          value={(row) => row.bytesPerClient}
          format={(value) => value.toLocaleString()}
        />
      </div>

      <div className="table-wrap" style={{ marginTop: 16 }}>
        <table>
          <thead>
            <tr>
              <th>Algorithm</th>
              <th>Throughput</th>
              <th>Avg</th>
              <th>P95</th>
              <th>Memory per client</th>
            </tr>
          </thead>
          <tbody>
            {ROWS.map((row) => (
              <tr key={row.algorithm}>
                <td>{algorithmLabel(row.algorithm)}</td>
                <td className="mono">{row.throughput.toLocaleString()}/s</td>
                <td className="mono">{row.avgMs} ms</td>
                <td className="mono">{row.p95Ms} ms</td>
                <td className="mono">
                  {row.bytesPerClient.toLocaleString()} B, {row.complexity}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="hint" style={{ marginBottom: 0 }}>
        Throughput is close because every algorithm is one Redis round trip. Memory is not: the
        sliding window keeps an entry per request, about 26 times the others here, and that grows
        with traffic. Token bucket is the default anyway, because it is the only one that allows a
        legitimate burst while capping the sustained rate. Moving the clock read inside Lua took
        it from 57 to 86 percent of fixed window's throughput by cutting a round trip.
      </p>
    </section>
  );
}
