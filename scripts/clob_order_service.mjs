import http from "http";
import fs from "fs";
import path from "path";
import crypto from "crypto";
import { fileURLToPath } from "url";
import { execFile, execFileSync } from "child_process";
import { Agent as HttpAgent } from "http";
import { Agent as HttpsAgent } from "https";
import axios from "axios";
import { bootstrap } from "global-agent";
import { ClobClient, Side, OrderType } from "@polymarket/clob-client-v2";
import { Wallet } from "ethers";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DATA_DIR = process.env.PM_DATA_DIR || path.join(ROOT, "data");
const DB_PATH = process.env.PM_ARCHIVE_DB || path.join(DATA_DIR, "archive.db");
const SERVICE_NAME = "clob_order_service";

function env(name, fallback = "") {
  const value = process.env[name];
  return value === undefined || value === null || value === "" ? fallback : String(value);
}

function truthy(value, fallback = false) {
  if (value === undefined || value === null || value === "") return Boolean(fallback);
  const text = String(value).trim().toLowerCase();
  if (["1", "true", "yes", "on", "y"].includes(text)) return true;
  if (["0", "false", "no", "off", "n"].includes(text)) return false;
  return Boolean(fallback);
}

function positiveNumber(value, fallback = 0) {
  const num = Number(value);
  return Number.isFinite(num) && num > 0 ? num : fallback;
}

function requireEnv(name) {
  const value = env(name);
  if (!value) throw new Error(`missing_env:${name}`);
  return value;
}

function parseProxyHostPort(proxyUrl) {
  if (!proxyUrl) return { host: "", port: 0 };
  let value = String(proxyUrl);
  if (!value.includes("://")) value = `http://${value}`;
  try {
    const parsed = new URL(value);
    return { host: parsed.hostname || "", port: Number(parsed.port || 0) };
  } catch {
    return { host: "", port: 0 };
  }
}

function proxyReachable(host, port, timeoutMs = 400) {
  return new Promise((resolve) => {
    if (!host || !port) return resolve(false);
    import("net").then((netMod) => {
      const net = netMod.default || netMod;
      const socket = net.createConnection({ host, port });
      let done = false;
      const finish = (ok) => {
        if (done) return;
        done = true;
        try { socket.destroy(); } catch {}
        resolve(ok);
      };
      socket.setTimeout(timeoutMs);
      socket.on("connect", () => finish(true));
      socket.on("timeout", () => finish(false));
      socket.on("error", () => finish(false));
    }).catch(() => resolve(false));
  });
}

function parsePortList(raw) {
  return String(raw || "")
    .split(/[,\s]+/)
    .map((item) => Number(item.trim()))
    .filter((port, idx, arr) => Number.isFinite(port) && port > 0 && port <= 65535 && arr.indexOf(port) === idx);
}

function scutilProxyPorts() {
  if (process.platform !== "darwin") return [];
  try {
    const out = execFileSync("scutil", ["--proxy"], { encoding: "utf8", timeout: 800 });
    const ports = [];
    for (const key of ["HTTPPort", "HTTPSPort", "SOCKSPort"]) {
      const m = out.match(new RegExp(`^\\s*${key}\\s*:\\s*(\\d+)`, "m"));
      if (m) ports.push(Number(m[1]));
    }
    return ports.filter((port, idx, arr) => port > 0 && port <= 65535 && arr.indexOf(port) === idx);
  } catch {
    return [];
  }
}

async function configureProxy() {
  const forceProxy = truthy(env("PM_CLOB_FORCE_PROXY", "0"));
  if (truthy(env("PM_CLOB_FORCE_DIRECT", "0")) && !forceProxy) {
    for (const key of ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "GLOBAL_AGENT_HTTP_PROXY"]) {
      delete process.env[key];
    }
    return { mode: "direct" };
  }
  let proxy = env("HTTPS_PROXY", env("HTTP_PROXY", env("ALL_PROXY", "")));
  if (proxy) {
    const { host, port } = parseProxyHostPort(proxy);
    if (["127.0.0.1", "localhost"].includes(host) && port) {
      if (!(await proxyReachable(host, port))) {
        proxy = "";
      }
    }
  }
  if (!proxy) {
    const ports = [
      ...parsePortList(env("PM_PROXY_PORTS", "")),
      ...scutilProxyPorts(),
      ...parsePortList(env("PM_PROXY_LOCAL_FALLBACK_PORTS", "7890,7897")),
    ].filter((port, idx, arr) => arr.indexOf(port) === idx);
    for (const port of ports) {
      if (await proxyReachable("127.0.0.1", port)) {
        proxy = `http://127.0.0.1:${port}`;
        break;
      }
    }
  }
  if (proxy) {
    process.env.HTTP_PROXY = proxy;
    process.env.HTTPS_PROXY = proxy;
    process.env.GLOBAL_AGENT_HTTP_PROXY = proxy;
    bootstrap();
    return { mode: "proxy", proxy };
  }
  if (forceProxy) {
    return { mode: "proxy_required_missing", forced_proxy: true, error: "proxy_unavailable" };
  }
  return { mode: "direct_no_proxy" };
}

