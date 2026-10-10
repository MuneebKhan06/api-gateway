import { useEffect } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { api } from "../api/client";
import { usePolling } from "../hooks/usePolling";
import { useSession } from "../session";
import DemoTour, { useTour } from "./DemoTour";
import ErrorBoundary from "./ErrorBoundary";
import StatusPill from "./StatusPill";
import ThemeToggle from "./ThemeToggle";

interface NavItem {
  to: string;
  label: string;
  soon?: boolean;
}

const EXPLORE: NavItem[] = [
  { to: "/", label: "Overview" },
  { to: "/auth", label: "Authentication" },
  { to: "/playground", label: "Request playground" },
];

const LABS: NavItem[] = [
  { to: "/rate-limits", label: "Rate limiting" },
  { to: "/breakers", label: "Circuit breakers" },
  { to: "/metrics", label: "Metrics" },
];

function NavGroup({ items }: { items: NavItem[] }) {
  return (
    <>
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/"}
          className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
        >
          {item.label}
          {item.soon && <span className="badge">soon</span>}
        </NavLink>
      ))}
    </>
  );
}

export default function Layout() {
  // The header indicator is the console's heartbeat: if this goes red, every
  // page below it is going to show errors, and this says why.
  const health = usePolling(api.health, 5000);
  const { session } = useSession();
  const tour = useTour();
  const { pathname } = useLocation();

  // A new page starts at the top, whether it was reached from the sidebar or
  // by the tour, rather than at the previous page's scroll position.
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [pathname]);
  const status = health.result
    ? health.result.networkError
      ? "offline"
      : (health.result.data?.status ?? "unhealthy")
    : "unknown";

  return (
    <div className="shell">
      <aside className="sidebar">
        <NavLink to="/" className="brand">
          <img src="/favicon.svg" width={30} height={30} alt="" />
          <span>
            Gateway Console
            <small>FastAPI, Redis, Postgres</small>
          </span>
        </NavLink>
        <div className="nav-section">Explore</div>
        <NavGroup items={EXPLORE} />
        <div className="nav-section">Labs</div>
        <NavGroup items={LABS} />
        <div className="sidebar-footer">
          {tour.step === null && (
            <button type="button" className="primary tour-start" onClick={tour.start}>
              Start demo tour
            </button>
          )}
          <ThemeToggle />
          <a href="https://github.com/MuneebKhan06/api-gateway" target="_blank" rel="noreferrer">
            Source on GitHub
          </a>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="row">
            <strong>Gateway</strong>
            <StatusPill
              status={status}
              label={status === "unknown" ? "connecting" : status}
            />
          </div>
          <div className="row">
            <span className="hint">Live, proxied through <code>/gw</code></span>
            {session ? (
              <NavLink to="/auth" className="pill ok">
                {session.email}
              </NavLink>
            ) : (
              <NavLink to="/auth" className="pill plain">
                signed out
              </NavLink>
            )}
          </div>
        </header>
        <main className="content">
          {/* Keyed by path, so leaving a broken page clears the error. */}
          <ErrorBoundary key={pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
      <DemoTour tour={tour} />
    </div>
  );
}
