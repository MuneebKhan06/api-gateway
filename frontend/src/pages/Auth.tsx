import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, gw, request } from "../api/client";
import { decodeJwt, secondsUntilExpiry } from "../api/jwt";
import type { ApiResult } from "../api/types";
import ResultLog, { type LogEntry } from "../components/ResultLog";
import { useSession } from "../session";
import { formatSeconds } from "../utils/format";

const DEMO_EMAIL = "demo@example.com";
const DEMO_PASSWORD = "password123";

function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

function TokenCard({ title, token, explain }: { title: string; token: string; explain: string }) {
  const now = useNow();
  const decoded = decodeJwt(token);
  const remaining = decoded ? secondsUntilExpiry(decoded.claims, now) : null;
  const [header, payload, signature] = token.split(".");

  return (
    <section className="card">
      <div className="card-header">
        <h2>{title}</h2>
        {remaining !== null && (
          <span className={`pill ${remaining > 60 ? "ok" : remaining > 0 ? "warn" : "bad"}`}>
            {remaining > 0 ? `expires in ${formatSeconds(remaining)}` : "expired"}
          </span>
        )}
      </div>
      <p className="hint" style={{ marginTop: 0 }}>
        {explain}
      </p>
      <pre className="code jwt">
        <span className="jwt-header">{header}</span>.<span className="jwt-payload">{payload}</span>.
        <span className="jwt-signature">{signature}</span>
      </pre>
      {decoded && (
        <table className="claims">
          <tbody>
            {Object.entries(decoded.claims).map(([key, value]) => (
              <tr key={key}>
                <td>
                  <code>{key}</code>
                </td>
                <td className="mono">
                  {key === "exp" || key === "iat"
                    ? `${value} (${new Date((value as number) * 1000).toLocaleTimeString()})`
                    : JSON.stringify(value)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export default function Auth() {
  const { session, signIn, rotate, signOut } = useSession();
  const [email, setEmail] = useState(DEMO_EMAIL);
  const [password, setPassword] = useState(DEMO_PASSWORD);
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState<LogEntry[]>([]);
  const nextId = useRef(1);

  // Tokens that are no longer valid, kept so the page can prove it.
  const [rotatedRefresh, setRotatedRefresh] = useState<string | null>(null);
  const [revokedAccess, setRevokedAccess] = useState<string | null>(null);

  const record = (label: string, result: ApiResult, note?: string) =>
    setLog((entries) => [{ id: nextId.current++, label, result, note }, ...entries].slice(0, 30));

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    try {
      await work();
    } finally {
      setBusy(false);
    }
  };

  const handleRegister = () =>
    run(async () => {
      const result = await api.register(email, password);
      record(
        "Register",
        result,
        result.status === 201
          ? "Account created. The password is stored as a bcrypt hash."
          : result.status === 409
            ? "That email already has an account, so just log in."
            : undefined,
      );
    });

  const handleLogin = (event?: FormEvent) => {
    event?.preventDefault();
    return run(async () => {
      const result = await api.login(email, password);
      record(
        "Log in",
        result,
        result.status === 401
          ? "Same answer for an unknown email and a wrong password, so accounts cannot be enumerated."
          : undefined,
      );
      if (result.data) {
        signIn(email, result.data);
        setRotatedRefresh(null);
        setRevokedAccess(null);
      }
    });
  };

  const handleDemo = () =>
    run(async () => {
      setEmail(DEMO_EMAIL);
      setPassword(DEMO_PASSWORD);
      const registered = await api.register(DEMO_EMAIL, DEMO_PASSWORD);
      if (registered.status === 201) record("Register demo account", registered);
      const result = await api.login(DEMO_EMAIL, DEMO_PASSWORD);
      record("Log in as demo", result);
      if (result.data) {
        signIn(DEMO_EMAIL, result.data);
        setRotatedRefresh(null);
        setRevokedAccess(null);
      }
    });

  const handleRefresh = () =>
    run(async () => {
      if (!session) return;
      const previous = session.refreshToken;
      const result = await api.refresh(previous);
      record(
        "Refresh",
        result,
        result.data ? "New pair issued. The refresh token just used is now revoked." : undefined,
      );
      if (result.data) {
        rotate(result.data);
        setRotatedRefresh(previous);
      }
    });

  const handleReuseRefresh = () =>
    run(async () => {
      if (!rotatedRefresh) return;
      const result = await api.refresh(rotatedRefresh);
      record(
        "Replay old refresh token",
        result,
        result.status === 401
          ? "Rejected. Rotation means a stolen refresh token dies the next time the real client refreshes."
          : undefined,
      );
    });

  const handleLogout = () =>
    run(async () => {
      if (!session) return;
      const result = await api.logout(session.accessToken, session.refreshToken);
      record(
        "Log out",
        result,
        result.ok
          ? "Access token jti written to the Redis blacklist, refresh token revoked in Postgres."
          : undefined,
      );
      if (result.ok) {
        setRevokedAccess(session.accessToken);
        signOut();
      }
    });

  const handleUseRevoked = () =>
    run(async () => {
      if (!revokedAccess) return;
      const result = await request(gw("/api/orders"), { token: revokedAccess });
      record(
        "Call /api/orders with the logged out token",
        result,
        result.status === 401
          ? "Signature and expiry are still valid, yet the blacklist refuses it. That is what makes logout real."
          : undefined,
      );
    });

  const handleCallOrders = () =>
    run(async () => {
      const result = await request(gw("/api/orders"), { token: session?.accessToken });
      record(
        session ? "Call /api/orders with the access token" : "Call /api/orders without a token",
        result,
        result.ok ? `Proxied to ${result.gateway.upstreamService ?? "the upstream"}.` : undefined,
      );
    });

  return (
    <>
      <div className="page-header">
        <h1>Authentication</h1>
        <p>
          Short lived access tokens (15 minutes) validated by signature alone, long lived refresh
          tokens (7 days) recorded in Postgres and rotated on every use, and a Redis blacklist that
          makes logout mean something. Try each step and watch the log.
        </p>
      </div>

      <div className="grid grid-2">
        <section className="card">
          <div className="card-header">
            <h2>{session ? "Signed in" : "Sign in"}</h2>
            {session && <span className="pill ok">{session.email}</span>}
          </div>

          {!session ? (
            <form className="stack" onSubmit={handleLogin}>
              <label>
                Email
                <input
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  autoComplete="username"
                />
              </label>
              <label>
                Password
                <input
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete="current-password"
                />
              </label>
              <div className="row">
                <button type="submit" className="primary" disabled={busy}>
                  Log in
                </button>
                <button type="button" onClick={handleRegister} disabled={busy}>
                  Register
                </button>
                <button type="button" onClick={handleDemo} disabled={busy}>
                  Use demo account
                </button>
              </div>
              <p className="hint" style={{ margin: 0 }}>
                Passwords must be 8 to 72 characters. bcrypt ignores anything past 72 bytes, so
                the gateway rejects longer ones instead of silently truncating them.
              </p>
            </form>
          ) : (
            <div className="stack">
              <div className="row">
                <button className="primary" onClick={handleCallOrders} disabled={busy}>
                  Call /api/orders
                </button>
                <button onClick={handleRefresh} disabled={busy}>
                  Refresh tokens
                </button>
                <button className="danger" onClick={handleLogout} disabled={busy}>
                  Log out
                </button>
              </div>
              <p className="hint" style={{ margin: 0 }}>
                The token is kept in this tab only and attached as a Bearer header on every
                console request that needs it.
              </p>
            </div>
          )}

          {(rotatedRefresh || revokedAccess || !session) && (
            <div className="stack" style={{ marginTop: 16 }}>
              <div className="hint">
                <strong>Prove it</strong>
              </div>
              <div className="row">
                {!session && (
                  <button className="small" onClick={handleCallOrders} disabled={busy}>
                    Call /api/orders without a token
                  </button>
                )}
                {rotatedRefresh && (
                  <button className="small" onClick={handleReuseRefresh} disabled={busy}>
                    Replay the old refresh token
                  </button>
                )}
                {revokedAccess && (
                  <button className="small" onClick={handleUseRevoked} disabled={busy}>
                    Use the logged out access token
                  </button>
                )}
              </div>
            </div>
          )}
        </section>

        <section className="card">
          <div className="card-header">
            <h2>Activity</h2>
            {log.length > 0 && (
              <button className="small" onClick={() => setLog([])}>
                Clear
              </button>
            )}
          </div>
          <ResultLog entries={log} />
        </section>
      </div>

      {session && (
        <div className="grid grid-2">
          <TokenCard
            title="Access token"
            token={session.accessToken}
            explain="Sent on every request. The gateway checks the signature and expiry locally, then looks up the jti in the Redis blacklist. Roles live in the token, so no database call is needed."
          />
          <TokenCard
            title="Refresh token"
            token={session.refreshToken}
            explain="Only sent to /auth/refresh. Its typ claim is refresh, so the auth middleware refuses it as a bearer token even though the signature is valid."
          />
        </div>
      )}
    </>
  );
}