function redactErrorText(text) {
  let out = String(text || "");
  for (const key of ["POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE", "PRIVATE_KEY"]) {
    out = out.replace(new RegExp(`(${key}=)([^\\s&]+)`, "g"), "$1***");
  }
  for (const key of [
    "POLY_API_KEY",
    "POLY_API_SECRET",
    "POLY_PASSPHRASE",
    "POLY_API_PASSPHRASE",
    "POLY_SIGNATURE",
    "signature",
  ]) {
    out = out.replace(new RegExp(`("${key}"\\s*:\\s*")([^"]+)(")`, "gi"), "$1***$3");
  }
  out = out.replace(/(signature=)([^&\s]+)/gi, "$1***");
  return out;
}

const originalConsoleError = console.error.bind(console);
console.error = (...args) => {
  originalConsoleError(
    ...args.map((arg) => {
      if (typeof arg === "string") return redactErrorText(arg);
      try {
        return redactErrorText(JSON.stringify(arg));
      } catch {
        return redactErrorText(String(arg || ""));
      }
    })
  );
};

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

function readJsonBody(req, limitBytes = 65536) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limitBytes) {
        reject(new Error("request_body_too_large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      try {
        const text = Buffer.concat(chunks).toString("utf8").trim();
        resolve(text ? JSON.parse(text) : {});
      } catch (err) {
        reject(new Error(`invalid_json:${err.message || err}`));
      }
    });
    req.on("error", reject);
  });
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function stableKey(payload) {
  const copy = { ...(payload || {}) };
  delete copy.idempotency_key;
  delete copy.request_id;
  return crypto.createHash("sha256").update(canonicalJson(copy)).digest("hex");
}

function isHttpErrorStatus(status) {
  const num = Number(status);
  return Number.isFinite(num) && num >= 400;
}

class ClobOrderService {
  constructor() {
    this.startedAt = new Date();
    this.proxy = {};
    this.client = null;
    this.metadata = new Map();
    this.prewarmTokenHint = "";
    this.inflight = new Map();
    this.idem = new Map();
    this.submitChain = Promise.resolve();
    this.idemPath = path.join(DATA_DIR, "clob_order_service_idempotency.json");
    this.idemTtlMs = positiveNumber(env("PM_CLOB_ORDER_SERVICE_IDEMPOTENCY_TTL_SEC", "7200"), 7200) * 1000;
    this.maxIdem = Math.max(32, Math.floor(positiveNumber(env("PM_CLOB_ORDER_SERVICE_IDEMPOTENCY_MAX", "512"), 512)));
    this.lastHeartbeat = null;
    this.lastHeartbeatError = "";
    this.metadataPrewarmInFlight = false;
    this.lastPrewarmAt = null;
    this.lastPrewarmMs = null;
    this.lastPrewarmError = "";
    this.lastSignerPrewarmAt = null;
    this.lastSignerPrewarmMs = null;
    this.lastSignerPrewarmError = "";
    this.signerPrewarmed = false;
    this.signerPrewarmInFlight = false;
    this.startPromise = null;
    this.readyAt = null;
    this.startError = "";
  }

  async start() {
    if (this.startPromise) return this.startPromise;
    this.startPromise = this._start();
    return this.startPromise;
  }

  async _start() {
    this.startError = "";
    fs.mkdirSync(DATA_DIR, { recursive: true });
    this.loadIdempotency();
    this.proxy = await configureProxy();
    const keepAliveSockets = Math.max(2, Math.floor(positiveNumber(env("PM_CLOB_ORDER_SERVICE_MAX_SOCKETS", "8"), 8)));
    axios.defaults.httpAgent = new HttpAgent({ keepAlive: true, maxSockets: keepAliveSockets });
    axios.defaults.httpsAgent = new HttpsAgent({ keepAlive: true, maxSockets: keepAliveSockets });
    axios.defaults.timeout = Math.round(positiveNumber(env("PM_CLOB_TIMEOUT_MS", "5000"), 5000));
    this.client = this.buildClient();
    this.prewarmMetadataFromDb();
    this.prewarmClobHttp("start").catch(() => {});
    setTimeout(() => this.prewarmClobHttp("start_delayed").catch(() => {}), 1000).unref?.();
    setTimeout(() => this.prewarmOrderSigner("start_delayed").catch(() => {}), 1500).unref?.();
    this.startHeartbeat();
    this.readyAt = new Date();
  }

