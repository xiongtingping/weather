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
import { AssetType, ClobClient, Side, OrderType, createL2Headers, getContractConfig } from "@polymarket/clob-client-v2";
import { Wallet } from "ethers";
import { DatabaseSync } from "node:sqlite";
import { hashTypedData } from "viem";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DATA_DIR = process.env.PM_DATA_DIR || path.join(ROOT, "data");
const DB_PATH = process.env.PM_ARCHIVE_DB || path.join(DATA_DIR, "archive.db");
const LIVE_PAUSE_FILE = process.env.PM_LIVE_PAUSE_FILE || path.join(DATA_DIR, "live_paused.flag");
const HELD_YES_INVENTORY_FILE = process.env.PM_HELD_YES_INVENTORY_FILE
  || path.join(DATA_DIR, "reported_trajectory_held_yes_tokens.json");
const HELD_YES_WS_MANIFEST_FILE = process.env.PM_HELD_YES_WS_MANIFEST_PATH
  || path.join(DATA_DIR, "market_ws_held_yes_quotes.json");
const HELD_YES_WS_SHARD_DIR = process.env.PM_HELD_YES_WS_SHARD_DIR
  || path.join(DATA_DIR, "market_ws_held_yes_quote_shards");
const HELD_YES_WS_MANIFEST_SCHEMA = "clob_ws_held_yes_quote_manifest_v1";
const HELD_YES_WS_TOKEN_SCHEMA = "clob_ws_held_yes_token_quote_snapshot_v1";
const SERVICE_NAME = "clob_order_service";
const ASOS_ARGMAX_LIVE_STRATEGY_ID = "asos_argmax_new_high_yes_v1";
const ASOS_ARGMAX_EXIT_STRATEGY_ID = "asos_argmax_yes_exit_099_v1";
const ASOS_ARGMAX_ROTATION_SELL_STRATEGY_ID = "asos_argmax_rotation_sell_fak_v1";
const ASOS_ARGMAX_LIVE_STATIONS = new Set([
  "KATL", "KAUS", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO",
]);
const RETIRED_STRATEGY_IDS = new Set([
  "weather_copy_follow_v1",
]);
const IDEMPOTENCY_TERMINAL_STATES = new Set(["exchange_accepted", "submit_failed"]);
const KNOWN_EXCHANGE_ORDER_STATUSES = new Set([
  "matched", "filled", "executed", "partial", "partially_filled",
  "live", "open", "submitted", "accepted",
  "cancelled", "canceled", "expired", "rejected", "failed",
]);
const UNCONFIRMED_EXCHANGE_ORDER_STATUSES = new Set([
  "", "unknown", "submitted_unconfirmed", "pending", "processing",
]);

function normalizedExchangeOrderStatus(value) {
  return String(value || "").trim().toLowerCase();
}

function unwrapProxyLookupResponse(value) {
  // requestJsonViaProxy wraps a JSON `null` response as { response: null }.
  // Preserve that as an empty response so reconciliation cannot mistake the
  // wrapper itself for an order object.
  if (
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.keys(value).length === 1
    && Object.prototype.hasOwnProperty.call(value, "response")
  ) {
    return value.response;
  }
  return value;
}

function exchangeOrderNeedsReconciliation(response = {}) {
  const nested = response?.exchange_response && typeof response.exchange_response === "object"
    ? response.exchange_response
    : response;
  const status = normalizedExchangeOrderStatus(
    response?.status || nested?.status,
  );
  return Boolean(response?.reconcile_required)
    || UNCONFIRMED_EXCHANGE_ORDER_STATUSES.has(status)
    || !KNOWN_EXCHANGE_ORDER_STATUSES.has(status);
}

function hasRecoveredExactExchangeIdentity(response = {}) {
  const orderID = String(response?.orderID || response?.order_id || "").trim().toLowerCase();
  const expectedOrderID = String(response?.expected_order_id || "").trim().toLowerCase();
  return Boolean(orderID)
    && Boolean(expectedOrderID)
    && orderID === expectedOrderID
    && response?.expected_order_id_matches === true
    && response?.reconciled_from_exchange === true
    && response?.reconcile_required === false;
}

function exchangeOrderReconcileKind(order = {}) {
  const status = normalizedExchangeOrderStatus(order?.status);
  const matchedRaw = order?.size_matched ?? order?.sizeMatched ?? order?.matched_size;
  const originalRaw = order?.original_size ?? order?.originalSize;
  const matched = Number(matchedRaw);
  const original = Number(originalRaw);
  const hasMatched = Number.isFinite(matched);
  const hasOriginal = Number.isFinite(original);
  const fillStatuses = new Set(["matched", "filled", "executed", "partial", "partially_filled"]);
  const closedWithoutFillStatuses = new Set(["cancelled", "canceled", "expired", "rejected", "failed"]);

  if (
    UNCONFIRMED_EXCHANGE_ORDER_STATUSES.has(status)
    || !KNOWN_EXCHANGE_ORDER_STATUSES.has(status)
  ) {
    return { kind: "reconcile_required", status, matchedRaw, originalRaw };
  }
  if (fillStatuses.has(status)) {
    if (!hasMatched && !hasOriginal) {
      return { kind: "reconcile_required", status, matchedRaw, originalRaw };
    }
    if (hasMatched && hasOriginal && matched < original - 1e-9) {
      return { kind: "reconcile_required", status, matchedRaw, originalRaw };
    }
    return { kind: "exchange_accepted", status, matchedRaw, originalRaw };
  }
  if (closedWithoutFillStatuses.has(status)) {
    if (!hasMatched || matched > 1e-9) {
      return { kind: "reconcile_required", status, matchedRaw, originalRaw };
    }
    return { kind: "submit_failed", status, matchedRaw, originalRaw };
  }
  // LIVE/OPEN/SUBMITTED/ACCEPTED are known exchange states. They must not be
  // replayed, even though they are not fills yet; the open-order reconciler
  // owns the remaining lifecycle.
  return { kind: "exchange_accepted", status, matchedRaw, originalRaw };
}

export function livePausePresent(pausePath = LIVE_PAUSE_FILE) {
  try {
    return fs.existsSync(pausePath);
  } catch {
    // An unreadable pause boundary must fail closed.
    return true;
  }
}

export function latestContiguousArgmaxPeriods(rawPeriods) {
  if (!Array.isArray(rawPeriods)) return [];
  const valid = rawPeriods
    .map((period) => ({
      period,
      start: Date.parse(String(period?.start_utc || "")),
      end: Date.parse(String(period?.end_utc || "")),
    }))
    .filter(({ start, end }) => Number.isFinite(start) && Number.isFinite(end) && end > start)
    .sort((left, right) => left.start - right.start || left.end - right.end);
  if (!valid.length) return [];
  const clusters = [];
  for (const item of valid) {
    const current = clusters.at(-1);
    if (!current || item.start > current.end) {
      clusters.push({ start: item.start, end: item.end, items: [item] });
    } else {
      current.end = Math.max(current.end, item.end);
      current.items.push(item);
    }
  }
  const selected = clusters.reduce((latest, cluster) => (
    !latest || cluster.end > latest.end
      || (cluster.end === latest.end && cluster.start > latest.start)
      ? cluster : latest
  ), null);
  return selected ? selected.items.map(({ period }) => period) : [];
}

export function asosArgmaxExitAdmission(payload, dbPath = DB_PATH) {
  if (!truthy(env("PM_ASOS_ARGMAX_LIVE_ENABLED", "0"), false)) {
    return { ok: false, reason: "asos_argmax_live_not_enabled" };
  }
  if (String(payload?.strategy_id || "") !== ASOS_ARGMAX_EXIT_STRATEGY_ID) {
    return { ok: false, reason: "not_asos_argmax_exit_strategy" };
  }
  const exitID = String(payload?.order_intent_id || "");
  const buyID = String(payload?.buy_order_intent_id || "");
  const entryID = String(payload?.entry_fact_id || "");
  const tokenID = String(payload?.token_id || "");
  if (!exitID || !buyID || !entryID || !/^[0-9]+$/.test(tokenID)) {
    return { ok: false, reason: "asos_argmax_exit_identity_invalid" };
  }
  let db;
  try {
    db = new DatabaseSync(dbPath, { readOnly: true });
    const row = db.prepare(`
      SELECT x.strategy_id,x.station_id,x.event_date,x.yes_token,x.side,x.order_type,
             x.limit_price,x.size,x.source_buy_size_matched,x.idempotency_key,
             x.guard_snapshot_sha256,x.state,x.buy_order_intent_id,x.entry_fact_id,
             b.state AS buy_state,b.size_matched AS buy_size_matched,b.yes_token AS buy_yes_token
        FROM reported_trajectory_asos_argmax_exit_order_intents x
        JOIN reported_trajectory_asos_argmax_order_intents b
          ON b.order_intent_id=x.buy_order_intent_id
       WHERE x.exit_order_intent_id=? AND x.buy_order_intent_id=? AND x.entry_fact_id=?
    `).get(exitID, buyID, entryID);
    if (!row) return { ok: false, reason: "asos_argmax_exit_intent_missing" };
    const exact = (left, right) => Math.abs(Number(left) - Number(right)) <= 1e-8;
    if (
      row.strategy_id !== ASOS_ARGMAX_EXIT_STRATEGY_ID
      || !ASOS_ARGMAX_LIVE_STATIONS.has(String(row.station_id || ""))
      || row.state !== "SUBMITTING" || row.buy_state !== "EXCHANGE_ACCEPTED"
      || row.yes_token !== tokenID || row.buy_yes_token !== tokenID
      || row.side !== "SELL" || row.order_type !== "GTC"
      || String(payload?.side || "").toUpperCase() !== "SELL"
      || String(payload?.order_type || "").toUpperCase() !== "GTC"
      || payload?.allow_market !== false || payload?.post_only !== false
      || String(payload?.idempotency_key || "") !== row.idempotency_key
      || String(payload?.guard_snapshot_sha256 || "") !== row.guard_snapshot_sha256
      || String(payload?.buy_order_intent_id || "") !== row.buy_order_intent_id
      || String(payload?.entry_fact_id || "") !== row.entry_fact_id
      || !exact(payload?.price, 0.99) || !exact(payload?.limit_price, 0.99)
      || !exact(row.limit_price, 0.99)
      || !exact(payload?.size, row.size)
      || !exact(row.size, row.source_buy_size_matched)
      || !exact(row.size, row.buy_size_matched)
      || Number(row.size) <= 0 || !exact(payload?.notional, 0)
    ) {
      return { ok: false, reason: "asos_argmax_exit_intent_mismatch" };
    }
    const market = db.prepare(`
      SELECT 1 AS ok FROM reported_trajectory_market_registry
       WHERE station_id=? AND event_date=? AND yes_token=?
         AND source_lane='awc' AND source_eligible=1
         AND market_active=1 AND market_accepting_orders=1
       LIMIT 1
    `).get(row.station_id, row.event_date, tokenID);
    if (!market) return { ok: false, reason: "asos_argmax_exit_market_not_active" };
    return {
      ok: true,
      reason: "asos_argmax_exit_exact_filled_shares",
      station_id: row.station_id,
    };
  } catch (err) {
    return { ok: false, reason: `asos_argmax_exit_admission_error:${redactErrorText(err?.message || String(err || ""))}` };
  } finally {
    try { db?.close(); } catch {}
  }
}

