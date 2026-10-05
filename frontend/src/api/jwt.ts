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

/** Seconds until expiry, negative once expired, null if the token has no exp. */
export function secondsUntilExpiry(claims: JwtClaims, now = Date.now()): number | null {
  if (typeof claims.exp !== "number") return null;
  return Math.round(claims.exp - now / 1000);
}
