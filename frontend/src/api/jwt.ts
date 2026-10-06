// Read a JWT's claims for display. This does not verify anything: the
// gateway is the only party that can, and the console only shows what a
// token says about itself.

export interface JwtClaims {
  sub?: string;
  email?: string;
  roles?: string[];
  typ?: string;
  jti?: string;
  iat?: number;
  exp?: number;
  [key: string]: unknown;
}

function base64UrlDecode(segment: string): string {
  const base64 = segment.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

export function decodeJwt(token: string): { header: Record<string, unknown>; claims: JwtClaims } | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    return {
      header: JSON.parse(base64UrlDecode(parts[0])),
      claims: JSON.parse(base64UrlDecode(parts[1])),
    };
  } catch {
    return null;
  }
}

function base64UrlEncode(text: string): string {
  const bytes = new TextEncoder().encode(text);
  const binary = Array.from(bytes, (byte) => String.fromCharCode(byte)).join("");
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * Promote a token to admin without re-signing it, the way an attacker would
 * try to. The payload changes and the signature does not, so the gateway
 * must refuse it as invalid_token.
 */
export function tamperToken(token: string): string {
  const decoded = decodeJwt(token);
  if (!decoded) return token;
  const [header, , signature] = token.split(".");
  const payload = base64UrlEncode(JSON.stringify({ ...decoded.claims, roles: ["admin"] }));
  return `${header}.${payload}.${signature}`;
}

/** Seconds until expiry, negative once expired, null if the token has no exp. */
export function secondsUntilExpiry(claims: JwtClaims, now = Date.now()): number | null {
  if (typeof claims.exp !== "number") return null;
  return Math.round(claims.exp - now / 1000);
}