  buildClient() {
    const host = env("PM_CLOB_HOST", "https://clob.polymarket.com");
    const chainId = Number(env("PM_CHAIN_ID", "137"));
    const signer = new Wallet(requireEnv("PRIVATE_KEY"));
    const userApiCreds = {
      key: requireEnv("POLY_API_KEY"),
      secret: requireEnv("POLY_API_SECRET"),
      passphrase: requireEnv("POLY_API_PASSPHRASE"),
    };
    const builderCode = env("POLY_BUILDER_CODE", env("PM_BUILDER_CODE", "")).trim();
    return new ClobClient({
      host,
      chain: chainId,
      signer,
      creds: userApiCreds,
      signatureType: Number(env("PM_SIGNATURE_TYPE", env("SIGNATURE_TYPE", "0"))),
      funderAddress: env("PM_FUNDER_ADDRESS", signer.address),
      builderConfig: builderCode ? { builderCode } : undefined,
      retryOnError: truthy(env("PM_CLOB_PLACE_ORDER_SDK_RETRY_ON_ERROR", "0")),
    });
  }

  loadIdempotency() {
    try {
      const raw = JSON.parse(fs.readFileSync(this.idemPath, "utf8"));
      const now = Date.now();
      for (const [key, item] of Object.entries(raw || {})) {
        const createdMs = Number(item?.created_ms || 0);
        if (createdMs > 0 && now - createdMs <= this.idemTtlMs) {
          this.idem.set(key, item);
        }
      }
    } catch {}
  }

  saveIdempotency() {
    try {
      const now = Date.now();
      for (const [key, item] of this.idem.entries()) {
        const createdMs = Number(item?.created_ms || 0);
        if (!createdMs || now - createdMs > this.idemTtlMs) this.idem.delete(key);
      }
      while (this.idem.size > this.maxIdem) {
        const first = this.idem.keys().next().value;
        if (!first) break;
        this.idem.delete(first);
      }
      fs.writeFileSync(this.idemPath, JSON.stringify(Object.fromEntries(this.idem.entries())));
    } catch {}
  }

  startHeartbeat() {
    const sec = positiveNumber(env("PM_CLOB_ORDER_SERVICE_HEARTBEAT_SEC", "45"), 45);
    if (!Number.isFinite(sec) || sec <= 0) return;
    const checkOpenOrders = truthy(env("PM_CLOB_ORDER_SERVICE_HEARTBEAT_OPEN_ORDERS", "0"), false);
    const run = async () => {
      try {
        this.prewarmMetadataFromDb();
        await this.prewarmClobHttp("heartbeat");
        if (checkOpenOrders) {
          await this.client.getOpenOrders(undefined, true);
        }
        this.lastHeartbeat = new Date().toISOString();
        this.lastHeartbeatError = "";
      } catch (err) {
        this.lastHeartbeat = new Date().toISOString();
        this.lastHeartbeatError = redactErrorText(err?.message || String(err || ""));
      }
    };
    setTimeout(run, 500).unref?.();
    setInterval(run, Math.round(sec * 1000)).unref?.();
  }

  applyMetadataRows(rows) {
    const tickSize = positiveNumber(env("PM_CLOB_DEFAULT_TICK_SIZE", "0.01"), 0.01);
    const negRisk = truthy(env("PM_CLOB_DEFAULT_NEG_RISK", "1"), true);
    let count = 0;
    for (const row of rows || []) {
      const tokenID = String(row?.token_id || "").trim();
      if (!tokenID) continue;
      const previous = this.metadata.get(tokenID) || {};
      this.metadata.set(tokenID, {
        tickSize: Number(previous.tickSize) > 0 ? Number(previous.tickSize) : tickSize,
        negRisk: typeof previous.negRisk === "boolean" ? previous.negRisk : negRisk,
        updated_at: new Date().toISOString(),
        source: previous.source || "market_tokens_prewarm",
      });
      if (!this.prewarmTokenHint) this.prewarmTokenHint = tokenID;
      count += 1;
    }
    return count;
  }

