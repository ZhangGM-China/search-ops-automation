#!/usr/bin/env node

import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";
import { URL } from "node:url";

const DEFAULT_MAX_BYTES = 8 * 1024 * 1024;
const DEFAULT_MIN_INTERVAL_MS = 1500;
const DEFAULT_MAX_INTERVAL_MS = 2000;

export function validateSourceUrl(value) {
  const parsed = new URL(String(value || ""));
  if (parsed.protocol !== "https:" || parsed.hostname !== "static.atlas.example.test") {
    throw new Error("only https://static.atlas.example.test image URLs are allowed");
  }
  return parsed.toString();
}

function isLoopbackHost(host) {
  return host === "127.0.0.1" || host === "::1" || host === "localhost";
}

export function parseArgs(argv) {
  const args = {
    chromePath: process.env.QG_BROWSER_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    profileDir: process.env.QG_BROWSER_PROFILE_DIR || "/tmp/qg-atlas-browser-profile",
    bindHost: process.env.QG_BROWSER_BIND_HOST || "127.0.0.1",
    port: Number(process.env.QG_BROWSER_PORT || 8790),
    cdpPort: Number(process.env.QG_BROWSER_CDP_PORT || 9231),
    minIntervalMs: Number(process.env.QG_BROWSER_MIN_INTERVAL_MS || DEFAULT_MIN_INTERVAL_MS),
    maxIntervalMs: Number(process.env.QG_BROWSER_MAX_INTERVAL_MS || DEFAULT_MAX_INTERVAL_MS),
    maxBytes: Number(process.env.QG_BROWSER_MAX_BYTES || DEFAULT_MAX_BYTES),
    proxyServer: process.env.QG_BROWSER_PROXY_SERVER || "",
    requireProxy: /^(1|true|yes)$/i.test(process.env.QG_BROWSER_REQUIRE_PROXY || "false"),
    probeUrl: process.env.QG_BROWSER_PROBE_URL || "",
    headless: /^(1|true|yes)$/i.test(process.env.QG_BROWSER_HEADLESS || "false"),
    displayMode: process.env.QG_BROWSER_DISPLAY_MODE || "",
    token: process.env.QG_BROWSER_BRIDGE_TOKEN || "",
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => argv[++i];
    if (arg === "--chrome-path") args.chromePath = next();
    else if (arg === "--profile-dir") args.profileDir = next();
    else if (arg === "--bind-host") args.bindHost = next();
    else if (arg === "--port") args.port = Number(next());
    else if (arg === "--cdp-port") args.cdpPort = Number(next());
    else if (arg === "--min-interval-ms") args.minIntervalMs = Number(next());
    else if (arg === "--max-interval-ms") args.maxIntervalMs = Number(next());
    else if (arg === "--max-bytes") args.maxBytes = Number(next());
    else if (arg === "--proxy-server") args.proxyServer = next();
    else if (arg === "--require-proxy") args.requireProxy = true;
    else if (arg === "--probe-url") args.probeUrl = next();
    else if (arg === "--display-mode") args.displayMode = next();
    else if (arg === "--headless") args.headless = true;
    else if (arg === "--headed") args.headless = false;
    else if (arg === "--token") args.token = next();
    else if (arg === "--help") {
      console.log(`Usage: atlas_browser_worker.mjs [options]

Starts a browser-backed Atlas image bridge. POST /fetch with {"url":"https://static.atlas.example.test/..."}.

Options:
  --bind-host HOST        HTTP bridge bind address (default 127.0.0.1)
  --profile-dir DIR       Dedicated Chrome profile directory
  --port PORT             HTTP bridge port (default 8790)
  --cdp-port PORT         Chrome DevTools port (default 9231)
  --min-interval-ms N     Minimum request start interval (default 1500)
  --max-interval-ms N     Maximum request start interval (default 2000)
  --max-bytes N           Maximum source image size (default 8388608)
  --proxy-server URL      Optional Chrome HTTP(S)/SOCKS proxy
  --require-proxy         Fail closed when no proxy is configured
  --probe-url URL         Optional canary URL used by GET /probe
  --headless              Run Chromium without a display server
  --headed                Run Chromium with a display server
  --display-mode MODE     headed, headless, or ozone-headless (full Chromium)
  --token TOKEN           Bearer token; required for non-loopback binds
`);
      process.exit(0);
    }
  }
  if (!Number.isFinite(args.port) || args.port <= 0 || args.port > 65535) {
    throw new Error("invalid bridge port");
  }
  if (!Number.isFinite(args.cdpPort) || args.cdpPort <= 0 || args.cdpPort > 65535) {
    throw new Error("invalid Chrome DevTools port");
  }
  if (!Number.isFinite(args.minIntervalMs) || !Number.isFinite(args.maxIntervalMs) || args.minIntervalMs <= 0 || args.maxIntervalMs < args.minIntervalMs) {
    throw new Error("invalid interval configuration");
  }
  if (!Number.isFinite(args.maxBytes) || args.maxBytes <= 0) throw new Error("invalid max-bytes");
  if (!isLoopbackHost(args.bindHost) && !String(args.token || "").trim()) {
    throw new Error("token is required for a non-loopback bind host");
  }
  if (args.proxyServer) {
    const proxy = new URL(args.proxyServer);
    if (!["http:", "https:", "socks5:", "socks5h:"].includes(proxy.protocol)) {
      throw new Error("proxy-server must use http(s) or socks5");
    }
  }
  if (args.probeUrl) validateSourceUrl(args.probeUrl);
  if (args.displayMode && !["headed", "headless", "ozone-headless"].includes(args.displayMode)) {
    throw new Error("invalid display-mode");
  }
  if (args.requireProxy && !args.proxyServer) {
    throw new Error("proxy-server is required");
  }
  return args;
}

