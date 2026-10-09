import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

// A presenter's script for showing the gateway, one page at a time. Each step
// says what to click and what the point is, so the demo tells a story
// instead of being a tour of screens.

interface TourStep {
  path: string;
  title: string;
  /** What to do on the page. */
  actions: string[];
  /** The one thing to say about it. */
  point: string;
}

export const TOUR: TourStep[] = [
  {
    path: "/",
    title: "What this is",
    actions: [
      "Point at the request path: correlation, metrics, auth, rate limit, breaker, proxy.",
      "Show the route table: three upstreams, each with its own algorithm.",
    ],
    point: "A gateway built from scratch: every layer here is code in the repository, not a Kong plugin.",
  },
  {
    path: "/auth",
    title: "Tokens that can be revoked",
    actions: [
      "Use demo account, then show the decoded access and refresh tokens.",
      "Refresh, then replay the old refresh token: 401.",
      "Log out, then use the logged out access token: 401 token_revoked.",
      "Log in again before moving on; the next pages need a session.",
    ],
    point: "A JWT is valid until it expires by design. The Redis blacklist is what makes logout real.",
  },
  {
    path: "/playground",
    title: "Which layer answered",
    actions: [
      "Send the Tampered token preset: the auth stage stops it.",
      "Send What the upstream sees: the spoofed identity header is gone, the real one injected.",
      "Send Unknown path: 404, and auth never ran.",
    ],
    point: "Every refusal carries its own status and error code, so the client can tell the gateway said no from the service said no.",
  },
  {
    path: "/rate-limits",
    title: "Atomic rate limiting",
    actions: [
      "Select /api/orders and fire a burst: about 100 allowed, then 429s with Retry-After.",
      "Fire at all three: the allowed counts land on each limit despite the concurrency.",
      "Scroll to the boundary demo: fixed window lets almost twice the limit through.",
    ],
    point: "Each check is one Lua script in Redis, so two concurrent requests can never both read the same counter.",
  },
  {
    path: "/breakers",
    title: "Failing fast",
    actions: [
      "Follow the walkthrough at the top: make service-a fail and start traffic.",
      "After five failures it opens, and refusals take a few milliseconds.",
      "Heal it; after 30s one trial request is let through, and two successes close it.",
    ],
    point: "State lives in Redis, so one instance discovering an outage protects every instance.",
  },
  {
    path: "/metrics",
    title: "All of it, measured",
    actions: [
      "Point at the auth reasons, the 429s and the breaker transitions you just caused.",
      "Show gateway overhead: end to end minus time waiting on the upstream.",
      "Open Grafana if the full stack is running.",
    ],
    point: "Every decision is a counter with bounded labels, so it can be graphed and alerted on without blowing up Prometheus.",
  },
];

const STORAGE_KEY = "gateway-console-tour";

function loadStep(): number | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    const step = raw === null ? null : Number(raw);
    return step !== null && step >= 0 && step < TOUR.length ? step : null;
  } catch {
    return null;
  }
}

function saveStep(step: number | null): void {
  try {
    if (step === null) sessionStorage.removeItem(STORAGE_KEY);
    else sessionStorage.setItem(STORAGE_KEY, String(step));
  } catch {
    // The tour still works for this page view without storage.
  }
}

export function useTour() {
  const [step, setStep] = useState<number | null>(loadStep);
  const navigate = useNavigate();

  const go = (next: number | null) => {
    saveStep(next);
    setStep(next);
    if (next !== null) navigate(TOUR[next].path);
  };

  return { step, start: () => go(0), go, stop: () => go(null) };
}

export default function DemoTour({ tour }: { tour: ReturnType<typeof useTour> }) {
  const { step, go, stop } = tour;
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    setCollapsed(false);
  }, [step]);

  if (step === null) return null;
  const current = TOUR[step];
  const onPage = location.pathname === current.path;

  return (
    <aside className={`tour${collapsed ? " collapsed" : ""}`} aria-label="Demo tour">
      <div className="tour-header">
        <span className="hint">
          Demo tour, {step + 1} of {TOUR.length}
        </span>
        <div className="row" style={{ gap: 4 }}>
          <button type="button" className="small" onClick={() => setCollapsed((value) => !value)}>
            {collapsed ? "Show" : "Hide"}
          </button>
          <button type="button" className="small" onClick={stop} aria-label="End tour">
            End
          </button>
        </div>
      </div>

      {!collapsed && (
        <>
          <strong className="tour-title">{current.title}</strong>
          {!onPage && (
            <button type="button" className="small" onClick={() => go(step)}>
              Go to this page
            </button>
          )}
          <ol className="tour-actions">
            {current.actions.map((action) => (
              <li key={action}>{action}</li>
            ))}
          </ol>
          <div className="tour-point">{current.point}</div>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <button type="button" className="small" disabled={step === 0} onClick={() => go(step - 1)}>
              Back
            </button>
            {step < TOUR.length - 1 ? (
              <button type="button" className="small primary" onClick={() => go(step + 1)}>
                Next: {TOUR[step + 1].title}
              </button>
            ) : (
              <button type="button" className="small primary" onClick={stop}>
                Finish
              </button>
            )}
          </div>
        </>
      )}
    </aside>
  );
}