  prewarmToken() {
    const configured = String(env("PM_CLOB_ORDER_SERVICE_PREWARM_TOKEN_ID", "") || "").trim();
    if (configured) return configured;
    const hint = String(this.prewarmTokenHint || "").trim();
    if (hint) return hint;
    const cached = String(this.metadata.keys().next().value || "").trim();
    if (cached) return cached;
    if (truthy(env("PM_CLOB_ORDER_SERVICE_ALLOW_SYNC_PREWARM_DB", "0"), false)) {
      return this.latestPrewarmTokenFromDb();
    }
    return "";
  }

  latestPrewarmTokenFromDb() {
    if (!fs.existsSync(DB_PATH)) return "";
    const sql = `
      WITH token_rows AS (
        SELECT yes_token AS token_id, updated_at FROM market_tokens
        WHERE COALESCE(yes_token, '') <> ''
          AND COALESCE(market_closed, 0) = 0
          AND COALESCE(market_archived, 0) = 0
          AND COALESCE(market_active, 1) != 0
        UNION ALL
        SELECT no_token AS token_id, updated_at FROM market_tokens
        WHERE COALESCE(no_token, '') <> ''
          AND COALESCE(market_closed, 0) = 0
          AND COALESCE(market_archived, 0) = 0
          AND COALESCE(market_active, 1) != 0
      )
      SELECT token_id
      FROM token_rows
      ORDER BY updated_at DESC
      LIMIT 1;
    `;
    try {
      return String(execFileSync("/usr/bin/sqlite3", ["-noheader", DB_PATH, sql], {
        encoding: "utf8",
        timeout: 800,
      }) || "").trim().split(/\s+/)[0] || "";
    } catch {
      return "";
    }
  }

  prewarmMetadataFromDb() {
    if (!truthy(env("PM_CLOB_ORDER_SERVICE_PREWARM_METADATA", "1"), true)) return 0;
    if (!fs.existsSync(DB_PATH)) return 0;
    if (this.metadataPrewarmInFlight) return 0;
    this.metadataPrewarmInFlight = true;
    const limit = Math.max(8, Math.min(Math.floor(positiveNumber(env("PM_CLOB_ORDER_SERVICE_PREWARM_TOKEN_LIMIT", "256"), 256)), 1000));
    const sql = `
      WITH token_rows AS (
        SELECT yes_token AS token_id, updated_at FROM market_tokens
        WHERE COALESCE(yes_token, '') <> ''
          AND COALESCE(market_closed, 0) = 0
          AND COALESCE(market_archived, 0) = 0
          AND COALESCE(market_active, 1) != 0
        UNION ALL
        SELECT no_token AS token_id, updated_at FROM market_tokens
        WHERE COALESCE(no_token, '') <> ''
          AND COALESCE(market_closed, 0) = 0
          AND COALESCE(market_archived, 0) = 0
          AND COALESCE(market_active, 1) != 0
      )
      SELECT token_id, MAX(updated_at) AS updated_at
      FROM token_rows
      GROUP BY token_id
      ORDER BY MAX(updated_at) DESC
      LIMIT ${limit};
    `;
    execFile("/usr/bin/sqlite3", ["-json", DB_PATH, sql], {
      encoding: "utf8",
      timeout: 1500,
    }, (err, stdout) => {
      this.metadataPrewarmInFlight = false;
      if (err) {
        this.lastHeartbeatError = redactErrorText(err?.message || String(err || ""));
        return;
      }
      try {
        const count = this.applyMetadataRows(JSON.parse(String(stdout || "[]")));
        if (count > 0 && !this.signerPrewarmed) {
          setTimeout(() => this.prewarmOrderSigner("metadata_ready").catch(() => {}), 0).unref?.();
        }
      } catch (parseErr) {
        this.lastHeartbeatError = redactErrorText(parseErr?.message || String(parseErr || ""));
      }
    });
    return 0;
  }

