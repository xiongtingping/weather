import { execFileSync } from "child_process";
import net from "net";
import fs from "fs";
import path from "path";
import { ClobClient, Side, OrderType, AssetType } from "@polymarket/clob-client";
import { createL2Headers } from "@polymarket/clob-client/dist/headers/index.js";
import { GET as HTTP_GET } from "@polymarket/clob-client/dist/http-helpers/index.js";
import axios from "axios";
import { Contract, Wallet, providers, constants, utils } from "ethers";
import https from "https";
import { bootstrap } from "global-agent";

let globalAgentBootstrapped = false;

// Redact secrets from dependencies that may log full Axios configs (including auth headers).
// This avoids leaking POLY_* / PRIVATE_KEY into `data/ui_server.*.log`.
const _origConsoleError = console.error.bind(console);
function _redactSecretText(text) {
  if (!text) return text;
  let out = String(text);
  for (const key of [
    "POLY_API_KEY",
    "POLY_API_SECRET",
    "POLY_API_PASSPHRASE",
    "POLY_PASSPHRASE",
    "POLY_SIGNATURE",
    "PRIVATE_KEY",
  ]) {
    // JSON-style: "KEY":"value"
    out = out.replace(new RegExp(`("${key}"\\s*:\\s*")([^"]+)(")`, "g"), '$1***$3');
    // Env-style: KEY=value
    out = out.replace(new RegExp(`(${key}=)([^\\s&]+)`, "g"), "$1***");
  }
  return out;
}

function _redactHeaders(headers) {
  if (!headers || typeof headers !== "object") return headers;
  const out = { ...headers };
  for (const k of Object.keys(out)) {
    const upper = String(k || "").toUpperCase();
    if (
      upper.includes("POLY_API_KEY")
      || upper.includes("POLY_API_SECRET")
      || upper.includes("POLY_API_PASSPHRASE")
      || upper.includes("POLY_PASSPHRASE")
      || upper.includes("POLY_SIGNATURE")
      || upper.includes("PRIVATE_KEY")
      || upper === "AUTHORIZATION"
    ) {
      out[k] = "***";
    } else if (typeof out[k] === "string") {
      out[k] = _redactSecretText(out[k]);
    }
  }
  return out;
}

function _redactClobClientErrorPayload(payload) {
  if (!payload || typeof payload !== "object") return payload;
  const out = { ...payload };
  if (out.config && typeof out.config === "object") {
    out.config = { ...out.config };
    if (out.config.headers) {
      out.config.headers = _redactHeaders(out.config.headers);
    }
  }
  if (typeof out.error === "string") out.error = _redactSecretText(out.error);
  if (typeof out.data === "string") out.data = _redactSecretText(out.data);
  if (typeof out.statusText === "string") out.statusText = _redactSecretText(out.statusText);
  return out;
}

console.error = (...args) => {
  try {
    if (args.length >= 2 && typeof args[0] === "string" && args[0].includes("[CLOB Client] request error")) {
      const second = args[1];
      if (typeof second === "string") {
        const trimmed = second.trim();
        if (trimmed.startsWith("{") && trimmed.endsWith("}")) {
          try {
            const parsed = JSON.parse(trimmed);
            args[1] = JSON.stringify(_redactClobClientErrorPayload(parsed));
          } catch (err) {
            args[1] = _redactSecretText(second);
          }
        } else {
          args[1] = _redactSecretText(second);
        }
      } else if (second && typeof second === "object") {
        args[1] = _redactClobClientErrorPayload(second);
      }
    }
    args = args.map((val) => (typeof val === "string" ? _redactSecretText(val) : val));
  } catch (err) {
    // ignore redact errors, never block logging
  }
  _origConsoleError(...args);
};

function ensureGlobalAgent() {
  if (!globalAgentBootstrapped) {
    bootstrap();
    globalAgentBootstrapped = true;
  }
}

function isTruthy(value) {
  if (!value) return false;
  const normalized = String(value).trim().toLowerCase();
  return ["1", "true", "yes", "on"].includes(normalized);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function proxyReachable(host, port, timeoutMs = 400) {
  return new Promise((resolve) => {
    if (!host || !port) return resolve(false);
    let settled = false;
    const done = (ok) => {
      if (settled) return;
      settled = true;
      try {
        socket.destroy();
      } catch (err) {
        // ignore
      }
      resolve(ok);
    };
    const socket = net.createConnection({ host, port });
    socket.setTimeout(timeoutMs);
    socket.on("connect", () => done(true));
    socket.on("timeout", () => done(false));
    socket.on("error", () => done(false));
  });
}

function parseScutilValue(text, key) {
  const pattern = new RegExp(`^\\s*${key}\\s*:\\s*(.+)$`, "m");
  const match = text.match(pattern);
  return match ? match[1].trim() : "";
}

function parseProxyHostPort(proxyUrl) {
  if (!proxyUrl) return { host: "", port: 0 };
  let value = proxyUrl;
  if (!value.includes("://")) {
    value = `http://${value}`;
  }
  try {
    const parsed = new URL(value);
    return { host: parsed.hostname || "", port: Number(parsed.port || 0) };
  } catch (err) {
    return { host: "", port: 0 };
  }
}

function parsePortList(value) {
  if (!value) return [];
  const parts = String(value).split(/[,\s]+/).map((part) => part.trim()).filter(Boolean);
  const ports = [];
  for (const part of parts) {
    const port = Number(part);
    if (!Number.isFinite(port) || port <= 0 || port > 65535) continue;
    if (!ports.includes(port)) ports.push(port);
  }
  return ports;
}

function resolveProxyPortCandidates(scutilOutput = "") {
  const ports = [];
  const envPorts = parsePortList(process.env.PM_PROXY_PORTS || "");
  for (const port of envPorts) {
    if (!ports.includes(port)) ports.push(port);
  }
  const proxyEnv = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.ALL_PROXY || "";
  const parsed = parseProxyHostPort(proxyEnv);
  if (parsed.port && !ports.includes(parsed.port)) {
    ports.push(parsed.port);
  }
  if (!ports.length && scutilOutput) {
    for (const key of ["HTTPPort", "HTTPSPort", "SOCKSPort"]) {
      const raw = parseScutilValue(scutilOutput, key);
      const port = Number(raw);
      if (Number.isFinite(port) && port > 0 && port <= 65535 && !ports.includes(port)) {
        ports.push(port);
      }
    }
  }
  return ports;
}

function stripProxyEnv() {
  delete process.env.HTTP_PROXY;
  delete process.env.HTTPS_PROXY;
  delete process.env.ALL_PROXY;
  delete process.env.http_proxy;
  delete process.env.https_proxy;
  delete process.env.all_proxy;
  delete process.env.GLOBAL_AGENT_HTTP_PROXY;
}

let proxyPortCandidates = [];
let proxyPortIndex = -1;
let savedProxySnapshot = null;
const DATA_DIR = env("PM_DATA_DIR", path.resolve(process.cwd(), "data"));
const PROXY_COOLDOWN_PATH = path.join(DATA_DIR, "clob_proxy_cooldown.json");
const PROXY_COOLDOWN_SEC = Number(env("PM_CLOB_PROXY_COOLDOWN_SEC", "900"));
const CLAIM_PENDING_PATH = path.join(DATA_DIR, "claim_pending_conditions.json");

function readProxyCooldown() {
  try {
    const raw = fs.readFileSync(PROXY_COOLDOWN_PATH, "utf8");
    const data = JSON.parse(raw);
    const until = Number(data?.until || 0);
    return Number.isFinite(until) ? until : 0;
  } catch (err) {
    return 0;
  }
}

function writeProxyCooldown(reason) {
  const ttl = Number.isFinite(PROXY_COOLDOWN_SEC) && PROXY_COOLDOWN_SEC > 0 ? PROXY_COOLDOWN_SEC : 900;
  const payload = {
    until: Date.now() + ttl * 1000,
    reason: String(reason || ""),
    updated_at: new Date().toISOString(),
  };
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(PROXY_COOLDOWN_PATH, JSON.stringify(payload));
  } catch (err) {
    // ignore
  }
}

