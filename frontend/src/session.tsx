import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import type { TokenPair } from "./api/types";

// The signed in session, shared by every page that sends authenticated
// requests. Kept in sessionStorage so a reload keeps the demo going but
// closing the tab forgets the tokens.

const STORAGE_KEY = "gateway-console-session";

export interface Session {
  email: string;
  accessToken: string;
  refreshToken: string;
  expiresIn: number;
  issuedAt: number;
}

interface SessionContextValue {
  session: Session | null;
  signIn: (email: string, pair: TokenPair) => void;
  rotate: (pair: TokenPair) => void;
  signOut: () => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

function load(): Session | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

function save(session: Session | null): void {
  try {
    if (session) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage blocked: the session still works until the page reloads.
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(load);

  const update = useCallback((next: Session | null) => {
    save(next);
    setSession(next);
  }, []);

  const signIn = useCallback(
    (email: string, pair: TokenPair) =>
      update({
        email,
        accessToken: pair.access_token,
        refreshToken: pair.refresh_token,
        expiresIn: pair.expires_in,
        issuedAt: Date.now(),
      }),
    [update],
  );

  const rotate = useCallback(
    (pair: TokenPair) =>
      setSession((current) => {
        if (!current) return current;
        const next = {
          ...current,
          accessToken: pair.access_token,
          refreshToken: pair.refresh_token,
          expiresIn: pair.expires_in,
          issuedAt: Date.now(),
        };
        save(next);
        return next;
      }),
    [],
  );

  const signOut = useCallback(() => update(null), [update]);

  const value = useMemo(
    () => ({ session, signIn, rotate, signOut }),
    [session, signIn, rotate, signOut],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside SessionProvider");
  return value;
}
