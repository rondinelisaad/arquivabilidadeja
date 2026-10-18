import assert from "node:assert/strict";
import test from "node:test";

import { isPublicAddress, validatePublicUrl, validateRequest } from "./security.mjs";

test("blocks private, special and documentation addresses", () => {
  for (const address of ["127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "192.0.2.1", "::1", "fd00::1", "2001:db8::1"]) {
    assert.equal(isPublicAddress(address), false, address);
  }
  assert.equal(isPublicAddress("93.184.216.34"), true);
  assert.equal(isPublicAddress("2606:2800:220:1:248:1893:25c8:1946"), true);
});

test("fails closed when any resolved address is blocked", async () => {
  const resolver = async () => [{ address: "93.184.216.34" }, { address: "127.0.0.1" }];
  await assert.rejects(validatePublicUrl("https://example.org/", resolver), /ADDRESS_BLOCKED/);
  await assert.rejects(validatePublicUrl("http://user@example.org/", resolver), /URL_INVALID/);
});

test("accepts the exact deterministic request contract", () => {
  const request = {
    analysis_id: "analysis-1", attempt_id: "attempt-1", subject_uri: "https://example.org/",
    requested_at: "2026-10-19T03:00:00+00:00",
    budgets: {
      navigation_timeout_milliseconds: 15000, observation_window_milliseconds: 5000,
      total_timeout_milliseconds: 30000, max_requests: 500,
      max_response_bytes: 5242880, max_total_transfer_bytes: 52428800,
      max_dom_bytes: 2097152, max_console_events: 100,
    },
    profile: {
      profile_id: "chromium-baseline-1", viewport_width: 1365, viewport_height: 768,
      user_agent: "arquivabilidade-ja-browser/0.1", locale: "en-US", timezone_id: "UTC",
      color_scheme: "light", reduced_motion: "reduce", service_workers: "block",
      cache_enabled: false, javascript_enabled: true,
    },
  };
  assert.equal(validateRequest(request), request);
  assert.throws(() => validateRequest({ ...request, unexpected: true }), /REQUEST_INVALID/);
});