function proxyCooldownActive() {
  const until = readProxyCooldown();
  return Number.isFinite(until) && until > Date.now();
}

function readClaimPending() {
  try {
    const raw = fs.readFileSync(CLAIM_PENDING_PATH, "utf8");
    const data = JSON.parse(raw);
    if (!data || typeof data !== "object") return {};
    const out = {};
    for (const [conditionId, item] of Object.entries(data)) {
      if (!/^0x[0-9a-fA-F]{64}$/.test(String(conditionId || ""))) continue;
      if (!item || typeof item !== "object") continue;
      const atMs = Number(item.at_ms || 0);
      if (!Number.isFinite(atMs) || atMs <= 0) continue;
      out[String(conditionId).toLowerCase()] = {
        tx_hash: String(item.tx_hash || ""),
        at_ms: atMs,
      };
    }
    return out;
  } catch (err) {
    return {};
  }
}

function writeClaimPending(data) {
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(CLAIM_PENDING_PATH, JSON.stringify(data));
  } catch (err) {
    // ignore
  }
}

function snapshotProxyEnv() {
  return {
    http: process.env.HTTP_PROXY || "",
    https: process.env.HTTPS_PROXY || "",
    all: process.env.ALL_PROXY || "",
    global: process.env.GLOBAL_AGENT_HTTP_PROXY || "",
  };
}

function hasProxySnapshot(snapshot) {
  if (!snapshot) return false;
  return Boolean(snapshot.http || snapshot.https || snapshot.all || snapshot.global);
}

function applyProxySnapshot(snapshot) {
  if (!snapshot) return false;
  if (snapshot.http) process.env.HTTP_PROXY = snapshot.http;
  if (snapshot.https) process.env.HTTPS_PROXY = snapshot.https;
  if (snapshot.all) process.env.ALL_PROXY = snapshot.all;
  if (snapshot.global) {
    process.env.GLOBAL_AGENT_HTTP_PROXY = snapshot.global;
  }
  const proxyEnv = currentProxyEnv();
  if (proxyEnv && !process.env.GLOBAL_AGENT_HTTP_PROXY) {
    process.env.GLOBAL_AGENT_HTTP_PROXY = proxyEnv;
  }
  if (process.env.GLOBAL_AGENT_HTTP_PROXY) {
    ensureGlobalAgent();
  }
  return Boolean(proxyEnv);
}

const proxyCooldownOnBoot = proxyCooldownActive();
if (proxyCooldownOnBoot) {
  stripProxyEnv();
}

function applyProxyPort(host, port) {
  if (!host || !port) return false;
  const proxyValue = `http://${host}:${port}`;
  process.env.HTTP_PROXY = proxyValue;
  process.env.HTTPS_PROXY = proxyValue;
  process.env.GLOBAL_AGENT_HTTP_PROXY = proxyValue;
  ensureGlobalAgent();
  return true;
}

function rotateProxyPort() {
  const current = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.ALL_PROXY || "";
  const parsed = parseProxyHostPort(current);
  if (!parsed.host || !parsed.port) return false;
  if (!["127.0.0.1", "localhost"].includes(parsed.host)) return false;
  if (!proxyPortCandidates.length) return false;
  const currentIndex = proxyPortCandidates.indexOf(parsed.port);
  if (currentIndex >= 0) {
    proxyPortIndex = currentIndex;
  }
  if (proxyPortCandidates.length <= 1) return false;
  const nextIndex = (proxyPortIndex + 1) % proxyPortCandidates.length;
  if (nextIndex === proxyPortIndex) return false;
  proxyPortIndex = nextIndex;
  return applyProxyPort(parsed.host, proxyPortCandidates[proxyPortIndex]);
}

let applyingSystemProxyEnv = false;

function maybeApplySystemProxy() {
  if (proxyCooldownOnBoot) {
    stripProxyEnv();
    return;
  }
  if (isTruthy(process.env.PM_CLOB_FORCE_DIRECT)) {
    stripProxyEnv();
    return;
  }
  if (
    process.env.HTTP_PROXY ||
    process.env.HTTPS_PROXY ||
    process.env.ALL_PROXY ||
    process.env.http_proxy ||
    process.env.https_proxy ||
    process.env.all_proxy
  ) {
    let output = "";
    if (process.platform === "darwin") {
      try {
        output = execFileSync("scutil", ["--proxy"], { encoding: "utf8" });
      } catch (err) {
        output = "";
      }
    }
    proxyPortCandidates = resolveProxyPortCandidates(output);
    const current = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.ALL_PROXY || "";
    const parsed = parseProxyHostPort(current);
    if (parsed.host && ["127.0.0.1", "localhost"].includes(parsed.host) && proxyPortCandidates.length) {
      const target = proxyPortCandidates[0];
      if (target && parsed.port !== target) {
        applyProxyPort(parsed.host, target);
        proxyPortIndex = proxyPortCandidates.indexOf(target);
      }
    }
    return;
  }
  if (process.platform !== "darwin") return;
  let output = "";
  try {
    output = execFileSync("scutil", ["--proxy"], { encoding: "utf8" });
  } catch (err) {
    return;
  }
  proxyPortCandidates = resolveProxyPortCandidates(output);
  const httpEnable = parseScutilValue(output, "HTTPEnable") === "1";
  const httpsEnable = parseScutilValue(output, "HTTPSEnable") === "1";
  const socksEnable = parseScutilValue(output, "SOCKSEnable") === "1";
  const httpHost = parseScutilValue(output, "HTTPProxy");
  const httpPort = parseScutilValue(output, "HTTPPort");
  const httpsHost = parseScutilValue(output, "HTTPSProxy");
  const httpsPort = parseScutilValue(output, "HTTPSPort");
  const socksHost = parseScutilValue(output, "SOCKSProxy");
  const socksPort = parseScutilValue(output, "SOCKSPort");
  if (httpEnable && httpHost && httpPort) {
    process.env.HTTP_PROXY = `http://${httpHost}:${httpPort}`;
  }
  if (httpsEnable && httpsHost && httpsPort) {
    process.env.HTTPS_PROXY = `http://${httpsHost}:${httpsPort}`;
  }
  if (socksEnable && socksHost && socksPort) {
    process.env.ALL_PROXY = `socks5://${socksHost}:${socksPort}`;
  }
  const proxyEnv = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.ALL_PROXY || "";
  const parsed = parseProxyHostPort(proxyEnv);
  if (parsed.host && ["127.0.0.1", "localhost"].includes(parsed.host) && proxyPortCandidates.length) {
    const target = proxyPortCandidates[0];
    if (target && parsed.port !== target) {
      applyProxyPort(parsed.host, target);
      proxyPortIndex = proxyPortCandidates.indexOf(target);
    }
  }
}

function applySystemProxyEnv() {
  if (applyingSystemProxyEnv) {
    return false;
  }
  applyingSystemProxyEnv = true;
  try {
  if (proxyCooldownOnBoot) {
    return false;
  }
  const before = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.ALL_PROXY || "";
  if (!before && hasProxySnapshot(savedProxySnapshot)) {
    const applied = applyProxySnapshot(savedProxySnapshot);
    if (applied) {
      return true;
    }
  }
  maybeApplySystemProxy();
  const proxyEnv = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.ALL_PROXY || "";
  if (proxyEnv && !process.env.GLOBAL_AGENT_HTTP_PROXY) {
    process.env.GLOBAL_AGENT_HTTP_PROXY = proxyEnv;
  }
  if (process.env.GLOBAL_AGENT_HTTP_PROXY) {
    ensureGlobalAgent();
  }
  return Boolean(proxyEnv || before);
  } finally {
    applyingSystemProxyEnv = false;
  }
}