export function asosArgmaxRotationSellAdmission(payload, dbPath = DB_PATH) {
  if (!truthy(env("PM_ASOS_ARGMAX_LIVE_ENABLED", "0"), false)) {
    return { ok: false, reason: "asos_argmax_live_not_enabled" };
  }
  if (String(payload?.strategy_id || "") !== ASOS_ARGMAX_ROTATION_SELL_STRATEGY_ID) {
    return { ok: false, reason: "not_asos_argmax_rotation_sell_strategy" };
  }
  const rotationID = String(payload?.order_intent_id || "");
  const targetEntryID = String(payload?.target_entry_fact_id || "");
  const sourceBuyID = String(payload?.source_buy_order_intent_id || "");
  const oldToken = String(payload?.token_id || "");
  const newToken = String(payload?.new_yes_token || "");
  if (
    !rotationID || !targetEntryID || !sourceBuyID
    || !/^[0-9]+$/.test(oldToken) || !/^[0-9]+$/.test(newToken)
    || oldToken === newToken
  ) {
    return { ok: false, reason: "asos_argmax_rotation_sell_identity_invalid" };
  }
  let db;
  try {
    db = new DatabaseSync(dbPath, { readOnly: true });
    const row = db.prepare(`
      SELECT r.strategy_id,r.station_id,r.event_date,r.old_yes_token,r.new_yes_token,
             r.side,r.order_type,r.limit_price,r.size,r.ws_snapshot_sha256,
             r.guard_snapshot_sha256,r.state,r.idempotency_key,
             r.target_entry_fact_id,r.source_buy_order_intent_id,
             b.state AS buy_state,b.yes_token AS buy_yes_token,
             b.size_matched AS buy_size_matched,
             f.station_id AS target_station_id,f.event_date AS target_event_date,
             f.yes_token AS target_yes_token
        FROM reported_trajectory_asos_argmax_rotation_sell_intents r
        JOIN reported_trajectory_asos_argmax_order_intents b
          ON b.order_intent_id=r.source_buy_order_intent_id
        JOIN reported_trajectory_asos_argmax_entry_facts f
          ON f.entry_fact_id=r.target_entry_fact_id
       WHERE r.rotation_sell_intent_id=?
         AND r.target_entry_fact_id=?
         AND r.source_buy_order_intent_id=?
    `).get(rotationID, targetEntryID, sourceBuyID);
    if (!row) return { ok: false, reason: "asos_argmax_rotation_sell_intent_missing" };
    const exact = (left, right) => Math.abs(Number(left) - Number(right)) <= 1e-8;
    if (
      row.strategy_id !== ASOS_ARGMAX_ROTATION_SELL_STRATEGY_ID
      || !ASOS_ARGMAX_LIVE_STATIONS.has(String(row.station_id || ""))
      || row.state !== "SUBMITTING" || row.buy_state !== "EXCHANGE_ACCEPTED"
      || row.station_id !== row.target_station_id || row.event_date !== row.target_event_date
      || row.old_yes_token !== oldToken || row.buy_yes_token !== oldToken
      || row.new_yes_token !== newToken || row.target_yes_token !== newToken
      || row.old_yes_token === row.new_yes_token
      || row.side !== "SELL" || row.order_type !== "FAK"
      || String(payload?.side || "").toUpperCase() !== "SELL"
      || String(payload?.order_type || "").toUpperCase() !== "FAK"
      || payload?.allow_market !== true || payload?.post_only !== false
      || String(payload?.idempotency_key || "") !== row.idempotency_key
      || String(payload?.ws_snapshot_sha256 || "") !== row.ws_snapshot_sha256
      || String(payload?.guard_snapshot_sha256 || "") !== row.guard_snapshot_sha256
      || !exact(payload?.price, row.limit_price)
      || !exact(payload?.limit_price, row.limit_price)
      || !exact(payload?.size, row.size)
      || Number(row.size) <= 0 || Number(row.size) > Number(row.buy_size_matched) + 1e-8
      || Number(row.limit_price) <= 0 || Number(row.limit_price) >= 1
      || !exact(payload?.notional, 0)
    ) {
      return { ok: false, reason: "asos_argmax_rotation_sell_intent_mismatch" };
    }
    const oldMarket = db.prepare(`
      SELECT 1 AS ok FROM reported_trajectory_market_registry
       WHERE station_id=? AND event_date=? AND yes_token=?
         AND source_lane='awc' AND source_eligible=1
         AND market_active=1 AND market_accepting_orders=1
       LIMIT 1
    `).get(row.station_id, row.event_date, oldToken);
    const newMarket = db.prepare(`
      SELECT 1 AS ok FROM reported_trajectory_market_registry
       WHERE station_id=? AND event_date=? AND yes_token=?
         AND source_lane='awc' AND source_eligible=1
         AND market_active=1 AND market_accepting_orders=1
       LIMIT 1
    `).get(row.station_id, row.event_date, newToken);
    if (!oldMarket || !newMarket) {
      return { ok: false, reason: "asos_argmax_rotation_sell_market_not_active" };
    }
    return {
      ok: true,
      reason: "asos_argmax_rotation_sell_exact_remaining_shares",
      station_id: row.station_id,
    };
  } catch (err) {
    return { ok: false, reason: `asos_argmax_rotation_sell_admission_error:${redactErrorText(err?.message || String(err || ""))}` };
  } finally {
    try { db?.close(); } catch {}
  }
}

export function asosArgmaxLiveAdmission(payload, dbPath = DB_PATH, nowMs = Date.now()) {
  if (!truthy(env("PM_ASOS_ARGMAX_LIVE_ENABLED", "0"), false)) {
    return { ok: false, reason: "asos_argmax_live_not_enabled" };
  }
  const strategyID = String(payload?.strategy_id || "");
  if (strategyID === ASOS_ARGMAX_EXIT_STRATEGY_ID) {
    return asosArgmaxExitAdmission(payload, dbPath);
  }
  if (strategyID === ASOS_ARGMAX_ROTATION_SELL_STRATEGY_ID) {
    return asosArgmaxRotationSellAdmission(payload, dbPath);
  }
  if (strategyID !== ASOS_ARGMAX_LIVE_STRATEGY_ID) {
    return { ok: false, reason: "not_asos_argmax_live_strategy" };
  }
  const intentID = String(payload?.order_intent_id || "");
  const entryFactID = String(payload?.entry_fact_id || "");
  const tokenID = String(payload?.token_id || "");
  if (!intentID || !entryFactID || !/^[0-9]+$/.test(tokenID)) {
    return { ok: false, reason: "asos_argmax_live_identity_invalid" };
  }
  let db;
  try {
    db = new DatabaseSync(dbPath, { readOnly: true });
    const row = db.prepare(`
      SELECT i.strategy_id,i.station_id,i.event_date,i.yes_token,i.side,i.order_type,
             i.limit_price,i.size,i.requested_notional,i.idempotency_key,
             i.ws_snapshot_sha256,i.guard_snapshot_sha256,i.state,
             f.observed_at_utc,f.received_at_utc,f.reason,w.argmax_periods_json
        FROM reported_trajectory_asos_argmax_order_intents i
        JOIN reported_trajectory_asos_argmax_entry_facts f
          ON f.entry_fact_id=i.entry_fact_id
        JOIN reported_trajectory_asos_argmax_windows w
          ON w.window_id=f.window_id
       WHERE i.order_intent_id=? AND i.entry_fact_id=?
    `).get(intentID, entryFactID);
    if (!row) return { ok: false, reason: "asos_argmax_live_intent_missing" };
    const exact = (left, right) => Math.abs(Number(left) - Number(right)) <= 1e-8;
    if (
      row.strategy_id !== ASOS_ARGMAX_LIVE_STRATEGY_ID
      || !ASOS_ARGMAX_LIVE_STATIONS.has(String(row.station_id || ""))
      || row.state !== "SUBMITTING"
      || row.reason !== "ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD"
      || row.yes_token !== tokenID
      || row.side !== "BUY" || row.order_type !== "FOK"
      || String(payload?.side || "").toUpperCase() !== "BUY"
      || String(payload?.order_type || "").toUpperCase() !== "FOK"
      || payload?.allow_market !== true || payload?.post_only !== false
      || String(payload?.idempotency_key || "") !== row.idempotency_key
      || String(payload?.ws_snapshot_sha256 || "") !== row.ws_snapshot_sha256
      || String(payload?.guard_snapshot_sha256 || "") !== row.guard_snapshot_sha256
      || !exact(payload?.price, row.limit_price)
      || !exact(payload?.size, row.size)
      || !exact(payload?.notional, row.requested_notional)
      || Number(row.requested_notional) <= 0 || Number(row.requested_notional) > 2.0
      || Number(row.limit_price) <= 0 || Number(row.limit_price) >= 1
      || Number(row.size) <= 0
    ) {
      return { ok: false, reason: "asos_argmax_live_intent_mismatch" };
    }
    const observed = Date.parse(String(row.observed_at_utc || ""));
    const received = Date.parse(String(row.received_at_utc || ""));
    let periods = [];
    try {
      periods = JSON.parse(String(row.argmax_periods_json || "[]"));
    } catch {}
    periods = latestContiguousArgmaxPeriods(periods);
    const exactPeriodActive = (
      Number.isFinite(observed)
      && Number.isFinite(received)
      && Number.isFinite(Number(nowMs))
      && observed <= received
      && Array.isArray(periods)
      && periods.some((period) => {
        const start = Date.parse(String(period?.start_utc || ""));
        const end = Date.parse(String(period?.end_utc || ""));
        return Number.isFinite(start) && Number.isFinite(end) && end > start
          && observed >= start && observed < end
          && received >= start && received < end
          && Number(nowMs) >= start && Number(nowMs) < end;
      })
    );
    if (!exactPeriodActive) {
      return { ok: false, reason: "asos_argmax_live_period_not_active" };
    }
    const market = db.prepare(`
      SELECT 1 AS ok FROM reported_trajectory_market_registry
       WHERE station_id=? AND event_date=? AND yes_token=?
         AND source_lane='awc' AND source_eligible=1
         AND market_active=1 AND market_accepting_orders=1
       LIMIT 1
    `).get(row.station_id, row.event_date, tokenID);
    if (!market) return { ok: false, reason: "asos_argmax_live_market_not_active" };
    const activeStates = "'PREPARED','SUBMITTING','EXCHANGE_ACCEPTED','RECONCILE_REQUIRED'";
    const total = Number(db.prepare(`
      SELECT COALESCE(SUM(requested_notional),0) AS used
        FROM reported_trajectory_asos_argmax_order_intents WHERE state IN (${activeStates})
    `).get()?.used || 0);
    const station = Number(db.prepare(`
      SELECT COALESCE(SUM(requested_notional),0) AS used
        FROM reported_trajectory_asos_argmax_order_intents
       WHERE station_id=? AND state IN (${activeStates})
    `).get(row.station_id)?.used || 0);
    if (total > 20.0 + 1e-8 || station > 2.0 + 1e-8) {
      return { ok: false, reason: "asos_argmax_live_budget_invalid" };
    }
    return { ok: true, reason: "asos_argmax_live_exact_intent", station_id: row.station_id };
  } catch (err) {
    return { ok: false, reason: `asos_argmax_live_admission_error:${redactErrorText(err?.message || String(err || ""))}` };
  } finally {
    try { db?.close(); } catch {}
  }
}

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

function alignMarketBuySizeToMakerCents(price, size) {
  const requested = Number(size);
  const limit = Number(price);
  if (!Number.isFinite(requested) || requested <= 0 || !Number.isFinite(limit) || limit <= 0) {
    return requested;
  }
  // Polymarket market BUY orders encode maker amount in USDC cents while the
  // share/taker amount may use up to four decimals.  A raw size such as
  // 84.62 at 0.15 creates 12.693 USDC and is rejected before the exchange
  // POST.  Round only downward to the largest four-decimal share size whose
  // limit-price maker amount is cent-aligned; this never increases exposure.
  let candidate = Math.floor((requested + 1e-9) * 10000) / 10000;
  for (let i = 0; i < 100000; i += 1) {
    const makerCents = limit * candidate * 100;
    if (Math.abs(makerCents - Math.round(makerCents)) <= 1e-7) {
      return Number(candidate.toFixed(4));
    }
    candidate = Math.round((candidate - 0.0001) * 10000) / 10000;
    if (candidate <= 0) break;
  }
  throw new Error("market_buy_maker_amount_cent_alignment_unavailable");
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

function proxyClobReachable(proxyUrl) {
  try {
    execFileSync("/usr/bin/curl", [
      "-fsS", "--max-time", "4", "--proxy", String(proxyUrl),
      "https://clob.polymarket.com/time",
    ], { encoding: "utf8", timeout: 5000, stdio: ["ignore", "ignore", "ignore"] });
    return true;
  } catch {
    return false;
  }
}

/**
 * Attest the exchange-visible region of an egress route.
 *
 * Reachability alone proves nothing about admission: the public CLOB reads
 * answer 200 from a restricted region and only the signed order POST is
 * rejected, so a route selected on reachability can look healthy for days
 * while every order comes back 403 `geoblocked`.  This asks the exchange
 * directly and is deliberately three-valued -- an unreachable or unparseable
 * probe returns `blocked: null` so a flaky probe can never close the lane on
 * its own.
 */
export function probeRegion(proxyUrl = "") {
  const args = ["-fsS", "--max-time", "6"];
  if (proxyUrl) args.push("--proxy", String(proxyUrl));
  args.push("https://polymarket.com/api/geoblock");
  let raw = "";
  try {
    raw = execFileSync("/usr/bin/curl", args, {
      encoding: "utf8", timeout: 8000, stdio: ["ignore", "pipe", "ignore"],
    });
  } catch (err) {
    return { blocked: null, error: `region_probe_failed:${String(err?.code || err?.message || "error")}` };
  }
  let parsed;
  try {
    parsed = JSON.parse(String(raw || ""));
  } catch {
    return { blocked: null, error: "region_probe_unparseable" };
  }
  if (typeof parsed?.blocked !== "boolean") {
    return { blocked: null, error: "region_probe_missing_blocked" };
  }
  return {
    blocked: parsed.blocked,
    country: String(parsed.country || ""),
    region: String(parsed.region || ""),
    error: "",
  };
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
      if (!(await proxyReachable(host, port)) || !proxyClobReachable(proxy)) {
        proxy = "";
      }
    }
  }
  if (!proxy) {
    const ports = [
      ...parsePortList(env("PM_PROXY_PORTS", "")),
      ...scutilProxyPorts(),
      ...parsePortList(env("PM_PROXY_LOCAL_FALLBACK_PORTS", "1082")),
      1082,
    ].filter((port, idx, arr) => arr.indexOf(port) === idx);
    const reachable = [];
    for (const port of ports) {
      const candidate = `http://127.0.0.1:${port}`;
      if (await proxyReachable("127.0.0.1", port) && proxyClobReachable(candidate)) {
        reachable.push(candidate);
      }
    }
    // Reachability decides nothing about admission, so prefer the first route
    // the exchange itself says it will accept orders from.  A reachable but
    // blocked route is still better than none -- the read-only evidence paths
    // depend on it -- but it is never chosen over an admitting one.
    proxy = reachable.find((candidate) => probeRegion(candidate).blocked === false)
      || reachable[0]
      || "";
  }
  if (proxy) {
    process.env.HTTP_PROXY = proxy;
    process.env.HTTPS_PROXY = proxy;
    process.env.GLOBAL_AGENT_HTTP_PROXY = proxy;
    bootstrap();
    return { mode: "proxy", proxy, region: probeRegion(proxy) };
  }
  if (forceProxy) {
    return { mode: "proxy_required_missing", forced_proxy: true, error: "proxy_unavailable" };
  }
  return { mode: "direct_no_proxy", region: probeRegion("") };
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