  health() {
    return {
      ok: true,
      service: SERVICE_NAME,
      ready: Boolean(this.client) && (!truthy(env("PM_CLOB_FORCE_PROXY", "0")) || this.proxy?.mode === "proxy"),
      pid: process.pid,
      started_at: this.startedAt.toISOString(),
      ready_at: this.readyAt ? this.readyAt.toISOString() : null,
      uptime_sec: Math.round(process.uptime() * 1000) / 1000,
      proxy: this.proxy,
      metadata_cache_size: this.metadata.size,
      idempotency_cache_size: this.idem.size,
      inflight_count: this.inflight.size,
      last_prewarm_at: this.lastPrewarmAt,
      last_prewarm_ms: this.lastPrewarmMs,
      last_prewarm_error: this.lastPrewarmError,
      last_signer_prewarm_at: this.lastSignerPrewarmAt,
      last_signer_prewarm_ms: this.lastSignerPrewarmMs,
      last_signer_prewarm_error: this.lastSignerPrewarmError,
      last_heartbeat_at: this.lastHeartbeat,
      last_heartbeat_error: this.lastHeartbeatError,
      start_error: this.startError,
    };
  }

  idempotencyStatus(key) {
    const normalized = String(key || "").trim();
    if (!normalized) {
      return { ok: false, found: false, pending: false, error: "missing_idempotency_key" };
    }
    if (this.idem.has(normalized)) {
      const cached = this.idem.get(normalized);
      const cachedResponse = cached?.response || {};
      return {
        ...cachedResponse,
        ok: typeof cachedResponse.ok === "boolean" ? cachedResponse.ok : true,
        found: true,
        pending: false,
        idempotency_key: normalized,
        idempotent_replay: true,
        source: SERVICE_NAME,
      };
    }
    if (this.inflight.has(normalized)) {
      const active = this.inflight.get(normalized) || {};
      return {
        ok: false,
        found: false,
        pending: true,
        idempotency_key: normalized,
        accepted: true,
        accepted_at: active.accepted_at || null,
        order_lane: active.lane || null,
        inflight_age_ms: active.started_ms ? Date.now() - Number(active.started_ms) : null,
        source: SERVICE_NAME,
      };
    }
    return {
      ok: false,
      found: false,
      pending: false,
      idempotency_key: normalized,
      source: SERVICE_NAME,
    };
  }

  async prewarmClobHttp(reason = "") {
    if (!truthy(env("PM_CLOB_ORDER_SERVICE_PREWARM_HTTP", "1"), true)) return false;
    const host = env("PM_CLOB_HOST", "https://clob.polymarket.com").replace(/\/+$/, "");
    if (!host) return false;
    const timeoutMs = Math.max(250, Math.floor(positiveNumber(env("PM_CLOB_ORDER_SERVICE_PREWARM_HTTP_TIMEOUT_MS", "1200"), 1200)));
    const tokenID = this.prewarmToken();
    const url = tokenID ? `${host}/tick-size` : host;
    const started = Date.now();
    try {
      await axios.get(url, {
        timeout: timeoutMs,
        validateStatus: () => true,
        headers: { "User-Agent": `${SERVICE_NAME}/prewarm`, "Cache-Control": "no-cache" },
        ...(tokenID ? { params: { token_id: tokenID } } : {}),
      });
      this.lastPrewarmAt = new Date().toISOString();
      this.lastPrewarmMs = Date.now() - started;
      this.lastPrewarmError = "";
      return true;
    } catch (err) {
      this.lastPrewarmAt = new Date().toISOString();
      this.lastPrewarmMs = Date.now() - started;
      this.lastPrewarmError = `${reason || "prewarm"}:${redactErrorText(err?.message || String(err || ""))}`;
      return false;
    }
  }

  async prewarmOrderSigner(reason = "") {
    if (!truthy(env("PM_CLOB_ORDER_SERVICE_PREWARM_SIGNER", "1"), true)) return false;
    if (this.signerPrewarmInFlight) return false;
    if (this.signerPrewarmed && !truthy(env("PM_CLOB_ORDER_SERVICE_PREWARM_SIGNER_REPEAT", "0"), false)) return true;
    const tokenID = this.prewarmToken();
    if (!this.client || !tokenID) return false;
    this.signerPrewarmInFlight = true;
    const started = Date.now();
    try {
      const cachedMeta = this.metadata.get(tokenID) || {};
      let tickSize = Number(cachedMeta.tickSize);
      if (!Number.isFinite(tickSize) || tickSize <= 0) tickSize = positiveNumber(env("PM_CLOB_DEFAULT_TICK_SIZE", "0.01"), 0.01);
      let negRisk = typeof cachedMeta.negRisk === "boolean" ? cachedMeta.negRisk : truthy(env("PM_CLOB_DEFAULT_NEG_RISK", "1"), true);
      await this.client.createOrder(
        {
          tokenID,
          price: positiveNumber(env("PM_CLOB_ORDER_SERVICE_PREWARM_SIGNER_PRICE", String(tickSize)), tickSize),
          size: positiveNumber(env("PM_CLOB_ORDER_SERVICE_PREWARM_SIGNER_SIZE", "1"), 1),
          side: Side.BUY,
        },
        { tickSize, negRisk }
      );
      this.signerPrewarmed = true;
      this.lastSignerPrewarmAt = new Date().toISOString();
      this.lastSignerPrewarmMs = Date.now() - started;
      this.lastSignerPrewarmError = "";
      return true;
    } catch (err) {
      this.lastSignerPrewarmAt = new Date().toISOString();
      this.lastSignerPrewarmMs = Date.now() - started;
      this.lastSignerPrewarmError = `${reason || "signer_prewarm"}:${redactErrorText(err?.message || String(err || ""))}`;
      return false;
    } finally {
      this.signerPrewarmInFlight = false;
    }
  }