function currentProxyEnv() {
  return (
    process.env.GLOBAL_AGENT_HTTP_PROXY ||
    process.env.HTTPS_PROXY ||
    process.env.HTTP_PROXY ||
    process.env.ALL_PROXY ||
    ""
  );
}

function isRetryableError(err) {
  if (!err) return false;
  if (axios.isAxiosError(err)) {
    if (err.response && isHttpErrorStatus(err.response.status)) {
      const status = Number(err.response.status);
      if (status >= 400 && status < 500 && status !== 408 && status !== 429) {
        return false;
      }
      return true;
    }
    if (err.code) {
      const code = String(err.code).toUpperCase();
      if (["ECONNABORTED", "ECONNREFUSED", "ETIMEDOUT", "EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ENETUNREACH"].includes(code)) {
        return true;
      }
      if (code === "ERR_FR_TOO_MANY_REDIRECTS") {
        return true;
      }
    }
    const msg = String(err.message || "").toLowerCase();
    return (
      msg.includes("timeout")
      || msg.includes("network error")
      || msg.includes("socket hang up")
      || (msg.includes("cannot read properties of undefined") && msg.includes("tostring"))
      || msg.includes("maximum number of redirects")
      || msg.includes("too many redirects")
      || msg.includes("status code 502")
      || msg.includes("bad gateway")
      || msg.includes("not iterable")
    );
  }
  const msg = String(err.message || err || "").toLowerCase();
  if (!msg) return false;
  return (
    msg.includes("timeout")
    || msg.includes("econn")
    || msg.includes("enotfound")
    || msg.includes("network")
    || (msg.includes("cannot read properties of undefined") && msg.includes("tostring"))
    || msg.includes("maximum number of redirects")
    || msg.includes("too many redirects")
    || msg.includes("status code 502")
    || msg.includes("bad gateway")
    || msg.includes("not iterable")
  );
}

function retryDelayMs(attempt) {
  const baseRaw = env("PM_CLOB_RETRY_BASE_MS", "500");
  const maxRaw = env("PM_CLOB_RETRY_MAX_MS", "5000");
  const base = Number(baseRaw);
  const max = Number(maxRaw);
  if (!Number.isFinite(base) || base <= 0) return 0;
  let delay = base * Math.pow(2, attempt);
  if (Number.isFinite(max) && max > 0 && delay > max) {
    delay = max;
  }
  return Math.round(delay);
}

async function runWithProxyFallback(label, run, opts = {}) {
  const maxAttemptsRaw = env("PM_CLOB_RETRY_MAX", "3");
  const maxAttempts = Number(maxAttemptsRaw);
  const configuredAttempts = Number.isFinite(maxAttempts) && maxAttempts > 0 ? Math.floor(maxAttempts) : 1;
  const minAttemptsOpt = Number(opts?.minAttempts || 1);
  const minAttempts = Number.isFinite(minAttemptsOpt) && minAttemptsOpt > 0 ? Math.floor(minAttemptsOpt) : 1;
  const autoDualAttempts = (!isTruthy(process.env.PM_CLOB_FORCE_DIRECT) && !isTruthy(process.env.PM_CLOB_FORCE_PROXY)) ? 2 : 1;
  const attempts = Math.max(configuredAttempts, minAttempts, autoDualAttempts);
  const directRetriesRaw = env("PM_CLOB_DIRECT_RETRIES", "2");
  const directRetries = Math.max(1, Number.isFinite(Number(directRetriesRaw)) ? Number(directRetriesRaw) : 1);
  let attempt = 0;
  let lastErr = null;
  const proxyPortsTried = new Set();
  let directTried = false;
  let directAttempts = 0;
  let proxyDisabled = proxyCooldownOnBoot;
  const isProxyBadError = (err) => {
    const msg = String(err?.message || err || "").toLowerCase();
    return msg.includes("maximum number of redirects")
      || msg.includes("too many redirects")
      || msg.includes("service unavailable")
      || msg.includes("status code 503")
      || msg.includes("status code 502")
      || msg.includes("bad gateway")
      || msg.includes(" 502");
  };
  const isProxyConnectError = (err) => {
    const msg = String(err?.message || err || "").toLowerCase();
    return msg.includes("econnrefused")
      || msg.includes("etimedout")
      || msg.includes("timeout")
      || msg.includes("tunnel connection failed")
      || msg.includes("proxy")
      || msg.includes("socket");
  };
  const forceProxy = isTruthy(process.env.PM_CLOB_FORCE_PROXY);
  if (forceProxy && !currentProxyEnv()) {
    applySystemProxyEnv();
  }
  while (attempt < attempts) {
    try {
      return await run();
    } catch (err) {
      lastErr = err;
      if (isTruthy(process.env.PM_CLOB_FORCE_DIRECT) || !isRetryableError(err)) {
        throw err;
      }
      const hadProxy = Boolean(currentProxyEnv());
      if (hadProxy) {
        const currentProxy = parseProxyHostPort(currentProxyEnv());
        const isLocalProxy = ["127.0.0.1", "localhost"].includes(currentProxy.host || "");
        if (isLocalProxy && isProxyConnectError(err)) {
          const reachable = await proxyReachable(currentProxy.host, currentProxy.port, 500);
          if (!reachable) {
            if (rotateProxyPort()) {
              console.error(`[CLOB Client] ${label} proxy unreachable, retry next port`);
            } else {
              if (forceProxy) {
                console.error(`[CLOB Client] ${label} proxy unreachable; force proxy enabled; retrying`);
              } else {
                console.error(`[CLOB Client] ${label} proxy unreachable, fallback to direct`);
                stripProxyEnv();
                proxyDisabled = true;
                directTried = true;
              }
            }
          }
        }
        if (isProxyBadError(err)) {
          if (!forceProxy) {
            console.error(`[CLOB Client] ${label} proxy error, fallback to direct`);
            stripProxyEnv();
            proxyDisabled = true;
            writeProxyCooldown(err?.message || "proxy_error");
            directTried = true;
          } else if (rotateProxyPort()) {
            console.error(`[CLOB Client] ${label} proxy error, retry next port`);
          } else {
            console.error(`[CLOB Client] ${label} proxy error; force proxy enabled; retrying`);
          }
        } else {
        const currentProxy = parseProxyHostPort(currentProxyEnv());
        if (currentProxy.port) {
          proxyPortsTried.add(currentProxy.port);
        }
        const allPortsTried = proxyPortCandidates.length
          ? proxyPortsTried.size >= proxyPortCandidates.length
          : false;
        if (!forceProxy && allPortsTried && !directTried) {
          console.error(`[CLOB Client] ${label} failed via proxy, retry direct`);
          stripProxyEnv();
          directTried = true;
        } else if (rotateProxyPort()) {
          const nextProxy = parseProxyHostPort(currentProxyEnv());
          if (nextProxy.port) {
            proxyPortsTried.add(nextProxy.port);
          }
          console.error(`[CLOB Client] ${label} failed via proxy, retry next port`);
        } else if (!forceProxy && attempt === 0 && !directTried) {
          console.error(`[CLOB Client] ${label} failed via proxy, retry direct`);
          stripProxyEnv();
          directTried = true;
        } else if (forceProxy) {
          console.error(`[CLOB Client] ${label} failed via proxy; force proxy enabled; retrying`);
        }
        }
      } else {
        directAttempts += 1;
        if (forceProxy) {
          const applied = applySystemProxyEnv();
          if (!applied) {
            throw err;
          }
          console.error(`[CLOB Client] ${label} failed direct, retry via proxy`);
        } else if (proxyDisabled || directAttempts < directRetries) {
          console.error(`[CLOB Client] ${label} failed direct, retry direct (${directAttempts}/${directRetries})`);
        } else {
          console.error(`[CLOB Client] ${label} failed direct, retry via proxy`);
          const applied = applySystemProxyEnv();
          if (!applied) {
            throw err;
          }
        }
      }
      attempt += 1;
      if (attempt >= attempts) break;
      const delayMs = retryDelayMs(attempt - 1);
      if (delayMs > 0) {
        console.error(`[CLOB Client] ${label} retrying in ${delayMs}ms (attempt ${attempt + 1}/${attempts})`);
        await sleep(delayMs);
      }
    }
  }
  throw lastErr;
}

