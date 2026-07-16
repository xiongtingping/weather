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
import { AssetType, ClobClient, Side, OrderType, getContractConfig } from "@polymarket/clob-client-v2";
import { Wallet } from "ethers";
import { DatabaseSync } from "node:sqlite";
import { hashTypedData } from "viem";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DATA_DIR = process.env.PM_DATA_DIR || path.join(ROOT, "data");
const DB_PATH = process.env.PM_ARCHIVE_DB || path.join(DATA_DIR, "archive.db");
const SERVICE_NAME = "clob_order_service";
const IDEMPOTENCY_TERMINAL_STATES = new Set(["exchange_accepted", "submit_failed"]);

const EIP712_DOMAIN = [
  { name: "name", type: "string" },
  { name: "version", type: "string" },
  { name: "chainId", type: "uint256" },
  { name: "verifyingContract", type: "address" },
];
const CLOB_ORDER_V1_TYPES = [
  { name: "salt", type: "uint256" },
  { name: "maker", type: "address" },
  { name: "signer", type: "address" },
  { name: "taker", type: "address" },
  { name: "tokenId", type: "uint256" },
  { name: "makerAmount", type: "uint256" },
  { name: "takerAmount", type: "uint256" },
  { name: "expiration", type: "uint256" },
  { name: "nonce", type: "uint256" },
  { name: "feeRateBps", type: "uint256" },
  { name: "side", type: "uint8" },
  { name: "signatureType", type: "uint8" },
];
const CLOB_ORDER_V2_TYPES = [
  { name: "salt", type: "uint256" },
  { name: "maker", type: "address" },
  { name: "signer", type: "address" },
  { name: "tokenId", type: "uint256" },
  { name: "makerAmount", type: "uint256" },
  { name: "takerAmount", type: "uint256" },
  { name: "side", type: "uint8" },
  { name: "signatureType", type: "uint8" },
  { name: "timestamp", type: "uint256" },
  { name: "metadata", type: "bytes32" },
  { name: "builder", type: "bytes32" },
];

export function expectedOrderIdForSignedOrder(signedOrder, { chainId, negRisk }) {
  if (!signedOrder || typeof signedOrder !== "object") {
    throw new Error("signed_order_missing");
  }
  const version = Object.prototype.hasOwnProperty.call(signedOrder, "timestamp") ? 2 : 1;
  const contracts = getContractConfig(Number(chainId));
  const verifyingContract = version === 2
    ? (negRisk ? contracts.negRiskExchangeV2 : contracts.exchangeV2)
    : (negRisk ? contracts.negRiskExchange : contracts.exchange);
  const fields = version === 2 ? CLOB_ORDER_V2_TYPES : CLOB_ORDER_V1_TYPES;
  const message = {};
  for (const field of fields) {
    const value = signedOrder[field.name];
    if (value === undefined || value === null || value === "") {
      throw new Error(`signed_order_hash_field_missing:${field.name}`);
    }
    message[field.name] = field.name === "side"
      ? (String(value).toUpperCase() === "BUY" ? 0 : 1)
      : value;
  }
  return hashTypedData({
    domain: {
      name: "Polymarket CTF Exchange",
      version: String(version),
      chainId: Number(chainId),
      verifyingContract,
    },
    types: { EIP712Domain: EIP712_DOMAIN, Order: fields },
    primaryType: "Order",
    message,
  });
}

