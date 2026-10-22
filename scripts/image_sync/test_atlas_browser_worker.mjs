import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";

import {
  CdpPage,
  buildChromeArgs,
  createSerialQueue,
  createRequestHandler,
  parseArgs,
  validateSourceUrl,
} from "./atlas_browser_worker.mjs";

test("Kubernetes health probe works without a token while fetch stays protected", async (t) => {
  let sourceCalls = 0;
  const server = createServer(createRequestHandler({ token: "test-secret" }, {
    fetchImage: async () => { sourceCalls += 1; throw new Error("unexpected source call"); },
  }));
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  assert.equal((await fetch(`${base}/health`)).status, 200);
  assert.equal((await fetch(`${base}/probe`)).status, 401);
  assert.equal((await fetch(`${base}/fetch`, { method: "POST", body: "{}" })).status, 401);
  assert.equal(sourceCalls, 0);
});

test("accepts only Atlas static HTTPS image URLs", () => {
  assert.equal(
    validateSourceUrl("https://static.atlas.example.test/assets/a.jpg?ts=1"),
    "https://static.atlas.example.test/assets/a.jpg?ts=1",
  );
});

test("rejects non-Atlas hosts and non-HTTPS URLs", () => {
  assert.throws(() => validateSourceUrl("http://static.atlas.example.test/assets/a.jpg"), /only https/);
  assert.throws(() => validateSourceUrl("https://example.com/assets/a.jpg"), /only https/);
});

test("requires a bearer token when the bridge binds beyond loopback", () => {
  assert.throws(
    () => parseArgs(["--bind-host", "0.0.0.0"]),
    /token is required/
  );

  const args = parseArgs([
    "--bind-host", "0.0.0.0",
    "--token", "bridge-secret",
    "--proxy-server", "http://egress-proxy:8080",
    "--headless",
  ]);
  assert.equal(args.bindHost, "0.0.0.0");
  assert.equal(args.token, "bridge-secret");
  assert.equal(args.proxyServer, "http://egress-proxy:8080");
  assert.equal(args.headless, true);
  const chromeArgs = buildChromeArgs(args);
  assert.ok(chromeArgs.includes("--headless=new"));
  assert.ok(chromeArgs.includes("--disable-gpu"));
  assert.ok(chromeArgs.includes("--no-sandbox"));
  assert.ok(chromeArgs.includes("--disable-setuid-sandbox"));
  assert.ok(chromeArgs.includes("--proxy-server=http://egress-proxy:8080"));
  assert.equal(chromeArgs.at(-1), "about:blank");
});

test("serializes browser fetches so one Atlas worker never overlaps requests", async () => {
  const queue = createSerialQueue();
  let active = 0;
  let maximumActive = 0;
  const order = [];

  const run = (name) => queue(async () => {
    active += 1;
    maximumActive = Math.max(maximumActive, active);
    order.push(`${name}:start`);
    await new Promise((resolve) => setTimeout(resolve, 5));
    order.push(`${name}:end`);
    active -= 1;
    return name;
  });

  const results = await Promise.all([run("first"), run("second")]);
  assert.deepEqual(results, ["first", "second"]);
  assert.equal(maximumActive, 1);
  assert.deepEqual(order, ["first:start", "first:end", "second:start", "second:end"]);
});

test("rejects malformed source URLs", () => {
  assert.throws(() => validateSourceUrl("not-a-url"));
});

test("accepts a Atlas probe URL for preflight validation", () => {
  const args = parseArgs([
    "--probe-url",
    "https://static.atlas.example.test/assets/probe.jpg?ts=1",
  ]);
  assert.equal(args.probeUrl, "https://static.atlas.example.test/assets/probe.jpg?ts=1");
  assert.throws(
    () => parseArgs(["--probe-url", "https://example.com/probe.jpg"]),
    /only https/,
  );
});

test("can fail closed when an online deployment has no proxy", () => {
  assert.throws(
    () => parseArgs(["--require-proxy"]),
    /proxy-server is required/,
  );
  const args = parseArgs([
    "--require-proxy",
    "--proxy-server",
    "https://proxy.example:443",
  ]);
  assert.equal(args.requireProxy, true);
});

test("supports the full-browser ozone headless mode for online containers", () => {
  const args = parseArgs(["--display-mode", "ozone-headless"]);
  assert.equal(args.displayMode, "ozone-headless");
  const chromeArgs = buildChromeArgs(args);
  assert.ok(chromeArgs.includes("--ozone-platform=headless"));
  assert.ok(chromeArgs.includes("--no-sandbox"));
  assert.ok(!chromeArgs.includes("--headless=new"));
  assert.throws(() => parseArgs(["--display-mode", "invalid"]), /display-mode/);
});


test("waits for matching loadingFinished before reading the image body", async () => {
  const page = Object.create(CdpPage.prototype);
  page.events = [];
  let finished = false;
  page.command = async (method) => {
    if (method === "Page.navigate") {
      page.events.push({method:"Network.responseReceived",params:{requestId:"image",response:{url:"https://static.atlas.example.test/assets/test.jpg",status:200,headers:{"content-type":"image/jpeg"}}}});
      page.events.push({method:"Network.loadingFinished",params:{requestId:"other"}});
      setTimeout(() => {
        finished = true;
        page.events.push({method:"Network.loadingFinished",params:{requestId:"image"}});
      }, 20);
      return {};
    }
    if (method === "Network.getResponseBody") {
      assert.equal(finished, true, "responseReceived does not mean the body is ready");
      return {body:Buffer.from("jpeg-bytes").toString("base64"),base64Encoded:true};
    }
    throw new Error("unexpected command");
  };
  const result = await page.fetchImage("https://static.atlas.example.test/assets/test.jpg", {maxBytes:1024,startRequest:async()=>{}});
  assert.equal(result.bytes.toString(), "jpeg-bytes");
});

test("handles responseReceived and loadingFinished in the same event batch", async () => {
  const page = Object.create(CdpPage.prototype);
  page.events = [];
  page.command = async (method) => {
    if (method === "Page.navigate") {
      page.events.push(
        {method:"Network.responseReceived",params:{requestId:"image",response:{url:"https://static.atlas.example.test/assets/test.jpg",status:200,headers:{"content-type":"image/jpeg"}}}},
        {method:"Network.loadingFinished",params:{requestId:"image"}},
      );
      return {};
    }
    return {body:Buffer.from("jpeg-bytes").toString("base64"),base64Encoded:true};
  };
  const result = await page.fetchImage("https://static.atlas.example.test/assets/test.jpg", {maxBytes:1024,startRequest:async()=>{}});
  assert.equal(result.bytes.toString(), "jpeg-bytes");
});

test("reports source body load failures rather than reading missing bytes", async () => {
  const page = Object.create(CdpPage.prototype);
  page.events = [];
  page.command = async (method) => {
    if (method === "Page.navigate") {
      page.events.push(
        {method:"Network.responseReceived",params:{requestId:"image",response:{url:"https://static.atlas.example.test/assets/test.jpg",status:200,headers:{"content-type":"image/jpeg"}}}},
        {method:"Network.loadingFailed",params:{requestId:"image"}},
      );
      return {};
    }
    throw new Error("must not read failed response body");
  };
  await assert.rejects(() => page.fetchImage("https://static.atlas.example.test/assets/test.jpg", {maxBytes:1024,startRequest:async()=>{}}), /source_body_load_failed/);
});
