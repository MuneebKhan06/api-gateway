const TONES: Record<string, "ok" | "warn" | "bad" | "info" | ""> = {
  healthy: "ok",
  connected: "ok",
  closed: "ok",
  degraded: "warn",
  half_open: "warn",
  unknown: "warn",
  unhealthy: "bad",
  unreachable: "bad",
  unavailable: "bad",
  open: "bad",
  offline: "bad",
  disabled: "",
};

const LABELS: Record<string, string> = {
  half_open: "half open",
};

export function toneFor(status: string): string {
  return TONES[status] ?? "";
}

export default function StatusPill({ status, label }: { status: string; label?: string }) {
  return <span className={`pill ${toneFor(status)}`}>{label ?? LABELS[status] ?? status}</span>;
}
