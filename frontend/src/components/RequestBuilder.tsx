import type { FormEvent } from "react";

export type AuthMode = "session" | "none" | "tampered" | "refresh";

export interface DraftRequest {
  method: string;
  path: string;
  auth: AuthMode;
  /** One "Name: value" per line, so headers are as easy to edit as curl -H. */
  headers: string;
  body: string;
}

export interface Preset {
  label: string;
  expect: string;
  draft: DraftRequest;
}

const base: DraftRequest = { method: "GET", path: "/", auth: "session", headers: "", body: "" };

export const PRESETS: Preset[] = [
  {
    label: "List orders",
    expect: "200 from service-a, token bucket limit",
    draft: { ...base, path: "/api/orders" },
  },
  {
    label: "Create an order",
    expect: "201, body forwarded upstream",
    draft: {
      ...base,
      method: "POST",
      path: "/api/orders",
      body: JSON.stringify({ customer: "ayesha", total: 42.5 }, null, 2),
    },
  },
  {
    label: "List users",
    expect: "200 from service-b, sliding window limit",
    draft: { ...base, path: "/api/users" },
  },
  {
    label: "Inventory item",
    expect: "200 from service-c, fixed window limit",
    draft: { ...base, path: "/api/inventory/SKU-1001" },
  },
  {
    label: "What the upstream sees",
    expect: "spoofed identity header stripped, real one injected",
    draft: {
      ...base,
      path: "/api/orders/_echo/headers",
      headers: "X-Gateway-User-Id: someone-else",
    },
  },
  {
    label: "Your own request ID",
    expect: "X-Request-ID honoured end to end",
    draft: { ...base, path: "/api/orders", headers: "X-Request-ID: demo-trace-0001" },
  },
  {
    label: "No token",
    expect: "401 missing_token",
    draft: { ...base, path: "/api/orders", auth: "none" },
  },
  {
    label: "Tampered token",
    expect: "401 invalid_token",
    draft: { ...base, path: "/api/orders", auth: "tampered" },
  },
  {
    label: "Refresh token as bearer",
    expect: "401 wrong_token_type",
    draft: { ...base, path: "/api/orders", auth: "refresh" },
  },
  {
    label: "Unknown path",
    expect: "404 before auth, so paths do not leak",
    draft: { ...base, path: "/api/does-not-exist", auth: "none" },
  },
  {
    label: "Not found upstream",
    expect: "404 from the upstream, not the gateway",
    draft: { ...base, path: "/api/orders/zzzz" },
  },
  {
    label: "Gateway health",
    expect: "public, answered by the gateway",
    draft: { ...base, path: "/health", auth: "none" },
  },
];

const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"];

const AUTH_OPTIONS: { value: AuthMode; label: string; needsSession: boolean }[] = [
  { value: "session", label: "Access token", needsSession: true },
  { value: "none", label: "No token", needsSession: false },
  { value: "tampered", label: "Tampered (roles: admin)", needsSession: true },
  { value: "refresh", label: "Refresh token", needsSession: true },
];

export function parseHeaderLines(text: string): Record<string, string> {
  const headers: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const index = line.indexOf(":");
    if (index <= 0) continue;
    const name = line.slice(0, index).trim();
    const value = line.slice(index + 1).trim();
    if (name) headers[name] = value;
  }
  return headers;
}

interface RequestBuilderProps {
  draft: DraftRequest;
  onChange: (draft: DraftRequest) => void;
  onSend: () => void;
  busy: boolean;
  signedIn: boolean;
}

export default function RequestBuilder({
  draft,
  onChange,
  onSend,
  busy,
  signedIn,
}: RequestBuilderProps) {
  const set = <K extends keyof DraftRequest>(key: K, value: DraftRequest[K]) =>
    onChange({ ...draft, [key]: value });

  const needsSession = AUTH_OPTIONS.find((option) => option.value === draft.auth)?.needsSession;
  const blocked = needsSession && !signedIn;
  const hasBody = draft.method !== "GET" && draft.method !== "DELETE";

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!blocked) onSend();
  };

  return (
    <form className="stack" onSubmit={submit}>
      <div className="presets">
        {PRESETS.map((preset) => (
          <button
            key={preset.label}
            type="button"
            className="preset"
            onClick={() => onChange({ ...preset.draft })}
            title={preset.expect}
          >
            <strong>{preset.label}</strong>
            <span>{preset.expect}</span>
          </button>
        ))}
      </div>

      <div className="request-line">
        <select
          value={draft.method}
          onChange={(event) => set("method", event.target.value)}
          aria-label="Method"
        >
          {METHODS.map((method) => (
            <option key={method}>{method}</option>
          ))}
        </select>
        <input
          className="mono"
          value={draft.path}
          onChange={(event) => set("path", event.target.value)}
          aria-label="Path"
          spellCheck={false}
        />
        <button type="submit" className="primary" disabled={busy || blocked}>
          {busy ? "Sending" : "Send"}
        </button>
      </div>

      <div className="grid grid-2">
        <label>
          Authorization
          <select value={draft.auth} onChange={(event) => set("auth", event.target.value as AuthMode)}>
            {AUTH_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Extra headers, one per line
          <textarea
            className="mono"
            rows={2}
            value={draft.headers}
            placeholder="X-Request-ID: my-trace"
            onChange={(event) => set("headers", event.target.value)}
            spellCheck={false}
          />
        </label>
      </div>

      {hasBody && (
        <label>
          JSON body
          <textarea
            className="mono"
            rows={4}
            value={draft.body}
            onChange={(event) => set("body", event.target.value)}
            spellCheck={false}
          />
        </label>
      )}

      {blocked && (
        <div className="callout">
          This needs a signed in session. Log in on the Authentication page first, or switch to
          No token to see the gateway refuse it.
        </div>
      )}
    </form>
  );
}