function signedOrderPayload(signedOrder, owner, orderType, postOnly = false, deferExec = false) {
  const order = {
    salt: parseInt(signedOrder.salt, 10),
    maker: signedOrder.maker,
    signer: signedOrder.signer,
    taker: signedOrder.taker,
    tokenId: signedOrder.tokenId,
    makerAmount: signedOrder.makerAmount,
    takerAmount: signedOrder.takerAmount,
    side: signedOrder.side,
    signatureType: signedOrder.signatureType,
    expiration: signedOrder.expiration,
    signature: signedOrder.signature,
  };
  if (Object.prototype.hasOwnProperty.call(signedOrder, "timestamp")) {
    order.timestamp = signedOrder.timestamp;
    order.metadata = signedOrder.metadata;
    order.builder = signedOrder.builder;
  } else {
    order.nonce = signedOrder.nonce;
    order.feeRateBps = signedOrder.feeRateBps;
  }
  return { deferExec, postOnly, order, owner, orderType };
}

function execFilePromise(file, args, options = {}) {
  return new Promise((resolve, reject) => {
    execFile(file, args, options, (err, stdout, stderr) => {
      if (err) {
        err.stdout = stdout;
        err.stderr = stderr;
        reject(err);
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

function balanceEvidenceValid(value) {
  if (!value || typeof value !== "object" || value.error) return false;
  const balance = String(value.balance ?? "");
  const directAllowance = String(value.allowance ?? "");
  const allowanceMap = value.allowances && typeof value.allowances === "object"
    ? Object.values(value.allowances).map((item) => String(item ?? ""))
    : [];
  return /^\d+$/.test(balance)
    && (/^\d+$/.test(directAllowance) || allowanceMap.some((item) => /^\d+$/.test(item)));
}

function conditionalBalanceIsZero(value) {
  return balanceEvidenceValid(value) && /^0+$/.test(String(value.balance ?? ""));
}

export function targetDirectEntryHardDeadline(payload, nowMs = Date.now()) {
  // Every target-owned direct entry carries one explicit deadline.  It is a
  // gateway admission fact, not a probability or strategy compatibility
  // envelope.  Missing or malformed target evidence must fail closed.
  const isDirectEntry = Boolean(payload?.direct_target_order_context)
    && !Boolean(payload?.direct_target_exit_order)
    && String(payload?.side || "BUY").toUpperCase() !== "SELL";
  if (!isDirectEntry) return { required: false, ok: true };
  const rawDeadline = payload?.target_execution_hard_deadline_at;
  const deadlineMs = Date.parse(String(rawDeadline || ""));
  if (!Number.isFinite(deadlineMs)) {
    return {
      required: true,
      ok: false,
      reason: "target_execution_hard_deadline_missing_or_invalid",
      deadline_at: String(rawDeadline || "") || null,
    };
  }
  if (nowMs >= deadlineMs) {
    return {
      required: true,
      ok: false,
      reason: "target_execution_hard_deadline_expired",
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
    this.region = { blocked: null, error: "region_not_probed" };
    this.regionObservedAt = "";
    this.client = null;
    this.signer = null;
    this.apiCreds = null;
    this.signatureType = 0;
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
    this.balancePrefetchInflight = 0;
    this.cachedCollateral = null;
    this.cachedConditional = new Map();
    this.registeredPositionTokens = new Set();
    this.conditionalPrefetchErrors = new Map();
    this.lastBalancePrefetchError = "";
    // Congestion guard: CLOB 429s previously opened hundreds of connections
    // through the shared local VPN proxy and starved Codex. Keep the
    // dedupe, bounded queue, and cooldown even when tuning for lower latency.
    this.balanceFetchInflightByToken = new Map();
    this.balanceFetchActive = 0;
    this.balanceFetchQueue = [];
    this.balanceRateLimitUntilMs = 0;
    this.balanceRateLimitStreak = 0;
    this.lastBalanceRateLimitAt = null;
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
    this.recordRegion(this.proxy?.region);
    const keepAliveSockets = Math.max(2, Math.floor(positiveNumber(env("PM_CLOB_ORDER_SERVICE_MAX_SOCKETS", "8"), 8)));
    axios.defaults.httpAgent = new HttpAgent({ keepAlive: true, maxSockets: keepAliveSockets });
    axios.defaults.httpsAgent = new HttpsAgent({ keepAlive: true, maxSockets: keepAliveSockets });
    axios.defaults.timeout = Math.max(
      5000, Math.round(positiveNumber(env("PM_CLOB_TIMEOUT_MS", "5000"), 5000)),
    );
    this.client = this.buildClient();
    // The current CLOB API advertises order schema v2.  Keep the signing hot
    // path independent from the proxy-sensitive /version read; the exchange
    // still remains the authority when the signed order is submitted.
    this.client.cachedVersion = 2;
    this.prewarmMetadataFromDb();
    this.prewarmClobHttp("start").catch(() => {});
    setTimeout(() => this.prewarmClobHttp("start_delayed").catch(() => {}), 1000).unref?.();
    setTimeout(() => this.prewarmOrderSigner("start_delayed").catch(() => {}), 1500).unref?.();
    this.startHeartbeat();
    this.refreshRegisteredPositionTokensFromInventory();
    // Do not continuously poll every held YES token.  The CLOB balance
    // endpoint is authenticated and rate-limited; a background fan-out here
    // competes with the latency-critical order POST and can make a healthy
    // gateway time out before exchange_post_started.  SELL/NO safety still
    // performs one exact-token read at event time when its cache is stale.
    if (truthy(env("PM_CLOB_BALANCE_PREFETCH_ENABLED", "0"), false)) {
      this.startBalancePrefetch();
    }
    this.startProxyRecoveryWatch();
    this.startRegionWatch();
    this.readyAt = new Date();
  }

  /**
   * Recover from a boot that raced the local proxy.
   *
   * `configureProxy()` runs exactly once in `_start()`, so a service that
   * booted while the local proxy port was still down stays pinned at
   * `proxy_required_missing` forever, and `placeOrder` rejects every request
   * with `proxy_required_unavailable` -- including the ASOS argmax side lane,
   * whose read-only/live-pause exemptions do not cover the proxy gate.
   *
   * Recovery deliberately re-execs the process rather than re-running
   * `configureProxy()` in place.  The global-agent `bootstrap()` inside that
   * function only affects agents constructed after it runs, so an in-process
   * retry would have to rebuild the axios agents and the CLOB client in the
   * exact original order; a clean launchd restart replays the one startup path
   * that is already proven.  This is safe precisely because the service cannot
   * have submitted anything in this state: there are no in-flight orders to
   * strand, and the WS snapshot plus balance caches rebuild on boot.
   *
   * The watch is a no-op unless the proxy is both required and currently
   * missing, and it only exits once a probe confirms the proxy is reachable
   * again -- a permanently unavailable proxy therefore cannot cause a restart
   * loop.
   */
  startProxyRecoveryWatch({ probe = configureProxy, exit = process.exit } = {}) {
    if (!truthy(env("PM_CLOB_FORCE_PROXY", "0"))) return null;
    if (this.proxy?.mode === "proxy") return null;
    const intervalMs = Math.max(5000, Math.floor(positiveNumber(
      env("PM_CLOB_PROXY_RECOVERY_INTERVAL_MS", "15000"), 15000,
    )));
    const timer = setInterval(async () => {
      if (this.proxy?.mode === "proxy") {
        clearInterval(timer);
        return;
      }
      let recovered = null;
      try {
        recovered = await probe();
      } catch {
        return;
      }
      if (recovered?.mode !== "proxy") return;
      clearInterval(timer);
      console.error(
        `[${SERVICE_NAME}] proxy recovered (${recovered.proxy}); exiting for a clean`
        + " launchd restart so the CLOB transport is rebuilt in startup order",
      );
      exit(0);
    }, intervalMs);
    timer.unref?.();
    return timer;
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
    this.signer = signer;
    this.apiCreds = userApiCreds;
    this.signatureType = Number(env("PM_SIGNATURE_TYPE", env("SIGNATURE_TYPE", "0")));
    return new ClobClient({
      host,
      chain: chainId,
      signer,
      creds: userApiCreds,
      signatureType: this.signatureType,
      funderAddress: env("PM_FUNDER_ADDRESS", signer.address),
      builderConfig: builderCode ? { builderCode } : undefined,
      retryOnError: truthy(env("PM_CLOB_PLACE_ORDER_SDK_RETRY_ON_ERROR", "0")),
    });
  }

  async requestJsonViaProxy(method, requestPath, bodyValue = undefined, query = undefined) {
    const proxy = String(this.proxy?.proxy || "").trim();
    if (!proxy) throw new Error("clob_proxy_request_proxy_missing");
    const hasBody = bodyValue !== undefined;
    const body = hasBody ? JSON.stringify(bodyValue) : "";
    const headers = await createL2Headers(this.signer, this.apiCreds, {
      method: String(method).toUpperCase(),
      requestPath,
      ...(hasBody ? { body } : {}),
    });
    const host = env("PM_CLOB_HOST", "https://clob.polymarket.com").replace(/\/+$/, "");
    const url = new URL(`${host}${requestPath}`);
    for (const [key, value] of Object.entries(query || {})) {
      if (value !== undefined && value !== null && String(value) !== "") {
        url.searchParams.set(key, String(value));
      }
    }
    const args = [
      "--silent",
      "--show-error",
      "--http1.1",
      "--proxy",
      proxy,
      "--connect-timeout",
      "5",
      "--max-time",
      String(Math.max(5, Math.ceil(Number(axios.defaults.timeout || 15000) / 1000))),
      "--request",
      String(method).toUpperCase(),
      url.toString(),
      "--header",
      "Content-Type: application/json",
    ];
    // SakuraCat may leave an authenticated CLOB GET connection open without
    // returning the response when curl reuses keep-alive through the proxy.
    // Force a bounded fresh connection for read-only authenticated probes;
    // order submission has its own path and is intentionally unchanged.
    if (String(method).toUpperCase() === "GET") args.push("--no-keepalive");
    for (const [key, value] of Object.entries(headers || {})) {
      if (value === undefined || value === null || String(value) === "") continue;
      args.push("--header", `${key}: ${String(value)}`);
    }
    if (hasBody) args.push("--data-binary", body);
    args.push("--write-out", "\n__CLOB_HTTP_STATUS__:%{http_code}");
    let result;
    try {
      result = await execFilePromise("/usr/bin/curl", args, {
        encoding: "utf8",
        maxBuffer: 1024 * 1024,
        timeout: Math.max(5000, Math.ceil(Number(axios.defaults.timeout || 15000)) + 1000),
      });
    } catch (err) {
      const detail = redactErrorText(err?.stderr || err?.message || String(err || ""));
      throw new Error(`clob_proxy_request_failed:${detail}`);
    }
    const marker = "\n__CLOB_HTTP_STATUS__:";
    const raw = String(result.stdout || "");
    const markerIndex = raw.lastIndexOf(marker);
    if (markerIndex < 0) throw new Error("clob_proxy_request_http_status_missing");
    const responseText = raw.slice(0, markerIndex).trim();
    const status = Number(raw.slice(markerIndex + marker.length).trim());
    let response = {};
    try {
      response = responseText ? JSON.parse(responseText) : {};
    } catch {
      response = { error: responseText };
    }
    if (!response || typeof response !== "object" || Array.isArray(response)) {
      response = { response };
    }
    if (Number.isFinite(status) && status >= 400) {
      const detail = response?.error || response?.errorMsg || `http_${status}`;
      throw new Error(`clob_proxy_http_${status}:${redactErrorText(detail)}`);
    }
    return response;
  }

  async postOrderViaProxy(signedOrder, orderType, postOnly, deferExec) {
    const proxy = String(this.proxy?.proxy || "").trim();
    if (!proxy) throw new Error("clob_proxy_submit_proxy_missing");
    const orderPayload = signedOrderPayload(
      signedOrder,
      this.apiCreds?.key || "",
      orderType,
      postOnly,
      deferExec,
    );
    const body = JSON.stringify(orderPayload);
    const headers = await createL2Headers(this.signer, this.apiCreds, {
      method: "POST",
      requestPath: "/order",
      body,
    });
    const args = [
      "--silent",
      "--show-error",
      "--http1.1",
      "--proxy",
      proxy,
      "--connect-timeout",
      "5",
      "--max-time",
      String(Math.max(5, Math.ceil(Number(axios.defaults.timeout || 15000) / 1000))),
      "--request",
      "POST",
      `${env("PM_CLOB_HOST", "https://clob.polymarket.com").replace(/\/+$/, "")}/order`,
      "--header",
      "Content-Type: application/json",
    ];
    for (const [key, value] of Object.entries(headers || {})) {
      if (value === undefined || value === null || String(value) === "") continue;
      args.push("--header", `${key}: ${String(value)}`);
    }
    args.push(
      "--data-binary",
      body,
      "--write-out",
      "\n__CLOB_HTTP_STATUS__:%{http_code}",
    );
    let result;
    try {
      result = await execFilePromise("/usr/bin/curl", args, {
        encoding: "utf8",
        maxBuffer: 1024 * 1024,
        timeout: Math.max(5000, Math.ceil(Number(axios.defaults.timeout || 15000)) + 1000),
      });
    } catch (err) {
      const detail = redactErrorText(err?.stderr || err?.message || String(err || ""));
      throw new Error(`clob_proxy_submit_failed:${detail}`);
    }
    const marker = "\n__CLOB_HTTP_STATUS__:";
    const raw = String(result.stdout || "");
    const markerIndex = raw.lastIndexOf(marker);
    if (markerIndex < 0) throw new Error("clob_proxy_submit_http_status_missing");
    const responseText = raw.slice(0, markerIndex).trim();
    const status = Number(raw.slice(markerIndex + marker.length).trim());
    let response = {};
    try {
      response = responseText ? JSON.parse(responseText) : {};
    } catch {
      response = { error: responseText };
    }
    if (!response || typeof response !== "object" || Array.isArray(response)) {
      response = { response };
    }
    if (Number.isFinite(status) && status >= 400) {
      response.status = status;
    }
    return response;
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
      // A previous gateway version incorrectly persisted a timeout lookup
      // with status `submitted_unconfirmed` as exchange_accepted.  Never
      // restore that state as terminal after restart; keep it visible to the
      // reconcile gate until an explicit exchange state is observed.
      const exactRecoveredIdentity = hasRecoveredExactExchangeIdentity(item.response);
      if (
        item.state === "exchange_accepted"
        && exchangeOrderNeedsReconciliation(item.response)
        && !exactRecoveredIdentity
      ) {
        item.state = "reconcile_required";
        try {
          this.idemStore.put(key, item.order_intent_id, item.state, item.request, item.response);
        } catch (err) {
          this.lastIdempotencyPersistenceError = redactErrorText(err?.message || String(err || ""));
        }
      }
      // Gateway versions before the strict status classifier could first
      // recover an exact exchange identity, persist `exchange_accepted`, and
      // then downgrade that same row on a later restart solely because the
      // copied status remained `submitted_unconfirmed`.  Exact recovered
      // identity is sufficient for the idempotency boundary: the intent must
      // never be replayed.  It is deliberately not a fill receipt; downstream
      // order-state reconciliation continues to own quantity and terminal
      // lifecycle.
      if (item.state === "reconcile_required" && exactRecoveredIdentity) {
        item.state = "exchange_accepted";
        item.response = {
          ...item.response,
          idempotency_terminal_reason: "exact_exchange_identity_recovered",
          fill_state: "unknown",
        };
        try {
          this.idemStore.put(key, item.order_intent_id, item.state, item.request, item.response);
        } catch (err) {
          this.lastIdempotencyPersistenceError = redactErrorText(err?.message || String(err || ""));
        }
      }
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

  recordRegion(result) {
    if (!result || typeof result !== "object") return this.region;
    const blocked = typeof result.blocked === "boolean" ? result.blocked : null;
    this.region = {
      blocked,
      country: String(result.country || ""),
      region: String(result.region || ""),
      error: String(result.error || ""),
    };
    // Only a decided answer carries an observation time.  Leaving the previous
    // timestamp in place on a failed probe would let a stale "blocked" verdict
    // masquerade as fresh and close the lane on evidence nobody re-confirmed.
    this.regionObservedAt = blocked === null ? "" : new Date().toISOString();
    return this.region;
  }

  /**
   * Keep the exchange-visible region attestation current, off the hot path.
   *
   * The probe shells out, so it never runs inline with an order: a blocking
   * refresh would add seconds of event-loop stall to the exact request that is
   * racing a frozen argmax period.
   */
  startRegionWatch() {
    const sec = positiveNumber(env("PM_CLOB_REGION_PROBE_SEC", "300"), 300);
    if (!Number.isFinite(sec) || sec <= 0) return null;
    const proxyUrl = String(this.proxy?.proxy || "").trim();
    const args = ["-fsS", "--max-time", "6"];
    if (proxyUrl) args.push("--proxy", proxyUrl);
    args.push("https://polymarket.com/api/geoblock");
    const run = () => {
      execFile("/usr/bin/curl", args, { encoding: "utf8", timeout: 8000 }, (err, stdout) => {
        if (err) {
          this.recordRegion({ blocked: null, error: "region_probe_failed" });
          return;
        }
        try {
          const parsed = JSON.parse(String(stdout || ""));
          this.recordRegion(
            typeof parsed?.blocked === "boolean"
              ? { blocked: parsed.blocked, country: parsed.country, region: parsed.region }
              : { blocked: null, error: "region_probe_missing_blocked" },
          );
        } catch {
          this.recordRegion({ blocked: null, error: "region_probe_unparseable" });
        }
      });
    };
    return setInterval(run, Math.round(sec * 1000)).unref?.() ?? null;
  }

  /**
   * Report a region verdict only while it is still fresh.
   *
   * A stale verdict is treated as unknown in both directions: an old "blocked"
   * must not keep rejecting after the route was fixed, and an old "allowed"
   * must not vouch for a route that has since moved.
   */
  regionBlockedNow() {
    if (this.region?.blocked !== true) return false;
    const observed = Date.parse(String(this.regionObservedAt || ""));
    if (!Number.isFinite(observed)) return false;
    const maxAgeMs = Math.max(60, positiveNumber(env("PM_CLOB_REGION_MAX_AGE_SEC", "900"), 900)) * 1000;
    return Date.now() - observed <= maxAgeMs;
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

  startBalancePrefetch() {
    const intervalMs = Math.max(250, Math.floor(positiveNumber(
      env("PM_CLOB_BALANCE_PREFETCH_INTERVAL_MS", "1000"), 1000,
    )));
    const run = async () => {
      // Balance reads share the selected CLOB transport with order POSTs.
      // Never let slow/failed background reads pile up and contend with the
      // latency-critical order path.
      if (!this.client || this.balancePrefetchInflight > 0) return;
      this.balancePrefetchInflight += 1;
      try {
        await this.prefetchRegisteredPositionBalances();
      } catch (err) {
        this.lastBalancePrefetchError = redactErrorText(err?.message || String(err || ""));
      } finally {
        this.balancePrefetchInflight = Math.max(0, this.balancePrefetchInflight - 1);
      }
    };
    run().catch(() => {});
    setInterval(() => run().catch(() => {}), intervalMs).unref?.();
  }

  async prefetchRegisteredPositionBalances() {
    this.refreshRegisteredPositionTokensFromInventory();
    const positionTokens = [...this.registeredPositionTokens];
    await Promise.all(positionTokens.map(async (tokenID) => {
      const tokenStarted = Date.now();
      try {
        const value = await this.getBalanceAllowanceForPrefetch({
          asset_type: AssetType.CONDITIONAL, token_id: tokenID,
        });
        if (!balanceEvidenceValid(value)) throw new Error("clob_balance_prefetch_evidence_invalid");
        this.cachedConditional.set(tokenID, {
          value,
          observed_at: new Date().toISOString(),
          latency_ms: Date.now() - tokenStarted,
        });
        this.conditionalPrefetchErrors.delete(tokenID);
      } catch (err) {
        this.conditionalPrefetchErrors.set(
          tokenID, redactErrorText(err?.message || String(err || "")),
        );
      }
    }));
    this.lastBalancePrefetchError = [...this.conditionalPrefetchErrors.entries()]
      .map(([tokenID, error]) => `${tokenID.slice(-12)}:${error}`)
      .join(",");
    return {
      requested: positionTokens.length,
      ready: positionTokens.filter((tokenID) => this.cachedConditional.has(tokenID)).length,
      failed: this.conditionalPrefetchErrors.size,
    };
  }

  balanceFetchLimit() {
    // This controls read-only balance evidence only. Order submit/cancel and
    // reconciliation intentionally do not wait behind this limiter.
    return Math.max(1, Math.floor(positiveNumber(
      env("PM_CLOB_BALANCE_FETCH_MAX_CONCURRENCY", "2"), 2,
    )));
  }

  balanceFetchQueueLimit() {
    // A full queue must fail closed instead of retaining unbounded local work
    // that would eventually recreate proxy congestion.
    return Math.max(1, Math.floor(positiveNumber(
      env("PM_CLOB_BALANCE_FETCH_MAX_QUEUE", "24"), 24,
    )));
  }

  balanceRateLimitedError() {
    const remainingMs = Math.max(0, this.balanceRateLimitUntilMs - Date.now());
    return new Error(`clob_balance_rate_limited_cooldown_${Math.ceil(remainingMs / 1000)}s`);
  }

  balanceRateLimitActive() {
    return this.balanceRateLimitUntilMs > Date.now();
  }

  recordBalanceRateLimit(err) {
    const text = String(err?.message || err || "").toLowerCase();
    if (!text.includes("http_429") && !text.includes("error 1015") && !text.includes("rate limit")) {
      return false;
    }
    const baseMs = Math.max(1000, Math.floor(positiveNumber(
      env("PM_CLOB_BALANCE_RATE_LIMIT_BACKOFF_SEC", "60"), 60,
    ) * 1000));
    const maxMs = Math.max(baseMs, Math.floor(positiveNumber(
      env("PM_CLOB_BALANCE_RATE_LIMIT_MAX_BACKOFF_SEC", "300"), 300,
    ) * 1000));
    // Cloudflare 1015 and HTTP 429 are an upstream back-pressure signal, not
    // a transient error to fan out or retry immediately.
    this.balanceRateLimitStreak = Math.min(4, this.balanceRateLimitStreak + 1);
    const delayMs = Math.min(maxMs, baseMs * (2 ** (this.balanceRateLimitStreak - 1)));
    this.balanceRateLimitUntilMs = Math.max(this.balanceRateLimitUntilMs, Date.now() + delayMs);
    this.lastBalanceRateLimitAt = new Date().toISOString();
    return true;
  }

  runLimitedBalanceFetch(work) {
    return new Promise((resolve, reject) => {
      if (this.balanceFetchQueue.length >= this.balanceFetchQueueLimit()) {
        reject(new Error("clob_balance_fetch_queue_full"));
        return;
      }
      const run = () => {
        if (this.balanceRateLimitActive()) {
          reject(this.balanceRateLimitedError());
          this.drainBalanceFetchQueue();
          return;
        }
        this.balanceFetchActive += 1;
        Promise.resolve()
          .then(work)
          .then(resolve, reject)
          .finally(() => {
            this.balanceFetchActive = Math.max(0, this.balanceFetchActive - 1);
            this.drainBalanceFetchQueue();
          });
      };
      this.balanceFetchQueue.push(run);
      this.drainBalanceFetchQueue();
    });
  }

  drainBalanceFetchQueue() {
    while (this.balanceFetchActive < this.balanceFetchLimit() && this.balanceFetchQueue.length > 0) {
      const next = this.balanceFetchQueue.shift();
      next();
    }
  }

  refreshRegisteredPositionTokensFromInventory() {
    try {
      const payload = JSON.parse(fs.readFileSync(HELD_YES_INVENTORY_FILE, "utf8"));
      if (payload?.schema !== "reported_trajectory_held_yes_inventory_v1"
          || payload?.complete !== true
          || !Array.isArray(payload?.active_token_ids)) return 0;
      const nextTokens = new Set();
      for (const raw of payload.active_token_ids) {
        const tokenID = String(raw || "").trim();
        if (/^\d+$/.test(tokenID)) nextTokens.add(tokenID);
      }
      const previousTokens = this.registeredPositionTokens;
      this.registeredPositionTokens = nextTokens;
      for (const tokenID of previousTokens) {
        if (nextTokens.has(tokenID)) continue;
        this.cachedConditional.delete(tokenID);
        this.conditionalPrefetchErrors.delete(tokenID);
      }
      return nextTokens.size;
    } catch {
      // The inventory is an optimization only. Event-time SELL still registers
      // and fetches the exact token, so an absent refresh cannot authorize or
      // suppress an order.
      return 0;
    }
  }

  async getBalanceAllowanceForPrefetch(params) {
    const tokenID = String(params?.token_id || "").trim();
    const existing = tokenID ? this.balanceFetchInflightByToken.get(tokenID) : null;
    if (existing) return existing;
    const request = this.runLimitedBalanceFetch(async () => {
      try {
        let response;
        // Native Node fetch does not honor HTTP(S)_PROXY by itself.  When the
        // service selected a proxy, reuse the already configured CLOB client
        // transport instead of letting this read-only prefetch time out directly.
        if (this.proxy?.mode === "proxy") {
          response = await this.requestJsonViaProxy("GET", "/balance-allowance", undefined, {
            ...params,
            signature_type: this.signatureType,
          });
        } else if (!truthy(env("PM_CLOB_BALANCE_PREFETCH_DIRECT", "1"), true)) {
          response = await this.client.getBalanceAllowance(params);
        } else {
          if (!this.signer || !this.apiCreds) throw new Error("clob_balance_prefetch_auth_unavailable");
          const requestPath = "/balance-allowance";
          const headers = await createL2Headers(this.signer, this.apiCreds, {
            method: "GET", requestPath,
          });
          const host = env("PM_CLOB_HOST", "https://clob.polymarket.com").replace(/\/+$/, "");
          const url = new URL(`${host}${requestPath}`);
          for (const [key, value] of Object.entries({ ...params, signature_type: this.signatureType })) {
            if (value !== undefined && value !== null && String(value) !== "") url.searchParams.set(key, String(value));
          }
          const controller = new AbortController();
          const timeoutMs = Math.max(1000, Math.floor(positiveNumber(
            env("PM_CLOB_BALANCE_PREFETCH_TIMEOUT_MS", "5000"), 5000,
          )));
          const timeout = setTimeout(() => controller.abort(), timeoutMs);
          try {
            const httpResponse = await fetch(url, {
              method: "GET", headers, signal: controller.signal, cache: "no-store",
            });
            if (!httpResponse.ok) throw new Error(`clob_balance_prefetch_http_${httpResponse.status}`);
            response = await httpResponse.json();
          } finally {
            clearTimeout(timeout);
          }
        }
        this.balanceRateLimitStreak = 0;
        return response;
      } catch (err) {
        this.recordBalanceRateLimit(err);
        throw err;
      }
    });
    if (tokenID) this.balanceFetchInflightByToken.set(tokenID, request);
    try {
      return await request;
    } finally {
      if (tokenID && this.balanceFetchInflightByToken.get(tokenID) === request) {
        this.balanceFetchInflightByToken.delete(tokenID);
      }
    }
  }

  readActiveHeldYesInventoryTokenIDs() {
    try {
      const payload = JSON.parse(fs.readFileSync(HELD_YES_INVENTORY_FILE, "utf8"));
      if (payload?.schema !== "reported_trajectory_held_yes_inventory_v1"
          || payload?.complete !== true
          || !Array.isArray(payload?.active_token_ids)
          || !Array.isArray(payload?.token_ids)
          || payload?.active_token_ids_source !== "official_local_clob_execution_guard_snapshot"
          || !String(payload?.active_token_ids_observed_at || "").trim()
          || !/^[0-9a-f]{64}$/.test(String(payload?.active_token_ids_receipt_sha256 || "").toLowerCase())
          || payload.active_token_ids.length > 64) return null;
      const active = payload.active_token_ids.map((raw) => String(raw || "").trim());
      const known = new Set(payload.token_ids.map((raw) => String(raw || "").trim()));
      if (active.some((tokenID) => !/^\d+$/.test(tokenID))
          || active.length !== new Set(active).size
          || active.some((tokenID) => !known.has(tokenID))) return null;
      return [...active].sort();
    } catch {
      return null;
    }
  }

  readHeldYesWsQuoteSnapshot(maxAgeMs, tokenID) {
    const requested = String(tokenID || "").trim();
    const scopeTokenIDs = this.readActiveHeldYesInventoryTokenIDs();
    if (!requested || !this.registeredPositionTokens.has(requested)
        || !scopeTokenIDs || !scopeTokenIDs.includes(requested)) {
      throw new Error("held_yes_ws_token_not_registered_or_scope_invalid");
    }
    const expectedScopeSha = crypto.createHash("sha256")
      .update(scopeTokenIDs.join("\0"))
      .digest("hex");
    const manifest = JSON.parse(fs.readFileSync(HELD_YES_WS_MANIFEST_FILE, "utf8"));
    const suppliedManifestHash = String(manifest?.snapshot_sha256 || "");
    const unsignedManifest = { ...manifest };
    delete unsignedManifest.snapshot_sha256;
    const calculatedManifestHash = crypto.createHash("sha256")
      .update(canonicalJson(unsignedManifest)).digest("hex");
    const manifestObservedMs = Date.parse(String(manifest?.connection_observed_at || ""));
    const manifestAgeMs = Number.isFinite(manifestObservedMs)
      ? Date.now() - manifestObservedMs : Number.POSITIVE_INFINITY;
    const manifestScope = Array.isArray(manifest?.scope_token_ids)
      ? manifest.scope_token_ids.map((raw) => String(raw || "").trim()) : [];
    const completeTokenIDs = Array.isArray(manifest?.complete_book_token_ids)
      ? manifest.complete_book_token_ids.map((raw) => String(raw || "").trim()) : [];
    const declaredShardDir = String(manifest?.quote_shard_dir || "");
    const scopeExact = manifestScope.length === scopeTokenIDs.length
      && manifestScope.every((token, index) => token === scopeTokenIDs[index]);
    const completeExact = completeTokenIDs.length === new Set(completeTokenIDs).size
      && completeTokenIDs.every((token) => scopeTokenIDs.includes(token));
    if (manifest?.schema !== HELD_YES_WS_MANIFEST_SCHEMA
        || !["ready", "partial_evidence"].includes(String(manifest?.state || ""))
        || manifest?.source !== "official_polymarket_clob_websocket"
        || manifest?.evidence_only !== true
        || manifest?.authorizes_order !== false
        || manifest?.quote_storage !== "sha256_token_shards_v1"
        || !declaredShardDir
        || path.resolve(declaredShardDir) !== path.resolve(HELD_YES_WS_SHARD_DIR)
        || !scopeExact
        || manifest?.scope_sha256 !== expectedScopeSha
        || !completeExact
        || !completeTokenIDs.includes(requested)
        || suppliedManifestHash.length !== 64
        || suppliedManifestHash !== calculatedManifestHash
        || !Number.isFinite(manifestAgeMs)
        || manifestAgeMs < 0
        || manifestAgeMs > maxAgeMs) {
      throw new Error("held_yes_ws_manifest_invalid_or_stale");
    }
    const filename = `${crypto.createHash("sha256").update(requested).digest("hex")}.json`;
    const tokenSnapshot = JSON.parse(fs.readFileSync(path.join(HELD_YES_WS_SHARD_DIR, filename), "utf8"));
    const suppliedTokenHash = String(tokenSnapshot?.snapshot_sha256 || "");
    const unsignedToken = { ...tokenSnapshot };
    delete unsignedToken.snapshot_sha256;
    const calculatedTokenHash = crypto.createHash("sha256")
      .update(canonicalJson(unsignedToken)).digest("hex");
    const tokenScope = Array.isArray(tokenSnapshot?.scope_token_ids)
      ? tokenSnapshot.scope_token_ids.map((raw) => String(raw || "").trim()) : [];
    const tokenScopeExact = tokenScope.length === scopeTokenIDs.length
      && tokenScope.every((token, index) => token === scopeTokenIDs[index]);
    const quote = tokenSnapshot?.quote;
    const bookObservedMs = Date.parse(String(quote?.book_observed_at || ""));
    const bookAgeMs = Number.isFinite(bookObservedMs)
      ? Date.now() - bookObservedMs : Number.POSITIVE_INFINITY;
    if (tokenSnapshot?.schema !== HELD_YES_WS_TOKEN_SCHEMA
        || tokenSnapshot?.token_id !== requested
        || tokenSnapshot?.scope_sha256 !== expectedScopeSha
        || !tokenScopeExact
        || tokenSnapshot?.source !== "official_polymarket_clob_websocket"
        || tokenSnapshot?.evidence_only !== true
        || tokenSnapshot?.authorizes_order !== false
        || suppliedTokenHash.length !== 64
        || suppliedTokenHash !== calculatedTokenHash
        || !quote
        || quote?.token_id !== requested
        || quote?.source !== "clob_ws"
        || quote?.source_detail !== "book"
        || !Array.isArray(quote?.bids)
        || !Array.isArray(quote?.asks)
        || !Number.isFinite(bookAgeMs)
        || bookAgeMs < 0
        || bookAgeMs > maxAgeMs) {
      throw new Error("held_yes_ws_token_quote_invalid_or_stale");
    }
    return {
      parsed: {
        schema: HELD_YES_WS_MANIFEST_SCHEMA,
        connection_observed_at: manifest.connection_observed_at,
        quotes_by_token: { [requested]: quote },
      },
      age_ms: Math.max(manifestAgeMs, bookAgeMs),
      book_age_ms: bookAgeMs,
      snapshot_sha256: suppliedManifestHash,
      token_snapshot_sha256: suppliedTokenHash,
      evidence_source: "held_yes_auxiliary_official_clob_ws",
    };
  }

  readLatestWsQuoteSnapshot(maxAgeMs, tokenID = "", allowPartialTopology = false, tokenIDs = []) {
    const snapshotPath = env(
      "PM_MARKET_WS_LATEST_QUOTES_PATH", path.join(DATA_DIR, "market_ws_latest_quotes.json"),
    );
    const parsed = JSON.parse(fs.readFileSync(snapshotPath, "utf8"));
    const suppliedHash = String(parsed?.snapshot_sha256 || "");
    const unsigned = { ...parsed };
    delete unsigned.snapshot_sha256;
    const calculatedHash = crypto.createHash("sha256").update(canonicalJson(unsigned)).digest("hex");
    const observedMs = Date.parse(String(parsed?.connection_observed_at || ""));
    const ageMs = Number.isFinite(observedMs) ? Date.now() - observedMs : Number.POSITIVE_INFINITY;
    const activeShards = Array.isArray(parsed?.active_shards) ? parsed.active_shards.length : 0;
    const expectedShards = Number(parsed?.expected_shards || 0);
    const requestedTokenID = String(tokenID || "").trim();
    const requestedTokenIDs = [...new Set([
      ...(requestedTokenID ? [requestedTokenID] : []),
      ...(Array.isArray(tokenIDs) ? tokenIDs : []),
    ].map((value) => String(value || "").trim()).filter(Boolean))];
    const requireCompleteTopology = !requestedTokenID && allowPartialTopology !== true;
    if (parsed?.schema !== "clob_ws_latest_quote_snapshot_v1"
        || parsed?.state !== "connected"
        || parsed?.source !== "official_polymarket_clob_websocket"
        || parsed?.evidence_only !== true
        || parsed?.authorizes_order !== false
        || !Number.isFinite(expectedShards)
        || expectedShards < 1
        || activeShards < 1
        || activeShards > expectedShards
        || (requireCompleteTopology && activeShards !== expectedShards)
        || suppliedHash.length !== 64
        || suppliedHash !== calculatedHash
        || ageMs < 0
        || ageMs > maxAgeMs) {
      throw new Error("clob_ws_latest_quote_snapshot_invalid_or_stale");
    }
    if (requestedTokenIDs.length && parsed?.quote_storage === "sha256_token_shards_v1") {
      const shardDir = env(
        "PM_MARKET_WS_QUOTE_SHARD_DIR", path.join(DATA_DIR, "market_ws_quote_shards"),
      );
      for (const requested of requestedTokenIDs) {
        const filename = `${crypto.createHash("sha256").update(requested).digest("hex")}.json`;
        const tokenSnapshot = JSON.parse(fs.readFileSync(path.join(shardDir, filename), "utf8"));
        const tokenSuppliedHash = String(tokenSnapshot?.snapshot_sha256 || "");
        const tokenUnsigned = { ...tokenSnapshot };
        delete tokenUnsigned.snapshot_sha256;
        const tokenCalculatedHash = crypto.createHash("sha256")
          .update(canonicalJson(tokenUnsigned)).digest("hex");
        if (tokenSnapshot?.schema !== "clob_ws_token_quote_snapshot_v1"
            || tokenSnapshot?.token_id !== requested
            || tokenSnapshot?.source !== "official_polymarket_clob_websocket"
            || tokenSnapshot?.evidence_only !== true
            || tokenSnapshot?.authorizes_order !== false
            || tokenSuppliedHash.length !== 64
            || tokenSuppliedHash !== tokenCalculatedHash
            || !tokenSnapshot?.quote) {
          throw new Error("clob_ws_token_quote_snapshot_invalid");
        }
        parsed.quotes_by_token[requested] = tokenSnapshot.quote;
      }
    }
    return { parsed, age_ms: ageMs, snapshot_sha256: suppliedHash };
  }

  applyMetadataRows(rows) {
    const tickSize = normalizeTickSize(env("PM_CLOB_DEFAULT_TICK_SIZE", "0.01"));
    const negRisk = truthy(env("PM_CLOB_DEFAULT_NEG_RISK", "1"), true);
    let count = 0;
    for (const row of rows || []) {
      const tokenID = String(row?.token_id || "").trim();
      if (!tokenID) continue;
      const conditionID = String(row?.condition_id || "").trim();
      const previous = this.metadata.get(tokenID) || {};
      this.rememberTokenMetadata(
        tokenID,
        previous.tickSize ? normalizeTickSize(previous.tickSize, tickSize) : tickSize,
        typeof previous.negRisk === "boolean" ? previous.negRisk : negRisk,
        previous.source || "market_tokens_prewarm_db"
      );
      // The SDK's signing path requires tokenConditionMap before it can
      // resolve fee/market metadata.  The local registry already contains the
      // authoritative condition id; use it before falling back to the
      // proxy-sensitive markets-by-token request.
      if (conditionID && this.client?.tokenConditionMap) {
        this.client.tokenConditionMap[tokenID] = conditionID;
      }
      if (!this.prewarmTokenHint) this.prewarmTokenHint = tokenID;
      count += 1;
    }
    return count;
  }

  ensureTokenConditionFromDb(tokenID) {
    const normalizedTokenID = String(tokenID || "").trim();
    const conditionMap = this.client?.tokenConditionMap;
    if (!normalizedTokenID || !this.client || !conditionMap || conditionMap[normalizedTokenID]) return "";
    if (!fs.existsSync(DB_PATH) || !/^[0-9]+$/.test(normalizedTokenID)) return "";
    const escapedTokenID = normalizedTokenID.replace(/'/g, "''");
    const sql = `
      SELECT json_extract(registry_json, '$.condition_id')
      FROM reported_trajectory_market_registry
      WHERE market_active=1 AND market_accepting_orders=1
        AND (yes_token='${escapedTokenID}' OR no_token='${escapedTokenID}')
        AND COALESCE(json_extract(registry_json, '$.condition_id'),'')<>''
      ORDER BY refreshed_at DESC
      LIMIT 1;
    `;
    try {
      const conditionID = String(execFileSync("/usr/bin/sqlite3", ["-noheader", DB_PATH, sql], {
        encoding: "utf8",
        timeout: 800,
      }) || "").trim().split(/\s+/)[0] || "";
      if (conditionID) {
        conditionMap[normalizedTokenID] = conditionID;
        return conditionID;
      }
    } catch {
      // The normal prewarm path remains authoritative; this is a bounded
      // last-mile repair for a token refreshed after service startup.
    }
    return "";
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
        const configuredFeeRateBps = Number(env("PM_CLOB_DEFAULT_FEE_RATE_BPS", ""));
        const feeRates = this.client.feeRates;
        if (Number.isInteger(configuredFeeRateBps) && configuredFeeRateBps >= 0
          && feeRates && !(normalizedTokenID in feeRates)) {
          // The configured value is operator-attested from the official CLOB
          // fee-rate endpoint and only removes a slow duplicate read from the
          // signed-order hot path.  A missing value keeps SDK discovery intact.
          feeRates[normalizedTokenID] = configuredFeeRateBps;
        }
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
      SELECT no_token
      FROM reported_trajectory_market_registry
      WHERE source_eligible=1 AND market_active=1 AND market_accepting_orders=1
        AND COALESCE(no_token,'')<>''
      ORDER BY refreshed_at DESC
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
    const limit = Math.max(8, Math.min(
      Math.floor(positiveNumber(env("PM_CLOB_ORDER_SERVICE_PREWARM_TOKEN_LIMIT", "4000"), 4000)), 5000,
    ));
    const sql = `
      SELECT token_id, MAX(condition_id) AS condition_id, MAX(updated_at) AS updated_at FROM (
        SELECT no_token AS token_id,
               json_extract(registry_json, '$.condition_id') AS condition_id,
               refreshed_at AS updated_at
        FROM reported_trajectory_market_registry
        WHERE source_eligible=1 AND market_active=1 AND market_accepting_orders=1
          AND COALESCE(no_token,'')<>''
          AND COALESCE(json_extract(registry_json, '$.condition_id'),'')<>''
        UNION ALL
        SELECT yes_token AS token_id,
               json_extract(registry_json, '$.condition_id') AS condition_id,
               refreshed_at AS updated_at
        FROM reported_trajectory_market_registry
        WHERE source_eligible=1 AND market_active=1 AND market_accepting_orders=1
          AND COALESCE(yes_token,'')<>''
          AND COALESCE(json_extract(registry_json, '$.condition_id'),'')<>''
      )
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
    // The held-YES inventory is a read-only safety input. Refresh it before
    // reporting conditional-balance readiness so a token removed from the
    // inventory cannot remain registered forever when background prefetch is
    // disabled. A real active position still remains fail-closed until its
    // exact-token balance evidence is fresh.
    this.refreshRegisteredPositionTokensFromInventory();
    const livePaused = livePausePresent();
    const marketMaxAgeMs = Math.max(100, Math.floor(positiveNumber(
      // The WS listener publishes an atomic topology snapshot on each
      // inbound book/update.  A quiet book must remain valid for the same
      // bounded freshness window used by the production performance budget
      // (120s); a one-second default spuriously blocks otherwise complete
      // official WS evidence whenever no tick arrives.
      env("PM_CLOB_LOCAL_EVIDENCE_MAX_AGE_MS", "120000"), 120000,
    )));
    const balanceMaxAgeMs = Math.max(marketMaxAgeMs, Math.floor(positiveNumber(
      env("PM_CLOB_BALANCE_CACHE_MAX_AGE_MS", "3000"), 3000,
    )));
    let wsSnapshotReady = false;
    let wsPartialSnapshotReady = false;
    let wsSnapshotAgeMs = null;
    let wsActiveShards = 0;
    let wsExpectedShards = 0;
    let wsMaterializedTokenCount = 0;
    let wsSubscribedTokenCount = 0;
    try {
      const ws = this.readLatestWsQuoteSnapshot(marketMaxAgeMs);
      wsSnapshotReady = true;
      wsSnapshotAgeMs = Math.round(ws.age_ms);
    } catch {
      wsSnapshotReady = false;
    }
    try {
      const ws = this.readLatestWsQuoteSnapshot(marketMaxAgeMs, "", true);
      wsPartialSnapshotReady = true;
      wsSnapshotAgeMs = Math.round(ws.age_ms);
      wsActiveShards = Array.isArray(ws.parsed?.active_shards) ? ws.parsed.active_shards.length : 0;
      wsExpectedShards = Number(ws.parsed?.expected_shards || 0);
      wsMaterializedTokenCount = Number(ws.parsed?.materialized_token_count || 0);
      wsSubscribedTokenCount = Number(ws.parsed?.subscribed_token_count || 0);
    } catch {
      wsPartialSnapshotReady = false;
    }
    const collateralAgeMs = null;
    const collateralCacheReady = true;
    const conditionalReadyTokens = [];
    const conditionalNotReadyTokens = [];
    for (const tokenID of this.registeredPositionTokens) {
      const cached = this.cachedConditional.get(tokenID);
      const cacheAgeMs = cached
        ? Date.now() - Date.parse(cached.observed_at || "") : Number.POSITIVE_INFINITY;
      if (cached && cacheAgeMs >= 0 && cacheAgeMs <= balanceMaxAgeMs
          && balanceEvidenceValid(cached.value)) {
        conditionalReadyTokens.push(tokenID);
      } else {
        conditionalNotReadyTokens.push(tokenID);
      }
    }
    return {
      ok: true,
      service: SERVICE_NAME,
      capabilities: [
        "execution_guard_snapshot_v1",
        "place_order_v1",
        "reconcile_order_v1",
        "cancel_order_v1",
        "lookup_exchange_order_v1",
        "local_prefetched_execution_guard_v1",
      ],
      ready: Boolean(this.client) && (!truthy(env("PM_CLOB_FORCE_PROXY", "0")) || this.proxy?.mode === "proxy"),
      pid: process.pid,
      started_at: this.startedAt.toISOString(),
      ready_at: this.readyAt ? this.readyAt.toISOString() : null,
      uptime_sec: Math.round(process.uptime() * 1000) / 1000,
      proxy: this.proxy,
      region: { ...this.region, observed_at: this.regionObservedAt, blocking_orders: this.regionBlockedNow() },
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
      balance_prefetch_inflight: this.balancePrefetchInflight,
      balance_fetch_active: this.balanceFetchActive,
      balance_fetch_queue_depth: this.balanceFetchQueue.length,
      balance_fetch_inflight_tokens: this.balanceFetchInflightByToken.size,
      balance_rate_limited_until: this.balanceRateLimitUntilMs
        ? new Date(this.balanceRateLimitUntilMs).toISOString() : null,
      balance_rate_limit_streak: this.balanceRateLimitStreak,
      last_balance_rate_limit_at: this.lastBalanceRateLimitAt,
      collateral_cache_observed_at: this.cachedCollateral?.observed_at || null,
      collateral_cache_age_ms: collateralAgeMs,
      collateral_cache_latency_ms: this.cachedCollateral?.latency_ms ?? null,
      conditional_balance_cache_size: this.cachedConditional.size,
      registered_position_token_count: this.registeredPositionTokens.size,
      conditional_balance_ready_token_ids: conditionalReadyTokens.sort(),
      conditional_balance_not_ready_token_ids: conditionalNotReadyTokens.sort(),
      active_held_yes_balance_ready: conditionalNotReadyTokens.length === 0,
      last_balance_prefetch_error: this.lastBalancePrefetchError,
      balance_prefetch_transport: (
        truthy(env("PM_CLOB_BALANCE_PREFETCH_DIRECT", "1"), true)
        && this.proxy?.mode !== "proxy"
      ) ? "direct_read_only_authenticated" : "clob_client_read_only_authenticated",
      ws_snapshot_ready: wsSnapshotReady,
      ws_partial_snapshot_ready: wsPartialSnapshotReady,
      ws_snapshot_age_ms: wsSnapshotAgeMs,
      ws_active_shards: wsActiveShards,
      ws_expected_shards: wsExpectedShards,
      ws_materialized_token_count: wsMaterializedTokenCount,
      ws_subscribed_token_count: wsSubscribedTokenCount,
      collateral_cache_ready: collateralCacheReady,
      collateral_balance_fetch_disabled: true,
      all_shards_prefetched_evidence_ready: wsSnapshotReady,
      signer_prewarmed: this.signerPrewarmed,
      hot_order_path_ready: wsPartialSnapshotReady && this.signerPrewarmed,
      local_prefetched_evidence_ready: wsPartialSnapshotReady && this.signerPrewarmed,
      last_place_order_error_at: this.lastPlaceOrderErrorAt,
      last_place_order_error_class: this.lastPlaceOrderErrorClass,
      last_place_order_http_status: this.lastPlaceOrderHttpStatus,
      last_place_order_geoblocked: this.lastPlaceOrderGeoblocked,
      last_place_order_retryable: this.lastPlaceOrderRetryable,
      last_heartbeat_at: this.lastHeartbeat,
      last_heartbeat_error: this.lastHeartbeatError,
      start_error: this.startError,
      read_only: truthy(env("PM_CLOB_ORDER_SERVICE_READ_ONLY", "0")),
      live_pause_file: LIVE_PAUSE_FILE,
      live_pause_present: livePaused,
      order_submission_enabled: !livePaused && !truthy(env("PM_CLOB_ORDER_SERVICE_READ_ONLY", "0")),
      asos_argmax_live_submission_enabled: truthy(env("PM_ASOS_ARGMAX_LIVE_ENABLED", "0"), false),
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
      // Reconciliation must use the same transport as lookup/cancel.  In
      // proxy mode the SDK's direct getOrder path can return an incomplete
      // response (or a response from a different network path), which makes
      // a valid exact order identity look mismatched and leaves the intent
      // permanently unresolved.
      const rawOrder = this.proxy?.mode === "proxy"
        ? await this.requestJsonViaProxy("GET", `/data/order/${expectedOrderID}`)
        : await this.client.getOrder(expectedOrderID);
      const order = unwrapProxyLookupResponse(rawOrder);
      if (!order || typeof order !== "object" || Array.isArray(order)) {
        return {
          ...this.idempotencyStatus(normalized),
          reconcile_required: true,
          reconcile_attempted: true,
          reconcile_reason: "exchange_order_empty_response",
          expected_order_id: expectedOrderID,
          status: "unknown",
        };
      }
      const recoveredOrderID = String(order?.id || order?.orderID || expectedOrderID).trim();
      if (recoveredOrderID.toLowerCase() !== expectedOrderID.toLowerCase()) {
        return {
          ...this.idempotencyStatus(normalized),
          reconcile_attempted: true,
          reconcile_reason: "exchange_order_identity_mismatch",
          expected_order_id: expectedOrderID,
          exchange_order_id: recoveredOrderID || null,
        };
      }
      const status = normalizedExchangeOrderStatus(order.status);
      const matched = order.size_matched ?? order.sizeMatched ?? order.matched_size ?? null;
      const sharesMatched = String(order.side || request.side || "").toUpperCase() === "SELL"
        ? (order.makingAmount ?? order.making_amount ?? matched)
        : matched;
      const original = order.original_size ?? order.originalSize ?? null;
      const reconcileKind = exchangeOrderReconcileKind(order);
      if (reconcileKind.kind === "reconcile_required") {
        // An order identity alone is not a fill receipt.  In particular,
        // FAK timeout responses can be echoed by the CLOB as
        // submitted_unconfirmed with no quantity fields.  Keep the intent in
        // unresolved storage so callers cannot submit a duplicate leg.
        return {
          ...this.idempotencyStatus(normalized),
          reconcile_required: true,
          reconcile_attempted: true,
          reconcile_reason: "exchange_order_state_not_terminal",
          expected_order_id: expectedOrderID,
          orderID: recoveredOrderID,
          status: status || "unknown",
          original_size: original,
          size_matched: matched,
          shares_matched: sharesMatched,
        };
      }
      const response = {
        ok: true,
        orderID: recoveredOrderID,
        expected_order_id: expectedOrderID,
        expected_order_id_matches: true,
        status,
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
        shares_matched: sharesMatched,
        price: order.price ?? null,
      };
      if (reconcileKind.kind === "submit_failed") {
        response.ok = false;
        response.accepted = false;
        response.pending = false;
        response.error = "exchange_order_not_filled";
        this.persistIntent(normalized, "submit_failed", request, response);
        this.unresolved.delete(normalized);
        this.idem.set(normalized, {
          created_ms: Number(unresolved.created_ms || Date.now()),
          request,
          response,
          state: "submit_failed",
          order_intent_id: response.order_intent_id,
        });
        this.saveIdempotency();
        return response;
      }
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
    // A registered token is populated at startup/health from the bounded
    // held-YES projection.  Do not reread inventory on every guard request:
    // the guard must not turn an unrelated inventory refresh into a change of
    // the exact event-time evidence boundary.
    const registeredHeldYesToken = side === "SELL" && this.registeredPositionTokens.has(tokenID);

    // This endpoint is deliberately local-only.  Official WS depth and
    // continuously prefetched balances are evaluated below; a stale cache
    // fails closed instead of adding an event-time REST round trip.
    const maxAgeMs = Math.max(100, Math.floor(positiveNumber(
      env("PM_CLOB_LOCAL_EVIDENCE_MAX_AGE_MS", "120000"), 120000,
    )));
    const balanceMaxAgeMs = Math.max(maxAgeMs, Math.floor(positiveNumber(
      env("PM_CLOB_BALANCE_CACHE_MAX_AGE_MS", "3000"), 3000,
    )));
    const evidenceErrors = [];
    let ws = null;
    let wsReadError = "";
    let wsEvidenceSource = "primary_official_clob_ws";
    if (registeredHeldYesToken) {
      try {
        ws = this.readHeldYesWsQuoteSnapshot(maxAgeMs, tokenID);
        wsEvidenceSource = String(ws.evidence_source || "held_yes_auxiliary_official_clob_ws");
      } catch (heldErr) {
        // A current-day held token can also have a fully valid primary shard.
        // Accept only that independently verified official alternative; a
        // historical token with neither book remains fail-closed.
        const heldError = redactErrorText(heldErr?.message || String(heldErr || ""));
        try {
          ws = this.readLatestWsQuoteSnapshot(maxAgeMs, tokenID);
          wsEvidenceSource = "primary_official_clob_ws";
        } catch (primaryErr) {
          const primaryError = redactErrorText(primaryErr?.message || String(primaryErr || ""));
          wsReadError = [heldError, primaryError].filter(Boolean).join(";");
        }
      }
    } else {
      try {
        ws = this.readLatestWsQuoteSnapshot(maxAgeMs, tokenID);
      } catch (err) {
        wsReadError = redactErrorText(err?.message || String(err || ""));
      }
    }
    const quote = ws?.parsed?.quotes_by_token?.[tokenID] || null;
    const bids = Array.isArray(quote?.bids) ? quote.bids : [];
    const asks = Array.isArray(quote?.asks) ? quote.asks : [];
    // A fully materialized official order book may legitimately have no
    // liquidity on one side.  Empty asks are market evidence (and the
    // strategy will classify them as no executable ask), not missing CLOB
    // evidence.  Fail closed only when the target token never received a
    // complete book snapshot or its depth arrays are structurally absent.
    const orderBookMissing = (!quote
        || !Array.isArray(quote?.bids)
        || !Array.isArray(quote?.asks)
        || !String(quote?.book_observed_at || ""));
    const orderBook = {
      asset_id: tokenID,
      timestamp: ws?.parsed?.connection_observed_at || "",
      bids,
      asks,
      quote_value_observed_at: quote?.created_at || "",
      book_value_observed_at: quote?.book_observed_at || "",
      continuity_attested_at: ws?.parsed?.connection_observed_at || "",
      evidence_source: wsEvidenceSource,
    };
    const collateralAgeMs = Number.POSITIVE_INFINITY;
    const collateral = null;
    let conditionalCache = this.cachedConditional.get(tokenID);
    let conditionalAgeMs = conditionalCache
      ? Date.now() - Date.parse(conditionalCache.observed_at || "") : Number.POSITIVE_INFINITY;
    if (side === "SELL" && (!conditionalCache || conditionalAgeMs < 0 || conditionalAgeMs > balanceMaxAgeMs)) {
      try {
        const value = await this.getBalanceAllowanceForPrefetch({
          asset_type: AssetType.CONDITIONAL,
          token_id: tokenID,
        });
        if (!balanceEvidenceValid(value)) throw new Error("clob_conditional_balance_evidence_invalid");
        conditionalCache = { value, observed_at: new Date().toISOString(), latency_ms: Date.now() - started };
        conditionalAgeMs = 0;
        this.cachedConditional.set(tokenID, conditionalCache);
      } catch (err) {
        evidenceErrors.push(redactErrorText(err?.message || String(err || "")));
      }
    }
    const conditional = side === "SELL" ? conditionalCache?.value || null : null;
    if (side === "SELL" && balanceEvidenceValid(conditional) && !conditionalBalanceIsZero(conditional)) {
      this.registeredPositionTokens.add(tokenID);
    }
    const zeroSellPosition = side === "SELL" && conditionalBalanceIsZero(conditional);
    if (side === "SELL" && (!conditionalCache || conditionalAgeMs < 0 || conditionalAgeMs > balanceMaxAgeMs)) {
      evidenceErrors.push("clob_conditional_balance_prefetch_missing_or_stale");
    }
    const observedAt = new Date().toISOString();
    if (wsReadError && !zeroSellPosition) evidenceErrors.push(wsReadError);
    if (orderBookMissing && !zeroSellPosition) {
      evidenceErrors.push("clob_ws_order_book_depth_missing");
    }
    if ((!orderBook || orderBook.error || String(orderBook.asset_id || "") !== tokenID
        || !Array.isArray(orderBook.bids) || !Array.isArray(orderBook.asks)
        || !String(orderBook.timestamp || "")) && !zeroSellPosition) {
      evidenceErrors.push("clob_order_book_evidence_unavailable_or_invalid");
    }
    if (side === "SELL" && !balanceEvidenceValid(conditional)) evidenceErrors.push("clob_conditional_balance_evidence_unavailable_or_invalid");
    const tokenMetadata = this.metadata.get(tokenID) || {};
    return {
      schema: "clob_execution_guard_snapshot_v1",
      ok: evidenceErrors.length === 0,
      evidence_only: true,
      authorizes_order: false,
      evidence_errors: [...new Set(evidenceErrors)].sort(),
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
      token_metadata: {
        tick_size: tokenMetadata.tickSize || null,
        neg_risk: typeof tokenMetadata.negRisk === "boolean" ? tokenMetadata.negRisk : null,
        source: tokenMetadata.source || "",
      },
      source: SERVICE_NAME,
      evidence_source: "local_prefetched_clob_evidence",
      ws_evidence_source: wsEvidenceSource,
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
    const strategyId = String(payload?.strategy_id || "");
    if (RETIRED_STRATEGY_IDS.has(strategyId)) {
      return {
        ok: false, accepted: false, pending: false, reconcile_required: false,
        error: "retired_strategy_forbidden",
        exchange_post_started: false,
        idempotency_key: String(payload?.idempotency_key || ""),
        order_intent_id: String(payload?.order_intent_id || ""),
        strategy_id: strategyId,
        source: SERVICE_NAME,
      };
    }
    const asosLiveAdmission = asosArgmaxLiveAdmission(payload);
    const isAsosLive = asosLiveAdmission.ok === true;
    if (
      [
        ASOS_ARGMAX_LIVE_STRATEGY_ID,
        ASOS_ARGMAX_EXIT_STRATEGY_ID,
        ASOS_ARGMAX_ROTATION_SELL_STRATEGY_ID,
      ].includes(
        String(payload?.strategy_id || "")
      ) && !isAsosLive
    ) {
      return {
        ok: false, accepted: false, pending: false, reconcile_required: false,
        error: asosLiveAdmission.reason,
        exchange_post_started: false,
        idempotency_key: String(payload?.idempotency_key || ""),
        order_intent_id: String(payload?.order_intent_id || ""),
        source: SERVICE_NAME,
      };
    }
    if (truthy(env("PM_CLOB_ORDER_SERVICE_READ_ONLY", "0")) && !isAsosLive) {
      return {
        ok: false, accepted: false, pending: false, reconcile_required: false,
        error: "clob_order_service_read_only",
        exchange_post_started: false,
        idempotency_key: String(payload?.idempotency_key || ""),
        order_intent_id: String(payload?.order_intent_id || ""),
        source: SERVICE_NAME,
      };
    }
    if (livePausePresent()) {
      if (!isAsosLive) {
        return {
          ok: false, accepted: false, pending: false, reconcile_required: false,
          error: "clob_order_service_live_paused",
          exchange_post_started: false,
          idempotency_key: String(payload?.idempotency_key || ""),
          order_intent_id: String(payload?.order_intent_id || ""),
          source: SERVICE_NAME,
        };
      }
    }
    // A fresh attestation that the exchange refuses this egress makes the POST
    // a guaranteed 403.  Reject here, alongside the other admission gates and
    // ahead of idempotency: nothing is signed, nothing is posted, and no
    // durable identity is created, so the caller sees an ordinary terminal
    // rejection it can retry on the next signal instead of an ambiguous
    // in-flight order whose budget stays reserved until someone reconciles it.
    if (this.regionBlockedNow() && !truthy(env("PM_CLOB_IGNORE_REGION_BLOCK", "0"))) {
      return {
        ok: false, accepted: false, pending: false, reconcile_required: false,
        error: `clob_region_blocked:${this.region?.country || "unknown"}`,
        error_class: "clob_order_geoblocked",
        geoblocked: true, retryable: false, fatal: true,
        exchange_post_started: false,
        idempotency_key: String(payload?.idempotency_key || ""),
        order_intent_id: String(payload?.order_intent_id || ""),
        source: SERVICE_NAME,
        service_pid: process.pid,
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
    const deadlineAdmission = targetDirectEntryHardDeadline(payload);
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
    const isSell = String(payload.side || "BUY").toUpperCase() === "SELL";
    const lane = isSell
      ? "sell_parallel"
      : priorityEntry
        ? "direct_target_primary_entry_parallel"
        : "default_serial";
    // Signal-coupled YES SELL and NO BUY are independent FAK orders. Once
    // each identity is durable they may build, sign and POST concurrently.
    const base = (isSell || priorityEntry) ? Promise.resolve() : this.submitChain;
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
          order_build_ms: Number.isFinite(Number(meta.order_build_ms)) ? Number(meta.order_build_ms) : null,
          signed_persist_ms: Number.isFinite(Number(meta.signed_persist_ms)) ? Number(meta.signed_persist_ms) : null,
          pre_post_deadline_check_ms: Number.isFinite(Number(meta.pre_post_deadline_check_ms))
            ? Number(meta.pre_post_deadline_check_ms)
            : null,
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
    if (!isSell && !priorityEntry && !parallelCopyFollow) {
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
    let size = Number(rawPayload.size);
    const amount = Number(rawPayload.amount);
    const sideRaw = String(rawPayload.side || "BUY").toUpperCase();
    const side = sideRaw === "SELL" ? Side.SELL : Side.BUY;
    const orderTypeRaw = String(rawPayload.order_type || rawPayload.orderType || "GTC").toUpperCase();
    const orderType = OrderType[orderTypeRaw] || OrderType.GTC;
    const isMarket = ["FOK", "FAK"].includes(orderTypeRaw);
    const explicitMarketableLimit = isMarket
      && Number.isFinite(price)
      && Number.isFinite(size)
      && truthy(env("PM_CLOB_EXPLICIT_FAK_LIMIT_ORDER", "1"), true);
    // A controlled basket reduction is a price-protected limit FAK, not a
    // naked market order.  Permit this one explicit SELL shape through the
    // gateway's limit-only boundary while keeping ordinary FAK/FOK callers
    // behind the existing allow_market gate.
    const controlledRebalanceSellFak = (
      side === Side.SELL
      && orderTypeRaw === "FAK"
      && explicitMarketableLimit
      && rawPayload.allow_market === false
      && String(rawPayload.order_execution_domain || "") === "settlement_floor_basket"
      && String(rawPayload.order_intent_action || "") === "controlled_rebalance"
    );
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
    this.ensureTokenConditionFromDb(tokenID);
    if (truthy(env("PM_CLOB_FORCE_PROXY", "0")) && this.proxy?.mode !== "proxy") {
      throw errorWithMeta(`proxy_required_unavailable:${this.proxy?.mode || "unknown"}`);
    }
    if (isMarket && !allowMarket && !controlledRebalanceSellFak) {
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
    let response;
    let signedOrder;
    let expectedOrderID = "";
    let exchangePostStarted = false;
    let exchangePostStartedAt = "";
    let orderBuildMs = null;
    let signedPersistMs = null;
    let prePostDeadlineCheckMs = null;
    const assertTargetDirectEntryDeadline = () => {
      const status = targetDirectEntryHardDeadline(rawPayload);
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
    if (isMarket && !explicitMarketableLimit) {
      let finalAmount = Number.isFinite(amount) ? amount : NaN;
      if (side === Side.SELL && Number.isFinite(size)) finalAmount = size;
      if (side !== Side.SELL && !Number.isFinite(finalAmount) && Number.isFinite(size) && Number.isFinite(price)) {
        finalAmount = size * price;
      }
      if (!Number.isFinite(finalAmount) && Number.isFinite(size)) finalAmount = size;
      if (!Number.isFinite(finalAmount) || finalAmount <= 0) throw errorWithMeta("missing_args:amount");
      const submitStarted = Date.now();
      try {
        const buildStarted = Date.now();
        signedOrder = await this.client.createMarketOrder({
          tokenID,
          side,
          amount: finalAmount,
          ...(Number.isFinite(price) ? { price } : {}),
          ...(builderCode ? { builderCode } : {}),
        }, { tickSize, negRisk });
        orderBuildMs = Date.now() - buildStarted;
        const persistStarted = Date.now();
        persistSignedPrepared();
        signedPersistMs = Date.now() - persistStarted;
        const deadlineCheckStarted = Date.now();
        assertTargetDirectEntryDeadline();
        prePostDeadlineCheckMs = Date.now() - deadlineCheckStarted;
        exchangePostStartedAt = new Date().toISOString();
        exchangePostStarted = true;
        response = this.proxy?.mode === "proxy"
          ? await this.postOrderViaProxy(signedOrder, orderType, false, deferExec)
          : await this.client.postOrder(signedOrder, orderType, false, deferExec);
      } catch (err) {
        err.serviceMeta = metaNow({
          submit_call_ms: Date.now() - submitStarted,
          expected_order_id: expectedOrderID || null,
          signed_order_prepared: Boolean(expectedOrderID),
          order_build_ms: orderBuildMs,
          signed_persist_ms: signedPersistMs,
          pre_post_deadline_check_ms: prePostDeadlineCheckMs,
          exchange_post_started: exchangePostStarted,
          exchange_post_started_at: exchangePostStartedAt || null,
        });
        throw err;
      }
    } else {
      const submitStarted = Date.now();
      try {
        // Protected BUY FAK/FOK orders must have a cent-aligned maker amount.
        // The exchange permits up to four decimal taker shares, but the
        // maker USDC amount is cent precision.  Round only downward so a
        // protected FAK never increases exposure; the caller's residual
        // reconciliation path will place any remainder separately.
        if (explicitMarketableLimit && ["FAK", "FOK"].includes(orderTypeRaw) && side === Side.BUY) {
          size = alignMarketBuySizeToMakerCents(price, size);
          if (!Number.isFinite(size) || size <= 0) throw errorWithMeta("market_buy_size_nonpositive_after_precision_alignment");
        }
        const buildStarted = Date.now();
        signedOrder = await this.client.createOrder({
          tokenID,
          price,
          size,
          side,
          ...(builderCode ? { builderCode } : {}),
        }, { tickSize, negRisk });
        orderBuildMs = Date.now() - buildStarted;
        const persistStarted = Date.now();
        persistSignedPrepared();
        signedPersistMs = Date.now() - persistStarted;
        const deadlineCheckStarted = Date.now();
        assertTargetDirectEntryDeadline();
        prePostDeadlineCheckMs = Date.now() - deadlineCheckStarted;
        exchangePostStartedAt = new Date().toISOString();
        exchangePostStarted = true;
        response = this.proxy?.mode === "proxy"
          ? await this.postOrderViaProxy(signedOrder, orderType, postOnly, deferExec)
          : await this.client.postOrder(signedOrder, orderType, postOnly, deferExec);
      } catch (err) {
        err.serviceMeta = metaNow({
          submit_call_ms: Date.now() - submitStarted,
          expected_order_id: expectedOrderID || null,
          signed_order_prepared: Boolean(expectedOrderID),
          order_build_ms: orderBuildMs,
          signed_persist_ms: signedPersistMs,
          pre_post_deadline_check_ms: prePostDeadlineCheckMs,
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
    this.cachedCollateral = null;
    this.cachedConditional.delete(tokenID);
    if (side === Side.BUY) this.registeredPositionTokens.add(tokenID);
    return {
      ok: true,
      orderID: response.orderID,
      expected_order_id: expectedOrderID || response.orderID,
      expected_order_id_matches: !expectedOrderID || expectedOrderID === response.orderID,
      status: response.status,
      original_size: response.original_size ?? response.originalSize ?? (Number.isFinite(size) ? size : null),
      size_matched: response.size_matched ?? response.sizeMatched ?? response.matched_size
        ?? response.takingAmount ?? response.taking_amount ?? null,
      shares_matched: side === Side.SELL
        ? (response.makingAmount ?? response.making_amount ?? response.size_matched
          ?? response.sizeMatched ?? response.matched_size ?? null)
        : (response.size_matched ?? response.sizeMatched ?? response.matched_size
          ?? response.takingAmount ?? response.taking_amount ?? null),
      exchange_response: response,
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
      order_build_ms: orderBuildMs,
      signed_persist_ms: signedPersistMs,
      pre_post_deadline_check_ms: prePostDeadlineCheckMs,
      tick_size: tickSize,
      neg_risk: negRisk,
    };
  }

  async cancelOrder(rawPayload) {
    const orderID = String(rawPayload?.order_id || rawPayload?.orderID || "").trim();
    const idempotencyKey = String(rawPayload?.idempotency_key || "").trim();
    if (!orderID || !idempotencyKey) throw new Error("cancel_order_identity_missing");
    if (!this.client) throw new Error(this.startError ? `service_not_ready:${this.startError}` : "service_not_ready");
    const startedAt = new Date().toISOString();
    try {
      const response = this.proxy?.mode === "proxy"
        ? await this.requestJsonViaProxy("DELETE", "/order", { orderID })
        : await this.client.cancelOrder({ orderID });
      if (response && response.error) {
        throw new Error(String(response.error));
      }
      return {
        ok: true,
        cancelled: true,
        orderID,
        idempotency_key: idempotencyKey,
        cancel_post_started: true,
        cancel_post_started_at: startedAt,
        response,
        source: SERVICE_NAME,
      };
    } catch (err) {
      return {
        ok: false,
        reconcile_required: true,
        orderID,
        idempotency_key: idempotencyKey,
        cancel_post_started: true,
        cancel_post_started_at: startedAt,
        error: redactErrorText(err?.message || String(err || "cancel_order_failed")),
        source: SERVICE_NAME,
      };
    }
  }

  async lookupExchangeOrder(rawPayload) {
    const orderID = String(rawPayload?.order_id || rawPayload?.orderID || "").trim();
    if (!orderID) throw new Error("lookup_exchange_order_identity_missing");
    if (!this.client) throw new Error(this.startError ? `service_not_ready:${this.startError}` : "service_not_ready");
    try {
      const rawResponse = this.proxy?.mode === "proxy"
        ? await this.requestJsonViaProxy("GET", `/data/order/${orderID}`)
        : await this.client.getOrder(orderID);
      const response = unwrapProxyLookupResponse(rawResponse);
      if (response && response.error) {
        throw new Error(String(response.error));
      }
      // A null/empty response is not evidence that an order is open.  The
      // CLOB endpoint can return an empty JSON payload during proxy failure or
      // for an order that is no longer queryable.  Treating that as `open`
      // previously allowed reconciliation to mark a partial/unknown order as
      // settled and could cause a duplicate top-up.
      const recoveredOrderID = String(response?.id || response?.orderID || "").trim();
      if (!response || typeof response !== "object" || Array.isArray(response)) {
        return {
          ok: false,
          orderID,
          status: "unknown",
          error: "clob_order_lookup_empty_response",
          source: SERVICE_NAME,
        };
      }
      if (!recoveredOrderID || recoveredOrderID.toLowerCase() !== orderID.toLowerCase()) {
        return {
          ok: false,
          orderID,
          status: "unknown",
          error: "clob_order_lookup_identity_mismatch",
          recovered_order_id: recoveredOrderID || null,
          response,
          source: SERVICE_NAME,
        };
      }
      return { ok: true, orderID, status: response.status || "unknown", response, source: SERVICE_NAME };
    } catch (err) {
      const message = redactErrorText(err?.message || String(err || "lookup_exchange_order_failed"));
      // The authenticated order endpoint may represent a successfully
      // cancelled order as not found.  Return explicit evidence; the Python
      // coordinator still requires the prior cancel POST identity.
      const notFound = /404|not.?found/i.test(message);
      return { ok: notFound, orderID, status: notFound ? "cancelled" : "unknown", error: notFound ? "" : message, source: SERVICE_NAME };
    }
  }

  async lookupOpenOrders(rawPayload = {}) {
    if (!this.client) throw new Error(this.startError ? `service_not_ready:${this.startError}` : "service_not_ready");
    const maxPagesRaw = Number(rawPayload?.max_pages ?? 3);
    const maxPages = Number.isFinite(maxPagesRaw) && maxPagesRaw > 0 ? Math.min(10, Math.floor(maxPagesRaw)) : 3;
    const requestedAsset = String(rawPayload?.asset_id || rawPayload?.token_id || "").trim();
    const requestedMarket = String(rawPayload?.market || "").trim();
    const collected = [];
    let cursor = "MA==";
    try {
      for (let page = 0; page < maxPages && cursor !== "LTE="; page += 1) {
        // The account-wide /data/orders page can become very slow behind a
        // proxy when the account has a large historical order set.  Preserve
        // the caller's exact market/asset scope in the upstream request so a
        // reconciliation probe can stay bounded to the target basket.
        const query = { next_cursor: cursor };
        if (requestedMarket) query.market = requestedMarket;
        if (requestedAsset) query.asset_id = requestedAsset;
        const response = this.proxy?.mode === "proxy"
          ? await this.requestJsonViaProxy("GET", "/data/orders", undefined, query)
          : await this.client.getOpenOrders(undefined, true, cursor);
        if (Array.isArray(response)) {
          collected.push(...response);
          break;
        }
        if (!response || typeof response !== "object" || Array.isArray(response)) {
          return { ok: false, status: "unknown", error: "clob_open_orders_response_invalid", source: SERVICE_NAME };
        }
        const rows = Array.isArray(response.data)
          ? response.data
          : Array.isArray(response.orders)
            ? response.orders
            : Array.isArray(response.open_orders)
              ? response.open_orders
              : null;
        if (!rows) {
          return { ok: false, status: "unknown", error: "clob_open_orders_response_shape_invalid", response, source: SERVICE_NAME };
        }
        collected.push(...rows);
        const next = String(response.next_cursor || response.nextCursor || "LTE=").trim();
        if (!next || next === cursor || rows.length === 0) break;
        cursor = next;
      }
      const openOrders = collected.filter((row) => {
        if (!row || typeof row !== "object") return false;
        const asset = String(row.asset_id || row.assetId || row.token_id || row.tokenId || "").trim();
        const market = String(row.market || row.condition_id || row.conditionId || "").trim();
        return (!requestedAsset || asset === requestedAsset) && (!requestedMarket || market === requestedMarket);
      });
      return {
        ok: true,
        status: "ok",
        open_orders: openOrders,
        total_open_orders: collected.length,
        pages_checked: maxPages,
        source: SERVICE_NAME,
      };
    } catch (err) {
      return {
        ok: false,
        status: "unknown",
        error: redactErrorText(err?.message || String(err || "lookup_open_orders_failed")),
        source: SERVICE_NAME,
      };
    }
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
    if (req.method === "POST" && parsedUrl.pathname === "/place-order") {
      const payload = await readJsonBody(req);
      const response = await service.placeOrderWithIdempotency(payload);
      sendJson(res, 200, response);
      return;
    }
    if (req.method === "POST" && parsedUrl.pathname === "/cancel-order") {
      const payload = await readJsonBody(req);
      sendJson(res, 200, await service.cancelOrder(payload));
      return;
    }
    if (req.method === "POST" && parsedUrl.pathname === "/lookup-exchange-order") {
      const payload = await readJsonBody(req);
      sendJson(res, 200, await service.lookupExchangeOrder(payload));
      return;
    }
    if (req.method === "POST" && parsedUrl.pathname === "/lookup-open-orders") {
      const payload = await readJsonBody(req);
      sendJson(res, 200, await service.lookupOpenOrders(payload));
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
