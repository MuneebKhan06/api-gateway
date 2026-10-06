import type { DraftRequest } from "../components/RequestBuilder";
import { parseHeaderLines } from "../components/RequestBuilder";

// The console talks to the gateway through /gw, but a curl command should
// look the way the README shows it: straight at the gateway's own port.
export const GATEWAY_ORIGIN = "http://localhost:8000";

// Tokens are referenced by variable rather than pasted in, so copying a
// command never copies a live credential into a terminal history.
const TOKEN_VARIABLE: Record<DraftRequest["auth"], string | null> = {
  session: "$TOKEN",
  tampered: "$TAMPERED_TOKEN",
  refresh: "$REFRESH_TOKEN",
  none: null,
};

function quote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

export function toCurl(draft: DraftRequest): string {
  const parts = ["curl -i"];
  if (draft.method !== "GET") parts.push(`-X ${draft.method}`);
  parts.push(quote(`${GATEWAY_ORIGIN}${draft.path}`));

  const token = TOKEN_VARIABLE[draft.auth];
  // Double quotes so the shell expands the variable.
  if (token) parts.push(`-H "Authorization: Bearer ${token}"`);

  for (const [name, value] of Object.entries(parseHeaderLines(draft.headers))) {
    parts.push(`-H ${quote(`${name}: ${value}`)}`);
  }

  const hasBody = draft.method !== "GET" && draft.method !== "DELETE";
  if (hasBody && draft.body.trim()) {
    parts.push(`-H 'Content-Type: application/json'`);
    let body = draft.body;
    try {
      body = JSON.stringify(JSON.parse(draft.body));
    } catch {
      // Send it as typed; the playground reports invalid JSON separately.
    }
    parts.push(`-d ${quote(body)}`);
  }

  return parts.join(" \\\n  ");
}