  isPriorityDirectEntry(payload) {
    if (!truthy(env("PM_CLOB_ORDER_SERVICE_PRIORITY_DIRECT_ENTRY", "1"), true)) return false;
    if (!payload || !payload.direct_target_order_context) return false;
    if (payload.direct_target_exit_order) return false;
    const side = String(payload.side || "BUY").toUpperCase();
    return side !== "SELL";
  }

  async placeOrderWithIdempotency(payload) {
    const key = String(payload.idempotency_key || stableKey(payload));
    const asyncAck = Boolean(payload.async_ack) || truthy(env("PM_CLOB_ORDER_SERVICE_ASYNC_ACK", "0"));
    if (this.idem.has(key)) {
      const cached = this.idem.get(key);
      return { ...cached.response, idempotency_key: key, idempotent_replay: true };
    }
    if (this.inflight.has(key)) {
      const active = this.inflight.get(key);
      if (asyncAck) {
        return {
          ok: true,
          accepted: true,
          pending: true,
          idempotency_key: key,
          idempotent_replay: true,
          idempotent_inflight: true,
          accepted_at: active?.accepted_at || new Date().toISOString(),
          order_lane: active?.lane || null,
          source: SERVICE_NAME,
          service_pid: process.pid,
        };
      }
      const response = await active.promise;
      return { ...response, idempotency_key: key, idempotent_replay: true, idempotent_inflight: true };
    }
    const acceptedAt = new Date().toISOString();
    const acceptedMs = Date.now();
    const priorityEntry = this.isPriorityDirectEntry(payload);
    const lane = priorityEntry ? "direct_target_primary_entry_priority" : "default_serial";
    const base = priorityEntry ? Promise.resolve() : this.submitChain;
    const promise = base
      .then(() => this.placeOrder(payload, key, { acceptedMs, lane }))
      .then((response) => {
        this.idem.set(key, { created_ms: Date.now(), response });
        this.saveIdempotency();
        return response;
      })
      .catch((err) => {
        const meta = err?.serviceMeta || {};
        const response = {
          ok: false,
          error: redactErrorText(err?.message || String(err || "place_order_failed")),
          source: SERVICE_NAME,
          service_pid: process.pid,
          idempotency_key: key,
          order_lane: lane,
          queue_wait_ms: Number.isFinite(Number(meta.queue_wait_ms)) ? Number(meta.queue_wait_ms) : null,
          service_submit_ms: Number.isFinite(Number(meta.service_submit_ms)) ? Number(meta.service_submit_ms) : null,
          service_total_ms: Number.isFinite(Number(meta.service_total_ms)) ? Number(meta.service_total_ms) : Math.max(0, Date.now() - acceptedMs),
          submit_call_ms: Number.isFinite(Number(meta.submit_call_ms)) ? Number(meta.submit_call_ms) : null,
          clob_timeout_ms: Number.isFinite(Number(meta.clob_timeout_ms)) ? Number(meta.clob_timeout_ms) : Number(axios.defaults.timeout || 0) || null,
        };
        this.idem.set(key, { created_ms: Date.now(), response });
        this.saveIdempotency();
        return response;
      })
      .finally(() => {
        this.inflight.delete(key);
      });
    if (!priorityEntry) {
      this.submitChain = promise.catch(() => {});
    }
    this.inflight.set(key, { promise, started_ms: acceptedMs, accepted_at: acceptedAt, lane });
    if (asyncAck) {
      return {
        ok: true,
        accepted: true,
        pending: true,
        idempotency_key: key,
        accepted_at: acceptedAt,
        order_lane: lane,
        source: SERVICE_NAME,
        service_pid: process.pid,
      };
    }
    const response = await promise;
    return { ...response, idempotency_key: key };
  }

