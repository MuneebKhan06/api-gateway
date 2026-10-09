import { BrowserRouter, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Auth from "./pages/Auth";
import Breakers from "./pages/Breakers";
import ComingSoon from "./pages/ComingSoon";
import MetricsPage from "./pages/Metrics";
import Overview from "./pages/Overview";
import Playground from "./pages/Playground";
import RateLimits from "./pages/RateLimits";
import { SessionProvider } from "./session";

export default function App() {
  return (
    <SessionProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Overview />} />
            <Route path="auth" element={<Auth />} />
            <Route path="playground" element={<Playground />} />
            <Route path="rate-limits" element={<RateLimits />} />
            <Route path="breakers" element={<Breakers />} />
            <Route path="metrics" element={<MetricsPage />} />
            <Route path="*" element={<ComingSoon title="Not found" summary="No page here." />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </SessionProvider>
  );
}
