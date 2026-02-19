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
 *   - GC_GET_PROFILE_CARD
 *
 * Erfordert: steam-user, better-sqlite3
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const https = require('https');
const { URL } = require('url');
const protobuf = require('protobufjs');
const SteamUser = require('steam-user');
const Database = require('better-sqlite3');
const { StatusAnzeige } = require('./statusanzeige');
const { DeadlockGcBot } = require('./deadlock_gc_bot');
const { GcBuildSearch, GC_MSG_FIND_HERO_BUILDS_RESPONSE } = require('./gc_build_search');
const { GcProfileCard } = require('./gc_profile_card');
const { BuildCatalogManager } = require('./build_catalog_manager');
const {
  DEADLOCK_GC_PROTOCOL_OVERRIDE_PATH,
  getHelloPayloadOverride,
  getPlaytestOverrides,
  getOverrideInfo: getGcOverrideInfo,
} = require('./deadlock_gc_protocol');

// ---------- Logging ----------
const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
const LOG_LEVEL = (process.env.LOG_LEVEL || 'info').toLowerCase();
const LOG_THRESHOLD = Object.prototype.hasOwnProperty.call(LOG_LEVELS, LOG_LEVEL)
  ? LOG_LEVELS[LOG_LEVEL]
  : LOG_LEVELS.info;

const STEAM_LOG_FILE = path.join(__dirname, '..', '..', '..', 'logs', 'steam_bridge.log');
const MAX_LOG_LINES = 10000;
let steamLogLineCount = 0;

function rotateLogFile(filePath) {
  try {
    const content = fs.readFileSync(filePath, 'utf8');
    const lines = content.split('\n');
    if (lines.length <= MAX_LOG_LINES) {
      return lines.length;
    }
    const newContent = lines.slice(-MAX_LOG_LINES).join('\n');
    fs.writeFileSync(filePath, newContent, 'utf8');
    return MAX_LOG_LINES;
  } catch (err) {
    if (err && err.code === 'ENOENT') {
      return 0;
    }
    return 0;
  }
}

// Initial check
steamLogLineCount = rotateLogFile(STEAM_LOG_FILE);

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
  const line = JSON.stringify(payload) + '\n';
  // Ignore EPIPE errors when parent process closes stdout
  try {
    console.log(JSON.stringify(payload));
  } catch (err) {
    // Ignore broken pipe errors (EPIPE)
  }
  // Also write to file
  try {
    fs.appendFileSync(STEAM_LOG_FILE, line, 'utf8');
    steamLogLineCount++;
    if (steamLogLineCount > MAX_LOG_LINES + 500) {
      steamLogLineCount = rotateLogFile(STEAM_LOG_FILE);
    }
  } catch (err) {
    // Ignore file write errors
  }
}

function convertKeysToCamelCase(obj) {
    if (Array.isArray(obj)) {
        return obj.map(v => convertKeysToCamelCase(v));
    } else if (obj !== null && obj.constructor === Object) {
        return Object.keys(obj).reduce((result, key) => {
            const camelCaseKey = key.replace(/_([a-z])/g, g => g[1].toUpperCase());
            result[camelCaseKey] = convertKeysToCamelCase(obj[key]);
            return result;
        }, {});
    }
    return obj;
}

async function getPersonaName(accountId) {
    if (!client || !accountId) return String(accountId);

    try {
        const steamId = SteamID.fromIndividualAccountID(accountId);
        const steamId64 = steamId.getSteamID64();

        // Check cache first
        if (client.users && client.users[steamId64]) {
            return client.users[steamId64].player_name || String(accountId);
        }

        // Fetch from network if not in cache
        if (typeof client.getPersonas === 'function') {
            const personas = await client.getPersonas([steamId]);
            const persona = personas[steamId64];
            if (persona && persona.player_name) {
                return persona.player_name;
            }
        }
    } catch (err) {
        log('warn', 'Failed to get persona name', { accountId, error: err.message });
    }

    return String(accountId); // Fallback to ID
}

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
const GC_MSG_CLIENT_HELLO_ALT = 9018;
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
const STEAM_API_KEY = ((process.env.STEAM_API_KEY || process.env.STEAM_WEB_API_KEY || '') + '').trim() || null;
if (STEAM_API_KEY) {
  log('info', 'Steam API key is configured', { 
    length: STEAM_API_KEY.length,
    prefix: STEAM_API_KEY.substring(0, 3) + '...',
    suffix: '...' + STEAM_API_KEY.substring(STEAM_API_KEY.length - 3)
  });
} else {
  log('warn', 'Steam API key is NOT configured');
}
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

// NOTE: External Deadlock API removed - now using In-Game GC for build discovery

const PROJECT_ROOT = path.resolve(__dirname, '..', '..', '..');
const STEAM_TOKEN_VAULT_SCRIPT = path.join(PROJECT_ROOT, 'cogs', 'steam', 'steam_token_vault_cli.py');
const STEAM_TOKEN_VAULT_ENABLED = process.platform === 'win32'
  && !['0', 'false', 'no', 'off'].includes(String(process.env.STEAM_USE_WINDOWS_VAULT || '1').trim().toLowerCase());
const STEAM_VAULT_REFRESH_TOKEN = 'refresh';
const STEAM_VAULT_MACHINE_TOKEN = 'machine';
const GC_TRACE_LOG_PATH = path.join(PROJECT_ROOT, 'logs', 'deadlock_gc_messages.log');
let gcTraceLineCount = 0;

function writeDeadlockGcTrace(event, details = {}) {
  try {
    if (gcTraceLineCount === 0) {
      gcTraceLineCount = rotateLogFile(GC_TRACE_LOG_PATH);
    }
    const entry = {
      time: new Date().toISOString(),
      event,
      ...details,
    };
    const line = JSON.stringify(entry) + os.EOL;
    fs.appendFileSync(GC_TRACE_LOG_PATH, line, 'utf8');
    gcTraceLineCount++;
    
    if (gcTraceLineCount > MAX_LOG_LINES + 200) {
      gcTraceLineCount = rotateLogFile(GC_TRACE_LOG_PATH);
    }
  } catch (err) {
    // Avoid recursive logging loops.
    console.error('Failed to write Deadlock GC trace', err && err.message ? err.message : err);
  }
}

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

// NOTE: sendFindBuildsRequest and upsertBuildsToDatabase removed
// Build discovery now uses GC via BuildCatalogManager