  async placeOrder(rawPayload, idempotencyKey, submitMeta = {}) {
    const started = Date.now();
    const acceptedMs = Number(submitMeta.acceptedMs || started);
    const lane = String(submitMeta.lane || "default_serial");
    const queueWaitMs = Math.max(0, started - acceptedMs);
    const metaNow = (extra = {}) => ({
      order_lane: lane,
      queue_wait_ms: queueWaitMs,
      service_submit_ms: Math.max(0, Date.now() - started),
      service_total_ms: Math.max(0, Date.now() - acceptedMs),
      clob_timeout_ms: Number(axios.defaults.timeout || 0) || null,
      ...extra,
    });
    const errorWithMeta = (message, extra = {}) => {
      const err = new Error(message);
      err.serviceMeta = metaNow(extra);
      return err;
    };
    const tokenID = String(rawPayload.token_id || rawPayload.tokenID || rawPayload.token || "").trim();
    const price = Number(rawPayload.price);
    const size = Number(rawPayload.size);
    const amount = Number(rawPayload.amount);
    const sideRaw = String(rawPayload.side || "BUY").toUpperCase();
    const side = sideRaw === "SELL" ? Side.SELL : Side.BUY;
    const orderTypeRaw = String(rawPayload.order_type || rawPayload.orderType || "GTC").toUpperCase();
    const orderType = OrderType[orderTypeRaw] || OrderType.GTC;
    const isMarket = ["FOK", "FAK"].includes(orderTypeRaw);
    const allowMarket = Boolean(rawPayload.allow_market) || truthy(env("PM_ALLOW_MARKET_ORDERS", "0"));
    const deferExec = Boolean(rawPayload.defer_exec) || truthy(env("PM_CLOB_DEFER_EXEC", "0"));
    if (!tokenID || (!Number.isFinite(price) && !isMarket) || (!Number.isFinite(size) && !Number.isFinite(amount))) {
      throw errorWithMeta("missing_args:token-id/price/size");
    }
    if (!this.client) {
      throw errorWithMeta(this.startError ? `service_not_ready:${this.startError}` : "service_not_ready");
    }
    if (truthy(env("PM_CLOB_FORCE_PROXY", "0")) && this.proxy?.mode !== "proxy") {
      throw errorWithMeta(`proxy_required_unavailable:${this.proxy?.mode || "unknown"}`);
    }
    if (isMarket && !allowMarket) {
      throw errorWithMeta(`limit_only_order_type:${orderTypeRaw}`);
    }

    let tickSize = Number(rawPayload.tick_size ?? rawPayload.tickSize);
    let negRisk = typeof rawPayload.neg_risk === "boolean" ? rawPayload.neg_risk : rawPayload.negRisk;
    const cachedMeta = this.metadata.get(tokenID) || {};
    if (!Number.isFinite(tickSize) || tickSize <= 0) tickSize = Number(cachedMeta.tickSize);
    if (typeof negRisk !== "boolean") negRisk = cachedMeta.negRisk;
    if (!Number.isFinite(tickSize) || tickSize <= 0) tickSize = positiveNumber(env("PM_CLOB_DEFAULT_TICK_SIZE", "0.01"), 0.01);
    if (typeof negRisk !== "boolean") negRisk = truthy(env("PM_CLOB_DEFAULT_NEG_RISK", "1"), true);
    this.metadata.set(tokenID, { tickSize, negRisk, updated_at: new Date().toISOString() });

    const builderCode = env("POLY_BUILDER_CODE", env("PM_BUILDER_CODE", "")).trim();
    const sellMarketableLimit = isMarket
      && side === Side.SELL
      && Number.isFinite(price)
      && Number.isFinite(size)
      && truthy(env("PM_CLOB_SELL_MARKETABLE_LIMIT_ORDER", "1"), true);

    let response;
    if (isMarket && !sellMarketableLimit) {
      let finalAmount = Number.isFinite(amount) ? amount : NaN;
      if (side === Side.SELL && Number.isFinite(size)) finalAmount = size;
      if (side !== Side.SELL && !Number.isFinite(finalAmount) && Number.isFinite(size) && Number.isFinite(price)) {
        finalAmount = size * price;
      }
      if (!Number.isFinite(finalAmount) && Number.isFinite(size)) finalAmount = size;
      if (!Number.isFinite(finalAmount) || finalAmount <= 0) throw errorWithMeta("missing_args:amount");
      const submitStarted = Date.now();
      try {
        response = await this.client.createAndPostMarketOrder(
        {
          tokenID,
          side,
          amount: finalAmount,
          ...(Number.isFinite(price) ? { price } : {}),
          ...(builderCode ? { builderCode } : {}),
        },
        { tickSize, negRisk },
        orderType,
        deferExec
        );
      } catch (err) {
        err.serviceMeta = metaNow({ submit_call_ms: Date.now() - submitStarted });
        throw err;
      }
    } else {
      const submitStarted = Date.now();
      try {
        response = await this.client.createAndPostOrder(
        {
          tokenID,
          price,
          size,
          side,
          ...(builderCode ? { builderCode } : {}),
        },
        { tickSize, negRisk },
        orderType,
        false,
        deferExec
        );
      } catch (err) {
        err.serviceMeta = metaNow({ submit_call_ms: Date.now() - submitStarted });
        throw err;
      }
    }

    if (response && (response.error || isHttpErrorStatus(response.status))) {
      throw errorWithMeta(response.error || response.statusText || `order_rejected:${response.status}`);
    }
    if (!response || !response.orderID) {
      throw errorWithMeta(`missing_order_id:${JSON.stringify(response || {})}`);
    }
    return {
      ok: true,
      orderID: response.orderID,
      status: response.status,
      source: SERVICE_NAME,
      service_pid: process.pid,
      idempotency_key: idempotencyKey,
      clob_timeout_ms: Number(axios.defaults.timeout || 0) || null,
      order_lane: lane,
      queue_wait_ms: queueWaitMs,
      service_submit_ms: Date.now() - started,
      service_total_ms: Math.max(0, Date.now() - acceptedMs),
      tick_size: tickSize,
      neg_risk: negRisk,
    };
  }
}

