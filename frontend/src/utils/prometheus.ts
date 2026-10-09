// A small reader for the Prometheus text exposition format, enough to turn
// the gateway's /metrics into numbers on a page without running Prometheus.
//
// Counters only ever go up, so a value on its own says little. The useful
// numbers are differences between two scrapes, which is what PromQL's rate()
// does and what delta() below does.

export type Labels = Record<string, string>;

export interface Sample {
  name: string;
  labels: Labels;
  value: number;
}

export interface Scrape {
  /** Milliseconds since the epoch, when the scrape returned. */
  at: number;
  samples: Sample[];
  /** Metric family name to its TYPE (counter, gauge, histogram). */
  types: Record<string, string>;
  help: Record<string, string>;
}

function parseValue(raw: string): number {
  if (raw === "+Inf") return Infinity;
  if (raw === "-Inf") return -Infinity;
  if (raw === "NaN") return NaN;
  return Number(raw);
}

/** Label values may contain escaped quotes, backslashes and newlines. */
function parseLabels(body: string): Labels {
  const labels: Labels = {};
  let index = 0;
  while (index < body.length) {
    const eq = body.indexOf("=", index);
    if (eq === -1) break;
    const key = body.slice(index, eq).trim().replace(/^,/, "").trim();
    let cursor = eq + 2; // skip ="
    let value = "";
    while (cursor < body.length && body[cursor] !== '"') {
      if (body[cursor] === "\\") {
        const next = body[cursor + 1];
        value += next === "n" ? "\n" : next;
        cursor += 2;
      } else {
        value += body[cursor];
        cursor += 1;
      }
    }
    labels[key] = value;
    index = cursor + 1;
  }
  return labels;
}

export function parseExposition(text: string, at = Date.now()): Scrape {
  const samples: Sample[] = [];
  const types: Record<string, string> = {};
  const help: Record<string, string> = {};

  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    if (line.startsWith("#")) {
      const [, kind, name, ...rest] = line.split(" ");
      if (kind === "TYPE" && name) types[name] = rest[0] ?? "untyped";
      if (kind === "HELP" && name) help[name] = rest.join(" ");
      continue;
    }

    const brace = line.indexOf("{");
    const space = line.lastIndexOf(" ");
    let name: string;
    let labels: Labels = {};
    if (brace !== -1 && brace < space) {
      name = line.slice(0, brace);
      labels = parseLabels(line.slice(brace + 1, line.lastIndexOf("}")));
    } else {
      name = line.slice(0, line.indexOf(" "));
    }
    // A trailing timestamp is allowed by the format; the value is the field
    // right after the labels either way.
    const tail = line.slice(brace !== -1 ? line.lastIndexOf("}") + 1 : name.length).trim().split(/\s+/);
    // _created series are client library bookkeeping, not measurements.
    if (name.endsWith("_created")) continue;
    samples.push({ name, labels, value: parseValue(tail[0]) });
  }

  return { at, samples, types, help };
}

export type LabelFilter = Partial<Record<string, string | ((value: string) => boolean)>>;

function matches(labels: Labels, filter: LabelFilter = {}): boolean {
  return Object.entries(filter).every(([key, expected]) => {
    const actual = labels[key] ?? "";
    return typeof expected === "function" ? expected(actual) : actual === expected;
  });
}

export function select(scrape: Scrape, name: string, filter?: LabelFilter): Sample[] {
  return scrape.samples.filter((sample) => sample.name === name && matches(sample.labels, filter));
}

export function sum(scrape: Scrape, name: string, filter?: LabelFilter): number {
  return select(scrape, name, filter).reduce((total, sample) => total + sample.value, 0);
}

/** Total per value of one label, like PromQL's sum by (label). */
export function sumBy(scrape: Scrape, name: string, label: string, filter?: LabelFilter): Record<string, number> {
  const totals: Record<string, number> = {};
  for (const sample of select(scrape, name, filter)) {
    const key = sample.labels[label] ?? "";
    totals[key] = (totals[key] ?? 0) + sample.value;
  }
  return totals;
}

/**
 * Increase of a counter between two scrapes. A drop means the process
 * restarted and the counter began again from zero, so the new value is the
 * increase since then, the same reset handling rate() uses.
 */
export function delta(before: number, after: number): number {
  return after >= before ? after - before : after;
}

/** Cumulative bucket counts by upper bound, summed across other labels. */
export function buckets(scrape: Scrape, histogram: string, filter?: LabelFilter): Map<number, number> {
  const counts = new Map<number, number>();
  for (const sample of select(scrape, `${histogram}_bucket`, filter)) {
    const le = parseValue(sample.labels.le);
    counts.set(le, (counts.get(le) ?? 0) + sample.value);
  }
  return counts;
}

/** Bucket counts that accrued between two scrapes. */
export function bucketDelta(before: Map<number, number>, after: Map<number, number>): Map<number, number> {
  const result = new Map<number, number>();
  for (const [le, count] of after) result.set(le, delta(before.get(le) ?? 0, count));
  return result;
}

/**
 * The q quantile from cumulative histogram buckets, interpolated linearly
 * inside the bucket it falls in. This is what histogram_quantile() does, and
 * it is why histograms (not summaries) are the right choice across several
 * gateway instances: buckets add up, precomputed percentiles do not.
 */
export function histogramQuantile(q: number, cumulative: Map<number, number>): number | null {
  const bounds = [...cumulative.keys()].sort((a, b) => a - b);
  if (bounds.length === 0) return null;
  const total = cumulative.get(Infinity) ?? cumulative.get(bounds[bounds.length - 1]) ?? 0;
  if (total <= 0) return null;

  const rank = q * total;
  let previousBound = 0;
  let previousCount = 0;
  for (const bound of bounds) {
    const count = cumulative.get(bound) ?? 0;
    if (count >= rank) {
      // Past the last finite bucket there is no upper edge to interpolate to,
      // so report the highest finite bound, as Prometheus does.
      if (bound === Infinity) return previousBound;
      const inBucket = count - previousCount;
      if (inBucket <= 0) return bound;
      return previousBound + (bound - previousBound) * ((rank - previousCount) / inBucket);
    }
    previousBound = bound;
    previousCount = count;
  }
  return previousBound;
}
