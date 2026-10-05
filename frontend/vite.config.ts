import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The console never calls the gateway cross origin. Everything goes through
// this dev server (and through nginx in the container build), so the gateway
// needs no CORS configuration and the browser can read every response header
// the gateway sets: X-Request-ID, X-RateLimit-*, X-Circuit-Breaker.
//
//   /gw/*                  -> the gateway, prefix stripped
//   /upstream/service-a/*  -> the mock upstream directly, for fault injection
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const gateway = env.GATEWAY_URL ?? "http://localhost:8000";
  const upstreams: Record<string, string> = {
    "service-a": env.SERVICE_A_URL ?? "http://localhost:8001",
    "service-b": env.SERVICE_B_URL ?? "http://localhost:8002",
    "service-c": env.SERVICE_C_URL ?? "http://localhost:8003",
  };

  const proxy: Record<string, object> = {
    "/gw": {
      target: gateway,
      changeOrigin: true,
      rewrite: (path: string) => path.replace(/^\/gw/, ""),
    },
  };
  for (const [name, target] of Object.entries(upstreams)) {
    proxy[`/upstream/${name}`] = {
      target,
      changeOrigin: true,
      rewrite: (path: string) => path.replace(`/upstream/${name}`, ""),
    };
  }

  return {
    plugins: [react()],
    server: { port: 5173, proxy },
    preview: { port: 4173, proxy },
  };
});