async function loadWebApiFriendIds(force = false) {
  if (!STEAM_API_KEY) {
    if (!webApiFriendCacheWarned) {
      webApiFriendCacheWarned = true;
      log('debug', 'Steam API key not configured - friendship fallback disabled');
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
  url.searchParams.set('key', STEAM_API_KEY);
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

let vaultPythonRunner = null;
let vaultPythonProbeDone = false;
let vaultUnavailableLogged = false;
let vaultScriptMissingLogged = false;

function _resolveVaultRunner() {
  if (!STEAM_TOKEN_VAULT_ENABLED) return null;
  if (vaultPythonProbeDone) return vaultPythonRunner;
  vaultPythonProbeDone = true;

  const configured = String(process.env.STEAM_VAULT_PYTHON || process.env.PYTHON || '').trim();
  const candidates = [];
  if (configured) candidates.push({ cmd: configured, prefix: [] });
  candidates.push({ cmd: 'python', prefix: [] });
  if (process.platform === 'win32') candidates.push({ cmd: 'py', prefix: ['-3'] });

  for (const candidate of candidates) {
    try {
      const probe = spawnSync(
        candidate.cmd,
        [...candidate.prefix, '--version'],
        { encoding: 'utf8', windowsHide: true, timeout: 4000 }
      );
      if (probe.error && probe.error.code === 'ENOENT') continue;
      if (probe.status === 0 || !probe.error) {
        vaultPythonRunner = candidate;
        break;
      }
    } catch (err) {
      continue;
    }
  }

  if (!vaultPythonRunner && !vaultUnavailableLogged) {
    vaultUnavailableLogged = true;
    log('warn', 'Windows vault enabled, but no Python interpreter found. Falling back to token files.');
  }
  return vaultPythonRunner;
}

function _runVaultCli(command, tokenType, value = null, savedAtIso = null) {
  if (!STEAM_TOKEN_VAULT_ENABLED || !tokenType) return { ok: false, output: '' };
  if (!fs.existsSync(STEAM_TOKEN_VAULT_SCRIPT)) {
    if (!vaultScriptMissingLogged) {
      vaultScriptMissingLogged = true;
      log('warn', 'Steam vault helper script missing. Falling back to token files.', {
        script: STEAM_TOKEN_VAULT_SCRIPT,
      });
    }
    return { ok: false, output: '' };
  }
  const runner = _resolveVaultRunner();
  if (!runner) return { ok: false, output: '' };

  const args = [...runner.prefix, STEAM_TOKEN_VAULT_SCRIPT, command, '--token', tokenType];
  if (command === 'set') {
    args.push('--value', value || '');
    if (savedAtIso) args.push('--saved-at', savedAtIso);
  }

  let result = null;
  try {
    result = spawnSync(runner.cmd, args, { encoding: 'utf8', windowsHide: true, timeout: 8000 });
  } catch (err) {
    log('warn', 'Failed to execute Steam vault helper', {
      operation: command,
      token_type: tokenType,
      error: err && err.message ? err.message : String(err),
    });
    return { ok: false, output: '' };
  }

  if (result.error) {
    log('warn', 'Steam vault helper execution error', {
      operation: command,
      token_type: tokenType,
      error: result.error && result.error.message ? result.error.message : String(result.error),
    });
    return { ok: false, output: '' };
  }
  if (result.status !== 0) {
    log('warn', 'Steam vault helper returned non-zero exit code', {
      operation: command,
      token_type: tokenType,
      exit_code: result.status,
    });
    return { ok: false, output: '' };
  }
  const output = String(result.stdout || '').replace(/\r?\n$/, '');
  return { ok: true, output };
}

function readToken(filePath, tokenType = null) {
  if (tokenType) {
    const vaultRead = _runVaultCli('get', tokenType);
    if (vaultRead.ok && vaultRead.output) return vaultRead.output.trim();
  }

  try {
    const token = fs.readFileSync(filePath, 'utf8').trim();
    if (!token) return '';

    // Migrate existing file-based tokens into Windows vault if available.
    if (tokenType && STEAM_TOKEN_VAULT_ENABLED) {
      let savedAtIso = null;
      try {
        savedAtIso = fs.statSync(filePath).mtime.toISOString();
      } catch (err) {
        savedAtIso = null;
      }
      const migrated = _runVaultCli('set', tokenType, token, savedAtIso);
      if (migrated.ok && migrated.output === 'windows_vault') {
        try { fs.rmSync(filePath, { force: true }); } catch (err) { }
        log('info', 'Migrated Steam token from file to Windows vault', { token_type: tokenType });
      }
    }

    return token;
  } catch (err) {
    if (err && err.code === 'ENOENT') return '';
    log('warn', 'Failed to read token file', { path: filePath, error: err.message });
    return '';
  }
}

function writeToken(filePath, value, tokenType = null) {
  const normalized = value ? String(value).trim() : '';

  if (tokenType) {
    const vaultResult = normalized
      ? _runVaultCli('set', tokenType, normalized)
      : _runVaultCli('delete', tokenType);
    if (vaultResult.ok && vaultResult.output === 'windows_vault') {
      try { fs.rmSync(filePath, { force: true }); } catch (err) { }
      return 'windows_vault';
    }
    if (vaultResult.ok && vaultResult.output === 'file') {
      return 'file';
    }
  }

  try {
    if (!normalized) {
      fs.rmSync(filePath, { force: true });
      return 'file';
    }
    fs.writeFileSync(filePath, `${normalized}\n`, 'utf8');
    return 'file';
  } catch (err) {
    log('warn', 'Failed to persist token', { path: filePath, error: err.message });
    return 'file';
  }
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

function composeBuildDescription(base, originId, authorName) {
  const parts = [];
  const desc = (base || '').trim();
  if (desc) parts.push(desc);
  parts.push('www.twitch.tv/earlysalty (deutsch)');
  parts.push('Deutsche Deadlock Community: discord.gg/XmnqMbUZ7Z');
  if (originId) parts.push(`Original Build ID: ${originId}`);
  if (authorName) parts.push(`Original Author: ${authorName}`);
  return parts.join('\n');
}

async function buildUpdateHeroBuild(row, meta = {}) {
  const tags = safeJsonParse(row.tags_json || '[]');
  const details = cleanBuildDetails(safeJsonParse(row.details_json || '{}'));
  const targetName = meta.target_name || row.name || '';
  const targetDescription = meta.target_description || row.description || '';
  const targetLanguage = safeNumber(meta.target_language) ?? safeNumber(row.language) ?? 0;
  const authorId = safeNumber(meta.author_account_id) ?? safeNumber(row.author_account_id);
  const nowTs = Math.floor(Date.now() / 1000);
  const baseVersion = safeNumber(row.version) || 1;
  const originId = safeNumber(meta.origin_build_id) ?? safeNumber(row.origin_build_id) ?? safeNumber(row.hero_build_id);

  const originalAuthorName = await getPersonaName(safeNumber(row.author_account_id));

  return {
    hero_build_id: safeNumber(row.hero_build_id),
    hero_id: safeNumber(row.hero_id),
    author_account_id: authorId,
    origin_build_id: originId,
    last_updated_timestamp: nowTs,
    publish_timestamp: nowTs,
    name: targetName,
    description: composeBuildDescription(targetDescription, originId, originalAuthorName),
    language: targetLanguage,
    version: baseVersion + 1,
    tags: Array.isArray(tags) ? tags.map((t) => Number(t)) : [],
    details: details && typeof details === 'object' ? details : {},
  };
}

async function buildMinimalHeroBuild(row, meta = {}) {
  log('info', 'buildMinimalHeroBuild: FIXED VERSION v2 - Creating minimal build');
  const targetName = meta.target_name || row.name || '';
  const targetDescription = meta.target_description || row.description || '';
  const targetLanguage = safeNumber(meta.target_language) ?? 0;
  const authorId = safeNumber(meta.author_account_id) ?? safeNumber(row.author_account_id);

  const originalAuthorName = await getPersonaName(safeNumber(row.author_account_id));

  const result = {
    hero_id: safeNumber(row.hero_id),
    author_account_id: authorId,
    origin_build_id: undefined,
    last_updated_timestamp: undefined,
    name: targetName,
    description: composeBuildDescription(targetDescription, row.hero_build_id, originalAuthorName),
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

async function mapHeroBuildFromRow(row, meta = {}) {
  if (!row) throw new Error('hero_build_sources row missing');
  const tags = safeJsonParse(row.tags_json || '[]');
  const details = cleanBuildDetails(safeJsonParse(row.details_json || '{}'));
  const targetName = meta.target_name || row.name || '';
  const targetDescription = meta.target_description || row.description || '';
  const targetLanguage = safeNumber(meta.target_language) ?? safeNumber(row.language) ?? 0;
  const publisherAccountId = safeNumber(meta.author_account_id) ?? safeNumber(row.author_account_id);
  const nowTs = Math.floor(Date.now() / 1000);

  const originalAuthorName = await getPersonaName(safeNumber(row.author_account_id));

  return {
    hero_id: safeNumber(row.hero_id),
    author_account_id: publisherAccountId,
    origin_build_id: safeNumber(meta.origin_build_id) ?? safeNumber(row.hero_build_id) ?? safeNumber(row.origin_build_id),
    last_updated_timestamp: nowTs,
    publish_timestamp: nowTs,
    name: targetName,
    description: composeBuildDescription(targetDescription, meta.origin_build_id ?? row.hero_build_id, originalAuthorName),
    language: targetLanguage,
    version: (safeNumber(row.version) || 0) + 1,
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

  let heroBuildMsg, message;
  try {
    const camelCaseHeroBuild = convertKeysToCamelCase(cleanedHeroBuild);
    heroBuildMsg = HeroBuildMsg.create(camelCaseHeroBuild);
  } catch (err) {
    log('error', 'sendHeroBuildUpdate: HeroBuildMsg.create() failed', {
      error: err.message,
      stack: err.stack,
      cleanedHeroBuild: JSON.stringify(cleanedHeroBuild)
    });
    throw new Error(`HeroBuildMsg.create failed: ${err.message}`);
  }

  try {
    message = UpdateHeroBuildMsg.create({ heroBuild: heroBuildMsg });
    log('info', 'sendHeroBuildUpdate: UpdateHeroBuildMsg created successfully');
  } catch (err) {
    log('error', 'sendHeroBuildUpdate: UpdateHeroBuildMsg.create() failed', {
      error: err.message,
      stack: err.stack
    });
    throw new Error(`UpdateHeroBuildMsg.create failed: ${err.message}`);
  }

  log('info', 'sendHeroBuildUpdate: Message created', {
    message: JSON.stringify(message),
    messageKeys: Object.keys(message)
  });

  let payload;
  try {
    log('info', 'sendHeroBuildUpdate: About to encode message');
    payload = UpdateHeroBuildMsg.encode(message).finish();
    log('info', 'sendHeroBuildUpdate: Payload encoded successfully', {
      payloadType: typeof payload,
      payloadIsBuffer: Buffer.isBuffer(payload),
      payloadLength: payload ? payload.length : 'null/undefined'
    });
  } catch (err) {
    log('error', 'sendHeroBuildUpdate: encode().finish() failed', {
      error: err.message,
      stack: err.stack,
      message: JSON.stringify(message)
    });
    throw new Error(`Protobuf encoding failed: ${err.message}`);
  }

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
    client.sendToGC(DEADLOCK_APP_ID, PROTO_MASK | GC_MSG_CLIENT_TO_GC_UPDATE_HERO_BUILD, {}, payload);
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
const FRIEND_SYNC_INTERVAL_MS = Math.max(60000, parseInt(process.env.STEAM_FRIEND_SYNC_MS || '300000', 10));
const FRIEND_REQUEST_BATCH_SIZE = Math.max(1, parseInt(process.env.STEAM_FRIEND_REQUEST_BATCH || '10', 10));
const FRIEND_REQUEST_RETRY_SECONDS = Math.max(60, parseInt(process.env.STEAM_FRIEND_REQUEST_RETRY_SEC || '900', 10));
const STEAM_TASKS_MAX_ROWS = Math.max(1, parseInt(process.env.STEAM_TASKS_MAX_ROWS || '1000', 10));

const dbPath = resolveDbPath();
ensureDir(path.dirname(dbPath));
log('info', 'Using SQLite database', { dbPath });
const db = new Database(dbPath);
db.pragma('journal_mode = WAL');
db.pragma('synchronous = NORMAL');
db.pragma(`busy_timeout = ${DB_BUSY_TIMEOUT_MS}`);

function installSteamTaskCapTrigger() {
  try {
    db.prepare(`
      CREATE TRIGGER IF NOT EXISTS trg_cap_steam_tasks
      AFTER INSERT ON steam_tasks
      BEGIN
        DELETE FROM steam_tasks
        WHERE id IN (
          SELECT id FROM steam_tasks
          WHERE status NOT IN ('PENDING','RUNNING')
          ORDER BY created_at ASC, id ASC
          LIMIT (
            SELECT CASE WHEN total > ${STEAM_TASKS_MAX_ROWS} THEN total - ${STEAM_TASKS_MAX_ROWS} ELSE 0 END
            FROM (SELECT COUNT(*) AS total FROM steam_tasks)
          )
        );
      END;
    `).run();
  } catch (err) {
    log('warn', 'Failed to ensure steam_tasks cap trigger', { error: err && err.message ? err.message : err });
  }
}

function pruneSteamTasks(reason = 'startup') {
  try {
    const row = db.prepare(`
      SELECT CASE WHEN COUNT(*) > @limit THEN COUNT(*) - @limit ELSE 0 END AS excess
      FROM steam_tasks
    `).get({ limit: STEAM_TASKS_MAX_ROWS });
    const excess = row && Number.isFinite(row.excess) ? row.excess : 0;
    if (!excess) return 0;

    const info = db.prepare(`
      DELETE FROM steam_tasks
      WHERE id IN (
        SELECT id FROM steam_tasks
        WHERE status NOT IN ('PENDING','RUNNING')
        ORDER BY created_at ASC, id ASC
        LIMIT @toDelete
      )
    `).run({ toDelete: excess });

    if (info && info.changes > 0) {
      log('info', 'Pruned steam_tasks rows', { deleted: info.changes, max_rows: STEAM_TASKS_MAX_ROWS, reason });
    }
    return info && info.changes ? info.changes : 0;
  } catch (err) {
    log('warn', 'Failed to prune steam_tasks', { error: err && err.message ? err.message : err, reason });
    return 0;
  }
}

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
installSteamTaskCapTrigger();
pruneSteamTasks('startup');

// Steam links + friend requests (keeps DB in sync with actual Steam friends)
db.prepare(`
  CREATE TABLE IF NOT EXISTS steam_links(
    user_id    INTEGER NOT NULL,
    steam_id   TEXT    NOT NULL,
    name       TEXT,
    verified   INTEGER DEFAULT 0,
    primary_account INTEGER DEFAULT 0,
    deadlock_rank INTEGER,
    deadlock_rank_name TEXT,
    deadlock_subrank INTEGER,
    deadlock_badge_level INTEGER,
    deadlock_rank_updated_at INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, steam_id)
  )
`).run();
for (const alterSql of [
  "ALTER TABLE steam_links ADD COLUMN deadlock_rank INTEGER",
  "ALTER TABLE steam_links ADD COLUMN deadlock_rank_name TEXT",
  "ALTER TABLE steam_links ADD COLUMN deadlock_subrank INTEGER",
  "ALTER TABLE steam_links ADD COLUMN deadlock_badge_level INTEGER",
  "ALTER TABLE steam_links ADD COLUMN deadlock_rank_updated_at INTEGER",
]) {
  try {
    db.prepare(alterSql).run();
  } catch (err) {
    const text = String((err && err.message) || "").toLowerCase();
    if (!text.includes("duplicate column name")) {
      log('warn', 'Failed to alter steam_links table', { alterSql, error: err && err.message ? err.message : String(err) });
    }
  }
}
db.prepare(`CREATE INDEX IF NOT EXISTS idx_steam_links_user ON steam_links(user_id)`).run();
db.prepare(`CREATE INDEX IF NOT EXISTS idx_steam_links_steam ON steam_links(steam_id)`).run();

db.prepare(`
  CREATE TABLE IF NOT EXISTS steam_friend_requests(
    steam_id TEXT PRIMARY KEY,
    status TEXT DEFAULT 'pending',
    requested_at INTEGER DEFAULT (strftime('%s','now')),
    last_attempt INTEGER,
    attempts INTEGER DEFAULT 0,
    error TEXT
  )
`).run();

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
const STALE_TASK_TIMEOUT_S = 600; // Tasks länger als 10 Min in RUNNING -> FAILED
const failStaleTasksStmt = db.prepare(`
  UPDATE steam_tasks
     SET status = 'FAILED',
         error = 'Task stale: keine Antwort innerhalb ' || ? || 's',
         finished_at = ?,
         updated_at = ?
   WHERE status = 'RUNNING'
     AND started_at IS NOT NULL
     AND started_at < ?
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

// NOTE: selectWatchedAuthorsStmt and updateWatchedAuthorMetadataStmt moved to BuildCatalogManager

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

// NOTE: insertHeroBuildCloneStmt moved to BuildCatalogManager

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
const steamLinksForSyncStmt = db.prepare(`
  SELECT steam_id, user_id, name, verified
    FROM steam_links
   WHERE steam_id IS NOT NULL
     AND steam_id != ''
`);
const upsertPendingFriendRequestStmt = db.prepare(`
  INSERT INTO steam_friend_requests(steam_id, status)
  VALUES(?, 'pending')
  ON CONFLICT(steam_id) DO UPDATE SET
    status='pending',
    last_attempt=CASE WHEN steam_friend_requests.status='sent' THEN NULL ELSE steam_friend_requests.last_attempt END,
    attempts=CASE WHEN steam_friend_requests.status='sent' THEN 0 ELSE COALESCE(steam_friend_requests.attempts, 0) END,
    error=NULL,
    requested_at=CASE WHEN steam_friend_requests.status='sent' THEN strftime('%s','now') ELSE steam_friend_requests.requested_at END
`);
const selectFriendRequestBatchStmt = db.prepare(`
  SELECT steam_id, status, attempts, last_attempt
    FROM steam_friend_requests
   WHERE status != 'sent'
ORDER BY COALESCE(last_attempt, 0) ASC
   LIMIT ?
`);
const markFriendRequestSentStmt = db.prepare(`
  UPDATE steam_friend_requests
     SET status='sent',
         last_attempt=?,
         attempts=COALESCE(attempts,0)+1,
         error=NULL
   WHERE steam_id=?
`);
const markFriendRequestFailedStmt = db.prepare(`
  UPDATE steam_friend_requests
     SET status='pending',
         last_attempt=?,
         attempts=COALESCE(attempts,0)+1,
         error=?
   WHERE steam_id=?
`);
const verifySteamLinkStmt = db.prepare(`
  UPDATE steam_links
  SET verified = 1,
      name = COALESCE(NULLIF(@name, ''), name),
      updated_at = CURRENT_TIMESTAMP
  WHERE steam_id = @steam_id
`);

const unverifySteamLinkStmt = db.prepare(`
  UPDATE steam_links
  SET verified = 0,
      updated_at = CURRENT_TIMESTAMP
  WHERE steam_id = ?
`);

function verifySteamLink(steamId64, displayName) {
  const sid = normalizeSteamId64(steamId64);
  if (!sid) return false;
  const name = displayName ? String(displayName).trim() : '';
  try {
    const info = verifySteamLinkStmt.run({ steam_id: sid, name });
    return info.changes > 0;
  } catch (err) {
    log('warn', 'Failed to verify steam link', {
      steam_id64: sid,
      error: err && err.message ? err.message : String(err),
    });
    return false;
  }
}

// ---------- Steam State ----------
let refreshToken = readToken(REFRESH_TOKEN_PATH, STEAM_VAULT_REFRESH_TOKEN);
let machineAuthToken = readToken(MACHINE_TOKEN_PATH, STEAM_VAULT_MACHINE_TOKEN);

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

let gcUnhealthySince = 0;
const GC_UNHEALTHY_THRESHOLD_MS = 15 * 60 * 1000; // 15 minutes

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
let friendSyncInProgress = false;

// ---------- Steam Client ----------
const client = new SteamUser();
const deadlockGcBot = new DeadlockGcBot({
  client,
  log: (level, msg, extra) => log(level, msg, extra),
  trace: writeDeadlockGcTrace,
  requestTokens: (reason) => requestDeadlockGcTokens(reason || 'deadlock_gc_bot'),
  getTokenCount: () => getDeadlockGcTokenCount(),
});
const gcBuildSearch = new GcBuildSearch({
  client,
  log: (level, msg, extra) => log(level, msg, extra),
  trace: writeDeadlockGcTrace,
  db,
  appId: DEADLOCK_APP_ID,
});
const gcProfileCard = new GcProfileCard({
  client,
  log: (level, msg, extra) => log(level, msg, extra),
  trace: writeDeadlockGcTrace,
  appId: DEADLOCK_APP_ID,
});
// BuildCatalogManager needs gcBuildSearch and getPersonaName (defined later)
// Initialize after all dependencies are ready
let buildCatalogManager = null;
let gcTokenRequestInFlight = false;
let lastLoggedGcTokenCount = 0;
let heroBuildPublishWaiter = null;

client.setOption('autoRelogin', false);
client.setOption('machineName', process.env.STEAM_MACHINE_NAME || 'DeadlockBridge');

const statusAnzeige = new StatusAnzeige(client, log, {
  appId: DEADLOCK_APP_ID,
  language: process.env.STEAM_PRESENCE_LANGUAGE || 'german',
  db,
  steamApiKey: STEAM_API_KEY,
  webApiTimeoutMs: WEB_API_HTTP_TIMEOUT_MS,
  webSummaryCacheTtlMs: WEB_API_FRIEND_CACHE_TTL_MS,
});
log('info', 'Statusanzeige initialisiert', {
  persistence: statusAnzeige.persistenceEnabled,
  pollIntervalMs: statusAnzeige.pollIntervalMs,
});
statusAnzeige.start();

// Initialize BuildCatalogManager (after getPersonaName is available)
buildCatalogManager = new BuildCatalogManager({
  db,
  gcBuildSearch,
  log: (level, msg, extra) => log(level, msg, extra),
  trace: writeDeadlockGcTrace,
  getPersonaName: async (accountId) => getPersonaName(accountId),
});
log('info', 'BuildCatalogManager initialisiert');

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
    client.sendToGC(appId, PROTO_MASK + GC_MSG_CLIENT_HELLO_ALT, {}, payload);
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
  1: { key: 'eResponse_InternalError', message: 'Interner Fehler bei Steam. Bitte versuche es in ein paar Minuten erneut.' },
  3: { key: 'eResponse_InvalidFriend', message: 'Wir sind keine Steam-Freunde. Bitte nimm die Freundschaftsanfrage des Bots auf Steam an.' },
  4: { key: 'eResponse_NotFriendsLongEnough', message: 'Steam-Beschränkung: Die Freundschaft muss mind. 30 Tage bestehen.' },
  5: { key: 'eResponse_AlreadyHasGame', message: 'Du hast Deadlock bereits! Prüfe deine Steam-Bibliothek oder https://store.steampowered.com/account/playtestinvites' },
  6: { key: 'eResponse_LimitedUser', message: 'Dein Steam-Account ist eingeschränkt (Limited User). Du musst mind. 5$ auf Steam ausgegeben haben, um Invites zu erhalten.' },
  7: { key: 'eResponse_InviteLimitReached', message: 'Das tägliche Invite-Limit ist erreicht. Bitte versuche es morgen erneut.' },
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
        if (err) {
          // EResult.DuplicateName (29) usually means friend request already sent or already friends
          if (err.message && err.message.includes('DuplicateName')) {
            return resolve(true);
          }
          return reject(err);
        }
        resolve(true);
      });
    } catch (err) {
      reject(err);
    }
  });
}

function normalizeSteamId64(value) {
  if (!value) return null;
  const sid = String(value).trim();
  if (!sid) return null;
  if (!/^\d{5,}$/.test(sid)) return null;
  return sid;
}

function queueFriendRequestForId(steamId64) {
  const sid = normalizeSteamId64(steamId64);
  if (!sid) return false;
  try {
    upsertPendingFriendRequestStmt.run(sid);
    return true;
  } catch (err) {
    log('warn', 'Failed to queue Steam friend request', {
      steam_id64: sid,
      error: err && err.message ? err.message : String(err),
    });
    return false;
  }
}

async function collectKnownFriendIds() {
  const ids = new Set();
  const relMap = SteamUser.EFriendRelationship || {};
  const friendCode = Number(relMap.Friend);
  let clientCount = 0;
  let webCount = 0;

  if (client && client.myFriends && Object.keys(client.myFriends).length) {
    for (const [sid, rel] of Object.entries(client.myFriends)) {
      if (Number(rel) === friendCode) {
        const norm = normalizeSteamId64(sid);
        if (norm) {
          ids.add(norm);
          clientCount += 1;
        }
      }
    }
  }

  try {
    const webIds = await loadWebApiFriendIds(false);
    if (webIds && webIds.size) {
      webIds.forEach((sid) => {
        const norm = normalizeSteamId64(sid);
        if (norm) ids.add(norm);
      });
      webCount = webIds.size;
    }
  } catch (err) {
    log('debug', 'Friend sync: failed to load WebAPI friend list', { error: err && err.message ? err.message : String(err) });
  }

  return { ids, clientCount, webCount };
}

function resolveCachedPersonaName(steamId64) {
  const sid = normalizeSteamId64(steamId64);
  if (!sid || !client || !client.users) return '';
  try {
    const cached = client.users[sid];
    if (cached && cached.player_name) {
      return String(cached.player_name).trim();
    }
  } catch (_) {}
  return '';
}

async function fetchPersonaNames(steamIds) {
  const result = new Map();
  const normalized = Array.from(new Set(
    Array.from(steamIds || []).map((sid) => normalizeSteamId64(sid)).filter(Boolean)
  ));
  if (!normalized.length) return result;

  // Try cached names first to avoid extra network calls
  for (const sid of normalized) {
    const cached = resolveCachedPersonaName(sid);
    if (cached) result.set(sid, cached);
  }

  const remaining = normalized.filter((sid) => !result.has(sid));
  if (!remaining.length || !client || typeof client.getPersonas !== 'function') {
    return result;
  }

  try {
    const personaArgs = remaining.map((sid) => {
      try { return new SteamID(sid); } catch (_) { return sid; }
    });
    const personas = await client.getPersonas(personaArgs);
    if (personas && typeof personas === 'object') {
      for (const [sidKey, persona] of Object.entries(personas)) {
        const sid = normalizeSteamId64(sidKey);
        const name = persona && persona.player_name ? String(persona.player_name).trim() : '';
        if (sid && name) {
          result.set(sid, name);
        }
      }
    }
  } catch (err) {
    log('debug', 'Friend sync: failed to load persona names', {
      error: err && err.message ? err.message : String(err),
    });
  }

  return result;
}

async function processFriendRequestQueue(currentFriends, reason) {
  const outcome = { sent: 0, failed: 0, skipped: 0 };
  if (!runtimeState.logged_on) return outcome;

  const rows = selectFriendRequestBatchStmt.all(FRIEND_REQUEST_BATCH_SIZE);
  const now = nowSeconds();

  for (const row of rows) {
    const sid = normalizeSteamId64(row.steam_id);
    if (!sid) continue;

    if (currentFriends && currentFriends.has(sid)) {
      try {
        markFriendRequestSentStmt.run(now, sid);
        outcome.skipped += 1;
      } catch (err) {
        log('debug', 'Friend sync: failed to mark existing friend request as sent', {
          steam_id64: sid,
          error: err && err.message ? err.message : String(err),
        });
      }
      continue;
    }

    if (row.last_attempt && now - Number(row.last_attempt) < FRIEND_REQUEST_RETRY_SECONDS) {
      continue;
    }

    let parsed;
    try {
      parsed = parseSteamID(sid);
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      markFriendRequestFailedStmt.run(now, message, sid);
      outcome.failed += 1;
      continue;
    }

    try {
      await sendFriendRequest(parsed);
      markFriendRequestSentStmt.run(now, sid);
      outcome.sent += 1;
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      markFriendRequestFailedStmt.run(now, message, sid);
      outcome.failed += 1;
      log('warn', 'Friend request failed', { steam_id64: sid, error: message, reason });
    }
  }

  return outcome;
}

async function syncFriendsAndLinks(reason = 'interval') {
  if (friendSyncInProgress) return;
  if (!runtimeState.logged_on || !client || !client.steamID) return;

  friendSyncInProgress = true;
  try {
    const { ids: friendIds, clientCount, webCount } = await collectKnownFriendIds();
    const ownSid = normalizeSteamId64(
      runtimeState.steam_id64 ||
      (client.steamID && typeof client.steamID.getSteamID64 === 'function' ? client.steamID.getSteamID64() : null)
    );
    if (ownSid) friendIds.delete(ownSid);

    const dbRows = steamLinksForSyncStmt.all();
    const dbSteamIds = new Set();
    const missingNameIds = new Set();
    let queued = 0;

    for (const row of dbRows) {
      const sid = normalizeSteamId64(row.steam_id);
      if (!sid || (ownSid && sid === ownSid)) continue;
      dbSteamIds.add(sid);
      if (Number(row.user_id) === 0) {
        const nameRaw = typeof row.name === 'string' ? row.name : '';
        if (!nameRaw || !String(nameRaw).trim()) {
          missingNameIds.add(sid);
        }
      }
      if (!friendIds.has(sid)) {
        if (queueFriendRequestForId(sid)) queued += 1;
      }
    }

    const idsNeedingName = new Set();
    for (const sid of friendIds) {
      if (ownSid && sid === ownSid) continue;
      if (!dbSteamIds.has(sid) || missingNameIds.has(sid)) {
        idsNeedingName.add(sid);
      }
    }
    const personaNames = await fetchPersonaNames(idsNeedingName);

    let inserted = 0;
    let nameUpdates = 0;
    for (const sid of friendIds) {
      if (ownSid && sid === ownSid) continue;
      const needsInsert = !dbSteamIds.has(sid);
      const needsName = missingNameIds.has(sid);
      const name = personaNames.get(sid) || '';
      const shouldUpsert = needsInsert || (needsName && name);
      if (!shouldUpsert) continue;

      if (!needsInsert && needsName && name) {
         verifySteamLink(sid, name);
         nameUpdates += 1;
      }
    }

    const requestOutcome = await processFriendRequestQueue(friendIds, reason);

    if (queued || inserted || nameUpdates || requestOutcome.sent || requestOutcome.failed) {
      log('info', 'Friend/DB sync completed', {
        reason,
        queued_requests: queued,
        inserted_links: inserted,
        name_updates: nameUpdates,
        friend_requests_sent: requestOutcome.sent,
        friend_requests_failed: requestOutcome.failed,
        friends_known: friendIds.size,
        links_known: dbSteamIds.size,
        friend_sources: { client: clientCount, webapi: webCount },
      });
    } else {
      log('debug', 'Friend/DB sync done (no changes)', {
        reason,
        friends_known: friendIds.size,
        links_known: dbSteamIds.size,
        friend_sources: { client: clientCount, webapi: webCount },
      });
    }
  } catch (err) {
    log('warn', 'Friend/DB sync failed', { reason, error: err && err.message ? err.message : String(err) });
  } finally {
    friendSyncInProgress = false;
  }
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
      attemptTimers: [],
      attempts: 0,
      maxAttempts: Math.max(1, (playtestMsgConfigs.length || DEFAULT_PLAYTEST_MSG_IDS.length)) * estimatedPayloadVariants,
    };

    const cleanup = () => {
      if (entry.timer) clearTimeout(entry.timer);
      if (entry.attemptTimers) {
        entry.attemptTimers.forEach((t) => clearTimeout(t));
        entry.attemptTimers = [];
      }
      removePendingPlaytestInvite(entry);
    };

    entry.resolve = (value) => {
      cleanup();
      resolve(value);
    };

    entry.reject = (err) => {
      cleanup();
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
        const t = setTimeout(() => {
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
        
        entry.attemptTimers.push(t);
        attemptCount++;
      }
    }

    // Also try with the originally working app (if we're not already using it)
    if (!buildPlaytestPayloadOverrideFn && DEADLOCK_APP_ID !== 1422450) {
      const t = setTimeout(() => {
        try {
          const payload = encodeSubmitPlaytestUserPayload(accountId, location);
          client.sendToGC(1422450, PROTO_MASK + GC_MSG_SUBMIT_PLAYTEST_USER, {}, payload);
          log('info', 'Fallback invite attempt to original Deadlock app', { accountId, location });
        } catch (err) {
          log('warn', 'Fallback attempt failed', { error: err.message });
        }
      }, attemptCount * 200);
      entry.attemptTimers.push(t);
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
  log('info', 'DEBUG: Decoded playtest response', { parsed: JSON.stringify(parsedResponse) });
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
  if (norm.includes('email') || norm.includes('@')) return 'email';
  if (norm.includes('two-factor') || norm.includes('authenticator') || norm.includes('mobile')) return 'totp';
  if (norm.includes('device')) return 'device';
  // If it's a domain (contains a dot), assume it's email (e.g., "steam.earlysalty.com")
  if (norm.includes('.')) return 'email';
  return 'unknown';
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
    tasks: { counts: {}, recent: [] },
  };

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
    ).finally(() => { taskInProgress = false; });
    return true; // async
  } else {
    const ok = outcome && outcome.ok;
    completeTask(task.id, ok ? 'DONE' : 'FAILED', outcome, outcome && !ok ? outcome.error : null);
    return false; // sync
  }
}

function processNextTask() {
  if (taskInProgress) return;
  taskInProgress = true;

  let task = null;
  let isAsync = false;
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

      case 'AUTH_REFRESH_GAME_VERSION': {
        const promise = (async () => {
          if (!runtimeState.logged_on) throw new Error('Not logged in');
          const success = await deadlockGcBot.refreshGameVersion();
          return {
            ok: success,
            data: {
              version: deadlockGcBot.sessionNeed,
              updated: success
            }
          };
        })();
        isAsync = finalizeTaskRun(task, promise);
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
        isAsync = finalizeTaskRun(task, promise);
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
        isAsync = finalizeTaskRun(task, promise);
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
                heroBuild = await buildUpdateHeroBuild(src, meta);
                if (minimalUpdate) {
                heroBuild.tags = [];
                heroBuild.details = { mod_categories: [] };
                }
            } else if (useMinimal) {
                heroBuild = await buildMinimalHeroBuild(src, meta);
            } else {
                heroBuild = await mapHeroBuildFromRow(src, meta);
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
            updateHeroBuildCloneUploadedStmt.run('done', null, resp.heroBuildId || null, resp.version || null, originId);
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
        isAsync = finalizeTaskRun(task, promise);
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
        isAsync = finalizeTaskRun(task, promise);
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
        isAsync = finalizeTaskRun(task, promise);
        break;
      }

      case 'GC_GET_PROFILE_CARD': {
        const promise = (async () => {
          if (!runtimeState.logged_on) throw new Error('Not logged in');

          const accountIdRaw = payload?.account_id;
          const steamInput = payload?.steam_id ?? payload?.steam_id64;
          const requestTimeoutRaw = payload?.timeout_ms ?? payload?.request_timeout_ms;
          const gcReadyTimeoutRaw = payload?.gc_ready_timeout_ms ?? payload?.gc_timeout_ms;
          const gcRetryRaw = payload?.gc_ready_retry_attempts ?? payload?.gc_retry_attempts;

          let sid = null;
          if (steamInput !== undefined && steamInput !== null && String(steamInput).trim()) {
            sid = parseSteamID(steamInput);
          }

          const accountId = accountIdRaw != null
            ? Number(accountIdRaw)
            : (sid ? Number(sid.accountid) : null);
          if (!Number.isFinite(accountId) || accountId <= 0) {
            throw new Error('account_id missing or invalid');
          }

          const gcTimeoutMs = Number.isFinite(Number(gcReadyTimeoutRaw))
            ? Number(gcReadyTimeoutRaw)
            : DEFAULT_GC_READY_TIMEOUT_MS;
          const gcRetryAttempts = Number.isFinite(Number(gcRetryRaw))
            ? Number(gcRetryRaw)
            : DEFAULT_GC_READY_ATTEMPTS;

          await waitForDeadlockGcReady(gcTimeoutMs, { retryAttempts: gcRetryAttempts });

          const timeoutMs = Number.isFinite(Number(requestTimeoutRaw))
            ? Number(requestTimeoutRaw)
            : undefined;
          const profileCard = await gcProfileCard.fetchPlayerCard({
            accountId: Number(accountId),
            timeoutMs,
            friendAccessHint: payload?.friend_access_hint !== false,
            devAccessHint: payload?.dev_access_hint,
          });

          const steamId64 = sid && typeof sid.getSteamID64 === 'function'
            ? sid.getSteamID64()
            : null;

          return {
            ok: true,
            data: {
              steam_id64: steamId64,
              account_id: Number(accountId),
              card: profileCard,
            },
          };
        })();
        isAsync = finalizeTaskRun(task, promise);
        break;
      }

      // ========== BUILD DISCOVERY (via GC) ==========
      // Discover builds from watched authors using In-Game GC (replaces external API)
      case 'DISCOVER_WATCHED_BUILDS': {
        const promise = (async () => {
          log('info', 'DISCOVER_WATCHED_BUILDS: Starting build discovery via GC');

          if (!deadlockGcReady) {
            log('warn', 'DISCOVER_WATCHED_BUILDS: Deadlock GC not ready');
            return { ok: false, error: 'Deadlock GC not connected. Please wait for GC connection.' };
          }

          if (!buildCatalogManager) {
            log('error', 'DISCOVER_WATCHED_BUILDS: BuildCatalogManager not initialized');
            return { ok: false, error: 'BuildCatalogManager not initialized' };
          }

          try {
            const result = await buildCatalogManager.discoverWatchedBuilds();
            return {
              ok: result.success,
              data: {
                authors_checked: result.authorsChecked,
                builds_discovered: result.totalNewBuilds + result.totalUpdatedBuilds,
                new_builds: result.totalNewBuilds,
                updated_builds: result.totalUpdatedBuilds,
                errors: result.errors?.length || 0
              }
            };
          } catch (err) {
            log('error', 'DISCOVER_WATCHED_BUILDS: Failed', { error: err.message });
            return { ok: false, error: err.message };
          }
        })();
        isAsync = finalizeTaskRun(task, promise);
        break;
      }

      // ========== HERO-BASED BUILD DISCOVERY ==========
      // Alternative discovery: search by hero instead of author (works when author-search times out)
      case 'DISCOVER_BUILDS_VIA_HEROES': {
        const promise = (async () => {
          log('info', 'DISCOVER_BUILDS_VIA_HEROES: Starting HERO-based build discovery via GC');

          if (!deadlockGcReady) {
            log('warn', 'DISCOVER_BUILDS_VIA_HEROES: Deadlock GC not ready');
            return { ok: false, error: 'Deadlock GC not connected. Please wait for GC connection.' };
          }

          if (!buildCatalogManager) {
            log('error', 'DISCOVER_BUILDS_VIA_HEROES: BuildCatalogManager not initialized');
            return { ok: false, error: 'BuildCatalogManager not initialized' };
          }

          try {
            const result = await buildCatalogManager.discoverWatchedBuildsViaHeroes();
            return {
              ok: result.success,
              data: {
                heroes_checked: result.heroesChecked,
                heroes_with_builds: result.heroesWithBuilds,
                matched_builds: result.totalMatchedBuilds,
                new_builds: result.totalNewBuilds,
                updated_builds: result.totalUpdatedBuilds,
                errors: result.errors?.length || 0
              }
            };
          } catch (err) {
            log('error', 'DISCOVER_BUILDS_VIA_HEROES: Failed', { error: err.message });
            return { ok: false, error: err.message };
          }
        })();
        isAsync = finalizeTaskRun(task, promise);
        break;
      }

      // ========== CATALOG MAINTENANCE ==========
      // Maintains the catalog: selects builds, creates/updates German clones
      case 'MAINTAIN_BUILD_CATALOG': {
        const promise = (async () => {
          log('info', 'MAINTAIN_BUILD_CATALOG: Starting catalog maintenance');

          if (!buildCatalogManager) {
            log('error', 'MAINTAIN_BUILD_CATALOG: BuildCatalogManager not initialized');
            return { ok: false, error: 'BuildCatalogManager not initialized' };
          }

          try {
            const result = await buildCatalogManager.maintainCatalog();
            return {
              ok: result.success,
              data: {
                builds_to_clone: result.buildsToClone,
                builds_to_update: result.buildsToUpdate,
                tasks_created: result.tasksCreated,
                skipped_builds: result.skippedBuilds
              }
            };
          } catch (err) {
            log('error', 'MAINTAIN_BUILD_CATALOG: Failed', { error: err.message });
            return { ok: false, error: err.message };
          }
        })();
        isAsync = finalizeTaskRun(task, promise);
        break;
      }

      // ========== FULL CATALOG CYCLE ==========
      // Runs discovery + maintenance in one task
      case 'BUILD_CATALOG_CYCLE': {
        const promise = (async () => {
          log('info', 'BUILD_CATALOG_CYCLE: Starting full catalog cycle');

          if (!deadlockGcReady) {
            log('warn', 'BUILD_CATALOG_CYCLE: Deadlock GC not ready');
            return { ok: false, error: 'Deadlock GC not connected. Please wait for GC connection.' };
          }

          if (!buildCatalogManager) {
            log('error', 'BUILD_CATALOG_CYCLE: BuildCatalogManager not initialized');
            return { ok: false, error: 'BuildCatalogManager not initialized' };
          }

          try {
            const result = await buildCatalogManager.runFullCycle();
            return {
              ok: result.success,
              data: {
                discovery: result.discovery,
                maintenance: result.maintenance
              }
            };
          } catch (err) {
            log('error', 'BUILD_CATALOG_CYCLE: Failed', { error: err.message });
            return { ok: false, error: err.message };
          }
        })();
        isAsync = finalizeTaskRun(task, promise);
        break;
      }

      // ========== GC BUILD SEARCH (manual) ==========
      // Search for builds directly via the Deadlock Game Coordinator
      case 'GC_SEARCH_BUILDS': {
        const promise = (async () => {
          log('info', 'GC_SEARCH_BUILDS: Starting In-Game build search');

          if (!deadlockGcReady) {
            log('warn', 'GC_SEARCH_BUILDS: Deadlock GC not ready');
            return { ok: false, error: 'Deadlock GC not connected. Please wait for GC connection.' };
          }

          const searchOptions = {};
          if (payload.author_account_id) searchOptions.authorAccountId = payload.author_account_id;
          if (payload.hero_id) searchOptions.heroId = payload.hero_id;
          if (payload.search_text) searchOptions.searchText = payload.search_text;
          if (payload.hero_build_id) searchOptions.heroBuildId = payload.hero_build_id;
          if (payload.languages) searchOptions.languages = payload.languages;
          if (payload.tags) searchOptions.tags = payload.tags;

          log('info', 'GC_SEARCH_BUILDS: Searching with options', searchOptions);

          try {
            const response = await gcBuildSearch.findBuilds(searchOptions);

            const responseCode = response.response;
            const results = response.results || [];

            log('info', 'GC_SEARCH_BUILDS: Got response', {
              responseCode,
              resultCount: results.length
            });

            if (responseCode !== 1) { // k_eSuccess
              return {
                ok: false,
                error: `GC returned error code: ${responseCode}`,
                responseCode
              };
            }

            // Process and store the builds
            let newBuilds = 0;
            let updatedBuilds = 0;
            const buildSummaries = [];

            for (const result of results) {
              const build = result.heroBuild;
              if (!build) continue;

              const stats = gcBuildSearch.upsertBuild(build, {
                numFavorites: result.numFavorites,
                numWeeklyFavorites: result.numWeeklyFavorites,
                numDailyFavorites: result.numDailyFavorites,
                numIgnores: result.numIgnores,
                numReports: result.numReports,
                source: 'gc_task_search'
              });

              if (stats.inserted) newBuilds++;
              if (stats.updated) updatedBuilds++;

              buildSummaries.push({
                id: build.heroBuildId || build.hero_build_id,
                name: build.name,
                heroId: build.heroId || build.hero_id,
                authorId: build.authorAccountId || build.author_account_id,
                favorites: result.numFavorites,
                weeklyFavorites: result.numWeeklyFavorites
              });
            }

            log('info', 'GC_SEARCH_BUILDS: Completed', {
              totalResults: results.length,
              newBuilds,
              updatedBuilds
            });

            return {
              ok: true,
              data: {
                totalResults: results.length,
                newBuilds,
                updatedBuilds,
                builds: buildSummaries
              }
            };

          } catch (err) {
            log('error', 'GC_SEARCH_BUILDS: Failed', { error: err.message });
            return { ok: false, error: err.message };
          }
        })();
        isAsync = finalizeTaskRun(task, promise);
        break;
      }

      default:
        throw new Error(`Unsupported task type: ${task.type}`);
    }
  } catch (err) {
    log('error', 'Failed to process steam task', { error: err.message });
    if (task && task.id) completeTask(task.id, 'FAILED', { ok:false, error: err.message }, err.message);
  } finally {
    if (!isAsync) taskInProgress = false;
  }
}

setInterval(() => {
  try {
    // Stale RUNNING Tasks aufräumen (z.B. nach Bridge-Crash)
    const now = nowSeconds();
    const staleCutoff = now - STALE_TASK_TIMEOUT_S;
    const staleResult = failStaleTasksStmt.run(STALE_TASK_TIMEOUT_S, now, now, staleCutoff);
    if (staleResult.changes > 0) {
      log('warn', 'Cleaned up stale RUNNING tasks', { count: staleResult.changes, cutoff_age_s: STALE_TASK_TIMEOUT_S });
      taskInProgress = false; // Erlaubt neuen Task nach Cleanup
    }
    processNextTask();
  } catch (err) { log('error', 'Task polling loop failed', { error: err.message }); }
}, Math.max(500, TASK_POLL_INTERVAL_MS));

setInterval(() => {
  syncFriendsAndLinks('interval').catch((err) => {
    log('warn', 'Friend sync loop failed', { error: err && err.message ? err.message : String(err) });
  });
}, FRIEND_SYNC_INTERVAL_MS);

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
  try {
    if (runtimeState.logged_on && deadlockAppActive && !deadlockGcReady) {
      if (gcUnhealthySince === 0) {
        gcUnhealthySince = Date.now();
      } else if (Date.now() - gcUnhealthySince > GC_UNHEALTHY_THRESHOLD_MS) {
        log('error', 'Deadlock GC session has been unhealthy for too long. Suspecting game update required. Restarting service.', {
          unhealthyMs: Date.now() - gcUnhealthySince,
          thresholdMs: GC_UNHEALTHY_THRESHOLD_MS
        });
        shutdown(1);
      }
    } else {
      gcUnhealthySince = 0;
    }
    publishStandaloneState({ reason: 'heartbeat' });
  }
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

  // Sync friends <-> DB shortly after login (wait a bit for friend list to load)
  setTimeout(() => {
    syncFriendsAndLinks('post-login').catch((err) => {
      log('warn', 'Friend sync after login failed', { error: err && err.message ? err.message : String(err) });
    });
  }, 5000);

  scheduleStatePublish({ reason: 'logged_on' });
}

client.on('loggedOn', (details) => {
  deadlockGcBot.refreshGameVersion().catch((err) => {
    log('warn', 'Auto-update of game version failed', { error: err.message });
  });
  markLoggedOn(details);
});
client.on('webSession', () => { log('debug', 'Steam web session established'); });
client.on('steamGuard', (domain, callback, lastCodeWrong) => {
  pendingGuard = { domain, callback };
  const norm = String(domain || '').toLowerCase();

  // Detect guard type based on domain
  // Email: contains "email", "@", or is a domain like "steam.earlysalty.com"
  const isEmail = norm.includes('email') || norm.includes('@') || (norm.includes('.') && !norm.includes('two-factor') && !norm.includes('authenticator') && !norm.includes('mobile') && !norm.includes('device'));

  runtimeState.guard_required = {
    domain: domain || null,
    type: isEmail ? 'email' : (norm.includes('two-factor') || norm.includes('authenticator') || norm.includes('mobile')) ? 'totp' : (norm.includes('device') ? 'device' : 'unknown'),
    last_code_wrong: Boolean(lastCodeWrong),
    requested_at: nowSeconds(),
  };
  runtimeState.logging_in = true;
  log('info', 'Steam Guard challenge received', { domain: domain || null, lastCodeWrong: Boolean(lastCodeWrong) });
  scheduleStatePublish({ reason: 'steam_guard', domain: domain || null, last_code_wrong: Boolean(lastCodeWrong) });
});
client.on('refreshToken', (token) => {
  updateRefreshToken(token);
  const storage = writeToken(REFRESH_TOKEN_PATH, refreshToken, STEAM_VAULT_REFRESH_TOKEN);
  log('info', 'Stored refresh token', { storage });
});
client.on('machineAuthToken', (token) => {
  updateMachineToken(token);
  const storage = writeToken(MACHINE_TOKEN_PATH, machineAuthToken, STEAM_VAULT_MACHINE_TOKEN);
  log('info', 'Stored machine auth token', { storage });
});
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
  gcProfileCard.flushPending(new Error('Deadlock app quit'));
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

    if (Number(relationship) === Number(EFriendRelationship.RequestRecipient)) {
      log('info', 'Accepting incoming friend request', { steam_id64: sid64 });
      try {
        client.addFriend(steamId, (err) => {
          if (err) log('warn', 'Failed to accept incoming friend request', { steam_id64: sid64, error: err.message });
          else log('info', 'Successfully accepted friend request', { steam_id64: sid64 });
        });
      } catch (err) {
        log('error', 'Error in addFriend for incoming request', { steam_id64: sid64, error: err.message });
      }
    } else if (Number(relationship) === Number(EFriendRelationship.Friend)) {

      log('info', 'New friend confirmed, checking steam_links', { steam_id64: sid64 });

  

      const cachedName = resolveCachedPersonaName(sid64);

      if (verifySteamLink(sid64, cachedName)) {

        log('info', 'Verified existing steam_link', {

          steam_id64: sid64,

          name: cachedName || undefined,

        });

      }

  

      // Fetch name asynchronously if it was not available in cache yet

      if (!cachedName) {

        fetchPersonaNames([sid64]).then((map) => {

          const normalized = normalizeSteamId64(sid64);

          const fetchedName = normalized ? map.get(normalized) : null;

          if (fetchedName) verifySteamLink(sid64, fetchedName);

        }).catch((err) => {

          log('debug', 'Failed to fetch persona for new friend', {

            steam_id64: sid64,

            error: err && err.message ? err.message : String(err),

          });

        });

      }

    } else if (Number(relationship) === Number(EFriendRelationship.None)) {

      // Unfriended -> Unverify

      log('info', 'Friendship ended, unverifying in steam_links', { steam_id64: sid64 });

      try {

          const info = unverifySteamLinkStmt.run(sid64);

          if (info.changes > 0) {

              log('info', 'Unverified steam link', { steam_id64: sid64, changes: info.changes });

          }

      } catch (err) {

          log('error', 'Failed to unverify steam link', { steam_id64: sid64, error: err.message });

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

  // Handle GC Build Search responses
  if (messageId === GC_MSG_FIND_HERO_BUILDS_RESPONSE && isDeadlockApp) {
    if (gcBuildSearch.handleGcMessage(appId, msgType, payload)) {
      return;
    }
  }

  // Fallback path: some GC responses may not be routed via steam-user job callbacks.
  if (isDeadlockApp && gcProfileCard.handleGcMessage(appId, msgType, payload)) {
    return;
  }

  if ((messageId === GC_MSG_CLIENT_WELCOME || messageId === 9019) && isDeadlockApp) {
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
  gcProfileCard.flushPending(new Error('Steam disconnected'));
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
    if (refreshToken) {
      log('warn', 'Clearing refresh token after authentication failure');
      updateRefreshToken('');
      writeToken(REFRESH_TOKEN_PATH, '', STEAM_VAULT_REFRESH_TOKEN);
    }
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

// ---------- Build Catalog Maintenance ----------
// Periodically maintain the build catalog (discovery + cloning + updates)
const CATALOG_MAINTENANCE_INTERVAL_MS = parseInt(process.env.CATALOG_MAINTENANCE_INTERVAL_MS || '600000', 10); // Default: 10 minutes

function scheduleCatalogMaintenance() {
  try {
    // Check if there's already a pending or running MAINTAIN_BUILD_CATALOG task
    const existing = db.prepare(`
      SELECT id FROM steam_tasks
      WHERE type = 'MAINTAIN_BUILD_CATALOG'
        AND status IN ('PENDING', 'RUNNING')
      LIMIT 1
    `).get();

    if (!existing) {
      const now = Math.floor(Date.now() / 1000);
      db.prepare(`
        INSERT INTO steam_tasks (type, payload, status, created_at, updated_at)
        VALUES ('MAINTAIN_BUILD_CATALOG', '{}', 'PENDING', ?, ?)
      `).run(now, now);
      log('info', 'Scheduled MAINTAIN_BUILD_CATALOG task');
    } else {
      log('debug', 'MAINTAIN_BUILD_CATALOG task already scheduled', { task_id: existing.id });
    }
  } catch (err) {
    log('error', 'Failed to schedule catalog maintenance', { error: err.message });
  }
}

// Schedule immediately on startup (after a short delay to let system stabilize)
setTimeout(() => {
  log('info', 'Running initial catalog maintenance');
  scheduleCatalogMaintenance();
}, 30000); // 30 seconds after startup

// Schedule catalog maintenance
setInterval(() => {
  scheduleCatalogMaintenance();
}, CATALOG_MAINTENANCE_INTERVAL_MS);

// Periodically refresh game version (every 30 minutes)
setInterval(() => {
  if (runtimeState.logged_on) {
    log('debug', 'Running periodic game version check');
    deadlockGcBot.refreshGameVersion().catch(err => {
      log('warn', 'Periodic game version check failed', { error: err.message });
    });
  }
}, 30 * 60 * 1000);

function shutdown(code = 0) {
  try {
    log('info', 'Shutting down Steam bridge');
    statusAnzeige.stop();
    flushPendingPlaytestInvites(new Error('Service shutting down'));
    flushDeadlockGcWaiters(new Error('Service shutting down'));
    gcProfileCard.flushPending(new Error('Service shutting down'));
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
process.on('uncaughtException', (err) => {
  // Ignore EPIPE errors (broken pipe when parent process closes stdout)
  if (err && err.code === 'EPIPE') return;
  log('error', 'Uncaught exception', { error: err && err.stack ? err.stack : err });
  shutdown(1);
});
process.on('unhandledRejection', (err) => { log('error', 'Unhandled rejection', { error: err && err.stack ? err.stack : err }); });