maybeApplySystemProxy();

const directFirst = !isTruthy(process.env.PM_CLOB_FORCE_DIRECT)
  && !isTruthy(process.env.PM_CLOB_FORCE_PROXY)
  && isTruthy(process.env.PM_CLOB_DIRECT_FIRST ?? "1");
if (directFirst) {
  const snapshot = snapshotProxyEnv();
  if (hasProxySnapshot(snapshot)) {
    savedProxySnapshot = snapshot;
    stripProxyEnv();
  }
}

const proxyEnv = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.ALL_PROXY || "";
if (proxyEnv && !process.env.GLOBAL_AGENT_HTTP_PROXY) {
  process.env.GLOBAL_AGENT_HTTP_PROXY = proxyEnv;
}
if (process.env.GLOBAL_AGENT_HTTP_PROXY) {
  ensureGlobalAgent();
}

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (token.startsWith("--")) {
      const key = token.slice(2);
      const value = argv[i + 1];
      out[key] = value;
      i += 1;
    } else {
      out._.push(token);
    }
  }
  return out;
}

function env(name, fallback = "") {
  return process.env[name] || fallback;
}

function requireEnv(name) {
  const value = env(name);
  if (!value) {
    throw new Error(`missing_env:${name}`);
  }
  return value;
}

function defaultTickSize() {
  const raw = env("PM_CLOB_DEFAULT_TICK_SIZE", "0.01");
  const val = Number(raw);
  if (!Number.isFinite(val) || val <= 0) return 0.01;
  return val;
}

function defaultNegRisk() {
  return isTruthy(env("PM_CLOB_DEFAULT_NEG_RISK", "0"));
}

function normalizeRpcUrl(raw) {
  let value = String(raw || "").trim();
  while (value.startsWith("POLY_RPC_URL=")) {
    value = value.slice("POLY_RPC_URL=".length).trim();
  }
  return value;
}

function normalizeHexAddress(raw) {
  const value = String(raw || "").trim();
  if (!/^0x[0-9a-fA-F]{40}$/.test(value)) return "";
  return value;
}

function normalizeHexBytes32(raw) {
  const value = String(raw || "").trim();
  if (!/^0x[0-9a-fA-F]{64}$/.test(value)) return "";
  return value.toLowerCase();
}

function parsePositionsPayload(payload) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== "object") return [];
  for (const key of ["positions", "data", "result", "items", "rows", "results"]) {
    const val = payload[key];
    if (Array.isArray(val)) return val;
    if (val && typeof val === "object") {
      for (const subKey of ["positions", "items", "rows", "results"]) {
        if (Array.isArray(val[subKey])) return val[subKey];
      }
    }
  }
  return [];
}

function parseOutcomeIndex(raw) {
  if (raw === null || raw === undefined || raw === "") return null;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 0 || n > 255) return null;
  return n;
}

function parseFiniteNumber(raw) {
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  return n;
}

function collectClaimTargets(positions, maxPerLoop, minSize) {
  const grouped = new Map();
  for (const row of positions || []) {
    if (!row || typeof row !== "object") continue;
    if (!Boolean(row.redeemable)) continue;
    const size = parseFiniteNumber(row.size);
    if (size === null || size <= minSize) continue;
    const conditionId = normalizeHexBytes32(row.conditionId);
    if (!conditionId) continue;
    const outcomeIndex = parseOutcomeIndex(row.outcomeIndex);
    if (outcomeIndex === null) continue;
    const indexSet = (1n << BigInt(outcomeIndex)).toString();
    const key = conditionId;
    if (!grouped.has(key)) {
      grouped.set(key, {
        conditionId,
        slug: String(row.slug || ""),
        title: String(row.title || ""),
        endDate: String(row.endDate || ""),
        totalSize: 0,
        rows: [],
        indexSets: new Set(),
      });
    }
    const item = grouped.get(key);
    item.totalSize += size;
    item.rows.push({
      outcome: String(row.outcome || ""),
      outcomeIndex,
      size,
      asset: String(row.asset || ""),
    });
    item.indexSets.add(indexSet);
  }
  const sorted = Array.from(grouped.values()).sort((a, b) => {
    const endA = String(a.endDate || "");
    const endB = String(b.endDate || "");
    if (endA && endB && endA !== endB) return endA < endB ? -1 : 1;
    if (a.totalSize !== b.totalSize) return b.totalSize - a.totalSize;
    return String(a.slug || "").localeCompare(String(b.slug || ""));
  });
  return sorted.slice(0, maxPerLoop > 0 ? maxPerLoop : sorted.length).map((item) => ({
    conditionId: item.conditionId,
    slug: item.slug,
    title: item.title,
    endDate: item.endDate,
    totalSize: item.totalSize,
    indexSets: Array.from(item.indexSets.values()),
    outcomes: item.rows,
  }));
}

const CONDITIONAL_TOKENS_ABI = [
  "function redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)",
];

function isHttpErrorStatus(status) {
  if (status === null || status === undefined) return false;
  const num = Number(status);
  if (!Number.isFinite(num)) return false;
  return num >= 400;
}

const timeoutRaw = env("PM_CLOB_TIMEOUT_MS", "30000");
const timeoutMs = Number(timeoutRaw);
if (Number.isFinite(timeoutMs) && timeoutMs > 0) {
  axios.defaults.timeout = timeoutMs;
}

