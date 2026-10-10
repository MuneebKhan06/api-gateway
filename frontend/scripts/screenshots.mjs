#!/usr/bin/env node
// Capture the README screenshots from a running stack.
//
//   docker compose up -d
//   node frontend/scripts/screenshots.mjs [--url http://localhost:8080] [--out docs/screenshots]
//
// Drives headless Chrome over the DevTools protocol with Node's built in
// WebSocket, so it needs no dependencies. Each page is put into an
// interesting state first (a burst fired, an upstream broken) because an idle
// console makes for screenshots that show nothing.

import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, value, index, all) => {
    if (value.startsWith("--")) pairs.push([value.slice(2), all[index + 1]]);
    return pairs;
  }, []),
);
const BASE = (args.url ?? "http://localhost:8080").replace(/\/$/, "");
const OUT = resolve(args.out ?? "docs/screenshots");
const CHROME = args.chrome ?? process.env.CHROME ?? "google-chrome";
const PORT = 9333;
const WIDTH = 1440;
const HEIGHT = 900;

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function waitForDevtools() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (response.ok) return;
    } catch {
      // Not listening yet.
    }
    await sleep(200);
  }
  throw new Error("Chrome did not open its DevTools port");
}

/** A minimal DevTools client: send a command, await its result. */
function connect(wsUrl) {
  const socket = new WebSocket(wsUrl);
  const pending = new Map();
  let nextId = 1;
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    if (message.error) waiter.reject(new Error(message.error.message));
    else waiter.resolve(message.result);
  });
  const send = (method, params = {}) =>
    new Promise((resolveSend, reject) => {
      const id = nextId++;
      pending.set(id, { resolve: resolveSend, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
  return new Promise((ready, fail) => {
    socket.addEventListener("open", () => ready({ send, close: () => socket.close() }));
    socket.addEventListener("error", () => fail(new Error("DevTools connection failed")));
  });
}

async function main() {
  mkdirSync(OUT, { recursive: true });
  const profile = mkdtempSync(join(tmpdir(), "console-shots-"));
  const chrome = spawn(
    CHROME,
    [
      "--headless=new",
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${profile}`,
      `--window-size=${WIDTH},${HEIGHT}`,
      "--hide-scrollbars",
      "--no-first-run",
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  try {
    await waitForDevtools();
    const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    const page = targets.find((target) => target.type === "page");
    const cdp = await connect(page.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: WIDTH,
      height: HEIGHT,
      deviceScaleFactor: 1,
      mobile: false,
    });
    // Screenshots are of the light theme, whatever the machine prefers.
    await cdp.send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-color-scheme", value: "light" }],
    });

    const evaluate = async (expression) => {
      const result = await cdp.send("Runtime.evaluate", {
        expression,
        awaitPromise: true,
        returnByValue: true,
      });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
      return result.result.value;
    };
    const go = async (path, settleMs = 2500) => {
      await cdp.send("Page.navigate", { url: `${BASE}${path}` });
      await sleep(settleMs);
    };
    // Buttons are found by their text, the same way a person would.
    const click = (text) =>
      evaluate(`(() => {
        const button = [...document.querySelectorAll("button")].find((b) => b.textContent.trim().startsWith(${JSON.stringify(text)}));
        if (!button) throw new Error("no button " + ${JSON.stringify(text)});
        button.click();
      })()`);
    const scrollTo = (selector) =>
      // Scrolled into view with room above, so the section's heading shows.
      evaluate(`(() => {
        const element = document.querySelector(${JSON.stringify(selector)});
        if (element) window.scrollTo(0, element.getBoundingClientRect().top + window.scrollY - 120);
      })()`);
    const shoot = async (name) => {
      const { data } = await cdp.send("Page.captureScreenshot", { format: "png" });
      writeFileSync(join(OUT, `${name}.png`), Buffer.from(data, "base64"));
      console.log(`saved ${name}.png`);
    };

    // Sign in once; the console keeps its session in sessionStorage.
    await go("/");
    await evaluate(`(async () => {
      const body = JSON.stringify({ email: "demo@example.com", password: "password123" });
      const headers = { "Content-Type": "application/json" };
      await fetch("/gw/auth/register", { method: "POST", headers, body });
      const pair = await (await fetch("/gw/auth/login", { method: "POST", headers, body })).json();
      sessionStorage.setItem("gateway-console-session", JSON.stringify({
        email: "demo@example.com",
        accessToken: pair.access_token,
        refreshToken: pair.refresh_token,
        expiresIn: pair.expires_in,
        issuedAt: Date.now(),
      }));
    })()`);

    await go("/", 4000);
    await shoot("overview");

    await go("/auth");
    await click("Refresh tokens");
    await sleep(1500);
    await shoot("auth");

    await go("/playground");
    await click("What the upstream sees");
    await click("Send");
    await sleep(1500);
    await scrollTo(".playground-result");
    await shoot("playground");

    await go("/rate-limits");
    await click("Fire at /api/orders");
    await sleep(7000);
    await scrollTo(".burst-controls");
    await shoot("rate-limits");

    // Break service-a and drive traffic until its breaker opens.
    await go("/breakers");
    await evaluate(`fetch("/upstream/service-a/_control/fail?enabled=true", { method: "POST" })`);
    await click("Start traffic");
    await sleep(9000);
    await shoot("breakers");
    await click("Stop traffic");
    await evaluate(`fetch("/upstream/service-a/_control/fail?enabled=false", { method: "POST" })`);
    await evaluate(`fetch("/gw/gateway/breakers/service-a/reset", { method: "POST" })`);

    // Rates need something happening, so keep a little traffic flowing
    // through two routes while the metrics page collects a few scrapes.
    await go("/metrics", 1000);
    await evaluate(`(() => {
      const { accessToken } = JSON.parse(sessionStorage.getItem("gateway-console-session"));
      const headers = { Authorization: "Bearer " + accessToken };
      const started = Date.now();
      const tick = () => {
        if (Date.now() - started > 12000) return;
        fetch("/gw/api/users", { headers });
        fetch("/gw/api/inventory/SKU-1001", { headers });
        setTimeout(tick, 400);
      };
      tick();
    })()`);
    await sleep(12000);
    await shoot("metrics");

    cdp.close();
  } finally {
    chrome.kill();
  }
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
