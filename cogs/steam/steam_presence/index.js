#!/usr/bin/env node
'use strict';

/**
 * Steam Bridge – Auth + Task Executor + Quick Invites
 * - Verbindet sich als Headless-Steam-Client
 * - Verarbeitet Tasks aus der SQLite-Tabelle `steam_tasks`
 * - Erzeugt/verwaltet Quick-Invite-Links über `quick_invites.js`
 *
 * Neue Tasks:
 *   - AUTH_QUICK_INVITE_CREATE
 *   - AUTH_QUICK_INVITE_ENSURE_POOL
 *
 * Beibehaltende Tasks:
 *   - AUTH_STATUS
 *   - AUTH_LOGIN
 *   - AUTH_GUARD_CODE
 *   - AUTH_LOGOUT
 *
 * Erfordert: steam-user, better-sqlite3
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const https = require('https');
const { URL } = require('url');
const protobuf = require('protobufjs');
const SteamUser = require('steam-user');
const Database = require('better-sqlite3');
const { QuickInvites } = require('./quick_invites');
const { StatusAnzeige } = require('./statusanzeige');
const { DeadlockGcBot } = require('./deadlock_gc_bot');
const {
  DEADLOCK_GC_PROTOCOL_OVERRIDE_PATH,
  getHelloPayloadOverride,
  getPlaytestOverrides,
  getOverrideInfo: getGcOverrideInfo,
} = require('./deadlock_gc_protocol');

let SteamID = null;
if (SteamUser && SteamUser.SteamID) {
  SteamID = SteamUser.SteamID;
} else {
  try {
    SteamID = require('steamid');
  } catch (err) {
    throw new Error(`SteamID helper unavailable: ${err && err.message ? err.message : String(err)}`);
  }
}

// Deadlock App ID - try multiple known IDs if needed
const DEADLOCK_APP_IDS = [
  Number.parseInt(process.env.DEADLOCK_APPID || '1422450', 10), // Primary
  1422450, // Official Deadlock App ID
  730,     // CS2 fallback for testing GC protocol
];
const DEADLOCK_APP_ID = DEADLOCK_APP_IDS[0];

// Function to try different App IDs if the primary fails
function getWorkingAppId() {
  return DEADLOCK_APP_IDS.find(id => id > 0) || 1422450;
}
const PROTO_MASK = SteamUser.GCMsgProtoBuf || 0x80000000;
const GC_MSG_CLIENT_HELLO = 4006;
const GC_MSG_CLIENT_WELCOME = 4004;
const GC_MSG_CLIENT_TO_GC_UPDATE_HERO_BUILD = 9193;
const GC_MSG_CLIENT_TO_GC_UPDATE_HERO_BUILD_RESPONSE = 9194;

// Multiple potential message IDs to try (Deadlock's actual IDs may have changed)
const DEFAULT_PLAYTEST_MSG_IDS = [
  { send: 9189, response: 9190, name: 'original' },
  { send: 9000, response: 9001, name: 'alternative_1' },
  { send: 8000, response: 8001, name: 'alternative_2' },
  { send: 7500, response: 7501, name: 'alternative_3' },
  { send: 10000, response: 10001, name: 'alternative_4' },
];

let playtestMsgConfigs = [...DEFAULT_PLAYTEST_MSG_IDS];
const playtestOverrideConfig = getPlaytestOverrides() || null;
let buildPlaytestPayloadOverrideFn = null;

if (playtestOverrideConfig) {
  if (
    playtestOverrideConfig.messageIds &&
    Number.isFinite(playtestOverrideConfig.messageIds.send) &&
    Number.isFinite(playtestOverrideConfig.messageIds.response)
  ) {
    const overrideEntry = {
      send: Number(playtestOverrideConfig.messageIds.send),
      response: Number(playtestOverrideConfig.messageIds.response),
      name: playtestOverrideConfig.name || 'config_override',
      appId: playtestOverrideConfig.appId,
    };
    if (playtestOverrideConfig.exclusive) {
      playtestMsgConfigs = [overrideEntry];
    } else {
      playtestMsgConfigs.unshift(overrideEntry);
    }
  }
  if (typeof playtestOverrideConfig.buildPayload === 'function') {
    buildPlaytestPayloadOverrideFn = playtestOverrideConfig.buildPayload;
  }
}

if (!playtestMsgConfigs.length) {
  playtestMsgConfigs = [...DEFAULT_PLAYTEST_MSG_IDS];
}

// Current message IDs (will be updated when working ones are found)
let GC_MSG_SUBMIT_PLAYTEST_USER = playtestMsgConfigs[0].send;
let GC_MSG_SUBMIT_PLAYTEST_USER_RESPONSE = playtestMsgConfigs[0].response;
const GC_CLIENT_HELLO_PROTOCOL_VERSION_RAW = Number.parseInt(process.env.DEADLOCK_GC_PROTOCOL_VERSION || '1', 10);
const GC_CLIENT_HELLO_PROTOCOL_VERSION = Number.isFinite(GC_CLIENT_HELLO_PROTOCOL_VERSION_RAW) && GC_CLIENT_HELLO_PROTOCOL_VERSION_RAW > 0
  ? GC_CLIENT_HELLO_PROTOCOL_VERSION_RAW
  : 1;
const STEAM_WEB_API_KEY = ((process.env.STEAM_API_KEY || process.env.STEAM_WEB_API_KEY || '') + '').trim() || null;
const WEB_API_FRIEND_CACHE_TTL_MS = Math.max(
  15000,
  Number.isFinite(Number(process.env.STEAM_WEBAPI_FRIEND_CACHE_MS))
    ? Number(process.env.STEAM_WEBAPI_FRIEND_CACHE_MS)
    : 60000
);
const WEB_API_HTTP_TIMEOUT_MS = Math.max(
  5000,
  Number.isFinite(Number(process.env.STEAM_WEBAPI_TIMEOUT_MS))
    ? Number(process.env.STEAM_WEBAPI_TIMEOUT_MS)
    : 12000
);

// ---------- Logging ----------
const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
const LOG_LEVEL = (process.env.LOG_LEVEL || 'info').toLowerCase();
const LOG_THRESHOLD = Object.prototype.hasOwnProperty.call(LOG_LEVELS, LOG_LEVEL)
  ? LOG_LEVELS[LOG_LEVEL]
  : LOG_LEVELS.info;

function log(level, message, extra = undefined) {
  const lvl = LOG_LEVELS[level];
  if (lvl === undefined || lvl > LOG_THRESHOLD) return;
  const payload = { time: new Date().toISOString(), level, msg: message };
  if (extra && typeof extra === 'object') {
    for (const [key, value] of Object.entries(extra)) {
      if (value === undefined) continue;
      payload[key] = value;
    }
  }
  console.log(JSON.stringify(payload));
}

const PROJECT_ROOT = path.resolve(__dirname, '..', '..', '..');
const GC_TRACE_LOG_PATH = path.join(PROJECT_ROOT, 'logs', 'deadlock_gc_messages.log');
let gcTraceStream = null;

function writeDeadlockGcTrace(event, details = {}) {
  try {
    if (!gcTraceStream) {
      fs.mkdirSync(path.dirname(GC_TRACE_LOG_PATH), { recursive: true });
      gcTraceStream = fs.createWriteStream(GC_TRACE_LOG_PATH, { flags: 'a' });
    }
    const entry = {
      time: new Date().toISOString(),
      event,
      ...details,
    };
    gcTraceStream.write(`${JSON.stringify(entry)}${os.EOL}`);
  } catch (err) {
    // Avoid recursive logging loops.
    console.error('Failed to write Deadlock GC trace', err && err.message ? err.message : err);
  }
}

function closeDeadlockGcTrace() {
  if (!gcTraceStream) return;
  try {
    gcTraceStream.end();
  } catch (_) {
    // ignore
  } finally {
    gcTraceStream = null;
  }
}

process.on('exit', closeDeadlockGcTrace);
process.on('SIGINT', closeDeadlockGcTrace);
process.on('SIGTERM', closeDeadlockGcTrace);

function normalizeToBuffer(value) {
  if (!value && value !== 0) return null;
  if (Buffer.isBuffer(value)) return value;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    const hexCandidate = trimmed.startsWith('0x') ? trimmed.slice(2) : trimmed;
    if (/^[0-9a-fA-F]+$/.test(hexCandidate) && hexCandidate.length % 2 === 0) {
      return Buffer.from(hexCandidate, 'hex');
    }
    return Buffer.from(trimmed, 'utf8');
  }
  if (ArrayBuffer.isView(value)) {
    return Buffer.from(value.buffer, value.byteOffset, value.byteLength);
  }
  if (value && typeof value === 'object' && value.type === 'Buffer' && Array.isArray(value.data)) {
    return Buffer.from(value.data);
  }
  return null;
}

function getDeadlockGcTokenCount() {
  if (!client) return 0;
  const tokens = client._gcTokens;
  if (Array.isArray(tokens)) return tokens.length;
  if (tokens && typeof tokens.length === 'number') return tokens.length;
  return 0;
}

async function requestDeadlockGcTokens(reason = 'unspecified') {
  if (!client || typeof client._sendAuthList !== 'function') {
    log('warn', 'Cannot request Deadlock GC tokens - steam-user _sendAuthList unavailable', { reason });
    return false;
  }
  if (!client.steamID) {
    log('debug', 'Skipping GC token request - SteamID missing', { reason });
    return false;
  }
  if (gcTokenRequestInFlight) {
    log('debug', 'GC token request already in flight', { reason });
    return false;
  }
  gcTokenRequestInFlight = true;
  const haveTokens = getDeadlockGcTokenCount();
  try {
    log('info', 'Requesting Deadlock GC tokens', {
      reason,
      haveTokens,
      appId: DEADLOCK_APP_ID,
    });
    writeDeadlockGcTrace('request_gc_tokens', {
      reason,
      haveTokens,
    });
    await client._sendAuthList(DEADLOCK_APP_ID);
    const current = getDeadlockGcTokenCount();
    log('debug', 'GC token request finished', {
      reason,
      before: haveTokens,
      after: current,
    });
    writeDeadlockGcTrace('request_gc_tokens_complete', {
      reason,
      before: haveTokens,
      after: current,
    });
    return true;
  } catch (err) {
    log('error', 'Failed to request Deadlock GC tokens', {
      reason,
      error: err && err.message ? err.message : String(err),
    });
    writeDeadlockGcTrace('request_gc_tokens_failed', {
      reason,
      error: err && err.message ? err.message : String(err),
    });
    return false;
  } finally {
    gcTokenRequestInFlight = false;
  }
}

const gcOverrideInfo = getGcOverrideInfo();
if (playtestOverrideConfig) {
  log('info', 'Deadlock GC override module active', {
    path: gcOverrideInfo.path,
    messageIdSend: playtestOverrideConfig.messageIds?.send,
    messageIdResponse: playtestOverrideConfig.messageIds?.response,
    exclusive: Boolean(playtestOverrideConfig.exclusive),
  });
} else {
  log('debug', 'No Deadlock GC override module detected', {
    path: gcOverrideInfo.path,
  });
}

const nowSeconds = () => Math.floor(Date.now() / 1000);

const sleep = (ms) =>
  new Promise((resolve) => setTimeout(resolve, Math.max(0, Number.isFinite(ms) ? ms : 0)));

function toPositiveInt(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || value <= 0) return null;
    return Math.floor(value);
  }
  if (typeof value === 'string' && value.trim().length > 0) {
    const parsed = Number.parseInt(value, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) return null;
    return parsed;
  }
  return null;
}

function httpGetJson(url, timeoutMs = WEB_API_HTTP_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    try {
      const req = https.request(
        url,
        {
          method: 'GET',
          headers: {
            'User-Agent': 'DeadlockSteamBridge/1.0 (+steam_presence)',
            Accept: 'application/json',
          },
        },
        (res) => {
          const chunks = [];
          res.on('data', (chunk) => chunks.push(chunk));
          res.on('end', () => {
            const body = Buffer.concat(chunks);
            const text = body.toString('utf8');
            if (res.statusCode < 200 || res.statusCode >= 300) {
              const err = new Error(`HTTP ${res.statusCode}`);
              err.statusCode = res.statusCode;
              err.body = text;
              return reject(err);
            }
            if (!text) {
              resolve(null);
              return;
            }
            try {
              resolve(JSON.parse(text));
            } catch (err) {
              err.body = text;
              reject(err);
            }
          });
        }
      );
      req.on('error', reject);
      req.setTimeout(Math.max(1000, timeoutMs || WEB_API_HTTP_TIMEOUT_MS), () => {
        req.destroy(new Error('Request timed out'));
      });
      req.end();
    } catch (err) {
      reject(err);
    }
  });
}

async function loadWebApiFriendIds(force = false) {
  if (!STEAM_WEB_API_KEY) {
    if (!webApiFriendCacheWarned) {
      webApiFriendCacheWarned = true;
      log('debug', 'Steam Web API key not configured - friendship fallback disabled');
    }
    return null;
  }
  if (!client || !client.steamID) return null;

  const now = Date.now();
  if (!force && webApiFriendCacheIds && now - webApiFriendCacheLastLoadedAt < WEB_API_FRIEND_CACHE_TTL_MS) {
    return webApiFriendCacheIds;
  }
  if (webApiFriendCachePromise) return webApiFriendCachePromise;

  const url = new URL('https://api.steampowered.com/ISteamUser/GetFriendList/v1/');
  url.searchParams.set('key', STEAM_WEB_API_KEY);
  url.searchParams.set('steamid', client.steamID.getSteamID64());
  url.searchParams.set('relationship', 'friend');

  webApiFriendCachePromise = httpGetJson(url.toString(), WEB_API_HTTP_TIMEOUT_MS)
    .then((body) => {
      const entries = body && body.friendslist && Array.isArray(body.friendslist.friends)
        ? body.friendslist.friends
        : [];
      const set = new Set();
      for (const entry of entries) {
        const sid = entry && entry.steamid ? String(entry.steamid).trim() : '';
        if (sid) set.add(sid);
      }
      webApiFriendCacheIds = set;
      webApiFriendCacheLastLoadedAt = Date.now();
      log('debug', 'Refreshed Steam Web API friend cache', {
        count: set.size,
        ttlMs: WEB_API_FRIEND_CACHE_TTL_MS,
      });
      return set;
    })
    .catch((err) => {
      log('warn', 'Steam Web API friend list request failed', {
        error: err && err.message ? err.message : String(err),
        statusCode: err && err.statusCode ? err.statusCode : undefined,
      });
      return null;
    })
    .finally(() => {
      webApiFriendCachePromise = null;
    });

  return webApiFriendCachePromise;
}

async function isFriendViaWebApi(steamId64) {
  const normalized = String(steamId64 || '').trim();
  if (!normalized) return { friend: false, source: 'webapi', refreshed: false };

  let ids = await loadWebApiFriendIds(false);
  if (ids && ids.has(normalized)) {
    return { friend: true, source: 'webapi-cache', refreshed: false };
  }

  ids = await loadWebApiFriendIds(true);
  if (ids && ids.has(normalized)) {
    return { friend: true, source: 'webapi-refresh', refreshed: true };
  }

  return { friend: false, source: 'webapi', refreshed: true };
}

function getWebApiFriendCacheAgeMs() {
  if (!webApiFriendCacheLastLoadedAt) return null;
  return Math.max(0, Date.now() - webApiFriendCacheLastLoadedAt);
}

function normalizeTimeoutMs(value, fallback, minimum) {
  const parsed = toPositiveInt(value);
  const base = parsed !== null ? parsed : fallback;
  const min = toPositiveInt(minimum);
  return Math.max(min !== null ? min : 0, Number.isFinite(base) ? base : fallback);
}

function normalizeAttempts(value, fallback, maximum = 4) {
  const parsed = toPositiveInt(value);
  const base = parsed !== null ? parsed : fallback;
  const max = toPositiveInt(maximum);
  const clampedMax = max !== null ? max : Math.max(1, base);
  return Math.max(1, Math.min(clampedMax, Number.isFinite(base) ? base : 1));
}

function isTimeoutError(err) {
  if (!err) return false;
  const message = err.message ? err.message : String(err);
  return String(message).toLowerCase().includes('timeout');
}

const MIN_GC_READY_TIMEOUT_MS = 5000;
const DEFAULT_GC_READY_TIMEOUT_MS = normalizeTimeoutMs(
  process.env.DEADLOCK_GC_READY_TIMEOUT_MS,
  120000,
  MIN_GC_READY_TIMEOUT_MS
);
const DEFAULT_GC_READY_ATTEMPTS = normalizeAttempts(
  process.env.DEADLOCK_GC_READY_ATTEMPTS,
  3,
  5
);
const GC_READY_RETRY_DELAY_MS = normalizeTimeoutMs(
  process.env.DEADLOCK_GC_READY_RETRY_DELAY_MS,
  1500,
  250
);

const MIN_PLAYTEST_INVITE_TIMEOUT_MS = 5000;
const DEFAULT_PLAYTEST_INVITE_TIMEOUT_MS = normalizeTimeoutMs(
  process.env.DEADLOCK_PLAYTEST_TIMEOUT_MS,
  30000,
  MIN_PLAYTEST_INVITE_TIMEOUT_MS
);
const DEFAULT_PLAYTEST_INVITE_ATTEMPTS = normalizeAttempts(
  process.env.DEADLOCK_PLAYTEST_RETRY_ATTEMPTS,
  3,
  5
);
const PLAYTEST_RETRY_DELAY_MS = normalizeTimeoutMs(
  process.env.DEADLOCK_PLAYTEST_RETRY_DELAY_MS,
  2000,
  250
);
const INVITE_RESPONSE_MIN_TIMEOUT_MS = MIN_PLAYTEST_INVITE_TIMEOUT_MS;

// ---------- Paths/Config ----------
function resolveDbPath() {
  if (process.env.DEADLOCK_DB_PATH) return path.resolve(process.env.DEADLOCK_DB_PATH);
  const baseDir = process.env.DEADLOCK_DB_DIR
    ? path.resolve(process.env.DEADLOCK_DB_DIR)
    : path.join(os.homedir(), 'Documents', 'Deadlock', 'service');
  return path.join(baseDir, 'deadlock.sqlite3');
}

function ensureDir(dirPath) {
  try { fs.mkdirSync(dirPath, { recursive: true }); } catch (err) { if (err && err.code !== 'EEXIST') throw err; }
}

function readToken(filePath) {
  try { if (!fs.existsSync(filePath)) return ''; return fs.readFileSync(filePath, 'utf8').trim(); }
  catch (err) { log('warn', 'Failed to read token file', { path: filePath, error: err.message }); return ''; }
}

function writeToken(filePath, value) {
  try {
    if (!value) { fs.rmSync(filePath, { force: true }); return; }
    fs.writeFileSync(filePath, `${value}\n`, 'utf8');
  } catch (err) { log('warn', 'Failed to persist token', { path: filePath, error: err.message }); }
}

function safeJsonStringify(value) {
  try { return JSON.stringify(value); }
  catch (err) { log('warn', 'Failed to stringify JSON', { error: err.message }); return null; }
}
function safeJsonParse(value) {
  if (!value) return {};
  try { return JSON.parse(value); }
  catch (err) { throw new Error(`Invalid JSON payload: ${err.message}`); }
}
function safeNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
}

function cleanBuildDetails(details) {
  if (!details || typeof details !== 'object') {
    // Return valid Details_V0 structure with empty arrays
    return { mod_categories: [] };
  }
  const clone = JSON.parse(JSON.stringify(details));

  // Ensure mod_categories exists and is an array
  if (!Array.isArray(clone.mod_categories)) {
    clone.mod_categories = [];
  } else {
    clone.mod_categories = clone.mod_categories.map((cat) => {
      const c = { ...cat };
      if (Array.isArray(c.mods)) {
        c.mods = c.mods.map((m) => {
          const mm = { ...m };
          Object.keys(mm).forEach((k) => { if (mm[k] === null) delete mm[k]; });
          return mm;
        });
      }
      Object.keys(c).forEach((k) => { if (c[k] === null) delete c[k]; });
      return c;
    });
  }

  if (clone.ability_order && Array.isArray(clone.ability_order.currency_changes)) {
    clone.ability_order.currency_changes = clone.ability_order.currency_changes.map((cc) => {
      const obj = { ...cc };
      Object.keys(obj).forEach((k) => { if (obj[k] === null) delete obj[k]; });
      return obj;
    });
  }
  return clone;
}

function composeBuildDescription(base, originId, authorId) {
  const parts = [];
  const desc = (base || '').trim();
  if (desc) parts.push(desc);
  parts.push('www.twitch.tv/earlysalty (deutsch)');
  parts.push('Deutsche Deadlock Community: discord.gg/z5TfVHuQq2');
  if (originId) parts.push(`Original Build ID: ${originId}`);
  if (authorId) parts.push(`Original Author: ${authorId}`);
  return parts.join('\n');
}

function buildUpdateHeroBuild(row, meta = {}) {
  const tags = safeJsonParse(row.tags_json || '[]');
  const details = cleanBuildDetails(safeJsonParse(row.details_json || '{}'));
  const targetName = meta.target_name || row.name || '';
  const targetDescription = meta.target_description || row.description || '';
  const targetLanguage = safeNumber(meta.target_language) ?? safeNumber(row.language) ?? 0;
  const authorId = safeNumber(meta.author_account_id) ?? safeNumber(row.author_account_id);
  const nowTs = Math.floor(Date.now() / 1000);
  const baseVersion = safeNumber(row.version) || 1;
  const originId = safeNumber(meta.origin_build_id) ?? safeNumber(row.origin_build_id) ?? safeNumber(row.hero_build_id);
  return {
    hero_build_id: safeNumber(row.hero_build_id),
    hero_id: safeNumber(row.hero_id),
    author_account_id: authorId,
    origin_build_id: originId,
    last_updated_timestamp: nowTs,
    publish_timestamp: nowTs,
    name: targetName,
    description: composeBuildDescription(targetDescription, originId, authorId),
    language: targetLanguage,
    version: baseVersion + 1,
    tags: Array.isArray(tags) ? tags.map((t) => Number(t)) : [],
    details: details && typeof details === 'object' ? details : {},
  };
}

function buildMinimalHeroBuild(row, meta = {}) {
  log('info', 'buildMinimalHeroBuild: FIXED VERSION v2 - Creating minimal build');
  const targetName = meta.target_name || row.name || '';
  const targetDescription = meta.target_description || row.description || '';
  const targetLanguage = safeNumber(meta.target_language) ?? 0;
  const authorId = safeNumber(meta.author_account_id) ?? safeNumber(row.author_account_id);
  const result = {
    hero_id: safeNumber(row.hero_id),
    author_account_id: authorId,
    origin_build_id: undefined,
    last_updated_timestamp: undefined,
    name: targetName,
    description: composeBuildDescription(targetDescription, row.hero_build_id, authorId),
    language: targetLanguage,
    version: 1,
    tags: [],
    details: { mod_categories: [] },
    publish_timestamp: undefined,
  };
  log('info', 'buildMinimalHeroBuild: Result details', {
    detailsType: typeof result.details,
    detailsKeys: Object.keys(result.details),
    modCategoriesIsArray: Array.isArray(result.details.mod_categories),
    modCategoriesLength: result.details.mod_categories.length
  });
  return result;
}
function truncateError(message, limit = 1500) {
  if (!message) return null;
  const text = String(message);
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 3)}...`;
}

function mapHeroBuildFromRow(row, meta = {}) {
  if (!row) throw new Error('hero_build_sources row missing');
  const tags = safeJsonParse(row.tags_json || '[]');
  const details = cleanBuildDetails(safeJsonParse(row.details_json || '{}'));
  const targetName = meta.target_name || row.name || '';
  const targetDescription = meta.target_description || row.description || '';
  const targetLanguage = safeNumber(meta.target_language) ?? safeNumber(row.language) ?? 0;
  const authorId = safeNumber(meta.author_account_id) ?? safeNumber(row.author_account_id);
  const nowTs = Math.floor(Date.now() / 1000);
  return {
    hero_id: safeNumber(row.hero_id),
    author_account_id: authorId,
    origin_build_id: safeNumber(meta.origin_build_id) ?? safeNumber(row.hero_build_id) ?? safeNumber(row.origin_build_id),
    last_updated_timestamp: nowTs,
    publish_timestamp: nowTs,
    name: targetName,
    description: composeBuildDescription(targetDescription, meta.origin_build_id ?? row.hero_build_id, authorId),
    language: targetLanguage,
    version: safeNumber(meta.version) ?? (safeNumber(row.version) || 1),
    tags: Array.isArray(tags) ? tags.map((t) => Number(t)) : [],
    details: details && typeof details === 'object' ? details : {},
  };
}

async function sendHeroBuildUpdate(heroBuild) {
  await loadHeroBuildProto();
  if (!heroBuild || typeof heroBuild !== 'object') throw new Error('heroBuild payload missing');

  log('info', 'sendHeroBuildUpdate: Creating message', {
    heroBuild: JSON.stringify(heroBuild),
    heroBuildKeys: Object.keys(heroBuild)
  });

  // Remove undefined fields - protobuf doesn't like them!
  const cleanedHeroBuild = {};
  for (const key in heroBuild) {
    if (heroBuild[key] !== undefined) {
      cleanedHeroBuild[key] = heroBuild[key];
    }
  }

  log('info', 'sendHeroBuildUpdate: Cleaned heroBuild', {
    cleanedKeys: Object.keys(cleanedHeroBuild),
    removedKeys: Object.keys(heroBuild).filter(k => heroBuild[k] === undefined)
  });

  // CRITICAL: Create a proper CMsgHeroBuild message first!
  // Passing a plain JS object to UpdateMsg.create() results in an empty payload.
  // ALSO CRITICAL: The field name is 'heroBuild' (camelCase), not 'hero_build'!
  // Protobufjs converts snake_case to camelCase automatically.
  log('info', 'sendHeroBuildUpdate: About to create HeroBuildMsg', {
    cleanedHeroBuild: JSON.stringify(cleanedHeroBuild),
    detailsType: typeof cleanedHeroBuild.details,
    detailsKeys: cleanedHeroBuild.details ? Object.keys(cleanedHeroBuild.details) : 'null/undefined',
    modCategoriesIsArray: Array.isArray(cleanedHeroBuild.details?.mod_categories)
  });
  const heroBuildMsg = HeroBuildMsg.create(cleanedHeroBuild);
  log('info', 'sendHeroBuildUpdate: HeroBuildMsg created successfully');
  const message = UpdateHeroBuildMsg.create({ heroBuild: heroBuildMsg });
  log('info', 'sendHeroBuildUpdate: UpdateHeroBuildMsg created successfully');

  log('info', 'sendHeroBuildUpdate: Message created', {
    message: JSON.stringify(message),
    messageKeys: Object.keys(message)
  });

  const payload = UpdateHeroBuildMsg.encode(message).finish();

  log('info', 'sendHeroBuildUpdate: Payload encoded', {
    payloadType: typeof payload,
    payloadIsBuffer: Buffer.isBuffer(payload),
    payloadLength: payload ? payload.length : 'null/undefined'
  });

  // Validate payload before using it
  if (!payload || !Buffer.isBuffer(payload)) {
    throw new Error(`Invalid payload after encoding: type=${typeof payload}, isBuffer=${Buffer.isBuffer(payload)}`);
  }
  if (payload.length === 0) {
    throw new Error('Encoded payload is empty - this indicates a protobuf encoding issue');
  }

  return new Promise((resolve, reject) => {
    if (heroBuildPublishWaiter) {
      reject(new Error('Another hero build publish is in flight'));
      return;
    }
    const timeout = setTimeout(() => {
      heroBuildPublishWaiter = null;
      reject(new Error('Timed out waiting for build publish response'));
    }, 20000);
    heroBuildPublishWaiter = {
      resolve: (resp) => { clearTimeout(timeout); heroBuildPublishWaiter = null; resolve(resp); },
      reject: (err) => { clearTimeout(timeout); heroBuildPublishWaiter = null; reject(err); },
    };
    writeDeadlockGcTrace('send_update_hero_build', {
      heroId: heroBuild.hero_id,
      language: heroBuild.language,
      name: heroBuild.name,
      mode: heroBuild.hero_build_id ? 'update' : 'new',
      version: heroBuild.version,
      origin_build_id: heroBuild.origin_build_id,
      author: heroBuild.author_account_id,
      payloadHex: payload.toString('hex'),
    });
    log('info', 'Sending UpdateHeroBuild', {
      payloadHex: payload.toString('hex').slice(0, 200),
      payloadLength: payload.length,
      heroId: heroBuild.hero_id,
      language: heroBuild.language,
      name: heroBuild.name,
      mode: heroBuild.hero_build_id ? 'update' : 'new',
      version: heroBuild.version,
      origin_build_id: heroBuild.origin_build_id,
      author: heroBuild.author_account_id,
    });
    client.sendToGC(DEADLOCK_APP_ID, PROTO_MASK | GC_MSG_CLIENT_TO_GC_UPDATE_HERO_BUILD, payload);
  });
}
function wrapOk(result) {
  if (result && typeof result === 'object' && Object.prototype.hasOwnProperty.call(result, 'ok')) {
    return result;
  }
  if (result === undefined) {
    return { ok: true };
  }
  return { ok: true, data: result };
}

const DATA_DIR = path.resolve(process.env.STEAM_PRESENCE_DATA_DIR || path.join(__dirname, '.steam-data'));
ensureDir(DATA_DIR);
const REFRESH_TOKEN_PATH = path.join(DATA_DIR, 'refresh.token');
const MACHINE_TOKEN_PATH = path.join(DATA_DIR, 'machine_auth_token.txt');

const ACCOUNT_NAME = process.env.STEAM_BOT_USERNAME || process.env.STEAM_LOGIN || process.env.STEAM_ACCOUNT || '';
const ACCOUNT_PASSWORD = process.env.STEAM_BOT_PASSWORD || process.env.STEAM_PASSWORD || '';

const TASK_POLL_INTERVAL_MS = parseInt(process.env.STEAM_TASK_POLL_MS || '2000', 10);
const RECONNECT_DELAY_MS = parseInt(process.env.STEAM_RECONNECT_DELAY_MS || '5000', 10);
const COMMAND_BOT_KEY = 'steam';
const COMMAND_POLL_INTERVAL_MS = parseInt(process.env.STEAM_COMMAND_POLL_MS || '2000', 10);
const STATE_PUBLISH_INTERVAL_MS = parseInt(process.env.STEAM_STATE_PUBLISH_MS || '15000', 10);
const DB_BUSY_TIMEOUT_MS = Math.max(5000, parseInt(process.env.DEADLOCK_DB_BUSY_TIMEOUT_MS || '15000', 10));

const dbPath = resolveDbPath();
ensureDir(path.dirname(dbPath));
log('info', 'Using SQLite database', { dbPath });
const db = new Database(dbPath);
db.pragma('journal_mode = WAL');
db.pragma('synchronous = NORMAL');
db.pragma(`busy_timeout = ${DB_BUSY_TIMEOUT_MS}`);

// ---------- Protobuf (Hero Builds) ----------
const HERO_BUILD_PROTO_PATH = path.join(__dirname, 'protos', 'hero_build.proto');
let heroBuildRoot = null;
let HeroBuildMsg = null;
let UpdateHeroBuildMsg = null;
let UpdateHeroBuildResponseMsg = null;

async function loadHeroBuildProto() {
  if (heroBuildRoot) return;
  heroBuildRoot = await protobuf.load(HERO_BUILD_PROTO_PATH);
  HeroBuildMsg = heroBuildRoot.lookupType('CMsgHeroBuild');
  UpdateHeroBuildMsg = heroBuildRoot.lookupType('CMsgClientToGCUpdateHeroBuild');
  UpdateHeroBuildResponseMsg = heroBuildRoot.lookupType('CMsgClientToGCUpdateHeroBuildResponse');
}
// ---------- Tasks Table ----------
db.prepare(`
  CREATE TABLE IF NOT EXISTS steam_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING|RUNNING|DONE|FAILED
    result TEXT,
    error TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    started_at INTEGER,
    finished_at INTEGER
  )
`).run();

db.prepare(`CREATE INDEX IF NOT EXISTS idx_steam_tasks_status ON steam_tasks(status, id)`).run();
db.prepare(`CREATE INDEX IF NOT EXISTS idx_steam_tasks_updated ON steam_tasks(updated_at)`).run();

const selectPendingTaskStmt = db.prepare(`
  SELECT id, type, payload FROM steam_tasks
  WHERE status = 'PENDING'
  ORDER BY id ASC
  LIMIT 1
`);
const markTaskRunningStmt = db.prepare(`
  UPDATE steam_tasks
     SET status = 'RUNNING',
         started_at = ?,
         updated_at = ?
   WHERE id = ? AND status = 'PENDING'
`);
const resetTaskPendingStmt = db.prepare(`
  UPDATE steam_tasks
     SET status = 'PENDING',
         started_at = NULL,
         updated_at = ?
   WHERE id = ?
`);
const finishTaskStmt = db.prepare(`
  UPDATE steam_tasks
     SET status = ?,
         result = ?,
         error = ?,
         finished_at = ?,
         updated_at = ?
   WHERE id = ?
`);

const selectHeroBuildSourceStmt = db.prepare(`
  SELECT * FROM hero_build_sources WHERE hero_build_id = ?
`);
const selectHeroBuildCloneMetaStmt = db.prepare(`
  SELECT target_name, target_description, target_language
    FROM hero_build_clones
   WHERE origin_hero_build_id = ?
`);

// ---------- Standalone Dashboard Tables ----------
db.prepare(`
  CREATE TABLE IF NOT EXISTS standalone_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot TEXT NOT NULL,
    command TEXT NOT NULL,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    error TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME,
    finished_at DATETIME
  )
`).run();

db.prepare(`
  CREATE TABLE IF NOT EXISTS standalone_bot_state (
    bot TEXT PRIMARY KEY,
    heartbeat INTEGER NOT NULL,
    payload TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )
`).run();

db.prepare(`CREATE INDEX IF NOT EXISTS idx_standalone_commands_status ON standalone_commands(bot, status, id)`).run();
db.prepare(`CREATE INDEX IF NOT EXISTS idx_standalone_commands_created ON standalone_commands(created_at)`).run();
db.prepare(`CREATE INDEX IF NOT EXISTS idx_standalone_state_updated ON standalone_bot_state(updated_at)`).run();

const selectPendingCommandStmt = db.prepare(`
  SELECT id, command, payload
    FROM standalone_commands
   WHERE bot = ?
     AND status = 'pending'
ORDER BY id ASC
   LIMIT 1
`);
const markCommandRunningStmt = db.prepare(`
  UPDATE standalone_commands
     SET status = 'running',
         started_at = CURRENT_TIMESTAMP
   WHERE id = ?
     AND status = 'pending'
`);
const finalizeCommandStmt = db.prepare(`
  UPDATE standalone_commands
     SET status = ?,
         result = ?,
         error = ?,
         finished_at = CURRENT_TIMESTAMP
   WHERE id = ?
`);

const upsertStandaloneStateStmt = db.prepare(`
  INSERT INTO standalone_bot_state(bot, heartbeat, payload, updated_at)
  VALUES (@bot, @heartbeat, @payload, CURRENT_TIMESTAMP)
  ON CONFLICT(bot) DO UPDATE SET
    heartbeat = excluded.heartbeat,
    payload = excluded.payload,
    updated_at = CURRENT_TIMESTAMP
`);
const updateHeroBuildCloneUploadedStmt = db.prepare(`
  UPDATE hero_build_clones
     SET status = ?,
         status_info = ?,
         uploaded_build_id = ?,
         uploaded_version = ?,
         updated_at = strftime('%s','now')
   WHERE origin_hero_build_id = ?
`);

const quickInviteCountsStmt = db.prepare(`
  SELECT status, COUNT(*) AS count
    FROM steam_quick_invites
GROUP BY status
`);
const quickInviteRecentStmt = db.prepare(`
  SELECT invite_link, status, created_at
    FROM steam_quick_invites
ORDER BY created_at DESC
   LIMIT 5
`);
const quickInviteAvailableStmt = db.prepare(`
  SELECT COUNT(*) AS count
    FROM steam_quick_invites
   WHERE status = 'available'
     AND (expires_at IS NULL OR expires_at > strftime('%s','now'))
`);
const steamTaskCountsStmt = db.prepare(`
  SELECT status, COUNT(*) AS count
    FROM steam_tasks
GROUP BY status
`);
const steamTaskRecentStmt = db.prepare(`
  SELECT id, type, status, updated_at, finished_at
    FROM steam_tasks
ORDER BY updated_at DESC
   LIMIT 10
`);

// ---------- Steam State ----------
let refreshToken = readToken(REFRESH_TOKEN_PATH);
let machineAuthToken = readToken(MACHINE_TOKEN_PATH);

const runtimeState = {
  account_name: ACCOUNT_NAME || null,
  logged_on: false,
  logging_in: false,
  steam_id64: null,
  refresh_token_present: Boolean(refreshToken),
  machine_token_present: Boolean(machineAuthToken),
  guard_required: null,
  last_error: null,
  last_login_attempt_at: null,
  last_login_source: null,
  last_logged_on_at: null,
  last_disconnect_at: null,
  last_disconnect_eresult: null,
  last_guard_submission_at: null,
  deadlock_gc_ready: false,
};

let loginInProgress = false;
let pendingGuard = null;
let reconnectTimer = null;
let manualLogout = false;

let deadlockAppActive = false;
let deadlockGameRequestedAt = 0;
let deadlockGcReady = false;
let lastGcHelloAttemptAt = 0;
const deadlockGcWaiters = [];
const pendingPlaytestInviteResponses = [];
let webApiFriendCacheIds = null;
let webApiFriendCacheLastLoadedAt = 0;
let webApiFriendCachePromise = null;
let webApiFriendCacheWarned = false;

// ---------- Steam Client ----------
const client = new SteamUser();
const deadlockGcBot = new DeadlockGcBot({
  client,
  log: (level, msg, extra) => log(level, msg, extra),
  trace: writeDeadlockGcTrace,
  requestTokens: (reason) => requestDeadlockGcTokens(reason || 'deadlock_gc_bot'),
  getTokenCount: () => getDeadlockGcTokenCount(),
});
let gcTokenRequestInFlight = false;
let lastLoggedGcTokenCount = 0;
let heroBuildPublishWaiter = null;
client.setOption('autoRelogin', false);
client.setOption('machineName', process.env.STEAM_MACHINE_NAME || 'DeadlockBridge');

// Quick Invites (DB + client)
// Auto-Ensure-Konfiguration: mind. 1 verfügbar halten (kein Ablauf, Limit=1)
const quickInvites = new QuickInvites(db, client, log, {
  inviteLimit: 1,
  inviteDuration: null,          // kein Ablauf
  poolTarget: 1,                 // mindestens 1 available
  autoEnsure: true,              // Hintergrund-Ensure aktiv
  autoEnsureIntervalMs: Number(process.env.STEAM_INVITE_AUTO_ENSURE_MS ?? 30000) // alle 30s prüfen
});

const statusAnzeige = new StatusAnzeige(client, log, {
  appId: DEADLOCK_APP_ID,
  language: process.env.STEAM_PRESENCE_LANGUAGE || 'german',
  db,
  steamWebApiKey: STEAM_WEB_API_KEY,
  webApiTimeoutMs: WEB_API_HTTP_TIMEOUT_MS,
  webSummaryCacheTtlMs: WEB_API_FRIEND_CACHE_TTL_MS,
});
log('info', 'Statusanzeige initialisiert', {
  persistence: statusAnzeige.persistenceEnabled,
  pollIntervalMs: statusAnzeige.pollIntervalMs,
});
statusAnzeige.start();

// ---------- Helpers ----------
function updateRefreshToken(token) {
  refreshToken = token ? String(token).trim() : '';
  runtimeState.refresh_token_present = Boolean(refreshToken);
  scheduleStatePublish({ reason: 'refresh_token' });
}
function updateMachineToken(token) {
  machineAuthToken = token ? String(token).trim() : '';
  runtimeState.machine_token_present = Boolean(machineAuthToken);
  scheduleStatePublish({ reason: 'machine_token' });
}
function clearReconnectTimer(){ if (reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer = null; } }
function scheduleReconnect(reason, delayMs = RECONNECT_DELAY_MS){
  if (!refreshToken || manualLogout || runtimeState.logged_on || loginInProgress || reconnectTimer) return;
  const delay = Math.max(1000, Number.isFinite(delayMs) ? delayMs : RECONNECT_DELAY_MS);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    try { const result = initiateLogin('auto-reconnect', {}); log('info', 'Auto reconnect attempt', { reason, result }); }
    catch (err) { log('warn', 'Auto reconnect failed to start', { error: err.message, reason }); }
  }, delay);
}



function ensureDeadlockGamePlaying(force = false) {
  const now = Date.now();
  if (!force && now - deadlockGameRequestedAt < 15000) {
    log('debug', 'Skipping gamesPlayed request - too recent', { 
      timeSinceLastRequest: now - deadlockGameRequestedAt 
    });
    return;
  }
  
  try {
    const previouslyActive = deadlockAppActive;
    const appId = getWorkingAppId();
    
    // First ensure we're not playing any other games
    if (!previouslyActive) {
      client.gamesPlayed([]);
      setTimeout(() => {
        // Now play Deadlock
        client.gamesPlayed([appId]);
        log('info', 'Started playing Deadlock', { appId });
      }, 1000);
    } else {
      client.gamesPlayed([appId]);
    }
    
    deadlockGameRequestedAt = now;
    deadlockAppActive = true;
    
    log('info', 'Requested Deadlock GC session via gamesPlayed()', {
      appId,
      force,
      previouslyActive,
      steamId: client.steamID ? String(client.steamID) : 'not_logged_in'
    });
    requestDeadlockGcTokens('games_played');
    
    if (!previouslyActive) {
      deadlockGcReady = false;
  runtimeState.deadlock_gc_ready = false;
      runtimeState.deadlock_gc_ready = false;
      // Give Steam more time to process the gamesPlayed request
      setTimeout(() => {
        log('debug', 'Initiating GC handshake after game start');
        sendDeadlockGcHello(true);
      }, 3000); // Increased from 2s to 3s
    }
  } catch (err) {
    log('error', 'Failed to call gamesPlayed for Deadlock', { 
      error: err.message,
      steamId: client.steamID ? String(client.steamID) : 'not_logged_in'
    });
  }
}


function sendDeadlockGcHello(force = false) {
  if (!deadlockAppActive) {
    log('debug', 'Skipping GC hello - app not active');
    return false;
  }
  
  const now = Date.now();
  if (!force && now - lastGcHelloAttemptAt < 2000) {
    log('debug', 'Skipping GC hello - too recent');
    return false;
  }

  const tokenCount = getDeadlockGcTokenCount();
  if (tokenCount <= 0) {
    log('warn', 'Sending GC hello without GC tokens', {
      tokenCount,
    });
    requestDeadlockGcTokens('hello_no_tokens');
  } else if (tokenCount < 2) {
    requestDeadlockGcTokens('hello_low_tokens');
  }
  
  try {
    const payload = getDeadlockGcHelloPayload(force);
    const appId = getWorkingAppId();
    
    log('info', 'Sending Deadlock GC hello', {
      appId,
      payloadLength: payload.length,
      force,
      steamId: client.steamID ? String(client.steamID) : 'not_logged_in'
    });
    
    client.sendToGC(appId, PROTO_MASK + GC_MSG_CLIENT_HELLO, {}, payload);
    writeDeadlockGcTrace('send_gc_hello', {
      appId,
      payloadHex: payload.toString('hex').substring(0, 200),
      force,
      tokenCount,
    });
    lastGcHelloAttemptAt = now;
    
    // Schedule a verification check
    setTimeout(() => {
      if (!deadlockGcReady) {
        log('warn', 'GC did not respond to hello within 5 seconds', {
          appId,
          timeSinceHello: Date.now() - now
        });
        
        // Try with a different protocol approach
        tryAlternativeGcHandshake();
      }
    }, 5000);
    
    return true;
  } catch (err) {
    log('error', 'Failed to send Deadlock GC hello', { 
      error: err.message,
      stack: err.stack
    });
    return false;
  }
}

// Alternative handshake method
function tryAlternativeGcHandshake() {
  try {
    log('info', 'Attempting alternative GC handshake');
    deadlockGcBot.cachedHello = null;
    deadlockGcBot.cachedLegacyHello = null;
    const payload = getDeadlockGcHelloPayload(true);
    client.sendToGC(DEADLOCK_APP_ID, PROTO_MASK + GC_MSG_CLIENT_HELLO, {}, payload);
    log('debug', 'Sent refreshed GC hello payload');
  } catch (err) {
    log('error', 'Alternative GC handshake failed', { error: err.message });
  }
}

function removeGcWaiter(entry) {
  const idx = deadlockGcWaiters.indexOf(entry);
  if (idx >= 0) deadlockGcWaiters.splice(idx, 1);
}

function flushDeadlockGcWaiters(error) {
  while (deadlockGcWaiters.length) {
    const waiter = deadlockGcWaiters.shift();
    try {
      if (waiter) waiter.reject(error || new Error('Deadlock GC session reset'));
    } catch (_) {}
  }
}

function notifyDeadlockGcReady() {
  deadlockGcReady = true;
  runtimeState.deadlock_gc_ready = true;
  scheduleStatePublish({ reason: 'gc_ready' });
  writeDeadlockGcTrace('gc_ready', { waiters: deadlockGcWaiters.length });
  while (deadlockGcWaiters.length) {
    const waiter = deadlockGcWaiters.shift();
    try {
      if (waiter) waiter.resolve(true);
    } catch (_) {}
  }
}


function getDeadlockGcHelloPayload(force = false) {
  const overridePayload = getHelloPayloadOverride({ client, SteamUser });
  const normalizedOverride = normalizeToBuffer(overridePayload);
  if (normalizedOverride && normalizedOverride.length) {
    log('info', 'Using override Deadlock GC hello payload', {
      length: normalizedOverride.length,
    });
    return Buffer.from(normalizedOverride);
  }
  if (overridePayload) {
    log('warn', 'Deadlock GC override hello payload invalid – falling back to auto builder', {
      path: DEADLOCK_GC_PROTOCOL_OVERRIDE_PATH,
    });
  }

  const payload = deadlockGcBot.getHelloPayload(force);
  if (!payload || !payload.length) {
    throw new Error('Unable to build Deadlock GC hello payload');
  }

  log('debug', 'Generated GC hello payload', {
    protocolVersion: GC_CLIENT_HELLO_PROTOCOL_VERSION,
    payloadLength: payload.length,
    payloadHex: payload.toString('hex'),
  });
  return payload;
}

function createDeadlockGcReadyPromise(timeout) {
  ensureDeadlockGamePlaying();
  requestDeadlockGcTokens('wait_gc_ready');
  if (deadlockGcReady) return Promise.resolve(true);

  const effectiveTimeout = Math.max(
    MIN_GC_READY_TIMEOUT_MS,
    Number.isFinite(timeout) ? Number(timeout) : DEFAULT_GC_READY_TIMEOUT_MS
  );

  return new Promise((resolve, reject) => {
    const entry = {
      resolve: null,
      reject: null,
      timer: null,
      interval: null,
      done: false,
    };

    entry.resolve = (value) => {
      if (entry.done) return;
      entry.done = true;
      if (entry.timer) clearTimeout(entry.timer);
      if (entry.interval) clearInterval(entry.interval);
      removeGcWaiter(entry);
      resolve(value);
    };

    entry.reject = (err) => {
      if (entry.done) return;
      entry.done = true;
      if (entry.timer) clearTimeout(entry.timer);
      if (entry.interval) clearInterval(entry.interval);
      removeGcWaiter(entry);
      reject(err || new Error('Deadlock GC not ready'));
    };

    entry.timer = setTimeout(
      () => entry.reject(new Error('Timeout waiting for Deadlock GC')),
      effectiveTimeout
    );
    entry.interval = setInterval(() => {
      ensureDeadlockGamePlaying();
      sendDeadlockGcHello(false);
    }, 2000);

    deadlockGcWaiters.push(entry);
    sendDeadlockGcHello(true);
  });
}


async function waitForDeadlockGcReady(timeoutMs = DEFAULT_GC_READY_TIMEOUT_MS, options = {}) {
  const timeout = normalizeTimeoutMs(timeoutMs, DEFAULT_GC_READY_TIMEOUT_MS, MIN_GC_READY_TIMEOUT_MS);
  const attempts = normalizeAttempts(
    Object.prototype.hasOwnProperty.call(options, 'retryAttempts') ? options.retryAttempts : undefined,
    DEFAULT_GC_READY_ATTEMPTS,
    5
  );
  let attempt = 0;
  let lastError = null;

  while (attempt < attempts) {
    attempt += 1;
    try {
      // Force a fresh GC connection attempt before each try
      ensureDeadlockGamePlaying(true);
      await sleep(1000); // Give GC time to initialize
      
      await createDeadlockGcReadyPromise(timeout);
      log('info', 'Deadlock GC ready after attempt', { attempt, attempts });
      return true;
    } catch (err) {
      lastError = err || new Error('Deadlock GC not ready');
      deadlockGcReady = false;
  runtimeState.deadlock_gc_ready = false;
      runtimeState.deadlock_gc_ready = false;
      
      // Log more detailed error information
      log('warn', 'Deadlock GC attempt failed', {
        attempt,
        attempts,
        timeoutMs: timeout,
        error: err?.message || String(err),
        isTimeoutError: isTimeoutError(err)
      });
      
      if (attempt >= attempts || !isTimeoutError(err)) {
        break;
      }
      
      log('info', 'Retrying Deadlock GC handshake after delay', {
        attempt,
        attempts,
        delayMs: GC_READY_RETRY_DELAY_MS
      });
      
      // Force a complete reset before retrying
      deadlockAppActive = false;
      deadlockGcReady = false;
  runtimeState.deadlock_gc_ready = false;
      runtimeState.deadlock_gc_ready = false;
      flushDeadlockGcWaiters(new Error('Retry attempt'));
      
      await sleep(GC_READY_RETRY_DELAY_MS);
    }
  }

  if (lastError && typeof lastError === 'object') {
    lastError.timeoutMs = timeout;
    lastError.attempts = attempt;
  }
  throw lastError;
}

const PLAYTEST_RESPONSE_MAP = {
  0: { key: 'eResponse_Success', message: 'Einladung erfolgreich übermittelt.' },
  1: { key: 'eResponse_InternalError', message: 'Interner Fehler beim Game Coordinator.' },
  3: { key: 'eResponse_InvalidFriend', message: 'Zielkonto ist kein bestätigter Steam-Freund.' },
  4: { key: 'eResponse_NotFriendsLongEnough', message: 'Freundschaft besteht noch keine 30 Tage.' },
  5: { key: 'eResponse_AlreadyHasGame', message: 'Account besitzt Deadlock bereits.' },
  6: { key: 'eResponse_LimitedUser', message: 'Zielkonto ist eingeschränkt (Limited User).' },
  7: { key: 'eResponse_InviteLimitReached', message: 'Invite-Limit erreicht – bitte später erneut versuchen.' },
};

function formatPlaytestError(response) {
  if (!response || typeof response !== 'object') return null;

  const message = response.message ? String(response.message).trim() : '';
  const codeRaw = Object.prototype.hasOwnProperty.call(response, 'code') ? response.code : null;
  let codeDisplay = null;
  if (codeRaw !== null && codeRaw !== undefined) {
    const maybeNumber = Number(codeRaw);
    if (Number.isFinite(maybeNumber)) codeDisplay = `Code ${maybeNumber}`;
    else if (typeof codeRaw === 'string' && codeRaw.trim()) codeDisplay = `Code ${codeRaw.trim()}`;
  }
  const key = response.key ? String(response.key).trim() : '';

  const meta = [];
  if (codeDisplay) meta.push(codeDisplay);
  if (key) meta.push(key);

  const parts = [];
  if (message) parts.push(message);
  if (meta.length) parts.push(`(${meta.join(' / ')})`);

  const formatted = parts.join(' ').trim();
  return formatted || null;
}

function encodeSubmitPlaytestUserPayload(accountId, location) {
  return deadlockGcBot.encodePlaytestInvitePayload(accountId, location);
}

function parseSteamID(input) {
  if (!input) throw new Error('SteamID erforderlich');
  try {
    const sid = new SteamID(String(input));
    if (!sid.isValid()) throw new Error('Ungültige SteamID');
    return sid;
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    throw new Error(`Ungültige SteamID: ${message}`);
  }
}

function relationshipName(code) {
  if (code === undefined || code === null) return 'unknown';
  for (const [name, value] of Object.entries(SteamUser.EFriendRelationship || {})) {
    if (Number(value) === Number(code)) return name;
  }
  return String(code);
}

function removePendingPlaytestInvite(entry) {
  const idx = pendingPlaytestInviteResponses.indexOf(entry);
  if (idx >= 0) pendingPlaytestInviteResponses.splice(idx, 1);
}

function flushPendingPlaytestInvites(error) {
  while (pendingPlaytestInviteResponses.length) {
    const entry = pendingPlaytestInviteResponses.shift();
    if (!entry) continue;
    if (entry.timer) clearTimeout(entry.timer);
    try {
      entry.reject(error || new Error('GC-Verbindung getrennt'));
    } catch (_) {}
  }
}

function sendFriendRequest(steamId) {
  return new Promise((resolve, reject) => {
    try {
      client.addFriend(steamId, (err) => {
        if (err) return reject(err);
        resolve(true);
      });
    } catch (err) {
      reject(err);
    }
  });
}

async function sendPlaytestInvite(accountId, location, timeoutMs = DEFAULT_PLAYTEST_INVITE_TIMEOUT_MS, options = {}) {
  const inviteTimeout = normalizeTimeoutMs(timeoutMs, DEFAULT_PLAYTEST_INVITE_TIMEOUT_MS, MIN_PLAYTEST_INVITE_TIMEOUT_MS);
  const inviteAttempts = normalizeAttempts(
    Object.prototype.hasOwnProperty.call(options, 'retryAttempts') ? options.retryAttempts : undefined,
    DEFAULT_PLAYTEST_INVITE_ATTEMPTS,
    5
  );
  const gcAttempts = normalizeAttempts(
    Object.prototype.hasOwnProperty.call(options, 'gcRetryAttempts') ? options.gcRetryAttempts : undefined,
    DEFAULT_GC_READY_ATTEMPTS,
    5
  );
  const gcTimeoutOverride = Object.prototype.hasOwnProperty.call(options, 'gcTimeoutMs')
    ? options.gcTimeoutMs
    : (
      Object.prototype.hasOwnProperty.call(options, 'gc_ready_timeout_ms')
        ? options.gc_ready_timeout_ms
        : options.gcTimeout
    );
  const gcTimeout = normalizeTimeoutMs(
    gcTimeoutOverride !== undefined ? gcTimeoutOverride : Math.max(inviteTimeout, DEFAULT_GC_READY_TIMEOUT_MS),
    Math.max(inviteTimeout, DEFAULT_GC_READY_TIMEOUT_MS),
    MIN_GC_READY_TIMEOUT_MS
  );
  let attempt = 0;
  let lastError = null;

  log('info', 'Deadlock playtest invite timings', {
    inviteTimeoutMs: inviteTimeout,
    inviteAttempts,
    gcTimeoutMs: gcTimeout,
    gcAttempts,
  });

  while (attempt < inviteAttempts) {
    attempt += 1;
    try {
      await waitForDeadlockGcReady(gcTimeout, { retryAttempts: gcAttempts });
      return await sendPlaytestInviteOnce(accountId, location, inviteTimeout);
    } catch (err) {
      lastError = err;
      if (attempt >= inviteAttempts || !isTimeoutError(err)) {
        break;
      }
      log('warn', 'Deadlock playtest invite timed out - retrying', {
        attempt,
        attempts: inviteAttempts,
        timeoutMs: inviteTimeout,
      });
      await sleep(PLAYTEST_RETRY_DELAY_MS);
    }
  }

  if (lastError && typeof lastError === 'object') {
    lastError.timeoutMs = inviteTimeout;
    lastError.gcTimeoutMs = gcTimeout;
    lastError.attempts = attempt;
  }
  throw lastError || new Error('Playtest invite failed');
}

function sendPlaytestInviteOnce(accountId, location, timeoutMs) {
  const effectiveTimeout = Math.max(
    INVITE_RESPONSE_MIN_TIMEOUT_MS,
    Number.isFinite(timeoutMs) ? Number(timeoutMs) : DEFAULT_PLAYTEST_INVITE_TIMEOUT_MS
  );
  const estimatedPayloadVariants = buildPlaytestPayloadOverrideFn ? 1 : 6;

  return new Promise((resolve, reject) => {
    const entry = {
      resolve: null,
      reject: null,
      timer: null,
      attempts: 0,
      maxAttempts: Math.max(1, (playtestMsgConfigs.length || DEFAULT_PLAYTEST_MSG_IDS.length)) * estimatedPayloadVariants, // Try all message IDs with all payload versions (now 6 versions)
    };

    entry.resolve = (value) => {
      if (entry.timer) clearTimeout(entry.timer);
      removePendingPlaytestInvite(entry);
      resolve(value);
    };

    entry.reject = (err) => {
      if (entry.timer) clearTimeout(entry.timer);
      removePendingPlaytestInvite(entry);
      reject(err);
    };

    entry.timer = setTimeout(
      () => entry.reject(new Error('Timeout beim Warten auf GC-Antwort')),
      effectiveTimeout
    );

    pendingPlaytestInviteResponses.push(entry);

    const payloadVersions = buildPlaytestPayloadOverrideFn ? ['override'] : ['native'];
    let attemptCount = 0;
    const messageConfigs = playtestMsgConfigs.length ? playtestMsgConfigs : [...DEFAULT_PLAYTEST_MSG_IDS];

    for (const msgConfig of messageConfigs) {
      for (const payloadVersion of payloadVersions) {
        setTimeout(() => {
          try {
            const context = {
              accountId,
              location,
              payloadVersion,
              attempt: attemptCount,
              message: msgConfig,
            };
            const payloadRaw = buildPlaytestPayloadOverrideFn
              ? buildPlaytestPayloadOverrideFn(context)
              : encodeSubmitPlaytestUserPayload(accountId, location);
            const payload = buildPlaytestPayloadOverrideFn ? normalizeToBuffer(payloadRaw) : payloadRaw;

            if (!payload || !payload.length) {
              throw new Error('Playtest payload is empty');
            }

            const targetAppId = Number.isFinite(msgConfig.appId)
              ? Number(msgConfig.appId)
              : getWorkingAppId();

            client.sendToGC(targetAppId, PROTO_MASK + msgConfig.send, {}, payload);
            writeDeadlockGcTrace('send_playtest_invite', {
              accountId,
              location,
              appId: targetAppId,
              messageId: msgConfig.send,
              payloadVersion,
              overridePayload: Boolean(buildPlaytestPayloadOverrideFn),
              payloadHex: payload.toString('hex').substring(0, 200),
            });

            log('info', 'Deadlock playtest invite requested', { 
              accountId, 
              location, 
              messageId: msgConfig.send, 
              messageName: msgConfig.name,
              payloadVersion,
              appId: targetAppId,
              payloadLength: payload.length,
              payloadHex: payload.toString('hex').substring(0, 50),
              overridePayload: Boolean(buildPlaytestPayloadOverrideFn),
            });
            
            // Update current message IDs if this is the first attempt
            if (attemptCount === 0) {
              GC_MSG_SUBMIT_PLAYTEST_USER = msgConfig.send;
              GC_MSG_SUBMIT_PLAYTEST_USER_RESPONSE = msgConfig.response;
            }
            
          } catch (err) {
            log('warn', 'Failed to send playtest invite attempt', { 
              error: err.message, 
              messageId: msgConfig.send,
              payloadVersion,
              overridePayload: Boolean(buildPlaytestPayloadOverrideFn),
            });
            writeDeadlockGcTrace('playtest_send_error', {
              error: err && err.message ? err.message : err,
              messageId: msgConfig.send,
              payloadVersion,
            });
          }
        }, attemptCount * 200); // Stagger attempts by 200ms
        
        attemptCount++;
      }
    }

    // Also try with the originally working app (if we're not already using it)
    if (!buildPlaytestPayloadOverrideFn && DEADLOCK_APP_ID !== 1422450) {
      setTimeout(() => {
        try {
          const payload = encodeSubmitPlaytestUserPayload(accountId, location);
          client.sendToGC(1422450, PROTO_MASK + GC_MSG_SUBMIT_PLAYTEST_USER, {}, payload);
          log('info', 'Fallback invite attempt to original Deadlock app', { accountId, location });
        } catch (err) {
          log('warn', 'Fallback attempt failed', { error: err.message });
        }
      }, attemptCount * 200);
    }
  });
}

function handlePlaytestInviteResponse(appid, msgType, buffer) {
  const safeMsgType = Number.isFinite(msgType) ? Number(msgType) : 0;
  const messageId = safeMsgType & ~PROTO_MASK;
  const payloadBuffer = Buffer.isBuffer(buffer) ? buffer : normalizeToBuffer(buffer);
  
  log('info', 'Received GC playtest response', {
    appId: appid,
    messageId,
    bufferLength: payloadBuffer ? payloadBuffer.length : 0,
    bufferHex: payloadBuffer ? payloadBuffer.toString('hex').substring(0, 100) : 'none'
  });

  writeDeadlockGcTrace('received_playtest_response', {
    appId: appid,
    messageId,
    payloadHex: payloadBuffer ? payloadBuffer.toString('hex').substring(0, 200) : 'none',
  });

  if (!payloadBuffer || !payloadBuffer.length) {
    log('warn', 'Received empty playtest response payload', { appId: appid, messageId });
    return;
  }

  if (!pendingPlaytestInviteResponses.length) {
    log('warn', 'Received unexpected playtest invite response', { appId: appid, messageId });
    return;
  }

  // Check if this message ID matches any of our expected response IDs
  const matchingConfig = playtestMsgConfigs.find(config => config.response === messageId);
  if (matchingConfig) {
    log('info', 'SUCCESS: Found working message ID pair!', {
      sendId: matchingConfig.send,
      responseId: matchingConfig.response,
      configName: matchingConfig.name,
      appId: appid
    });
    
    // Update the current message IDs to use the working ones
    GC_MSG_SUBMIT_PLAYTEST_USER = matchingConfig.send;
    GC_MSG_SUBMIT_PLAYTEST_USER_RESPONSE = matchingConfig.response;
  }

  const entry = pendingPlaytestInviteResponses.shift();
  if (entry && entry.timer) clearTimeout(entry.timer);

  const parsedResponse = deadlockGcBot.decodePlaytestInviteResponse(payloadBuffer);
  const code = parsedResponse && typeof parsedResponse.code === 'number' ? parsedResponse.code : null;
  const mapping = Object.prototype.hasOwnProperty.call(PLAYTEST_RESPONSE_MAP, code || 0)
    ? PLAYTEST_RESPONSE_MAP[code || 0]
    : { key: 'unknown', message: 'Unbekannte Antwort des Game Coordinators.' };

  const response = {
    success: parsedResponse ? Boolean(parsedResponse.success) : code === 0,
    code: code === null ? null : Number(code),
    key: mapping.key,
    message: mapping.message,
    messageId,
    appId: appid,
    workingConfig: matchingConfig?.name || 'unknown'
  };

  log('info', 'Playtest invite response decoded', {
    success: response.success,
    code: response.code,
    key: response.key,
    message: response.message,
    workingConfig: response.workingConfig
  });

  if (entry && entry.resolve) {
    try {
      entry.resolve({ success: response.success, response });
      return;
    } catch (err) {
      log('warn', 'Failed to resolve playtest invite promise', { error: err.message });
    }
  }

  log('warn', 'No pending playtest promise to resolve');
}


function guardTypeFromDomain(domain) {
  const norm = String(domain || '').toLowerCase();
  if (norm.includes('email')) return 'email';
  if (norm.includes('two-factor') || norm.includes('authenticator') || norm.includes('mobile')) return 'totp';
  if (norm.includes('device')) return 'device';
  return norm || 'unknown';
}

function buildLoginOptions(overrides = {}) {
  if (overrides.refreshToken) return { refreshToken: overrides.refreshToken };
  if (refreshToken && !overrides.forceAccountCredentials) return { refreshToken };
  const accountName = overrides.accountName ?? ACCOUNT_NAME;
  const password = overrides.password ?? ACCOUNT_PASSWORD;

  if (!accountName) throw new Error('Missing Steam account name');
  if (!password) throw new Error('Missing Steam account password');

  const options = { accountName, password };
  if (overrides.twoFactorCode) options.twoFactorCode = String(overrides.twoFactorCode);
  if (overrides.authCode) options.authCode = String(overrides.authCode);
  if (Object.prototype.hasOwnProperty.call(overrides, 'rememberPassword')) options.rememberPassword = Boolean(overrides.rememberPassword);
  if (overrides.machineAuthToken) options.machineAuthToken = String(overrides.machineAuthToken);
  else if (machineAuthToken) options.machineAuthToken = machineAuthToken;
  return options;
}

function initiateLogin(source, payload) {
  if (client.steamID && client.steamID.isValid()) {
    const steamId64 = typeof client.steamID.getSteamID64 === 'function' ? client.steamID.getSteamID64() : String(client.steamID);
    return { started: false, reason: 'already_logged_on', steam_id64: steamId64 };
  }
  if (loginInProgress) return { started: false, reason: 'login_in_progress' };

  const overrides = {};
  if (payload) {
    if (Object.prototype.hasOwnProperty.call(payload, 'use_refresh_token') && !payload.use_refresh_token) overrides.forceAccountCredentials = true;
    if (Object.prototype.hasOwnProperty.call(payload, 'force_credentials') && payload.force_credentials) overrides.forceAccountCredentials = true;
    if (payload.account_name) overrides.accountName = payload.account_name;
    if (payload.password) overrides.password = payload.password;
    if (payload.refresh_token) overrides.refreshToken = payload.refresh_token;
    if (payload.two_factor_code) overrides.twoFactorCode = payload.two_factor_code;
    if (payload.auth_code) overrides.authCode = payload.auth_code;
    if (Object.prototype.hasOwnProperty.call(payload, 'remember_password')) overrides.rememberPassword = Boolean(payload.remember_password);
    if (payload.machine_auth_token) overrides.machineAuthToken = payload.machine_auth_token;
  }

  const options = buildLoginOptions(overrides);
  if (options.accountName) runtimeState.account_name = options.accountName;

  loginInProgress = true;
  runtimeState.logging_in = true;
  runtimeState.last_login_attempt_at = nowSeconds();
  runtimeState.last_login_source = source;
  runtimeState.last_error = null;
  pendingGuard = null;
  runtimeState.guard_required = null;
  manualLogout = false;
  clearReconnectTimer();

  log('info', 'Initiating Steam login', { using_refresh_token: Boolean(options.refreshToken), source });
  try { client.logOn(options); }
  catch (err) {
    loginInProgress = false; runtimeState.logging_in = false; runtimeState.last_error = { message: err.message };
    scheduleStatePublish({ reason: 'login_error', source, message: err.message });
    throw err;
  }

  scheduleStatePublish({ reason: 'login_start', source });
  return { started: true, using_refresh_token: Boolean(options.refreshToken), source };
}

function handleGuardCodeTask(payload) {
  if (!pendingGuard || !pendingGuard.callback) throw new Error('No Steam Guard challenge is pending');
  const code = payload && payload.code ? String(payload.code).trim() : '';
  if (!code) throw new Error('Steam Guard code is required');

  const callback = pendingGuard.callback;
  const domain = pendingGuard.domain;
  pendingGuard = null;
  runtimeState.guard_required = null;
  runtimeState.last_guard_submission_at = nowSeconds();

  try { callback(code); log('info', 'Submitted Steam Guard code', { domain: domain || null }); }
  catch (err) { throw new Error(`Failed to submit guard code: ${err.message}`); }

  scheduleStatePublish({ reason: 'guard_submit', domain: domain || null });
  return { accepted: true, domain: domain || null, type: guardTypeFromDomain(domain) };
}

function handleLogoutTask() {
  manualLogout = true;
  clearReconnectTimer();
  runtimeState.logging_in = false;
  loginInProgress = false;
  pendingGuard = null;
  runtimeState.guard_required = null;
  runtimeState.last_error = null;
  try { client.logOff(); } catch (err) { log('warn', 'logOff failed', { error: err.message }); }
  scheduleStatePublish({ reason: 'logout_command' });
  return { logged_off: true };
}

function getStatusPayload() {
  return {
    account_name: runtimeState.account_name,
    logged_on: runtimeState.logged_on,
    logging_in: runtimeState.logging_in,
    steam_id64: runtimeState.steam_id64,
    refresh_token_present: runtimeState.refresh_token_present,
    machine_token_present: runtimeState.machine_token_present,
    guard_required: runtimeState.guard_required,
    last_error: runtimeState.last_error,
    last_login_attempt_at: runtimeState.last_login_attempt_at,
    last_login_source: runtimeState.last_login_source,
    last_logged_on_at: runtimeState.last_logged_on_at,
    last_disconnect_at: runtimeState.last_disconnect_at,
    last_disconnect_eresult: runtimeState.last_disconnect_eresult,
    last_guard_submission_at: runtimeState.last_guard_submission_at,
    deadlock_gc_ready: runtimeState.deadlock_gc_ready,
  };
}

function buildStandaloneSnapshot() {
  const snapshot = {
    timestamp: new Date().toISOString(),
    runtime: getStatusPayload(),
    quick_invites: { counts: {}, total: 0, available: 0, recent: [] },
    tasks: { counts: {}, recent: [] },
  };

  try {
    const rows = quickInviteCountsStmt.all();
    const counts = {};
    let total = 0;
    for (const row of rows) {
      const status = row && row.status ? String(row.status) : 'unknown';
      const count = Number(row && row.count ? row.count : 0) || 0;
      counts[status] = count;
      total += count;
    }
    snapshot.quick_invites.counts = counts;
    snapshot.quick_invites.total = total;
  } catch (err) {
    log('warn', 'Failed to collect quick invite counts', { error: err.message });
  }

  try {
    const row = quickInviteAvailableStmt.get();
    snapshot.quick_invites.available = Number(row && row.count ? row.count : 0) || 0;
  } catch (err) {
    log('warn', 'Failed to determine available quick invites', { error: err.message });
  }

  try {
    const rows = quickInviteRecentStmt.all();
    snapshot.quick_invites.recent = rows.map((row) => ({
      invite_link: row.invite_link,
      status: row.status,
      created_at: row.created_at,
    }));
  } catch (err) {
    log('warn', 'Failed to collect recent quick invites', { error: err.message });
  }

  try {
    const rows = steamTaskCountsStmt.all();
    const counts = {};
    for (const row of rows) {
      const status = row && row.status ? String(row.status).toUpperCase() : 'UNKNOWN';
      const count = Number(row && row.count ? row.count : 0) || 0;
      counts[status] = count;
    }
    snapshot.tasks.counts = counts;
  } catch (err) {
    log('warn', 'Failed to collect steam task counts', { error: err.message });
  }

  try {
    const rows = steamTaskRecentStmt.all();
    snapshot.tasks.recent = rows.map((row) => ({
      id: Number(row.id),
      type: row.type,
      status: row.status,
      updated_at: row.updated_at,
      finished_at: row.finished_at,
    }));
  } catch (err) {
    log('warn', 'Failed to collect recent steam tasks', { error: err.message });
  }

  return snapshot;
}

function publishStandaloneState(context) {
  try {
    const snapshot = buildStandaloneSnapshot();
    if (context) {
      snapshot.context = context;
    }
    const payloadJson = safeJsonStringify(snapshot) || '{}';
    upsertStandaloneStateStmt.run({
      bot: COMMAND_BOT_KEY,
      heartbeat: nowSeconds(),
      payload: payloadJson,
    });
  } catch (err) {
    log('warn', 'Failed to publish standalone state', { error: err.message });
  }
}

function scheduleStatePublish(context) {
  try { publishStandaloneState(context); }
  catch (err) { log('warn', 'State publish failed', { error: err.message }); }
}

function completeTask(id, status, result = undefined, error = undefined) {
  const finishedAt = nowSeconds();
  const resultJson = result === undefined ? null : safeJsonStringify(result);
  const errorText = error ? String(error) : null;
  finishTaskStmt.run(status, resultJson, errorText, finishedAt, finishedAt, id);
  scheduleStatePublish({ reason: 'task', status, task_id: id });
}

// ---------- Task Dispatcher (Promise-fähig) ----------
let taskInProgress = false;

function finalizeTaskRun(task, outcome) {
  // outcome kann sync (Objekt) oder Promise sein
  if (outcome && typeof outcome.then === 'function') {
    outcome.then(
      (res) => completeTask(task.id, (res && res.ok) ? 'DONE' : 'FAILED', res, res && !res.ok ? res.error : null),
      (err) => completeTask(task.id, 'FAILED', { ok: false, error: err?.message || String(err) }, err?.message || String(err))
    );
  } else {
    const ok = outcome && outcome.ok;
    completeTask(task.id, ok ? 'DONE' : 'FAILED', outcome, outcome && !ok ? outcome.error : null);
  }
}

function processNextTask() {
  if (taskInProgress) return;
  taskInProgress = true;

  let task = null;
  try {
    task = selectPendingTaskStmt.get();
    if (!task) return;

    const startedAt = nowSeconds();
    const updated = markTaskRunningStmt.run(startedAt, startedAt, task.id);
    if (!updated.changes) return;

    const payload = safeJsonParse(task.payload);
    log('info', 'Executing steam task', { id: task.id, type: task.type });

    switch (task.type) {
      case 'AUTH_STATUS':
        finalizeTaskRun(task, { ok: true, data: getStatusPayload() });
        break;
      case 'AUTH_LOGIN':
        finalizeTaskRun(task, { ok: true, data: initiateLogin('task', payload) });
        break;
      case 'AUTH_GUARD_CODE':
        finalizeTaskRun(task, { ok: true, data: handleGuardCodeTask(payload) });
        break;
      case 'AUTH_LOGOUT':
        finalizeTaskRun(task, { ok: true, data: handleLogoutTask() });
        break;

      // -------- Quick Invites --------
      case 'AUTH_QUICK_INVITE_CREATE': {
        const p = {
          inviteLimit: payload?.invite_limit ?? 1,
          inviteDuration: (Object.prototype.hasOwnProperty.call(payload || {}, 'invite_duration'))
            ? payload.invite_duration
            : null, // default null (kein Ablauf)
        };
        const promise = (async () => {
          if (!runtimeState.logged_on) throw new Error('Not logged in');
          const rec = await quickInvites.createOne(p);
          return { ok: true, data: rec };
        })();
        finalizeTaskRun(task, promise);
        break;
      }
      case 'AUTH_QUICK_INVITE_ENSURE_POOL': {
        const p = {
          target: payload?.target ?? 5, // Default: Poolgröße 5
          inviteLimit: payload?.invite_limit ?? 1,
          inviteDuration: (Object.prototype.hasOwnProperty.call(payload || {}, 'invite_duration'))
            ? payload.invite_duration
            : null,
        };
        const promise = (async () => {
          if (!runtimeState.logged_on) throw new Error('Not logged in');
          const summary = await quickInvites.ensurePool(p);
          return { ok: true, data: summary };
        })();
        finalizeTaskRun(task, promise);
        break;
      }

      case 'AUTH_SEND_FRIEND_REQUEST': {
        const promise = (async () => {
          if (!runtimeState.logged_on) throw new Error('Not logged in');
          const raw = payload?.steam_id ?? payload?.steam_id64;
          const sid = parseSteamID(raw);
          await sendFriendRequest(sid);
          const sid64 = typeof sid.getSteamID64 === 'function' ? sid.getSteamID64() : String(sid);
          return {
            ok: true,
            data: {
              steam_id64: sid64,
              account_id: sid.accountid ?? null,
            },
          };
        })();
        finalizeTaskRun(task, promise);
        break;
      }

      case 'AUTH_CHECK_FRIENDSHIP': {
        const promise = (async () => {
          if (!runtimeState.logged_on) throw new Error('Not logged in');
          const raw = payload?.steam_id ?? payload?.steam_id64;
          const sid = parseSteamID(raw);
          const sid64 = typeof sid.getSteamID64 === 'function' ? sid.getSteamID64() : String(sid);
          let relationshipRaw = client.myFriends ? client.myFriends[sid64] : undefined;
          let friendSource = 'client';
          let isFriend = Number(relationshipRaw) === Number((SteamUser.EFriendRelationship || {}).Friend);

          if (!isFriend) {
            const viaWeb = await isFriendViaWebApi(sid64);
            if (viaWeb && viaWeb.friend) {
              isFriend = true;
              friendSource = viaWeb.source || 'webapi';
              if (relationshipRaw === undefined) {
                if (SteamUser.EFriendRelationship && Object.prototype.hasOwnProperty.call(SteamUser.EFriendRelationship, 'Friend')) {
                  relationshipRaw = SteamUser.EFriendRelationship.Friend;
                } else {
                  relationshipRaw = 'Friend';
                }
              }
            }
          }

          return {
            ok: true,
            data: {
              steam_id64: sid64,
              account_id: sid.accountid ?? null,
              friend: isFriend,
              relationship: relationshipRaw ?? null,
              relationship_name: relationshipName(relationshipRaw),
              friend_source: friendSource,
              webapi_cache_age_ms: getWebApiFriendCacheAgeMs(),
            },
          };
        })();
        finalizeTaskRun(task, promise);
        break;
      }

      case 'BUILD_PUBLISH': {
        // Check if another build publish is already in progress
        if (heroBuildPublishWaiter) {
          log('info', 'Build publish already in progress, requeueing task', { id: task.id });
          resetTaskPendingStmt.run(nowSeconds(), task.id);
          break;
        }

        const promise = (async () => {
          try {
            log('info', 'BUILD_PUBLISH: Starting', { task_id: task.id, origin_id: payload?.origin_hero_build_id });

            if (!runtimeState.logged_on) throw new Error('Not logged in');

            log('info', 'BUILD_PUBLISH: Loading proto');
            await loadHeroBuildProto();

            const originId = payload?.origin_hero_build_id ?? payload?.hero_build_id;
            if (!originId) throw new Error('origin_hero_build_id missing');

            log('info', 'BUILD_PUBLISH: Fetching build source', { originId });
            const src = selectHeroBuildSourceStmt.get(originId);
            if (!src) throw new Error(`hero_build_sources missing for ${originId}`);

            log('info', 'BUILD_PUBLISH: Fetching clone meta', { originId });
            const cloneMeta = selectHeroBuildCloneMetaStmt.get(originId) || {};

            log('info', 'BUILD_PUBLISH: Building metadata', {
              cloneMeta: cloneMeta ? Object.keys(cloneMeta) : 'none'
            });
            const targetName = payload?.target_name || cloneMeta.target_name;
      const targetDescription = payload?.target_description || cloneMeta.target_description;
      const targetLanguage = safeNumber(payload?.target_language) ?? safeNumber(cloneMeta.target_language) ?? 1;
      const authorAccountId = client?.steamID?.accountid ? Number(client.steamID.accountid) : undefined;
      const useMinimal = payload?.minimal === true;
      const useUpdate = payload?.update === true;
      const minimalUpdate = payload?.minimal_update === true;
      const meta = {
        target_name: targetName,
        target_description: targetDescription,
        target_language: targetLanguage,
        author_account_id: useUpdate ? safeNumber(src.author_account_id) : authorAccountId,
        origin_build_id: src.hero_build_id,
      };
      let heroBuild;
      if (useUpdate) {
        heroBuild = buildUpdateHeroBuild(src, meta);
        if (minimalUpdate) {
          heroBuild.tags = [];
          heroBuild.details = { mod_categories: [] };
        }
      } else if (useMinimal) {
        heroBuild = buildMinimalHeroBuild(src, meta);
      } else {
        heroBuild = mapHeroBuildFromRow(src, meta);
      }
      if (!useUpdate) {
        // new build => clear hero_build_id so GC assigns fresh
        delete heroBuild.hero_build_id;
      }
      log('info', 'BUILD_PUBLISH: Building hero object', {
        useMinimal,
        useUpdate,
        minimalUpdate,
      });

      log('info', 'Publishing hero build', {
        originId,
        heroId: heroBuild.hero_id,
        author: heroBuild.author_account_id,
        language: heroBuild.language,
        name: heroBuild.name,
        mode: useUpdate ? (minimalUpdate ? 'update-minimal' : 'update') : (useMinimal ? 'new-minimal' : 'new'),
        hero_build_id: heroBuild.hero_build_id,
      });

            log('info', 'BUILD_PUBLISH: Calling sendHeroBuildUpdate');
            log('info', 'BUILD_PUBLISH: heroBuild object', { heroBuild: JSON.stringify(heroBuild) });
            const resp = await sendHeroBuildUpdate(heroBuild);

            log('info', 'BUILD_PUBLISH: Update successful', { resp });
            updateHeroBuildCloneUploadedStmt.run('done', null, resp.hero_build_id || null, resp.version || null, originId);
            return { ok: true, response: resp, origin_id: originId };
          } catch (err) {
            log('error', 'BUILD_PUBLISH: Failed', {
              task_id: task.id,
              origin_id: payload?.origin_hero_build_id,
              error: err?.message || String(err),
              stack: err?.stack || 'no stack'
            });
            throw err;
          }
        })();
        finalizeTaskRun(task, promise);
        break;
      }

      case 'AUTH_SEND_PLAYTEST_INVITE': {
        const promise = (async () => {
          if (!runtimeState.logged_on) throw new Error('Not logged in');
          const raw = payload?.steam_id ?? payload?.steam_id64;
          const timeoutMs = payload?.timeout_ms ?? payload?.response_timeout_ms;
          const inviteRetryAttempts = payload?.retry_attempts ?? payload?.invite_retry_attempts ?? payload?.attempts;
          const gcReadyRetryAttempts = payload?.gc_ready_retry_attempts ?? payload?.gc_retry_attempts;
          const gcReadyTimeoutMs = payload?.gc_ready_timeout_ms ?? payload?.gc_timeout_ms;
          const sid = raw ? parseSteamID(raw) : null;
          const accountId = payload?.account_id != null ? Number(payload.account_id) : (sid ? sid.accountid : null);
          if (!Number.isFinite(accountId) || accountId <= 0) throw new Error('account_id missing or invalid');
          const locationRaw = typeof payload?.location === 'string' ? payload.location.trim() : '';
          const location = locationRaw || 'discord-betainvite';
          const inviteTimeout = Number(timeoutMs);
          const response = await sendPlaytestInvite(
            Number(accountId),
            location,
            Number.isFinite(inviteTimeout) ? inviteTimeout : undefined,
            {
              retryAttempts: Number.isFinite(Number(inviteRetryAttempts)) ? Number(inviteRetryAttempts) : undefined,
              gcRetryAttempts: Number.isFinite(Number(gcReadyRetryAttempts)) ? Number(gcReadyRetryAttempts) : undefined,
              gcTimeoutMs: Number.isFinite(Number(gcReadyTimeoutMs)) ? Number(gcReadyTimeoutMs) : undefined,
            }
          );
          const sid64 = sid && typeof sid.getSteamID64 === 'function' ? sid.getSteamID64() : (sid ? String(sid) : null);
          const success = Boolean(response && response.success);
          const errorText = success
            ? null
            : formatPlaytestError(response) || 'Game Coordinator hat die Einladung abgelehnt.';
          const data = {
            steam_id64: sid64,
            account_id: Number(accountId),
            location,
            response,
          };
          return success
            ? { ok: true, data }
            : { ok: false, data, error: errorText };
        })();
        finalizeTaskRun(task, promise);
        break;
      }

      case 'AUTH_GET_FRIENDS_LIST': {
        const promise = (async () => {
          if (!runtimeState.logged_on) throw new Error('Not logged in');

          // Get friends from Web API
          const friendIds = await loadWebApiFriendIds(true);
          if (!friendIds) {
            throw new Error('Failed to load friends list from Steam Web API');
          }

          const friends = [];
          for (const steamId64 of friendIds) {
            friends.push({
              steam_id64: steamId64,
              // Try to get account_id from steamID
              account_id: null, // We'll compute this on Python side if needed
            });
          }

          return {
            ok: true,
            data: {
              count: friends.length,
              friends: friends,
            },
          };
        })();
        finalizeTaskRun(task, promise);
        break;
      }

      default:
        throw new Error(`Unsupported task type: ${task.type}`);
    }
  } catch (err) {
    log('error', 'Failed to process steam task', { error: err.message });
    if (task && task.id) completeTask(task.id, 'FAILED', { ok:false, error: err.message }, err.message);
  } finally {
    taskInProgress = false;
  }
}

setInterval(() => {
  try { processNextTask(); } catch (err) { log('error', 'Task polling loop failed', { error: err.message }); }
}, Math.max(500, TASK_POLL_INTERVAL_MS));

// ---------- Standalone Command Handling ----------
let commandInProgress = false;

const COMMAND_HANDLERS = {
  status: () => ({ ok: true, data: getStatusPayload() }),
  login: (payload) => {
    const result = initiateLogin('command', payload || {});
    scheduleStatePublish({ reason: 'command-login' });
    return { ok: true, data: result };
  },
  logout: () => ({ ok: true, data: handleLogoutTask() }),
  'quick.ensure': (payload = {}) => {
    if (!runtimeState.logged_on) throw new Error('Not logged in');
    const opts = {};
    const targetValue = payload.target ?? payload.pool_target;
    if (targetValue != null) opts.target = Number(targetValue);
    const limitValue = payload.invite_limit ?? payload.inviteLimit;
    if (limitValue != null) opts.inviteLimit = Number(limitValue);
    if (Object.prototype.hasOwnProperty.call(payload, 'invite_duration')) {
      opts.inviteDuration = payload.invite_duration;
    } else if (Object.prototype.hasOwnProperty.call(payload, 'inviteDuration')) {
      opts.inviteDuration = payload.inviteDuration;
    }
    return quickInvites.ensurePool(opts).then((summary) => {
      scheduleStatePublish({ reason: 'quick.ensure' });
      return { ok: true, data: summary };
    });
  },
  'quick.create': (payload = {}) => {
    if (!runtimeState.logged_on) throw new Error('Not logged in');
    const opts = {};
    const limitValue = payload.invite_limit ?? payload.inviteLimit;
    if (limitValue != null) opts.inviteLimit = Number(limitValue);
    if (Object.prototype.hasOwnProperty.call(payload, 'invite_duration')) {
      opts.inviteDuration = payload.invite_duration;
    } else if (Object.prototype.hasOwnProperty.call(payload, 'inviteDuration')) {
      opts.inviteDuration = payload.inviteDuration;
    }
    return quickInvites.createOne(opts).then((record) => {
      scheduleStatePublish({ reason: 'quick.create' });
      return { ok: true, data: record };
    });
  },
  'guard.submit': (payload) => ({ ok: true, data: handleGuardCodeTask(payload || {}) }),
  restart: () => {
    log('info', 'Restart command received - terminating process for restart');
    process.exit(0);
  },
};

function finalizeStandaloneCommand(commandId, status, resultObj, errorMessage) {
  const resultJson = resultObj === undefined ? null : safeJsonStringify(resultObj);
  const errorText = truncateError(errorMessage);
  try {
    finalizeCommandStmt.run(status, resultJson, errorText, commandId);
  } catch (err) {
    log('error', 'Failed to finalize standalone command', { error: err.message, command_id: commandId, status });
  }
}

function processNextCommand() {
  if (commandInProgress) return;

  let row;
  try {
    row = selectPendingCommandStmt.get(COMMAND_BOT_KEY);
  } catch (err) {
    log('error', 'Failed to fetch standalone command', { error: err.message });
    return;
  }

  if (!row) {
    return;
  }

  try {
    const claimed = markCommandRunningStmt.run(row.id);
    if (!claimed.changes) {
      setTimeout(processNextCommand, 0);
      return;
    }
  } catch (err) {
    log('error', 'Failed to mark standalone command running', { error: err.message, id: row.id });
    return;
  }

  commandInProgress = true;

  let payloadData = {};
  if (row.payload) {
    try {
      payloadData = safeJsonParse(row.payload);
    } catch (err) {
      log('warn', 'Invalid standalone command payload', { error: err.message, id: row.id });
      payloadData = {};
    }
  }

  const handler = COMMAND_HANDLERS[row.command];

  const finalize = (status, resultObj, errorMessage) => {
    finalizeStandaloneCommand(row.id, status, resultObj, errorMessage);
    try { publishStandaloneState({ reason: 'command', command: row.command, status }); }
    catch (err) { log('warn', 'Failed to publish state after command', { error: err.message, command: row.command }); }
    commandInProgress = false;
    setTimeout(processNextCommand, 0);
  };

  if (!handler) {
    finalize('error', { ok: false, error: 'unknown_command' }, `Unsupported command: ${row.command}`);
    return;
  }

  let outcome;
  try {
    outcome = handler(payloadData || {}, row);
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    finalize('error', { ok: false, error: message }, message);
    return;
  }

  if (outcome && typeof outcome.then === 'function') {
    outcome.then(
      (res) => finalize('success', wrapOk(res), null),
      (err) => {
        const message = err && err.message ? err.message : String(err);
        finalize('error', { ok: false, error: message }, message);
      },
    );
  } else {
    finalize('success', wrapOk(outcome), null);
  }
}

setInterval(() => {
  try { processNextCommand(); } catch (err) { log('error', 'Standalone command loop failed', { error: err.message }); }
}, Math.max(500, COMMAND_POLL_INTERVAL_MS));

processNextCommand();

setInterval(() => {
  try { publishStandaloneState({ reason: 'heartbeat' }); }
  catch (err) { log('warn', 'Standalone state heartbeat failed', { error: err.message }); }
}, Math.max(5000, STATE_PUBLISH_INTERVAL_MS));

// ---------- Steam Events ----------
function markLoggedOn(details) {
  runtimeState.logged_on = true;
  runtimeState.logging_in = false;
  loginInProgress = false;
  runtimeState.guard_required = null;
  pendingGuard = null;
  runtimeState.last_logged_on_at = nowSeconds();
  runtimeState.last_error = null;
  deadlockAppActive = false;
  deadlockGcReady = false;
  runtimeState.deadlock_gc_ready = false;
  deadlockGameRequestedAt = 0;
  lastGcHelloAttemptAt = 0;

  if (client.steamID && typeof client.steamID.getSteamID64 === 'function') {
    runtimeState.steam_id64 = client.steamID.getSteamID64();
  } else if (client.steamID) {
    runtimeState.steam_id64 = String(client.steamID);
  } else {
    runtimeState.steam_id64 = null;
  }

  try {
    client.setPersona(SteamUser.EPersonaState.Away);
  } catch (err) {
    log('warn', 'Failed to set persona away', { error: err.message });
  }
  ensureDeadlockGamePlaying(true);
  requestDeadlockGcTokens('post-login');

  log('info', 'Steam login successful', {
    country: details ? details.publicIPCountry : undefined,
    cellId: details ? details.cellID : undefined,
    steam_id64: runtimeState.steam_id64,
  });

  // Direkt nach erfolgreichem Login: mind. 1 Invite sicherstellen
  if (typeof quickInvites.ensureAtLeastOne === 'function') {
    quickInvites.ensureAtLeastOne();
  } else if (typeof quickInvites.ensurePool === 'function') {
    quickInvites.ensurePool({ target: 1 }).catch((e) => log('warn', 'ensurePool-after-login failed', { error: e.message }));
  }

  scheduleStatePublish({ reason: 'logged_on' });
}

client.on('loggedOn', (details) => { markLoggedOn(details); });
client.on('webSession', () => { log('debug', 'Steam web session established'); });
client.on('steamGuard', (domain, callback, lastCodeWrong) => {
  pendingGuard = { domain, callback };
  const norm = String(domain || '').toLowerCase();
  runtimeState.guard_required = {
    domain: domain || null,
    type: norm.includes('email') ? 'email' : (norm.includes('two-factor') || norm.includes('authenticator') || norm.includes('mobile')) ? 'totp' : (norm.includes('device') ? 'device' : 'unknown'),
    last_code_wrong: Boolean(lastCodeWrong),
    requested_at: nowSeconds(),
  };
  runtimeState.logging_in = true;
  log('info', 'Steam Guard challenge received', { domain: domain || null, lastCodeWrong: Boolean(lastCodeWrong) });
  scheduleStatePublish({ reason: 'steam_guard', domain: domain || null, last_code_wrong: Boolean(lastCodeWrong) });
});
client.on('refreshToken', (token) => { updateRefreshToken(token); writeToken(REFRESH_TOKEN_PATH, refreshToken); log('info', 'Stored refresh token', { path: REFRESH_TOKEN_PATH }); });
client.on('machineAuthToken', (token) => { updateMachineToken(token); writeToken(MACHINE_TOKEN_PATH, machineAuthToken); log('info', 'Stored machine auth token', { path: MACHINE_TOKEN_PATH }); });
client.on('_gcTokens', () => {
  const count = getDeadlockGcTokenCount();
  const delta = count - lastLoggedGcTokenCount;
  lastLoggedGcTokenCount = count;
  log('info', 'Received GC tokens update', {
    count,
    delta,
  });
  writeDeadlockGcTrace('gc_tokens_update', {
    count,
    delta,
  });
  deadlockGcBot.cachedHello = null;
  deadlockGcBot.cachedLegacyHello = null;
  if (deadlockAppActive && !deadlockGcReady) {
    log('debug', 'Retrying GC hello after token update');
    sendDeadlockGcHello(true);
  }
});

client.on('appLaunched', (appId) => {
  log('info', 'Steam app launched', { appId });
  if (Number(appId) !== Number(DEADLOCK_APP_ID)) return;
  
  log('info', 'Deadlock app launched - GC session starting');
  deadlockAppActive = true;
  deadlockGcReady = false;
  runtimeState.deadlock_gc_ready = false;
  requestDeadlockGcTokens('app_launch');
  
  // Wait a bit longer for GC to initialize
  setTimeout(() => {
    log('debug', 'Sending GC hello after app launch');
    sendDeadlockGcHello(true);
  }, 4000); // Increased delay
});
client.on('appQuit', (appId) => {
  log('info', 'Steam app quit', { appId });
  if (Number(appId) !== Number(DEADLOCK_APP_ID)) return;

  log('info', 'Deadlock app quit – GC session ended');
  deadlockAppActive = false;
  deadlockGcReady = false;
  runtimeState.deadlock_gc_ready = false;
  flushDeadlockGcWaiters(new Error('Deadlock app quit'));
  flushPendingPlaytestInvites(new Error('Deadlock app quit'));
});

// Track friend relationship changes to auto-save steam links to DB
client.on('friendRelationship', (steamId, relationship) => {
  const sid64 = steamId && typeof steamId.getSteamID64 === 'function' ? steamId.getSteamID64() : String(steamId);
  const relName = relationshipName(relationship);

  log('info', 'Friend relationship changed', {
    steam_id64: sid64,
    relationship: relationship,
    relationship_name: relName,
  });

  // If we became friends, save to database
  const EFriendRelationship = SteamUser.EFriendRelationship || {};
  if (Number(relationship) === Number(EFriendRelationship.Friend)) {
    log('info', 'New friend confirmed, saving to steam_links', { steam_id64: sid64 });

    // Save to database
    try {
      const stmt = db.prepare(`
        INSERT INTO steam_links(user_id, steam_id, name, verified)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id, steam_id) DO UPDATE SET
          verified=1,
          updated_at=CURRENT_TIMESTAMP
      `);

      // Use steam_id64 as both user_id and steam_id since we don't have Discord ID yet
      // This will be updated later when the user links via Discord
      stmt.run(0, sid64, '', 1);

      log('info', 'Saved new friend to steam_links', { steam_id64: sid64 });
    } catch (err) {
      log('error', 'Failed to save friend to steam_links', {
        steam_id64: sid64,
        error: err && err.message ? err.message : String(err),
      });
    }
  }
});

client.on('receivedFromGC', (appId, msgType, payload) => {
  const messageId = msgType & ~PROTO_MASK;
  const payloadHex = payload ? payload.toString('hex').substring(0, 100) : 'none';
  const isDeadlockApp = DEADLOCK_APP_IDS.includes(Number(appId));

  writeDeadlockGcTrace('gc_message', {
    appId,
    msgType,
    messageId,
    payloadHex,
    isDeadlockApp,
  });

  // ENHANCED DEBUG: Log ALL GC messages for diagnosis
  log('info', '🚀 GC MESSAGE RECEIVED', {
    appId,
    messageId,
    messageIdHex: messageId.toString(16),
    msgType,
    msgTypeHex: msgType.toString(16),
    payloadLength: payload ? payload.length : 0,
    payloadHex,
    isDeadlockApp,
    expectedWelcome: GC_MSG_CLIENT_WELCOME,
    expectedResponses: playtestMsgConfigs.map(p => p.response)
  });

  if (messageId === GC_MSG_CLIENT_TO_GC_UPDATE_HERO_BUILD_RESPONSE && heroBuildPublishWaiter) {
    loadHeroBuildProto()
      .then(() => {
        const resp = UpdateHeroBuildResponseMsg.decode(payload);
        heroBuildPublishWaiter.resolve(resp);
      })
      .catch((err) => heroBuildPublishWaiter.reject(err));
    return;
  }

  if (messageId === GC_MSG_CLIENT_WELCOME && isDeadlockApp) {
    log('info', '?? RECEIVED DEADLOCK GC WELCOME - GC CONNECTION ESTABLISHED!', {
      appId,
      messageId,
      payloadLength: payload ? payload.length : 0
    });
    notifyDeadlockGcReady();
    return;
  }

  const matchingResponse = playtestMsgConfigs.find(config => config.response === messageId);
  if (matchingResponse || messageId === GC_MSG_SUBMIT_PLAYTEST_USER_RESPONSE) {
    log('info', '?? POTENTIAL PLAYTEST RESPONSE DETECTED!', {
      appId,
      messageId,
      configName: matchingResponse?.name || 'direct_match',
      sendId: matchingResponse?.send ?? GC_MSG_SUBMIT_PLAYTEST_USER,
      responseId: matchingResponse?.response ?? GC_MSG_SUBMIT_PLAYTEST_USER_RESPONSE
    });
    handlePlaytestInviteResponse(appId, msgType, payload);
    return;
  }

  if (!isDeadlockApp) return;

  log('debug', 'Received unknown GC message', {
    msgType: messageId,
    expectedWelcome: GC_MSG_CLIENT_WELCOME,
    expectedPlaytestResponse: GC_MSG_SUBMIT_PLAYTEST_USER_RESPONSE
  });
});
client.on('disconnected', (eresult, msg) => {
  runtimeState.logged_on = false;
  runtimeState.logging_in = false;
  loginInProgress = false;
  runtimeState.last_disconnect_at = nowSeconds();
  runtimeState.last_disconnect_eresult = eresult;
  deadlockAppActive = false;
  deadlockGcReady = false;
  runtimeState.deadlock_gc_ready = false;
  lastLoggedGcTokenCount = 0;
  flushDeadlockGcWaiters(new Error('Steam disconnected'));
  flushPendingPlaytestInvites(new Error('Steam disconnected'));
  log('warn', 'Steam disconnected', { eresult, msg });
  scheduleReconnect('disconnect');
  scheduleStatePublish({ reason: 'disconnected', eresult });
});
client.on('error', (err) => {
  runtimeState.last_error = { message: err && err.message ? err.message : String(err), eresult: err && typeof err.eresult === 'number' ? err.eresult : undefined };
  runtimeState.logging_in = false; loginInProgress = false;
  const text = String(err && err.message ? err.message : '').toLowerCase();
  log('error', 'Steam client error', { error: runtimeState.last_error.message, eresult: runtimeState.last_error.eresult });
  if (text.includes('invalid refresh') || text.includes('expired') || text.includes('refresh token')) {
    if (refreshToken) { log('warn', 'Clearing refresh token after authentication failure'); updateRefreshToken(''); writeToken(REFRESH_TOKEN_PATH, ''); }
    return;
  }
  if (text.includes('ratelimit') || text.includes('rate limit') || text.includes('throttle')) {
    log('warn', 'Rate limit encountered; waiting for explicit login task');
    return;
  }
  scheduleReconnect('error');
  scheduleStatePublish({ reason: 'error', message: runtimeState.last_error ? runtimeState.last_error.message : null });
});
client.on('sessionExpired', () => {
  log('warn', 'Steam session expired');
  runtimeState.logged_on = false;
  scheduleReconnect('session-expired');
  scheduleStatePublish({ reason: 'session_expired' });
});

// ---------- Startup ----------
function autoLoginIfPossible() {
  if (!refreshToken) { log('info', 'Auto-login disabled (no refresh token). Waiting for tasks.'); scheduleStatePublish({ reason: 'auto_login_skipped' }); return; }
  const result = initiateLogin('auto-start', {});
  log('info', 'Auto-login kick-off', result);
  const started = Boolean(result?.started);
  scheduleStatePublish({ reason: 'auto_login', started });
}
autoLoginIfPossible();
publishStandaloneState({ reason: 'startup' });

// QuickInvites: Auto-Ensure-Loop starten (hält >=1 available), sofern Modul die Methode anbietet
if (typeof quickInvites.startAutoEnsure === 'function') {
  quickInvites.startAutoEnsure();
}

function shutdown(code = 0) {
  try {
    log('info', 'Shutting down Steam bridge');
    if (typeof quickInvites.stopAutoEnsure === 'function') quickInvites.stopAutoEnsure();
    statusAnzeige.stop();
    flushPendingPlaytestInvites(new Error('Service shutting down'));
    flushDeadlockGcWaiters(new Error('Service shutting down'));
    client.logOff();
  } catch (err) {
    log('warn', 'Error during shutdown cleanup', { error: err && err.message ? err.message : String(err) });
  }
  try { db.close(); } catch (err) {
    log('warn', 'Failed to close database during shutdown', { error: err && err.message ? err.message : String(err) });
  }
  process.exit(code);
}
process.on('SIGINT', () => shutdown(0));
process.on('SIGTERM', () => shutdown(0));
process.on('uncaughtException', (err) => { log('error', 'Uncaught exception', { error: err && err.stack ? err.stack : err }); shutdown(1); });
process.on('unhandledRejection', (err) => { log('error', 'Unhandled rejection', { error: err && err.stack ? err.stack : err }); });
