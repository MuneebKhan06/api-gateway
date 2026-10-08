import { api } from "../api/client";
import BreakerCard from "../components/BreakerCard";
import { usePolling } from "../hooks/usePolling";
import { useSession } from "../session";

export default function Breakers() {
  const { session } = useSession();
  // Once a second: an open breaker counts down in seconds, and half open can
  // last a single request.
  const breakers = usePolling(api.breakers, 1000);
  const routes = usePolling(api.routes, 15000);
  const list = breakers.result?.data ?? [];
  const routeList = routes.result?.data ?? [];

  return (
    <>
      <div className="page-header">
        <h1>Circuit breakers</h1>
        <p>
          One breaker per upstream, with its state in Redis so every gateway instance shares what
          any one of them learns. An upstream that keeps failing gets cut off with an instant 503
          instead of being hammered while it is down, then is let back in one request at a time.
        </p>
      </div>

      {breakers.result?.networkError && (
        <div className="callout bad">The gateway is not answering, so breaker state is unavailable.</div>
      )}

      {list.map((breaker) => (
        <BreakerCard
          key={breaker.upstream}
          breaker={breaker}
          routes={routeList}
          token={session?.accessToken ?? null}
          onChanged={breakers.refresh}
        />
      ))}
      {breakers.result && list.length === 0 && !breakers.result.networkError && (
        <div className="card empty">No route has a circuit breaker enabled.</div>
      )}

    </>
  );
}