async function main() {
  const argv = process.argv.slice(2);
  const command = argv[0];
  const args = parseArgs(argv.slice(1));

  if (!command || !["place-order", "cancel-orders", "derive-api-keys", "fetch-trades", "fetch-open-orders", "balance", "get-order", "claim"].includes(command)) {
    throw new Error("usage: place-order | cancel-orders | derive-api-keys | fetch-trades | fetch-open-orders | balance | get-order | claim");
  }

  const host = env("PM_CLOB_HOST", "https://clob.polymarket.com");
  const chainId = Number(env("PM_CHAIN_ID", "137"));
  const signer = new Wallet(requireEnv("PRIVATE_KEY"));

  if (command === "derive-api-keys") {
    const client = new ClobClient(host, chainId, signer);
    let userApiCreds = null;
    try {
      userApiCreds = await client.createApiKey();
    } catch (err) {
      userApiCreds = null;
    }
    if (!userApiCreds || !userApiCreds.key) {
      userApiCreds = await client.deriveApiKey();
    }
    console.log(JSON.stringify(userApiCreds));
    return;
  }

  if (command === "claim") {
    const maxArg = Number(args.max ?? args.limit ?? env("POLY_CLAIM_MAX_PER_LOOP", "2"));
    const maxPerLoop = Number.isFinite(maxArg) && maxArg > 0 ? Math.floor(maxArg) : 2;
    const minSizeArg = Number(args["min-size"] ?? env("POLY_CLAIM_MIN_SIZE", "0"));
    const minSize = Number.isFinite(minSizeArg) && minSizeArg > 0 ? minSizeArg : 0;
    const fetchLimitArg = Number(env("POLY_CLAIM_FETCH_LIMIT", "500"));
    const fetchLimit = Number.isFinite(fetchLimitArg) && fetchLimitArg > 0 ? Math.floor(fetchLimitArg) : 500;
    const retriesArg = Number(env("POLY_DATA_API_RETRIES", "2"));
    const retries = Number.isFinite(retriesArg) && retriesArg > 0 ? Math.floor(retriesArg) : 2;
    const timeoutSecArg = Number(env("POLY_DATA_API_TIMEOUT", "8"));
    const timeoutMs = Number.isFinite(timeoutSecArg) && timeoutSecArg > 0 ? Math.round(timeoutSecArg * 1000) : 8000;
    const verify = isTruthy(env("POLY_DATA_API_VERIFY", "1"));
    const insecureFallback = isTruthy(env("POLY_DATA_API_INSECURE_FALLBACK", "0"));
    const dryRun = isTruthy(args["dry-run"] || env("POLY_CLAIM_DRY_RUN", "0"));
    const waitConfirm = !isTruthy(env("POLY_CLAIM_SKIP_WAIT", "1"));
    const confirmBlocksArg = Number(env("POLY_CLAIM_CONFIRM_BLOCKS", "1"));
    const confirmBlocks = Number.isFinite(confirmBlocksArg) && confirmBlocksArg > 0 ? Math.floor(confirmBlocksArg) : 1;
    const rpcTimeoutSecArg = Number(env("POLY_CLAIM_RPC_TIMEOUT_SEC", "30"));
    const rpcTimeoutMs = Number.isFinite(rpcTimeoutSecArg) && rpcTimeoutSecArg > 0 ? Math.round(rpcTimeoutSecArg * 1000) : 30000;
    const waitTimeoutSecArg = Number(env("POLY_CLAIM_WAIT_TIMEOUT_SEC", "90"));
    const waitTimeoutMs = Number.isFinite(waitTimeoutSecArg) && waitTimeoutSecArg > 0 ? Math.round(waitTimeoutSecArg * 1000) : 90000;
    const gasLimitArg = Number(env("POLY_CLAIM_GAS_LIMIT", "650000"));
    const gasLimit = Number.isFinite(gasLimitArg) && gasLimitArg > 0 ? Math.floor(gasLimitArg) : 650000;
    const maxPriorityGweiArg = Number(env("POLY_CLAIM_MAX_PRIORITY_GWEI", "30"));
    const maxFeeGweiArg = Number(env("POLY_CLAIM_MAX_FEE_GWEI", "120"));
    const pendingTtlSecArg = Number(env("POLY_CLAIM_PENDING_TTL_SEC", "600"));
    const pendingTtlSec = Number.isFinite(pendingTtlSecArg) && pendingTtlSecArg > 0 ? pendingTtlSecArg : 600;
    const rpcUrl = normalizeRpcUrl(env("POLY_RPC_URL", env("RPC_URL", "")));
    if (!rpcUrl) {
      throw new Error("missing_env:POLY_RPC_URL");
    }
    const dataApiHostRaw = String(env("POLY_DATA_API_HOST", "https://data-api.polymarket.com") || "").trim();
    const dataApiHost = dataApiHostRaw.replace(/\/+$/, "");
    if (!dataApiHost) {
      throw new Error("missing_env:POLY_DATA_API_HOST");
    }
    const user = env("POLY_DATA_API_USER", env("PM_FUNDER_ADDRESS", signer.address));
    if (!user) {
      throw new Error("missing_env:POLY_DATA_API_USER_or_PM_FUNDER_ADDRESS");
    }
    const ctfAddress = normalizeHexAddress(
      env("POLY_CTF_ADDRESS", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
    );
    if (!ctfAddress) {
      throw new Error("invalid_env:POLY_CTF_ADDRESS");
    }
    const collateralAddress = normalizeHexAddress(
      env("POLY_COLLATERAL_ADDRESS", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
    );
    if (!collateralAddress) {
      throw new Error("invalid_env:POLY_COLLATERAL_ADDRESS");
    }

    const fetchPositions = async (allowInsecureAgent) => {
      const config = {
        params: {
          user,
          sizeThreshold: 0,
          limit: fetchLimit,
          offset: 0,
        },
        timeout: timeoutMs,
      };
      if (allowInsecureAgent) {
        config.httpsAgent = new https.Agent({ rejectUnauthorized: false });
      }
      const response = await axios.get(`${dataApiHost}/positions`, config);
      const rows = parsePositionsPayload(response?.data);
      return Array.isArray(rows) ? rows : [];
    };

    let positions = [];
    const fetchErrors = [];
    for (let attempt = 0; attempt < retries; attempt += 1) {
      try {
        positions = await runWithProxyFallback(
          "claim-fetch-positions",
          async () => fetchPositions(false),
          { minAttempts: 2 }
        );
        break;
      } catch (err) {
        fetchErrors.push(String(err?.message || err || ""));
      }
    }
    if (!positions.length && insecureFallback) {
      try {
        positions = await fetchPositions(true);
      } catch (err) {
        fetchErrors.push(String(err?.message || err || ""));
      }
    }
    if (!Array.isArray(positions) || positions.length === 0) {
      throw new Error(`claim_positions_fetch_failed:${fetchErrors[fetchErrors.length - 1] || "empty_positions"}`);
    }
    const pendingMap = readClaimPending();
    const nowMs = Date.now();
    const pendingFiltered = {};
    for (const [conditionId, item] of Object.entries(pendingMap)) {
      const atMs = Number(item?.at_ms || 0);
      if (!Number.isFinite(atMs) || atMs <= 0) continue;
      if ((nowMs - atMs) > pendingTtlSec * 1000) continue;
      pendingFiltered[String(conditionId).toLowerCase()] = item;
    }
    const rawTargets = collectClaimTargets(positions, maxPerLoop * 4, minSize);
    let skippedPending = 0;
    const targets = [];
    for (const item of rawTargets) {
      const cond = normalizeHexBytes32(item?.conditionId || "");
      if (!cond) continue;
      if (pendingFiltered[cond]) {
        skippedPending += 1;
        continue;
      }
      targets.push(item);
      if (targets.length >= maxPerLoop) break;
    }
    if (!targets.length) {
      writeClaimPending(pendingFiltered);
      console.log(
        JSON.stringify({
          ok: true,
          command: "claim",
          attempted: 0,
          claimed: 0,
          failed: 0,
          skipped: skippedPending,
          reason: skippedPending > 0 ? "all_pending_inflight" : "no_redeemable_positions",
          max_per_loop: maxPerLoop,
          total_positions: positions.length,
        })
      );
      return;
    }
    if (dryRun) {
      console.log(
        JSON.stringify({
          ok: true,
          command: "claim",
          dry_run: true,
          attempted: targets.length,
          claimed: 0,
          failed: 0,
          skipped: skippedPending,
          max_per_loop: maxPerLoop,
          targets,
        })
      );
      return;
    }

    const parseClaimError = (err) => {
      const pieces = [];
      if (err?.reason) pieces.push(String(err.reason));
      if (err?.error?.message) pieces.push(String(err.error.message));
      if (err?.data?.message) pieces.push(String(err.data.message));
      if (err?.message) pieces.push(String(err.message));
      if (!pieces.length) pieces.push(String(err || "claim_failed"));
      return pieces.join(" | ");
    };
    const withTimeout = async (promise, ms, label) => {
      if (!Number.isFinite(ms) || ms <= 0) return promise;
      let timer = null;
      try {
        return await Promise.race([
          promise,
          new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error(`${label}_timeout`)), ms);
          }),
        ]);
      } finally {
        if (timer) clearTimeout(timer);
      }
    };
    const isRpcTransportError = (errorText) => {
      const msg = String(errorText || "").toLowerCase();
      return (
        msg.includes("processing response error")
        || msg.includes("timeout")
        || msg.includes("network")
        || msg.includes("econn")
        || msg.includes("invalid json rpc")
      );
    };
    const executeClaims = async (useDirectRpc) => {
      const snapshot = snapshotProxyEnv();
      if (useDirectRpc) {
        stripProxyEnv();
      }
      try {
        const provider = new providers.JsonRpcProvider(rpcUrl);
        const claimSigner = signer.connect(provider);
        const ctf = new Contract(ctfAddress, CONDITIONAL_TOKENS_ABI, claimSigner);
        const txOverrides = { gasLimit };
        let maxPriorityGwei = Number.isFinite(maxPriorityGweiArg) && maxPriorityGweiArg > 0
          ? maxPriorityGweiArg
          : 0;
        let maxFeeGwei = Number.isFinite(maxFeeGweiArg) && maxFeeGweiArg > 0
          ? maxFeeGweiArg
          : 0;
        if (maxPriorityGwei > 0 && maxFeeGwei <= 0) {
          maxFeeGwei = maxPriorityGwei * 2;
        }
        if (maxFeeGwei > 0 && maxPriorityGwei > maxFeeGwei) {
          maxFeeGwei = maxPriorityGwei;
        }
        if (maxPriorityGwei > 0) {
          txOverrides.maxPriorityFeePerGas = utils.parseUnits(String(maxPriorityGwei), "gwei");
        }
        if (maxFeeGwei > 0) {
          txOverrides.maxFeePerGas = utils.parseUnits(String(maxFeeGwei), "gwei");
        }
        const txs = [];
        const failures = [];
        for (const target of targets) {
          const conditionId = normalizeHexBytes32(target?.conditionId || "");
          const indexSets = Array.isArray(target?.indexSets) ? target.indexSets.filter(Boolean) : [];
          if (!conditionId || !indexSets.length) {
            failures.push({
              conditionId: target?.conditionId || "",
              slug: String(target?.slug || ""),
              error: "invalid_target",
            });
            continue;
          }
          try {
            const tx = await withTimeout(
              ctf.redeemPositions(
                collateralAddress,
                constants.HashZero,
                conditionId,
                indexSets,
                txOverrides
              ),
              rpcTimeoutMs,
              "claim_send"
            );
            const txItem = {
              conditionId,
              slug: String(target?.slug || ""),
              txHash: String(tx?.hash || ""),
              outcomes: target?.outcomes || [],
            };
            if (waitConfirm) {
              const receipt = await withTimeout(
                tx.wait(confirmBlocks),
                waitTimeoutMs,
                "claim_wait"
              );
              txItem.status = Number(receipt?.status || 0);
              txItem.blockNumber = Number(receipt?.blockNumber || 0);
              if (txItem.status <= 0) {
                failures.push({
                  conditionId,
                  slug: String(target?.slug || ""),
                  error: "tx_receipt_failed",
                });
                continue;
              }
            } else {
              pendingFiltered[conditionId] = {
                tx_hash: txItem.txHash,
                at_ms: Date.now(),
              };
            }
            txs.push(txItem);
          } catch (err) {
            failures.push({
              conditionId,
              slug: String(target?.slug || ""),
              error: parseClaimError(err),
            });
            delete pendingFiltered[conditionId];
          }
        }
        return { txs, failures };
      } finally {
        if (useDirectRpc) {
          applyProxySnapshot(snapshot);
        }
      }
    };
    const rpcPreferDirect = isTruthy(env("POLY_RPC_PREFER_DIRECT", "1"));
    const rpcFallbackEnabled = !isTruthy(env("POLY_RPC_DISABLE_MODE_FALLBACK", "0"));
    const firstModeDirect = rpcPreferDirect;
    let claimRun = await executeClaims(firstModeDirect);
    if (
      rpcFallbackEnabled
      && claimRun.txs.length === 0
      && claimRun.failures.length > 0
      && claimRun.failures.every((item) => isRpcTransportError(item?.error))
    ) {
      const fallbackRun = await executeClaims(!firstModeDirect);
      fallbackRun.failures = (fallbackRun.failures || []).map((item) => ({
        ...item,
        error: `fallback:${item.error}`,
      }));
      claimRun = fallbackRun;
    }
    const txs = Array.isArray(claimRun?.txs) ? claimRun.txs : [];
    const failures = Array.isArray(claimRun?.failures) ? claimRun.failures : [];
    writeClaimPending(pendingFiltered);
    console.log(
      JSON.stringify({
        ok: failures.length === 0,
        command: "claim",
        attempted: targets.length,
        claimed: txs.length,
        failed: failures.length,
        skipped: skippedPending,
        max_per_loop: maxPerLoop,
        txs,
        failures,
      })
    );
    return;
  }

  const userApiCreds = {
    key: requireEnv("POLY_API_KEY"),
    secret: requireEnv("POLY_API_SECRET"),
    passphrase: requireEnv("POLY_API_PASSPHRASE"),
  };

  const signatureType = Number(env("PM_SIGNATURE_TYPE", env("SIGNATURE_TYPE", "0")));
  const funderAddress = env("PM_FUNDER_ADDRESS", signer.address);

  const buildClient = () =>
    new ClobClient(
      host,
      chainId,
      signer,
      userApiCreds,
      signatureType,
      funderAddress
    );

  const OPEN_ORDERS_ENDPOINT = "/data/orders";
  const OPEN_ORDERS_INITIAL_CURSOR = "MA==";
  const OPEN_ORDERS_END_CURSOR = "LTE=";

  const parseOpenOrdersPage = (response) => {
    if (response && (response.error || isHttpErrorStatus(response.status))) {
      const errorText = response.error || response.statusText || `status_${response.status ?? "unknown"}`;
      throw new Error(String(errorText || "fetch_open_orders_failed"));
    }
    const openOrders = Array.isArray(response)
      ? response
      : Array.isArray(response?.data)
        ? response.data
        : Array.isArray(response?.orders)
          ? response.orders
          : Array.isArray(response?.open_orders)
            ? response.open_orders
            : [];
    const nextCursorRaw = response?.next_cursor ?? response?.nextCursor ?? OPEN_ORDERS_END_CURSOR;
    const nextCursor = nextCursorRaw ? String(nextCursorRaw) : OPEN_ORDERS_END_CURSOR;
    return { openOrders, nextCursor };
  };

  const fetchOpenOrdersSafe = async (client, onlyFirstPage = true) => {
    try {
      const sdkOrders = await client.getOpenOrders(undefined, onlyFirstPage);
      if (Array.isArray(sdkOrders)) {
        return { openOrders: sdkOrders, source: "sdk", page_count: 1 };
      }
    } catch (err) {
      const sdkErr = String(err?.message || err || "").trim();
      if (sdkErr) {
        console.error(`[CLOB Client] fetch-open-orders sdk fallback: ${sdkErr}`);
      }
    }

    const maxPagesRaw = Number(env("PM_CLOB_OPEN_ORDERS_MAX_PAGES", "3"));
    const maxPages = Number.isFinite(maxPagesRaw) && maxPagesRaw > 0 ? Math.floor(maxPagesRaw) : 3;
    let cursor = OPEN_ORDERS_INITIAL_CURSOR;
    let pageCount = 0;
    const collected = [];

    while (cursor !== OPEN_ORDERS_END_CURSOR && pageCount < maxPages) {
      const headers = await createL2Headers(
        signer,
        userApiCreds,
        {
          method: HTTP_GET,
          requestPath: OPEN_ORDERS_ENDPOINT,
        },
        undefined
      );
      const response = await client.get(`${host}${OPEN_ORDERS_ENDPOINT}`, {
        headers,
        params: { next_cursor: cursor },
      });
      const { openOrders, nextCursor } = parseOpenOrdersPage(response);
      if (openOrders.length) {
        collected.push(...openOrders);
      }
      pageCount += 1;
      if (onlyFirstPage || !nextCursor || nextCursor === OPEN_ORDERS_END_CURSOR || openOrders.length === 0) {
        break;
      }
      cursor = nextCursor;
    }

    return { openOrders: collected, source: "manual", page_count: pageCount };
  };

  if (command === "fetch-trades") {
    const payload = await runWithProxyFallback("fetch-trades", async () => {
      const client = buildClient();
      const maxPagesRaw = env("PM_CLOB_TRADES_MAX_PAGES", "10");
      const maxPages = Number(maxPagesRaw);
      const budgetRaw = env("PM_CLOB_TRADES_BUDGET_MS", "");
      const budgetMs = Number(budgetRaw);
      const hasBudget = Number.isFinite(budgetMs) && budgetMs > 0;
      const hardTimeoutRaw = env(
        "PM_CLOB_TRADES_HARD_TIMEOUT_MS",
        env("PM_CLOB_HARD_TIMEOUT_MS", "")
      );
      let hardTimeoutMs = Number(hardTimeoutRaw);
      if (!Number.isFinite(hardTimeoutMs) || hardTimeoutMs <= 0) {
        hardTimeoutMs = hasBudget ? budgetMs + 5000 : 0;
      }
      let hardTimer = null;
      if (Number.isFinite(hardTimeoutMs) && hardTimeoutMs > 0) {
        hardTimer = setTimeout(() => {
          console.error(`[CLOB Client] fetch-trades hard timeout after ${hardTimeoutMs}ms`);
          process.exit(2);
        }, hardTimeoutMs);
      }
      try {
        const startedAt = Date.now();
        const after = env("PM_CLOB_TRADES_AFTER", "");
        const before = env("PM_CLOB_TRADES_BEFORE", "");
        const makerAddress = env("PM_CLOB_TRADES_MAKER_ADDRESS", "");
        const marketSingle = env("PM_CLOB_TRADES_MARKET", "");
        const assetSingle = env("PM_CLOB_TRADES_ASSET_ID", "");
        const parseCsv = (raw) => String(raw || "")
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean);
        const requestedMarkets = parseCsv(env("PM_CLOB_TRADES_MARKETS", ""));
        const requestedAssetIds = parseCsv(env("PM_CLOB_TRADES_ASSET_IDS", ""));
        const baseParams = {};
        if (after) baseParams.after = after;
        if (before) baseParams.before = before;
        if (makerAddress) baseParams.maker_address = makerAddress;
        const requestParamsList = [];
        if (requestedAssetIds.length) {
          for (const assetId of requestedAssetIds) {
            requestParamsList.push({ ...baseParams, asset_id: assetId });
          }
        } else if (requestedMarkets.length) {
          for (const market of requestedMarkets) {
            requestParamsList.push({ ...baseParams, market });
          }
        } else {
          const params = { ...baseParams };
          if (marketSingle) params.market = marketSingle;
          if (assetSingle) params.asset_id = assetSingle;
          requestParamsList.push(params);
        }
        const seenTradeIds = new Set();
        let trades = [];
        let complete = true;
        let partialError = "";
        let budgetExhausted = false;
        let pageCount = 0;
        let nextCursor = undefined;
        for (let paramsIndex = 0; paramsIndex < requestParamsList.length; paramsIndex += 1) {
          const params = requestParamsList[paramsIndex] || {};
          let localComplete = true;
          let localNextCursor = undefined;
          if (Number.isFinite(maxPages) && maxPages > 0) {
            let cursor = undefined;
            localComplete = false;
            for (let page = 0; page < maxPages; page += 1) {
              if (hasBudget && Date.now() - startedAt >= budgetMs) {
                budgetExhausted = true;
                complete = false;
                break;
              }
              try {
                const pageRes = await client.getTradesPaginated(params, cursor);
                if (pageRes?.error) {
                  throw new Error(String(pageRes.error));
                }
                const pageTrades = Array.isArray(pageRes?.trades) ? pageRes.trades : [];
                for (const trade of pageTrades) {
                  const tradeId = String(
                    trade?.id || trade?.tradeID || trade?.tradeId || ""
                  ).trim();
                  const dedupeKey = tradeId || JSON.stringify(trade);
                  if (seenTradeIds.has(dedupeKey)) continue;
                  seenTradeIds.add(dedupeKey);
                  trades.push(trade);
                }
                pageCount += 1;
                localNextCursor = pageRes?.next_cursor;
                nextCursor = requestParamsList.length === 1 ? localNextCursor : null;
                if (!localNextCursor || localNextCursor === "LTE=" || pageTrades.length === 0) {
                  localComplete = true;
                  break;
                }
                cursor = localNextCursor;
              } catch (err) {
                const msg = err && err.message ? err.message : String(err || "fetch_trades_failed");
                if (trades.length) {
                  partialError = msg;
                  localComplete = false;
                  break;
                }
                throw err;
              }
            }
            if (!localComplete && pageCount < 1) {
              localComplete = true;
            }
          } else {
            const res = await client.getTrades(params);
            if (res?.error) {
              throw new Error(String(res.error));
            }
            const pageTrades = Array.isArray(res)
              ? res
              : Array.isArray(res?.trades)
                ? res.trades
                : Array.isArray(res?.data)
                  ? res.data
                  : [];
            for (const trade of pageTrades) {
              const tradeId = String(
                trade?.id || trade?.tradeID || trade?.tradeId || ""
              ).trim();
              const dedupeKey = tradeId || JSON.stringify(trade);
              if (seenTradeIds.has(dedupeKey)) continue;
              seenTradeIds.add(dedupeKey);
              trades.push(trade);
            }
            localComplete = true;
            pageCount += 1;
            localNextCursor = undefined;
            nextCursor = requestParamsList.length === 1 ? undefined : null;
          }
          if (budgetExhausted) break;
          if (!localComplete) {
            complete = false;
            if (partialError) break;
          }
        }
        if (budgetExhausted && !partialError) {
          partialError = "budget_exhausted";
        }
        return {
          ok: true,
          trades,
          complete,
          page_count: pageCount,
          next_cursor: nextCursor || null,
          partial_error: partialError || null,
          budget_exhausted: budgetExhausted,
        };
      } finally {
        if (hardTimer) clearTimeout(hardTimer);
      }
    }, { minAttempts: 2 });
    console.log(JSON.stringify(payload));
    return;
  }

  if (command === "fetch-open-orders") {
    const payload = await runWithProxyFallback("fetch-open-orders", async () => {
      const client = buildClient();
      const onlyFirstPage = isTruthy(env("PM_CLOB_OPEN_ORDERS_FIRST_PAGE", "1"));
      const loaded = await fetchOpenOrdersSafe(client, onlyFirstPage);
      return {
        ok: true,
        open_orders: loaded.openOrders,
        only_first_page: onlyFirstPage,
        source: loaded.source,
        page_count: loaded.page_count,
      };
    }, { minAttempts: 2 });
    console.log(JSON.stringify(payload));
    return;
  }

  if (command === "get-order") {
    const orderID = args["order-id"] || args.order || args.orderId || "";
    if (!orderID) {
      throw new Error("missing_args:order-id");
    }
    const hardTimeoutRaw = env(
      "PM_CLOB_GET_ORDER_HARD_TIMEOUT_MS",
      env("PM_CLOB_HARD_TIMEOUT_MS", "")
    );
    let hardTimeoutMs = Number(hardTimeoutRaw);
    if (!Number.isFinite(hardTimeoutMs) || hardTimeoutMs <= 0) {
      hardTimeoutMs = 0;
    }
    let hardTimer = null;
    if (hardTimeoutMs > 0) {
      hardTimer = setTimeout(() => {
        console.error(`[CLOB Client] get-order hard timeout after ${hardTimeoutMs}ms`);
        process.exit(2);
      }, hardTimeoutMs);
    }
    try {
      const payload = await runWithProxyFallback("get-order", async () => {
        const client = buildClient();
        const order = await client.getOrder(orderID);
        return { ok: true, order };
      }, { minAttempts: 2 });
      console.log(JSON.stringify(payload));
      return;
    } finally {
      if (hardTimer) clearTimeout(hardTimer);
    }
  }

  if (command === "balance") {
    const assetRaw = String(args["asset-type"] || "COLLATERAL").toUpperCase();
    const assetType = AssetType[assetRaw] || AssetType.COLLATERAL;
    const tokenID = args["token-id"] || args.token || undefined;
    const refreshRaw = args.refresh || args.update;
    const refresh = typeof refreshRaw === "string"
      ? ["1", "true", "yes"].includes(refreshRaw.toLowerCase())
      : Boolean(refreshRaw);

    const payload = await runWithProxyFallback("balance", async () => {
      const client = buildClient();
      if (refresh) {
        await client.updateBalanceAllowance({ asset_type: assetType, token_id: tokenID });
      }
      const res = await client.getBalanceAllowance({ asset_type: assetType, token_id: tokenID });
      return { ok: true, ...res };
    });
    console.log(JSON.stringify(payload));
    return;
  }

  if (command === "place-order") {
    const tokenID = args["token-id"] || args.token || "";
    const price = Number(args.price);
    const size = Number(args.size);
    const amount = Number(args.amount);
    const sideRaw = String(args.side || "BUY").toUpperCase();
    const side = sideRaw === "SELL" ? Side.SELL : Side.BUY;
    const orderTypeRaw = String(args["order-type"] || "GTC").toUpperCase();
    const allowMarket = isTruthy(env("PM_ALLOW_MARKET_ORDERS", "0"));
    const isMarket = ["FOK", "FAK"].includes(orderTypeRaw);
    if (!allowMarket && isMarket) {
      throw new Error(`limit_only_order_type:${orderTypeRaw}`);
    }
    const orderType = OrderType[orderTypeRaw] || OrderType.GTC;
    const tickSizeArg = args["tick-size"];
    const negRiskArg = args["neg-risk"];

    if (!tokenID || (!Number.isFinite(price) && !isMarket) || (!Number.isFinite(size) && !Number.isFinite(amount))) {
      throw new Error("missing_args:token-id/price/size");
    }

    const payload = await runWithProxyFallback("place-order", async () => {
      const client = buildClient();
      let tickSize = Number(tickSizeArg);
      let negRisk;
      if (typeof negRiskArg === "string") {
        negRisk = ["1", "true", "yes"].includes(negRiskArg.toLowerCase());
      }

      if (!Number.isFinite(tickSize) || typeof negRisk !== "boolean") {
        let book = null;
        try {
          book = await client.getOrderBook(tokenID);
        } catch (err) {
          book = null;
        }
        if (!Number.isFinite(tickSize)) {
          const fromBook = book?.tick_size ?? book?.tickSize ?? book?.minimum_tick_size;
          tickSize = Number(fromBook);
        }
        if (typeof negRisk !== "boolean") {
          const fromBook = book?.neg_risk ?? book?.negRisk;
          negRisk = typeof fromBook === "boolean" ? fromBook : defaultNegRisk();
        }
      }
      if (!Number.isFinite(tickSize) || tickSize <= 0) {
        tickSize = defaultTickSize();
      }
      if (typeof negRisk !== "boolean") {
        negRisk = defaultNegRisk();
      }

      let response;
      if (isMarket) {
        let finalAmount = Number.isFinite(amount) ? amount : NaN;
        if (side === Side.SELL) {
          if (Number.isFinite(size)) {
            finalAmount = size;
          }
        } else {
          if (!Number.isFinite(finalAmount) && Number.isFinite(size) && Number.isFinite(price)) {
            finalAmount = size * price;
          }
          if (!Number.isFinite(finalAmount) && Number.isFinite(size)) {
            finalAmount = size;
          }
        }
        if (!Number.isFinite(finalAmount) || finalAmount <= 0) {
          throw new Error("missing_args:amount");
        }
        const marketOrder = {
          tokenID,
          side,
          amount: finalAmount,
        };
        if (Number.isFinite(price)) {
          marketOrder.price = price;
        }
        response = await client.createAndPostMarketOrder(
          marketOrder,
          {
            tickSize,
            negRisk,
          },
          orderType
        );
      } else {
        response = await client.createAndPostOrder(
          {
            tokenID,
            price,
            size,
            side,
          },
          {
            tickSize,
            negRisk,
          },
          orderType
        );
      }

      return { ok: true, response };
    });

    const response = payload?.response;
    if (response && (response.error || isHttpErrorStatus(response.status))) {
      console.error(
        JSON.stringify({
          ok: false,
          error: response.error || "order_rejected",
          status: response.status ?? null,
          statusText: response.statusText ?? "",
        })
      );
      process.exit(1);
    }

    if (!response || !response.orderID) {
      console.error(
        JSON.stringify({
          ok: false,
          error: "missing_order_id",
          status: response?.status ?? null,
          statusText: response?.statusText ?? "",
        })
      );
      process.exit(1);
    }

    console.log(JSON.stringify({ ok: true, orderID: response.orderID, status: response.status }));
    return;
  }

  if (command === "cancel-orders") {
    const payload = await runWithProxyFallback("cancel-orders", async () => {
      const client = buildClient();
      const tokenID = args["token-id"] || args.token || "";
      const sideRaw = args.side ? String(args.side).toUpperCase() : "";
      const loaded = await fetchOpenOrdersSafe(client, false);
      const openOrders = Array.isArray(loaded.openOrders) ? loaded.openOrders : [];
      const filtered = openOrders.filter((order) => {
        const orderToken = order.tokenID || order.tokenId || order.asset_id || order.assetId || "";
        if (tokenID && String(orderToken) !== String(tokenID)) return false;
        if (sideRaw && String(order.side || "").toUpperCase() !== sideRaw) return false;
        return true;
      });
      const cancelled = [];
      for (const order of filtered) {
        const orderID = order.orderID || order.orderId || order.id || order.order_id || "";
        if (!orderID) continue;
        const cancelRes = await client.cancelOrder({ orderID });
        if (cancelRes && (cancelRes.error || isHttpErrorStatus(cancelRes.status))) {
          throw new Error(String(cancelRes.error || cancelRes.statusText || cancelRes.status || "cancel_failed"));
        }
        cancelled.push(orderID);
      }
      return {
        ok: true,
        count: cancelled.length,
        cancelled,
        source: loaded.source,
      };
    }, { minAttempts: 2 });
    console.log(JSON.stringify(payload));
    return;
  }
}

main().catch((err) => {
  const message = err && err.message ? err.message : String(err || "error");
  console.error(JSON.stringify({ ok: false, error: message }));
  process.exit(1);
});