export function createSerialQueue() {
  let tail = Promise.resolve();
  return (task) => {
    const result = tail.then(task);
    tail = result.catch(() => undefined);
    return result;
  };
}

export function buildChromeArgs(args) {
  const chromeArgs = [
    `--remote-debugging-port=${args.cdpPort}`,
    `--user-data-dir=${args.profileDir}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-popup-blocking",
    "--disable-dev-shm-usage",
    "--new-window",
  ];
  const displayMode = args.displayMode || (args.headless ? "headless" : "headed");
  if (displayMode === "headless") {
    chromeArgs.push("--headless=new", "--disable-gpu", "--no-sandbox", "--disable-setuid-sandbox");
  } else if (displayMode === "ozone-headless") {
    chromeArgs.push("--ozone-platform=headless", "--no-sandbox", "--disable-setuid-sandbox");
  }
  if (args.proxyServer) chromeArgs.push(`--proxy-server=${args.proxyServer}`);
  chromeArgs.push("about:blank");
  return chromeArgs;
}

async function httpJson(port, path, method = "GET") {
  const response = await fetch(`http://127.0.0.1:${port}${path}`, { method });
  if (!response.ok) throw new Error(`Chrome DevTools HTTP ${response.status}`);
  return response.json();
}

async function waitForDevTools(port) {
  let lastError;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const targets = await httpJson(port, "/json/list");
      const page = targets.find((target) => target.type === "page");
      if (page) return page;
    } catch (error) {
      lastError = error;
    }
    await sleep(100);
  }
  throw lastError || new Error("Chrome DevTools did not start");
}

export class CdpPage {
  constructor(target) {
    this.target = target;
    this.nextId = 0;
    this.pending = new Map();
    this.events = [];
    this.ws = new WebSocket(target.webSocketDebuggerUrl);
  }

  async connect() {
    await new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", reject, { once: true });
    });
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const resolve = this.pending.get(message.id);
        this.pending.delete(message.id);
        resolve(message);
      } else {
        this.events.push(message);
      }
    });
    await this.command("Network.enable");
    await this.command("Page.enable");
  }

  command(method, params = {}) {
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, (message) => {
        if (message.error) reject(new Error(message.error.message));
        else resolve(message.result || {});
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async fetchImage(url, { maxBytes, startRequest }) {
    await startRequest();
    this.events.length = 0;
    await this.command("Page.navigate", { url });
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      while (this.events.length) {
        const event = this.events.shift();
        if (event.method !== "Network.responseReceived" || event.params.response.url !== url) continue;
        const response = event.params.response;
        if (response.status < 200 || response.status >= 300) {
          const error = new Error(`source_http_${response.status}`);
          error.status = response.status;
          throw error;
        }
        const contentType = String(response.headers["content-type"] || response.headers["Content-Type"] || "").split(";", 1)[0].trim().toLowerCase();
        if (!contentType.startsWith("image/") && contentType !== "application/octet-stream") {
          const error = new Error("unsupported_media");
          error.status = 502;
          throw error;
        }
        const loadingDeadline = Date.now() + 10_000;
        let loadingFinished = false;
        while (Date.now() < loadingDeadline) {
          for (const followUp of this.events.splice(0)) {
            if (followUp.method === "Network.loadingFinished" && followUp.params.requestId === event.params.requestId) {
              loadingFinished = true;
              break;
            }
            if (followUp.method === "Network.loadingFailed" && followUp.params.requestId === event.params.requestId) {
              const error = new Error("source_body_load_failed");
              error.status = 502;
              throw error;
            }
          }
          if (loadingFinished) break;
          await sleep(25);
        }
        if (!loadingFinished) {
          const error = new Error("source_body_load_timeout");
          error.status = 504;
          throw error;
        }
        const body = await this.command("Network.getResponseBody", { requestId: event.params.requestId });
        const bytes = body.base64Encoded ? Buffer.from(body.body, "base64") : Buffer.from(body.body);
        if (bytes.length > maxBytes) {
          const error = new Error("image_too_large");
          error.status = 413;
          throw error;
        }
        if (bytes.length === 0) {
          const error = new Error("image_empty");
          error.status = 502;
          throw error;
        }
        return { bytes, contentType, status: response.status };
      }
      await sleep(50);
    }
    const error = new Error("source_timeout");
    error.status = 504;
    throw error;
  }

  close() {
    this.ws.close();
  }
}

