import { BrowserRouter, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import ComingSoon from "./pages/ComingSoon";
import Overview from "./pages/Overview";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Overview />} />
          <Route
            path="auth"
            element={
              <ComingSoon
                title="Authentication"
                summary="Register, log in, refresh and log out, and see what each token contains."
              />
            }
          />
          <Route
            path="playground"
            element={
              <ComingSoon
                title="Request playground"
                summary="Send a request through the gateway and watch which middleware answers."
              />
            }
          />
          <Route
            path="rate-limits"
            element={
              <ComingSoon
                title="Rate limiting"
                summary="Fire bursts at each route and compare token bucket, sliding window and fixed window."
              />
            }
          />
          <Route
            path="breakers"
            element={
              <ComingSoon
                title="Circuit breakers"
                summary="Make an upstream fail and watch its breaker open, recover and close."
              />
            }
          />
          <Route
            path="metrics"
            element={
              <ComingSoon
                title="Metrics"
                summary="The gateway's own Prometheus metrics, read live from /metrics."
              />
            }
          />
          <Route path="*" element={<ComingSoon title="Not found" summary="No page here." />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
