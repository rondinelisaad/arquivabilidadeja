import dns from "node:dns/promises";
import net from "node:net";

const UNSAFE = /[\u0000-\u0020\u007f\\]/u;

export function sanitizeOrigin(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > 2048 || UNSAFE.test(value)) {
    throw new Error("URL_INVALID");
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("URL_INVALID");
  }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error("URL_INVALID");
  }
  return parsed.origin;
}

function ipv4Number(address) {
  return address.split(".").reduce((value, octet) => value * 256 + Number(octet), 0);
}

function inCidr4(address, network, prefix) {
  const shift = 32 - prefix;
  return (ipv4Number(address) >>> shift) === (ipv4Number(network) >>> shift);
}

export function isPublicAddress(address) {
  const family = net.isIP(address);
  if (family === 4) {
    return ![
      ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10],
      ["127.0.0.0", 8], ["169.254.0.0", 16], ["172.16.0.0", 12],
      ["192.0.0.0", 24], ["192.0.2.0", 24], ["192.168.0.0", 16],
      ["198.18.0.0", 15], ["198.51.100.0", 24], ["203.0.113.0", 24],
      ["224.0.0.0", 4], ["240.0.0.0", 4],
    ].some(([network, prefix]) => inCidr4(address, network, prefix));
  }
  if (family === 6) {
    const normalized = address.toLowerCase();
    if (normalized.startsWith("::ffff:")) {
      return isPublicAddress(normalized.slice(7));
    }
    return !(
      normalized === "::" || normalized === "::1" ||
      normalized.startsWith("fc") || normalized.startsWith("fd") ||
      normalized.startsWith("fe8") || normalized.startsWith("fe9") ||
      normalized.startsWith("fea") || normalized.startsWith("feb") ||
      normalized.startsWith("ff") || normalized.startsWith("2001:db8:")
    );
  }
  return false;
}

export async function validatePublicUrl(value, resolver = dns.lookup) {
  if (typeof value !== "string" || value.length === 0 || value.length > 2048 || UNSAFE.test(value)) {
    throw new Error("URL_INVALID");
  }
  sanitizeOrigin(value);
  const parsed = new URL(value);
  const port = parsed.port ? Number(parsed.port) : (parsed.protocol === "https:" ? 443 : 80);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("URL_INVALID");
  }
  let records;
  try {
    records = await resolver(parsed.hostname, { all: true, verbatim: true });
  } catch {
    throw new Error("DNS_FAILED");
  }
  if (!Array.isArray(records) || records.length === 0 || records.some(({ address }) => !isPublicAddress(address))) {
    throw new Error("ADDRESS_BLOCKED");
  }
  return parsed;
}

export function validateRequest(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("REQUEST_INVALID");
  const keys = Object.keys(value).sort();
  const expected = ["analysis_id", "attempt_id", "budgets", "profile", "requested_at", "subject_uri"].sort();
  if (JSON.stringify(keys) !== JSON.stringify(expected)) throw new Error("REQUEST_INVALID");
  for (const field of ["analysis_id", "attempt_id"]) {
    if (typeof value[field] !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/u.test(value[field])) {
      throw new Error("REQUEST_INVALID");
    }
  }
  const budgets = value.budgets;
  const profile = value.profile;
  if (!budgets || typeof budgets !== "object" || !profile || typeof profile !== "object") throw new Error("REQUEST_INVALID");
  const bounds = {
    navigation_timeout_milliseconds: [1000, 60000], observation_window_milliseconds: [0, 30000],
    total_timeout_milliseconds: [1000, 120000], max_requests: [1, 2000],
    max_response_bytes: [1, 20 * 1024 * 1024], max_total_transfer_bytes: [1, 200 * 1024 * 1024],
    max_dom_bytes: [1, 10 * 1024 * 1024], max_console_events: [0, 1000],
  };
  if (JSON.stringify(Object.keys(budgets).sort()) !== JSON.stringify(Object.keys(bounds).sort())) throw new Error("REQUEST_INVALID");
  for (const [field, [minimum, maximum]] of Object.entries(bounds)) {
    if (!Number.isInteger(budgets[field]) || budgets[field] < minimum || budgets[field] > maximum) throw new Error("REQUEST_INVALID");
  }
  if (budgets.navigation_timeout_milliseconds + budgets.observation_window_milliseconds > budgets.total_timeout_milliseconds || budgets.max_response_bytes > budgets.max_total_transfer_bytes) throw new Error("REQUEST_INVALID");
  if (profile.profile_id !== "chromium-baseline-1" || profile.user_agent !== "arquivabilidade-ja-browser/0.1" || profile.locale !== "en-US" || profile.timezone_id !== "UTC" || profile.reduced_motion !== "reduce" || profile.service_workers !== "block" || profile.cache_enabled !== false || profile.javascript_enabled !== true) throw new Error("REQUEST_INVALID");
  if (!Number.isInteger(profile.viewport_width) || !Number.isInteger(profile.viewport_height) || !["light", "dark"].includes(profile.color_scheme)) throw new Error("REQUEST_INVALID");
  if (!Number.isFinite(Date.parse(value.requested_at))) throw new Error("REQUEST_INVALID");
  return value;
}