export class DurableIdempotencyStore {
  constructor(dbPath, ttlMs, maxRows) {
    this.dbPath = dbPath;
    this.ttlMs = ttlMs;
    this.maxRows = maxRows;
    this.db = new DatabaseSync(dbPath);
    this.db.exec("PRAGMA journal_mode=WAL");
    this.db.exec("PRAGMA synchronous=FULL");
    this.db.exec("PRAGMA busy_timeout=5000");
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS clob_order_intents (
        idempotency_key TEXT PRIMARY KEY,
        order_intent_id TEXT NOT NULL,
        state TEXT NOT NULL,
        request_json TEXT NOT NULL DEFAULT '{}',
        response_json TEXT NOT NULL DEFAULT '{}',
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL
      )
    `);
    this.db.exec(`
      CREATE INDEX IF NOT EXISTS idx_clob_order_intents_state_updated
      ON clob_order_intents(state, updated_ms)
    `);
    this.upsertStatement = this.db.prepare(`
      INSERT INTO clob_order_intents(
        idempotency_key, order_intent_id, state, request_json, response_json,
        created_ms, updated_ms
      ) VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(idempotency_key) DO UPDATE SET
        order_intent_id = excluded.order_intent_id,
        state = excluded.state,
        request_json = CASE
          WHEN excluded.request_json <> '{}' THEN excluded.request_json
          ELSE clob_order_intents.request_json
        END,
        response_json = excluded.response_json,
        updated_ms = excluded.updated_ms
    `);
    this.deleteStatement = this.db.prepare(
      "DELETE FROM clob_order_intents WHERE idempotency_key = ?"
    );
    this.loadStatement = this.db.prepare(`
      SELECT idempotency_key, order_intent_id, state, request_json, response_json,
             created_ms, updated_ms
      FROM clob_order_intents
      WHERE updated_ms >= ?
         OR state NOT IN ('exchange_accepted', 'submit_failed')
      ORDER BY updated_ms ASC
    `);
    this.cleanup();
  }

  cleanup() {
    const cutoff = Date.now() - this.ttlMs;
    this.db.prepare(
      "DELETE FROM clob_order_intents WHERE updated_ms < ? AND state IN ('exchange_accepted', 'submit_failed')"
    ).run(cutoff);
    const countRow = this.db.prepare("SELECT COUNT(*) AS n FROM clob_order_intents").get();
    const excess = Math.max(0, Number(countRow?.n || 0) - this.maxRows);
    if (excess > 0) {
      this.db.prepare(`
        DELETE FROM clob_order_intents
        WHERE idempotency_key IN (
          SELECT idempotency_key FROM clob_order_intents
          WHERE state IN ('exchange_accepted', 'submit_failed')
          ORDER BY updated_ms ASC
          LIMIT ?
        )
      `).run(excess);
    }
  }

  load() {
    const cutoff = Date.now() - this.ttlMs;
    return this.loadStatement.all(cutoff).map((row) => {
      let request = {};
      let response = {};
      try { request = JSON.parse(String(row.request_json || "{}")); } catch {}
      try { response = JSON.parse(String(row.response_json || "{}")); } catch {}
      return { ...row, request, response };
    });
  }

  put(key, orderIntentId, state, request = {}, response = {}) {
    const now = Date.now();
    this.upsertStatement.run(
      String(key),
      String(orderIntentId),
      String(state),
      JSON.stringify(request || {}),
      JSON.stringify(response || {}),
      now,
      now,
    );
  }

  delete(key) {
    this.deleteStatement.run(String(key));
  }
}

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

function normalizeTickSize(value, fallback = "0.01") {
  const allowed = new Set(["0.1", "0.01", "0.001", "0.0001"]);
  const candidates = [value, fallback, "0.01"];
  for (const item of candidates) {
    if (item === undefined || item === null || item === "") continue;
    const text = String(item).trim();
    if (allowed.has(text)) return text;
    const num = Number(text);
    if (!Number.isFinite(num) || num <= 0) continue;
    const normalized = num.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    if (allowed.has(normalized)) return normalized;
  }
  return "0.01";
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
  delete copy.execution_domain;
  delete copy.order_execution_domain;
  delete copy.order_execution_source;
  delete copy.order_execution_context_version;
  delete copy.order_execution_reason;
  delete copy.order_intent_action;
  delete copy.manual_confirm_source;
  return crypto.createHash("sha256").update(canonicalJson(copy)).digest("hex");
}

function directEntryHardDeadline(payload, nowMs = Date.now()) {
  const isDirectEntry = Boolean(payload?.direct_target_order_context)
    && !Boolean(payload?.direct_target_exit_order)
    && String(payload?.side || "BUY").toUpperCase() !== "SELL";
  if (!isDirectEntry) return { required: false, ok: true };
  const executionContract = payload?.sequential_bayes_execution_contract;
  const rawDeadline = payload?.observation_to_order_hard_deadline_at
    || executionContract?.observation_to_order_hard_deadline_at
    || payload?.signal_to_order_hard_deadline_at
    || executionContract?.signal_to_order_hard_deadline_at;
  const deadlineMs = Date.parse(String(rawDeadline || ""));
  if (!Number.isFinite(deadlineMs)) {
    return {
      required: true,
      ok: false,
      reason: "direct_entry_hard_deadline_missing_or_invalid",
      deadline_at: String(rawDeadline || "") || null,
    };
  }
  if (nowMs > deadlineMs) {
    return {
      required: true,
      ok: false,
      reason: "direct_entry_hard_deadline_expired",
      deadline_at: new Date(deadlineMs).toISOString(),
      overdue_ms: nowMs - deadlineMs,
    };
  }
  return {
    required: true,
    ok: true,
    deadline_at: new Date(deadlineMs).toISOString(),
    remaining_ms: deadlineMs - nowMs,
  };
}

function isHttpErrorStatus(status) {
  const num = Number(status);
  return Number.isFinite(num) && num >= 400;
}

function isRetryableLocalOrderBuildError(text) {
  const message = String(text || "");
  return message.includes("Cannot read properties of undefined (reading 'toString')");
}

function classifyClobOrderError(err) {
  const status = Number(err?.response?.status || err?.status || 0);
  const responseData = err?.response?.data;
  let responseText = "";
  if (responseData !== undefined && responseData !== null) {
    if (typeof responseData === "string") responseText = responseData;
    else {
      try {
        responseText = JSON.stringify(responseData);
      } catch {
        responseText = String(responseData);
      }
    }
  }
  const message = String(err?.message || err || "");
  const text = `${message} ${responseText}`.toLowerCase();
  if (text.includes("trading restricted") || text.includes("geoblock")) {
    return {
      error_class: "clob_order_geoblocked",
      http_status: status || 403,
      geoblocked: true,
      retryable: false,
      fatal: true,
    };
  }
  if (status === 429 || text.includes("too many requests") || text.includes("rate limit")) {
    return {
      error_class: "clob_order_rate_limited",
      http_status: status || 429,
      geoblocked: false,
      retryable: true,
      fatal: false,
    };
  }
  if ([500, 502, 503, 504].includes(status) || text.includes("bad gateway") || text.includes("gateway timeout")) {
    return {
      error_class: "clob_order_gateway_error",
      http_status: status || null,
      geoblocked: false,
      retryable: true,
      fatal: false,
    };
  }
  if (text.includes("timeout") || text.includes("etimedout")) {
    return {
      error_class: "clob_order_timeout",
      http_status: status || null,
      geoblocked: false,
      retryable: true,
      fatal: false,
    };
  }
  if (text.includes("connection refused") || text.includes("newconnectionerror") || text.includes("econnrefused")) {
    return {
      error_class: "clob_order_service_local_failure",
      http_status: status || null,
      geoblocked: false,
      retryable: true,
      fatal: false,
    };
  }
  return {
    error_class: "clob_order_rejected",
    http_status: status || null,
    geoblocked: false,
    retryable: false,
    fatal: false,
  };
}

export class ClobOrderService {
  constructor() {
    this.startedAt = new Date();
    this.proxy = {};
    this.client = null;
    this.metadata = new Map();
    this.prewarmTokenHint = "";
    this.inflight = new Map();
    this.idem = new Map();
    this.unresolved = new Map();
    this.submitChain = Promise.resolve();
    this.idemDbPath = env(
      "PM_CLOB_ORDER_IDEMPOTENCY_DB",
      path.join(DATA_DIR, "clob_order_service_idempotency.sqlite"),
    );
    this.idemStore = null;
    this.lastIdempotencyPersistenceError = "";
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
    this.lastPlaceOrderErrorAt = null;
    this.lastPlaceOrderErrorClass = "";
    this.lastPlaceOrderHttpStatus = null;
    this.lastPlaceOrderGeoblocked = false;
    this.lastPlaceOrderRetryable = null;
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
    this.idemStore = new DurableIdempotencyStore(this.idemDbPath, this.idemTtlMs, this.maxIdem);
    for (const row of this.idemStore.load()) {
      const key = String(row.idempotency_key || "");
      const item = {
        created_ms: Number(row.created_ms || Date.now()),
        updated_ms: Number(row.updated_ms || row.created_ms || Date.now()),
        request: row.request || {},
        response: row.response || {},
        state: String(row.state || ""),
        order_intent_id: String(row.order_intent_id || key),
      };
      if (IDEMPOTENCY_TERMINAL_STATES.has(item.state)) {
        this.idem.set(key, item);
      } else {
        this.unresolved.set(key, item);
      }
    }
  }

  saveIdempotency() {
    if (!this.idemStore) throw new Error("idempotency_store_not_initialized");
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
    this.idemStore.cleanup();
  }

  persistIntent(key, state, request = {}, response = {}) {
    if (!this.idemStore) throw new Error("idempotency_store_not_initialized");
    const orderIntentId = String(request?.order_intent_id || key);
    this.idemStore.put(key, orderIntentId, state, request, response);
    this.lastIdempotencyPersistenceError = "";
    return orderIntentId;
  }

  deleteIntent(key) {
    if (!this.idemStore) throw new Error("idempotency_store_not_initialized");
    this.idemStore.delete(key);
    this.idem.delete(key);
    this.unresolved.delete(key);
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
    const tickSize = normalizeTickSize(env("PM_CLOB_DEFAULT_TICK_SIZE", "0.01"));
    const negRisk = truthy(env("PM_CLOB_DEFAULT_NEG_RISK", "1"), true);
    let count = 0;
    for (const row of rows || []) {
      const tokenID = String(row?.token_id || "").trim();
      if (!tokenID) continue;
      const previous = this.metadata.get(tokenID) || {};
      this.rememberTokenMetadata(
        tokenID,
        previous.tickSize ? normalizeTickSize(previous.tickSize, tickSize) : tickSize,
        typeof previous.negRisk === "boolean" ? previous.negRisk : negRisk,
        previous.source || "market_tokens_prewarm"
      );
      if (!this.prewarmTokenHint) this.prewarmTokenHint = tokenID;
      count += 1;
    }
    return count;
  }

  rememberTokenMetadata(tokenID, tickSize, negRisk, source = "runtime") {
    const normalizedTokenID = String(tokenID || "").trim();
    if (!normalizedTokenID) return;
    const normalizedTickSize = normalizeTickSize(tickSize, env("PM_CLOB_DEFAULT_TICK_SIZE", "0.01"));
    const normalizedNegRisk = typeof negRisk === "boolean"
      ? negRisk
      : truthy(env("PM_CLOB_DEFAULT_NEG_RISK", "1"), true);
    this.metadata.set(normalizedTokenID, {
      tickSize: normalizedTickSize,
      negRisk: normalizedNegRisk,
      updated_at: new Date().toISOString(),
      source,
    });
    if (this.client) {
      this.client.tickSizes[normalizedTokenID] = normalizedTickSize;
      this.client.negRisk[normalizedTokenID] = normalizedNegRisk;
    }
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
      idempotency_unresolved_count: this.unresolved.size,
      idempotency_store: "sqlite",
      idempotency_db_path: this.idemDbPath,
      last_idempotency_persistence_error: this.lastIdempotencyPersistenceError,
      inflight_count: this.inflight.size,
      last_prewarm_at: this.lastPrewarmAt,
      last_prewarm_ms: this.lastPrewarmMs,
      last_prewarm_error: this.lastPrewarmError,
      last_signer_prewarm_at: this.lastSignerPrewarmAt,
      last_signer_prewarm_ms: this.lastSignerPrewarmMs,
      last_signer_prewarm_error: this.lastSignerPrewarmError,
      last_place_order_error_at: this.lastPlaceOrderErrorAt,
      last_place_order_error_class: this.lastPlaceOrderErrorClass,
      last_place_order_http_status: this.lastPlaceOrderHttpStatus,
      last_place_order_geoblocked: this.lastPlaceOrderGeoblocked,
      last_place_order_retryable: this.lastPlaceOrderRetryable,
      last_heartbeat_at: this.lastHeartbeat,
      last_heartbeat_error: this.lastHeartbeatError,
      start_error: this.startError,
      read_only: truthy(env("PM_CLOB_ORDER_SERVICE_READ_ONLY", "0")),
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
    if (this.unresolved.has(normalized)) {
      const unresolved = this.unresolved.get(normalized) || {};
      return {
        ok: false,
        found: true,
        pending: false,
        reconcile_required: true,
        error: "order_submit_outcome_unknown",
        idempotency_key: normalized,
        order_intent_id: unresolved.order_intent_id || normalized,
        intent_state: unresolved.state || "reconcile_required",
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

  async reconcileOrder(key) {
    const normalized = String(key || "").trim();
    if (!normalized) {
      return { ok: false, found: false, error: "missing_idempotency_key" };
    }
    if (this.idem.has(normalized) || this.inflight.has(normalized)) {
      return this.idempotencyStatus(normalized);
    }
    const unresolved = this.unresolved.get(normalized);
    if (!unresolved) {
      return this.idempotencyStatus(normalized);
    }
    const request = unresolved.request || {};
    const previousResponse = unresolved.response || {};
    const expectedOrderID = String(previousResponse.expected_order_id || "").trim();
    if (!expectedOrderID) {
      return {
        ...this.idempotencyStatus(normalized),
        reconcile_attempted: false,
        reconcile_reason: "expected_order_id_missing",
      };
    }
    if (!this.client) {
      return {
        ...this.idempotencyStatus(normalized),
        reconcile_attempted: false,
        reconcile_reason: "service_not_ready",
        expected_order_id: expectedOrderID,
      };
    }
    try {
      const order = await this.client.getOrder(expectedOrderID);
      const recoveredOrderID = String(order?.id || order?.orderID || expectedOrderID).trim();
      if (!order || recoveredOrderID.toLowerCase() !== expectedOrderID.toLowerCase()) {
        return {
          ...this.idempotencyStatus(normalized),
          reconcile_attempted: true,
          reconcile_reason: "exchange_order_identity_mismatch",
          expected_order_id: expectedOrderID,
          exchange_order_id: recoveredOrderID || null,
        };
      }
      const response = {
        ok: true,
        orderID: recoveredOrderID,
        expected_order_id: expectedOrderID,
        expected_order_id_matches: true,
        status: String(order.status || "submitted_unconfirmed"),
        source: SERVICE_NAME,
        service_pid: process.pid,
        idempotency_key: normalized,
        order_intent_id: String(unresolved.order_intent_id || normalized),
        reconcile_required: false,
        reconcile_attempted: true,
        reconciled_from_exchange: true,
        reconciled_at: new Date().toISOString(),
        token_id: String(order.asset_id || request.token_id || ""),
        side: String(order.side || request.side || "").toUpperCase(),
        original_size: order.original_size ?? null,
        size_matched: order.size_matched ?? null,
        price: order.price ?? null,
      };
      this.persistIntent(normalized, "exchange_accepted", request, response);
      this.unresolved.delete(normalized);
      this.idem.set(normalized, {
        created_ms: Number(unresolved.created_ms || Date.now()),
        request,
        response,
        state: "exchange_accepted",
        order_intent_id: response.order_intent_id,
      });
      this.saveIdempotency();
      return response;
    } catch (err) {
      const classification = classifyClobOrderError(err);
      return {
        ...this.idempotencyStatus(normalized),
        reconcile_attempted: true,
        reconcile_reason: "exchange_order_not_confirmed",
        expected_order_id: expectedOrderID,
        reconcile_error: redactErrorText(err?.message || String(err || "")),
        reconcile_error_class: classification.error_class,
        reconcile_http_status: classification.http_status,
      };
    }
  }

  async dynamicThreeLegSnapshot(rawPayload) {
    const rawBuckets = Array.isArray(rawPayload?.buckets) ? rawPayload.buckets : [];
    const rawPositionTokens = Array.isArray(rawPayload?.position_token_ids)
      ? rawPayload.position_token_ids : [];
    if (!this.client) throw new Error(this.startError ? `service_not_ready:${this.startError}` : "service_not_ready");
    if (rawBuckets.length < 1 || rawBuckets.length > 12) throw new Error("dynamic_three_leg_bucket_count_invalid");
    const buckets = rawBuckets.map((row) => ({
      bucket: String(row?.bucket || "").trim(),
      yes_token_id: String(row?.yes_token_id || "").trim(),
      no_token_id: String(row?.no_token_id || "").trim(),
    }));
    if (buckets.some((row) => !row.bucket || !row.yes_token_id || !row.no_token_id)) {
      throw new Error("dynamic_three_leg_bucket_token_identity_missing");
    }
    const marketTokenIDs = [...new Set(buckets.flatMap((row) => [row.yes_token_id, row.no_token_id]))];
    const positionTokenIDs = [...new Set(rawPositionTokens.map((value) => String(value || "").trim()).filter(Boolean))];
    if (marketTokenIDs.length > 24 || positionTokenIDs.length > 24) {
      throw new Error("dynamic_three_leg_token_count_invalid");
    }
    const started = Date.now();
    const [books, collateral, conditional] = await Promise.all([
      Promise.all(marketTokenIDs.map(async (tokenID) => [tokenID, await this.client.getOrderBook(tokenID)])),
      this.client.getBalanceAllowance({ asset_type: AssetType.COLLATERAL }),
      Promise.all(positionTokenIDs.map(async (tokenID) => [
        tokenID,
        await this.client.getBalanceAllowance({ asset_type: AssetType.CONDITIONAL, token_id: tokenID }),
      ])),
    ]);
    const observedAt = new Date().toISOString();
    return {
      schema: "clob_dynamic_three_leg_snapshot_v1",
      ok: true,
      evidence_only: true,
      authorizes_order: false,
      request_id: String(rawPayload?.request_id || "").trim(),
      observed_at: observedAt,
      service_latency_ms: Date.now() - started,
      amount_decimals: 6,
      buckets,
      order_books_by_token: Object.fromEntries(books),
      conditional_balance_allowance_by_token: Object.fromEntries(conditional),
      collateral_balance_allowance: collateral,
      source: SERVICE_NAME,
      service_pid: process.pid,
    };
  }

  async executionGuardSnapshot(rawPayload) {
    const tokenID = String(rawPayload?.token_id || rawPayload?.tokenID || "").trim();
    const side = String(rawPayload?.side || "").trim().toUpperCase();
    const limitPrice = Number(rawPayload?.limit_price ?? rawPayload?.price);
    const size = Number(rawPayload?.size);
    const notional = Number(rawPayload?.notional);
    const started = Date.now();
    if (!this.client) throw new Error(this.startError ? `service_not_ready:${this.startError}` : "service_not_ready");
    if (!tokenID || !["BUY", "SELL"].includes(side)) throw new Error("execution_guard_identity_missing");
    if (!Number.isFinite(limitPrice) || limitPrice <= 0 || limitPrice >= 1 || !Number.isFinite(size) || size <= 0) {
      throw new Error("execution_guard_order_terms_invalid");
    }
    if (side === "BUY" && (!Number.isFinite(notional) || notional <= 0)) {
      throw new Error("execution_guard_buy_notional_invalid");
    }

    // This endpoint returns raw exchange evidence only.  It never decides that
    // an order is authorized; the Python execution boundary independently
    // checks depth, price, position, collateral and durable risk reservation.
    const conditionalPromise = this.client.getBalanceAllowance({
      asset_type: AssetType.CONDITIONAL,
      token_id: tokenID,
    });
    const collateralPromise = side === "BUY"
      ? this.client.getBalanceAllowance({ asset_type: AssetType.COLLATERAL })
      : Promise.resolve(null);
    const [orderBook, conditional, collateral] = await Promise.all([
      this.client.getOrderBook(tokenID),
      conditionalPromise,
      collateralPromise,
    ]);
    const observedAt = new Date().toISOString();
    const balanceShapeOk = (value) => {
      if (!value || typeof value !== "object" || value.error) return false;
      const balance = String(value.balance ?? "");
      const directAllowance = String(value.allowance ?? "");
      const allowanceMap = value.allowances && typeof value.allowances === "object"
        ? Object.values(value.allowances).map((item) => String(item ?? ""))
        : [];
      return /^\d+$/.test(balance)
        && (/^\d+$/.test(directAllowance) || allowanceMap.some((item) => /^\d+$/.test(item)));
    };
    const evidenceErrors = [];
    if (!orderBook || orderBook.error || String(orderBook.asset_id || "") !== tokenID
        || !Array.isArray(orderBook.bids) || !Array.isArray(orderBook.asks)
        || !String(orderBook.timestamp || "")) {
      evidenceErrors.push("clob_order_book_evidence_unavailable_or_invalid");
    }
    if (!balanceShapeOk(conditional)) evidenceErrors.push("clob_conditional_balance_evidence_unavailable_or_invalid");
    if (side === "BUY" && !balanceShapeOk(collateral)) evidenceErrors.push("clob_collateral_balance_evidence_unavailable_or_invalid");
    return {
      schema: "clob_execution_guard_snapshot_v1",
      ok: evidenceErrors.length === 0,
      evidence_only: true,
      authorizes_order: false,
      evidence_errors: evidenceErrors,
      token_id: tokenID,
      side,
      limit_price: limitPrice,
      size,
      notional: Number.isFinite(notional) ? notional : null,
      observed_at: observedAt,
      service_latency_ms: Date.now() - started,
      amount_decimals: 6,
      order_book: orderBook,
      conditional_balance_allowance: conditional,
      collateral_balance_allowance: collateral,
      source: SERVICE_NAME,
      service_pid: process.pid,
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
      const tickSize = normalizeTickSize(cachedMeta.tickSize, env("PM_CLOB_DEFAULT_TICK_SIZE", "0.01"));
      let negRisk = typeof cachedMeta.negRisk === "boolean" ? cachedMeta.negRisk : truthy(env("PM_CLOB_DEFAULT_NEG_RISK", "1"), true);
      await this.client.createOrder(
        {
          tokenID,
          price: positiveNumber(env("PM_CLOB_ORDER_SERVICE_PREWARM_SIGNER_PRICE", tickSize), Number(tickSize)),
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
    if (truthy(env("PM_CLOB_ORDER_SERVICE_READ_ONLY", "0"))) {
      return {
        ok: false, accepted: false, pending: false, reconcile_required: false,
        error: "clob_order_service_read_only",
        exchange_post_started: false,
        idempotency_key: String(payload?.idempotency_key || ""),
        order_intent_id: String(payload?.order_intent_id || ""),
        source: SERVICE_NAME,
      };
    }
    const key = String(payload.idempotency_key || stableKey(payload));
    const orderIntentId = String(payload.order_intent_id || key);
    payload.order_intent_id = orderIntentId;
    const asyncAck = Boolean(payload.async_ack) || truthy(env("PM_CLOB_ORDER_SERVICE_ASYNC_ACK", "0"));
    if (this.idem.has(key)) {
      const cached = this.idem.get(key);
      const cachedResponse = cached?.response || {};
      if (cachedResponse?.ok === false && isRetryableLocalOrderBuildError(cachedResponse?.error)) {
        this.deleteIntent(key);
      } else {
        return { ...cachedResponse, idempotency_key: key, order_intent_id: orderIntentId, idempotent_replay: true };
      }
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
      return { ...response, idempotency_key: key, order_intent_id: orderIntentId, idempotent_replay: true, idempotent_inflight: true };
    }
    if (this.unresolved.has(key)) {
      return this.idempotencyStatus(key);
    }
    const deadlineAdmission = directEntryHardDeadline(payload);
    if (!deadlineAdmission.ok) {
      return {
        ok: false,
        accepted: false,
        pending: false,
        reconcile_required: false,
        error: deadlineAdmission.reason,
        hard_deadline_at: deadlineAdmission.deadline_at || null,
        hard_deadline_overdue_ms: Number(deadlineAdmission.overdue_ms || 0),
        exchange_post_started: false,
        idempotency_key: key,
        order_intent_id: orderIntentId,
        source: SERVICE_NAME,
        service_pid: process.pid,
      };
    }
    try {
      this.persistIntent(key, "pending_submit", payload, {
        ok: false,
        pending: true,
        accepted: false,
        idempotency_key: key,
        order_intent_id: orderIntentId,
      });
      this.persistIntent(key, "submitting", payload, {
        ok: false,
        pending: true,
        accepted: true,
        idempotency_key: key,
        order_intent_id: orderIntentId,
      });
      this.unresolved.set(key, {
        state: "submitting",
        order_intent_id: orderIntentId,
        created_ms: Date.now(),
        request: { ...payload },
        response: {},
      });
    } catch (err) {
      this.lastIdempotencyPersistenceError = redactErrorText(err?.message || String(err || ""));
      return {
        ok: false,
        pending: false,
        reconcile_required: false,
        error: "idempotency_persist_failed_before_submit",
        error_detail: this.lastIdempotencyPersistenceError,
        idempotency_key: key,
        order_intent_id: orderIntentId,
        source: SERVICE_NAME,
      };
    }
    const acceptedAt = new Date().toISOString();
    const acceptedMs = Date.now();
    const priorityEntry = this.isPriorityDirectEntry(payload);
    const lane = priorityEntry ? "direct_target_primary_entry_priority" : "default_serial";
    const base = priorityEntry ? Promise.resolve() : this.submitChain;
    const promise = base
      .then(() => this.placeOrder(payload, key, { acceptedMs, lane }))
      .then((response) => {
        if (!isRetryableLocalOrderBuildError(response.error)) {
          const persistedResponse = { ...response, order_intent_id: orderIntentId };
          try {
            this.persistIntent(key, "exchange_accepted", payload, persistedResponse);
          } catch (err) {
            this.lastIdempotencyPersistenceError = redactErrorText(err?.message || String(err || ""));
            this.unresolved.set(key, {
              state: "reconcile_required",
              order_intent_id: orderIntentId,
              created_ms: Date.now(),
              request: { ...payload },
              response: persistedResponse,
            });
            return {
              ok: false,
              reconcile_required: true,
              error: "idempotency_persist_failed_after_submit",
              error_detail: this.lastIdempotencyPersistenceError,
              idempotency_key: key,
              order_intent_id: orderIntentId,
              exchange_response: persistedResponse,
              source: SERVICE_NAME,
            };
          }
          this.unresolved.delete(key);
          this.idem.set(key, {
            created_ms: Date.now(),
            response: persistedResponse,
            state: "exchange_accepted",
            order_intent_id: orderIntentId,
          });
          this.saveIdempotency();
          return persistedResponse;
        }
        this.deleteIntent(key);
        return response;
      })
      .catch((err) => {
        const meta = err?.serviceMeta || {};
        const classification = classifyClobOrderError(err);
        this.lastPlaceOrderErrorAt = new Date().toISOString();
        this.lastPlaceOrderErrorClass = classification.error_class;
        this.lastPlaceOrderHttpStatus = classification.http_status;
        this.lastPlaceOrderGeoblocked = Boolean(classification.geoblocked);
        this.lastPlaceOrderRetryable = Boolean(classification.retryable);
        const response = {
          ok: false,
          error: redactErrorText(err?.message || String(err || "place_order_failed")),
          ...classification,
          source: SERVICE_NAME,
          service_pid: process.pid,
          idempotency_key: key,
          order_intent_id: orderIntentId,
          order_lane: lane,
          queue_wait_ms: Number.isFinite(Number(meta.queue_wait_ms)) ? Number(meta.queue_wait_ms) : null,
          service_submit_ms: Number.isFinite(Number(meta.service_submit_ms)) ? Number(meta.service_submit_ms) : null,
          service_total_ms: Number.isFinite(Number(meta.service_total_ms)) ? Number(meta.service_total_ms) : Math.max(0, Date.now() - acceptedMs),
          submit_call_ms: Number.isFinite(Number(meta.submit_call_ms)) ? Number(meta.submit_call_ms) : null,
          expected_order_id: String(meta.expected_order_id || "") || null,
          signed_order_prepared: Boolean(meta.signed_order_prepared),
          exchange_post_started: Boolean(meta.exchange_post_started),
          exchange_post_started_at: String(meta.exchange_post_started_at || "") || null,
          hard_deadline_at: String(meta.hard_deadline_at || "") || null,
          hard_deadline_overdue_ms: Number.isFinite(Number(meta.hard_deadline_overdue_ms))
            ? Number(meta.hard_deadline_overdue_ms)
            : null,
          clob_timeout_ms: Number.isFinite(Number(meta.clob_timeout_ms)) ? Number(meta.clob_timeout_ms) : Number(axios.defaults.timeout || 0) || null,
        };
        const outcomeUnknown = response.exchange_post_started && (
          Boolean(classification.retryable)
          || /timeout|timed out/i.test(String(response.error || ""))
        );
        const terminalState = outcomeUnknown ? "reconcile_required" : "submit_failed";
        response.reconcile_required = outcomeUnknown;
        try {
          this.persistIntent(key, terminalState, payload, response);
        } catch (persistErr) {
          this.lastIdempotencyPersistenceError = redactErrorText(
            persistErr?.message || String(persistErr || ""),
          );
          response.reconcile_required = true;
          response.idempotency_persistence_error = this.lastIdempotencyPersistenceError;
        }
        if (response.reconcile_required) {
          this.unresolved.set(key, {
            state: "reconcile_required",
            order_intent_id: orderIntentId,
            created_ms: Date.now(),
            request: { ...payload },
            response: { ...response },
          });
        } else {
          this.unresolved.delete(key);
          this.idem.set(key, {
            created_ms: Date.now(),
            response,
            state: "submit_failed",
            order_intent_id: orderIntentId,
          });
          this.saveIdempotency();
        }
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
        order_intent_id: orderIntentId,
        accepted_at: acceptedAt,
        order_lane: lane,
        source: SERVICE_NAME,
        service_pid: process.pid,
      };
    }
    const response = await promise;
    return { ...response, idempotency_key: key, order_intent_id: orderIntentId };
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
    const postOnly = truthy(
      rawPayload.post_only ?? rawPayload.postOnly ?? env("PM_CLOB_POST_ONLY", "0")
    );
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
    if (postOnly && isMarket) {
      throw errorWithMeta(`post_only_market_order_forbidden:${orderTypeRaw}`);
    }

    let tickSize = normalizeTickSize(rawPayload.tick_size ?? rawPayload.tickSize, "");
    let negRisk = typeof rawPayload.neg_risk === "boolean" ? rawPayload.neg_risk : rawPayload.negRisk;
    const cachedMeta = this.metadata.get(tokenID) || {};
    if (!rawPayload.tick_size && !rawPayload.tickSize && cachedMeta.tickSize) {
      tickSize = normalizeTickSize(cachedMeta.tickSize, tickSize);
    }
    if (typeof negRisk !== "boolean") negRisk = cachedMeta.negRisk;
    tickSize = normalizeTickSize(tickSize, env("PM_CLOB_DEFAULT_TICK_SIZE", "0.01"));
    if (typeof negRisk !== "boolean") negRisk = truthy(env("PM_CLOB_DEFAULT_NEG_RISK", "1"), true);
    this.rememberTokenMetadata(tokenID, tickSize, negRisk, "place_order_payload");

    const builderCode = env("POLY_BUILDER_CODE", env("PM_BUILDER_CODE", "")).trim();
    const sellMarketableLimit = isMarket
      && side === Side.SELL
      && Number.isFinite(price)
      && Number.isFinite(size)
      && truthy(env("PM_CLOB_SELL_MARKETABLE_LIMIT_ORDER", "1"), true);

    let response;
    let signedOrder;
    let expectedOrderID = "";
    let exchangePostStarted = false;
    let exchangePostStartedAt = "";
    const assertDirectEntryDeadline = () => {
      const status = directEntryHardDeadline(rawPayload);
      if (!status.ok) {
        throw errorWithMeta(status.reason, {
          hard_deadline_at: status.deadline_at || null,
          hard_deadline_overdue_ms: Number(status.overdue_ms || 0),
          exchange_post_started: false,
          exchange_post_started_at: null,
        });
      }
      return status;
    };
    const persistSignedPrepared = () => {
      expectedOrderID = expectedOrderIdForSignedOrder(signedOrder, {
        chainId: Number(env("PM_CHAIN_ID", "137")),
        negRisk,
      });
      const prepared = {
        ok: false,
        pending: true,
        accepted: true,
        expected_order_id: expectedOrderID,
        signed_order_prepared: true,
        idempotency_key: idempotencyKey,
        order_intent_id: String(rawPayload.order_intent_id || idempotencyKey),
      };
      this.persistIntent(idempotencyKey, "signed_prepared", rawPayload, prepared);
      this.unresolved.set(idempotencyKey, {
        state: "signed_prepared",
        order_intent_id: String(rawPayload.order_intent_id || idempotencyKey),
        created_ms: Date.now(),
        request: { ...rawPayload },
        response: prepared,
      });
    };
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
        signedOrder = await this.client.createMarketOrder({
          tokenID,
          side,
          amount: finalAmount,
          ...(Number.isFinite(price) ? { price } : {}),
          ...(builderCode ? { builderCode } : {}),
        }, { tickSize, negRisk });
        persistSignedPrepared();
        assertDirectEntryDeadline();
        exchangePostStartedAt = new Date().toISOString();
        exchangePostStarted = true;
        response = await this.client.postOrder(signedOrder, orderType, false, deferExec);
      } catch (err) {
        err.serviceMeta = metaNow({
          submit_call_ms: Date.now() - submitStarted,
          expected_order_id: expectedOrderID || null,
          signed_order_prepared: Boolean(expectedOrderID),
          exchange_post_started: exchangePostStarted,
          exchange_post_started_at: exchangePostStartedAt || null,
        });
        throw err;
      }
    } else {
      const submitStarted = Date.now();
      try {
        signedOrder = await this.client.createOrder({
          tokenID,
          price,
          size,
          side,
          ...(builderCode ? { builderCode } : {}),
        }, { tickSize, negRisk });
        persistSignedPrepared();
        assertDirectEntryDeadline();
        exchangePostStartedAt = new Date().toISOString();
        exchangePostStarted = true;
        response = await this.client.postOrder(signedOrder, orderType, postOnly, deferExec);
      } catch (err) {
        err.serviceMeta = metaNow({
          submit_call_ms: Date.now() - submitStarted,
          expected_order_id: expectedOrderID || null,
          signed_order_prepared: Boolean(expectedOrderID),
          exchange_post_started: exchangePostStarted,
          exchange_post_started_at: exchangePostStartedAt || null,
        });
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
      expected_order_id: expectedOrderID || response.orderID,
      expected_order_id_matches: !expectedOrderID || expectedOrderID === response.orderID,
      status: response.status,
      source: SERVICE_NAME,
      service_pid: process.pid,
      idempotency_key: idempotencyKey,
      order_intent_id: String(rawPayload.order_intent_id || idempotencyKey),
      exchange_post_started: true,
      exchange_post_started_at: exchangePostStartedAt,
      order_submitted_at: exchangePostStartedAt,
      post_only: postOnly,
      maker_only: postOnly,
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
    if (req.method === "POST" && parsedUrl.pathname === "/reconcile-order") {
      const payload = await readJsonBody(req);
      const key = String(payload.idempotency_key || payload.key || "");
      sendJson(res, 200, await service.reconcileOrder(key));
      return;
    }
    if (req.method === "POST" && parsedUrl.pathname === "/execution-guard-snapshot") {
      const payload = await readJsonBody(req);
      sendJson(res, 200, await service.executionGuardSnapshot(payload));
      return;
    }
    if (req.method === "POST" && parsedUrl.pathname === "/dynamic-three-leg-snapshot") {
      const payload = await readJsonBody(req);
      sendJson(res, 200, await service.dynamicThreeLegSnapshot(payload));
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

const invokedAsMain = Boolean(
  process.argv[1]
  && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))
);
if (invokedAsMain) {
  main().catch((err) => {
    console.error(`[${SERVICE_NAME}] fatal ${redactErrorText(err?.stack || err?.message || String(err))}`);
    process.exit(1);
  });
}