async function createWorker(args) {
  mkdirSync(args.profileDir, { recursive: true });
  const chrome = spawn(args.chromePath, buildChromeArgs(args), { stdio: "ignore" });
  const target = await waitForDevTools(args.cdpPort);
  const page = new CdpPage(target);
  await page.connect();
  let lastStartedAt = 0;
  const startRequest = async () => {
    const gap = Date.now() - lastStartedAt;
    const targetDelay = args.minIntervalMs + Math.random() * (args.maxIntervalMs - args.minIntervalMs);
    if (gap < targetDelay) await sleep(targetDelay - gap);
    lastStartedAt = Date.now();
  };
  const serialFetch = createSerialQueue();
  return {
    page,
    chrome,
    fetchImage: (url) => serialFetch(() => page.fetchImage(url, { maxBytes: args.maxBytes, startRequest })),
    close: () => { page.close(); chrome.kill(); },
  };
}

function setCors(response) {
  response.setHeader("Access-Control-Allow-Origin", "*");
  response.setHeader("Access-Control-Allow-Headers", "Authorization, Content-Type");
}

function writeJson(response, status, payload) {
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(JSON.stringify(payload));
}

async function readJson(request, maxBytes = 16 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBytes) throw new Error("request too large");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

export function createRequestHandler(args, worker) {
  return async (request, response) => {
    setCors(response);
    if (request.method === "OPTIONS") {
      response.writeHead(204);
      response.end();
      return;
    }
    const isHealth = request.method === "GET" && request.url === "/health";
    if (!isHealth && args.token && request.headers.authorization !== `Bearer ${args.token}`) {
      response.writeHead(401, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: "unauthorized" }));
      return;
    }
    if (request.method === "GET" && request.url === "/health") {
      writeJson(response, 200, { status: "ready", source_host: "static.atlas.example.test" });
      return;
    }
    if (request.method === "GET" && request.url === "/probe") {
      if (!args.probeUrl) {
        writeJson(response, 409, { error: "probe_url_not_configured" });
        return;
      }
      try {
        const result = await worker.fetchImage(args.probeUrl);
        writeJson(response, 200, {
          status: "ready",
          source_host: "static.atlas.example.test",
          source_status: result.status,
          content_type: result.contentType,
          bytes: result.bytes.length,
        });
      } catch (error) {
        writeJson(response, Number(error.status) || 502, {
          status: "not_ready",
          source_host: "static.atlas.example.test",
          error: error.message || "browser_probe_failed",
        });
      }
      return;
    }
    if (request.method !== "POST" || request.url !== "/fetch") {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: "not_found" }));
      return;
    }
    try {
      const body = await readJson(request);
      const url = validateSourceUrl(body.url);
      const result = await worker.fetchImage(url);
      response.writeHead(200, { "Content-Type": result.contentType, "Content-Length": result.bytes.length });
      response.end(result.bytes);
    } catch (error) {
      const status = Number(error.status) || 502;
      response.writeHead(status, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: error.message || "browser_fetch_failed" }));
    }
  };
}

export async function startWorker(options = {}) {
  const args = { ...parseArgs([]), ...options };
  if (!isLoopbackHost(args.bindHost) && !String(args.token || "").trim()) {
    throw new Error("token is required for a non-loopback bind host");
  }
  if (args.requireProxy && !args.proxyServer) {
    throw new Error("proxy-server is required");
  }
  if (args.probeUrl) validateSourceUrl(args.probeUrl);
  const worker = await createWorker(args);
  const server = createServer(createRequestHandler(args, worker));
  await new Promise((resolve) => server.listen(args.port, args.bindHost, resolve));
  const close = () => {
    server.close();
    worker.close();
  };
  return { server, worker, port: args.port, bindHost: args.bindHost, close };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const args = parseArgs(process.argv.slice(2));
  startWorker(args).then(({ port, bindHost }) => {
    console.log(`Atlas browser worker listening on http://${bindHost}:${port}`);
  }).catch((error) => {
    console.error(error.stack || error.message);
    process.exitCode = 1;
  });
}