const service = new ClobOrderService();

async function handle(req, res) {
  try {
    const parsedUrl = new URL(req.url || "/", "http://127.0.0.1");
    if (req.method === "GET" && parsedUrl.pathname === "/health") {
      sendJson(res, 200, service.health());
      return;
    }
    if (req.method === "GET" && parsedUrl.pathname === "/order-status") {
      const key = parsedUrl.searchParams.get("idempotency_key") || parsedUrl.searchParams.get("key") || "";
      sendJson(res, 200, service.idempotencyStatus(key));
      return;
    }
    if (req.method === "POST" && parsedUrl.pathname === "/order-status") {
      const payload = await readJsonBody(req);
      const key = String(payload.idempotency_key || payload.key || "");
      sendJson(res, 200, service.idempotencyStatus(key));
      return;
    }
    if (req.method === "POST" && parsedUrl.pathname === "/place-order") {
      const payload = await readJsonBody(req);
      const response = await service.placeOrderWithIdempotency(payload);
      sendJson(res, 200, response);
      return;
    }
    sendJson(res, 404, { ok: false, error: "not_found" });
  } catch (err) {
    sendJson(res, 500, {
      ok: false,
      error: redactErrorText(err?.message || String(err || "service_error")),
      source: SERVICE_NAME,
    });
  }
}

async function main() {
  const host = env("PM_CLOB_ORDER_SERVICE_HOST", "127.0.0.1");
  const port = Math.floor(positiveNumber(env("PM_CLOB_ORDER_SERVICE_PORT", "8765"), 8765));
  const server = http.createServer((req, res) => {
    handle(req, res);
  });
  server.keepAliveTimeout = Math.round(positiveNumber(env("PM_CLOB_ORDER_SERVICE_KEEPALIVE_TIMEOUT_MS", "65000"), 65000));
  server.headersTimeout = server.keepAliveTimeout + 5000;
  server.listen(port, host, () => {
    console.error(`[${SERVICE_NAME}] listening http://${host}:${port} pid=${process.pid}`);
    service.start()
      .then(() => {
        console.error(`[${SERVICE_NAME}] ready http://${host}:${port} pid=${process.pid} proxy=${JSON.stringify(service.proxy)}`);
      })
      .catch((err) => {
        service.startError = redactErrorText(err?.message || String(err || "start_failed"));
        service.lastHeartbeatError = service.startError;
        console.error(`[${SERVICE_NAME}] start_failed ${redactErrorText(err?.stack || err?.message || String(err))}`);
      });
  });
}

main().catch((err) => {
  console.error(`[${SERVICE_NAME}] fatal ${redactErrorText(err?.stack || err?.message || String(err))}`);
  process.exit(1);
});
