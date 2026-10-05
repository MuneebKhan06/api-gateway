export const ALGORITHM_LABELS: Record<string, string> = {
  token_bucket: "Token bucket",
  sliding_window: "Sliding window",
  fixed_window: "Fixed window",
};

export function algorithmLabel(name: string): string {
  return ALGORITHM_LABELS[name] ?? name;
}

export function formatMs(ms: number): string {
  if (ms < 1) return `${ms.toFixed(2)} ms`;
  if (ms < 100) return `${ms.toFixed(1)} ms`;
  return `${Math.round(ms)} ms`;
}

/** Two most significant units: "45s", "14m 54s", "6d 23h". */
export function formatSeconds(seconds: number): string {
  const units: [string, number][] = [
    ["d", 86400],
    ["h", 3600],
    ["m", 60],
    ["s", 1],
  ];
  const parts: string[] = [];
  let rest = Math.max(0, Math.round(seconds));
  for (const [suffix, size] of units) {
    const count = Math.floor(rest / size);
    rest -= count * size;
    if (count > 0 || parts.length > 0) parts.push(`${count}${suffix}`);
    if (parts.length === 2) break;
  }
  if (parts.length === 0) return "0s";
  return parts.filter((part, index) => index === 0 || !part.startsWith("0")).join(" ");
}

export function timeAgo(timestamp: number | null, now = Date.now()): string {
  if (timestamp === null) return "never";
  const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
  return seconds < 2 ? "just now" : `${seconds}s ago`;
}
