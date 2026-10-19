import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import { chromium } from "playwright";

import { validatePublicUrl, validateRequest } from "./security.mjs";

const HOST = "127.0.0.1";
const PORT = 9223;
const MAX_BODY_BYTES = 64 * 1024;
let active = false;

function hash(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function hashSet(values) {
  return hash([...new Set(values)].sort().join("\n"));
}

function isolationAttestation() {
  const mounts = fs.readFileSync("/proc/mounts", "utf8").split("\n");
  const root = mounts.find((line) => line.split(" ")[1] === "/");
  const rootOptions = root?.split(" ")[3]?.split(",") ?? [];
  const forbiddenEnvironment = Object.keys(process.env).some((name) =>
    /(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|DATABASE_DSN)/iu.test(name),
  );
  const dockerSocket = fs.existsSync("/var/run/docker.sock");
  const hostMount = mounts.some((line) => /\s\/(host|workspace|app\/secrets)(\/|\s)/u.test(line));
  return {
    policy_id: "browser-container-1",
    sandbox_enabled: true,
    running_as_root: typeof process.getuid !== "function" || process.getuid() === 0,
    read_only_root_filesystem: rootOptions.includes("ro"),
    ephemeral_profile: true,
    host_mounts_present: hostMount,
    secrets_available: forbiddenEnvironment,
    docker_socket_available: dockerSocket,
  };
}

function assertIsolation(attestation) {
  if (!attestation.sandbox_enabled || attestation.running_as_root ||
      !attestation.read_only_root_filesystem || !attestation.ephemeral_profile ||
      attestation.host_mounts_present || attestation.secrets_available ||
      attestation.docker_socket_available) {
    throw new Error("ISOLATION_UNSAFE");
  }
}

async function extractUrls(page) {
  return page.evaluate(() => {
    const values = [document.URL];
    for (const element of document.querySelectorAll("[href], [src]")) {
      const value = element.href || element.src;
      if (typeof value === "string" && value.length <= 2048) values.push(value);
    }
    return values;
  });
}

function scope(url, origin) {
  try {
    return new URL(url).origin === origin ? "internal" : "external";
  } catch {
    return "external";
  }
}

function externalDependencyType(resourceType) {
  if (["xhr", "fetch"].includes(resourceType)) return "api";
  if (resourceType === "script") return "script";
  if (resourceType === "stylesheet") return "stylesheet";
  if (resourceType === "image") return "image";
  if (resourceType === "font") return "font";
  if (resourceType === "document") return "frame";
  return "other";
}

async function configurePage(page, targetOrigin, budgets, stats) {
  page.on("console", () => {
    stats.consoleEvents = Math.min(stats.consoleEvents + 1, budgets.max_console_events);
  });
  page.on("requestfailed", () => { stats.failedRequests += 1; });
  page.on("requestfinished", async (request) => {
    try {
      const sizes = await request.sizes();
      stats.transferBytes = Math.min(
        stats.transferBytes + Math.max(0, sizes.responseBodySize),
        budgets.max_total_transfer_bytes,
      );
    } catch {
      stats.failedRequests += 1;
    }
  });
  await page.route("**/*", async (route) => {
    const request = route.request();
    if (stats.requestCount >= budgets.max_requests) {
      stats.blockedRequests += 1;
      stats.termination = "request_budget";
      await route.abort("blockedbyclient");
      return;
    }
    stats.requestCount += 1;
    if (scope(request.url(), targetOrigin) === "internal") stats.internalRequests += 1;
    else {
      stats.externalRequests += 1;
      try { stats.externalOrigins.add(new URL(request.url()).origin); } catch { /* validated below */ }
      stats.externalTypes[externalDependencyType(request.resourceType())] += 1;
    }
    if (["xhr", "fetch"].includes(request.resourceType())) {
      stats.apiRequests += 1;
      if (scope(request.url(), targetOrigin) === "external") stats.externalApiRequests += 1;
    }
    try {
      await validatePublicUrl(request.url());
      await route.continue();
    } catch {
      stats.blockedRequests += 1;
      await route.abort("blockedbyclient");
    }
  });
}

async function capture(browser, request, javascriptEnabled, stats) {
  const { budgets, profile } = request;
  const context = await browser.newContext({
    viewport: { width: profile.viewport_width, height: profile.viewport_height },
    userAgent: profile.user_agent,
    locale: profile.locale,
    timezoneId: profile.timezone_id,
    colorScheme: profile.color_scheme,
    reducedMotion: profile.reduced_motion,
    serviceWorkers: "block",
    javaScriptEnabled: javascriptEnabled,
    bypassCSP: false,
    ignoreHTTPSErrors: false,
  });
  try {
    const page = await context.newPage();
    await configurePage(page, new URL(request.subject_uri).origin, budgets, stats);
    await page.goto(request.subject_uri, {
      waitUntil: "domcontentloaded",
      timeout: budgets.navigation_timeout_milliseconds,
    });
    if (javascriptEnabled && budgets.observation_window_milliseconds > 0) {
      await page.waitForTimeout(budgets.observation_window_milliseconds);
    }
    const content = await page.content();
    const urls = await extractUrls(page);
    return { content, urls };
  } finally {
    await context.close();
  }
}

async function render(request) {
  validateRequest(request);
  await validatePublicUrl(request.subject_uri);
  const isolation = isolationAttestation();
  assertIsolation(isolation);
  const startedMs = Date.now();
  const stats = {
    requestCount: 0, internalRequests: 0, externalRequests: 0, apiRequests: 0,
    externalApiRequests: 0, failedRequests: 0, blockedRequests: 0,
    externalOrigins: new Set(),
    externalTypes: { api: 0, script: 0, stylesheet: 0, image: 0, font: 0, frame: 0, other: 0 },
    transferBytes: 0, consoleEvents: 0,
    termination: "complete",
  };
  const browser = await chromium.launch({ chromiumSandbox: true, headless: true });
  try {
    const raw = await capture(browser, request, false, stats);
    const rendered = await capture(browser, request, true, stats);
    const rawBytes = Buffer.from(raw.content, "utf8");
    const renderedBytes = Buffer.from(rendered.content, "utf8");
    if (renderedBytes.length > request.budgets.max_dom_bytes) stats.termination = "dom_budget";
    if (stats.transferBytes >= request.budgets.max_total_transfer_bytes) stats.termination = "byte_budget";
    const elapsed = Math.max(
      0,
      Math.min(Date.now() - startedMs, request.budgets.total_timeout_milliseconds),
    );
    if (elapsed >= request.budgets.total_timeout_milliseconds) stats.termination = "time_budget";
    const rawHashBytes = rawBytes.subarray(0, request.budgets.max_dom_bytes);
    const renderedHashBytes = renderedBytes.subarray(0, request.budgets.max_dom_bytes);
    const rawSet = new Set(raw.urls);
    const renderedSet = new Set(rendered.urls);
    return {
      analysis_id: request.analysis_id,
      attempt_id: request.attempt_id,
      engine_name: "chromium",
      engine_version: browser.version(),
      started_at: new Date(startedMs).toISOString(),
      finished_at: new Date(startedMs + elapsed).toISOString(),
      isolation,
      aggregate: {
        raw_dom_hash: hash(rawHashBytes), rendered_dom_hash: hash(renderedHashBytes),
        raw_url_set_hash: hashSet(rawSet), rendered_url_set_hash: hashSet(renderedSet),
        raw_url_count: rawSet.size, rendered_url_count: renderedSet.size,
        additional_url_count: [...renderedSet].filter((url) => !rawSet.has(url)).length,
        request_count: stats.requestCount, internal_request_count: stats.internalRequests,
        external_request_count: stats.externalRequests, api_request_count: stats.apiRequests,
        external_api_request_count: stats.externalApiRequests,
        failed_request_count: Math.min(stats.failedRequests, stats.requestCount),
        blocked_request_count: Math.min(stats.blockedRequests, stats.requestCount),
        external_origin_count: stats.externalOrigins.size,
        external_script_count: stats.externalTypes.script,
        external_stylesheet_count: stats.externalTypes.stylesheet,
        external_image_count: stats.externalTypes.image,
        external_font_count: stats.externalTypes.font,
        external_frame_count: stats.externalTypes.frame,
        external_other_count: stats.externalTypes.other,
        dependency_graph_observed: true,
        transfer_bytes: stats.transferBytes,
        raw_dom_bytes: Math.min(rawBytes.length, request.budgets.max_dom_bytes),
        rendered_dom_bytes: Math.min(renderedBytes.length, request.budgets.max_dom_bytes),
        console_event_count: stats.consoleEvents, elapsed_milliseconds: elapsed,
        termination_reason: stats.termination,
      },
    };
  } finally {
    await browser.close();
  }
}

function send(response, status, value) {
  const body = Buffer.from(JSON.stringify(value), "utf8");
  response.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": body.length,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(body);
}

const server = http.createServer((incoming, response) => {
  if (incoming.method === "GET" && incoming.url === "/health") {
    try {
      assertIsolation(isolationAttestation());
      response.writeHead(204, { "Cache-Control": "no-store" });
      response.end();
    } catch {
      send(response, 503, { error: "ISOLATION_UNSAFE" });
    }
    return;
  }
  if (incoming.method !== "POST" || incoming.url !== "/render") {
    send(response, 404, { error: "NOT_FOUND" });
    return;
  }
  if (active) {
    send(response, 503, { error: "BUSY" });
    return;
  }
  const declared = Number(incoming.headers["content-length"] ?? -1);
  if (!Number.isInteger(declared) || declared < 2 || declared > MAX_BODY_BYTES ||
      incoming.headers["content-type"] !== "application/json") {
    send(response, 400, { error: "REQUEST_INVALID" });
    return;
  }
  const chunks = [];
  let size = 0;
  incoming.on("data", (chunk) => {
    size += chunk.length;
    if (size <= MAX_BODY_BYTES) chunks.push(chunk);
    else incoming.destroy();
  });
  incoming.on("end", async () => {
    if (size !== declared) {
      send(response, 400, { error: "REQUEST_INVALID" });
      return;
    }
    active = true;
    try {
      const request = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      send(response, 200, await render(request));
    } catch {
      send(response, 422, { error: "RENDER_FAILED" });
    } finally {
      active = false;
    }
  });
});

server.requestTimeout = 125_000;
server.headersTimeout = 5_000;
server.listen(PORT, HOST);
