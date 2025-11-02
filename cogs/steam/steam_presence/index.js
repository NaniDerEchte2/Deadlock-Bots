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
const SteamUser = require('steam-user');
const Database = require('better-sqlite3');
const { QuickInvites } = require('./quick_invites');
const { DeadlockPresenceLogger } = require('./deadlock_presence_logger');

const SteamID = SteamUser.SteamID;

const DEADLOCK_APP_ID = Number.parseInt(process.env.DEADLOCK_APPID || '1422450', 10);
const PROTO_MASK = SteamUser.GCMsgProtoBuf || 0x80000000;
const GC_MSG_CLIENT_HELLO = 4004;
const GC_MSG_CLIENT_WELCOME = 4005;
const GC_MSG_SUBMIT_PLAYTEST_USER = 9189;
const GC_MSG_SUBMIT_PLAYTEST_USER_RESPONSE = 9190;

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

const nowSeconds = () => Math.floor(Date.now() / 1000);

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
function truncateError(message, limit = 1500) {
  if (!message) return null;
  const text = String(message);
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 3)}...`;
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

const dbPath = resolveDbPath();
ensureDir(path.dirname(dbPath));
log('info', 'Using SQLite database', { dbPath });
const db = new Database(dbPath);
db.pragma('journal_mode = WAL');
db.pragma('synchronous = NORMAL');
db.pragma('busy_timeout = 5000');

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
const finishTaskStmt = db.prepare(`
  UPDATE steam_tasks
     SET status = ?,
         result = ?,
         error = ?,
         finished_at = ?,
         updated_at = ?
   WHERE id = ?
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

// ---------- Steam Client ----------
const client = new SteamUser();
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

const presenceLogger = new DeadlockPresenceLogger(client, log, {
  appId: DEADLOCK_APP_ID,
  language: process.env.STEAM_PRESENCE_LANGUAGE || 'german',
  csvPath: path.join(DATA_DIR, 'deadlock_presence_log.csv'),
});
log('info', 'Presence logger configured', { csvPath: presenceLogger.csvPath });
presenceLogger.start();

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
  if (!force && now - deadlockGameRequestedAt < 15000) return;
  try {
    client.gamesPlayed([DEADLOCK_APP_ID]);
    deadlockGameRequestedAt = now;
    log('debug', 'Requested Deadlock GC session via gamesPlayed()', { appId: DEADLOCK_APP_ID });
  } catch (err) {
    log('warn', 'Failed to call gamesPlayed for Deadlock', { error: err.message });
  }
}

function sendDeadlockGcHello(force = false) {
  if (!deadlockAppActive) return false;
  const now = Date.now();
  if (!force && now - lastGcHelloAttemptAt < 2000) return false;
  try {
    client.sendToGC(DEADLOCK_APP_ID, PROTO_MASK + GC_MSG_CLIENT_HELLO, {}, Buffer.alloc(0));
    lastGcHelloAttemptAt = now;
    log('debug', 'Sent Deadlock GC hello');
    return true;
  } catch (err) {
    log('warn', 'Failed to send Deadlock GC hello', { error: err.message });
    return false;
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
  while (deadlockGcWaiters.length) {
    const waiter = deadlockGcWaiters.shift();
    try {
      if (waiter) waiter.resolve(true);
    } catch (_) {}
  }
}

function waitForDeadlockGcReady(timeoutMs = 10000) {
  ensureDeadlockGamePlaying();
  if (deadlockGcReady) return Promise.resolve(true);

  const timeout = Math.max(1000, Number.isFinite(timeoutMs) ? Number(timeoutMs) : 10000);

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

    entry.timer = setTimeout(() => entry.reject(new Error('Timeout waiting for Deadlock GC')), timeout);
    entry.interval = setInterval(() => {
      ensureDeadlockGamePlaying();
      sendDeadlockGcHello(false);
    }, 2000);

    deadlockGcWaiters.push(entry);
    sendDeadlockGcHello(true);
  });
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

function encodeVarint(value) {
  let v = Number(value >>> 0);
  const bytes = [];
  while (v >= 0x80) {
    bytes.push((v & 0x7f) | 0x80);
    v >>>= 7;
  }
  bytes.push(v);
  return Buffer.from(bytes);
}

function decodeVarint(buffer, offset = 0) {
  let result = 0;
  let shift = 0;
  let position = offset;
  while (position < buffer.length) {
    const byte = buffer[position++];
    result |= (byte & 0x7f) << shift;
    if ((byte & 0x80) === 0) {
      return { value: result >>> 0, nextOffset: position };
    }
    shift += 7;
    if (shift > 35) break;
  }
  throw new Error('Truncated varint');
}

function skipField(buffer, offset, wireType) {
  switch (wireType) {
    case 0: {
      const { nextOffset } = decodeVarint(buffer, offset);
      return nextOffset;
    }
    case 1:
      return offset + 8;
    case 2: {
      const { value: length, nextOffset } = decodeVarint(buffer, offset);
      return nextOffset + length;
    }
    case 5:
      return offset + 4;
    default:
      return -1;
  }
}

function encodeSubmitPlaytestUserPayload(accountId, location) {
  const parts = [];
  if (location) {
    const locStr = Buffer.from(String(location), 'utf8');
    parts.push(encodeVarint((3 << 3) | 2));
    parts.push(encodeVarint(locStr.length));
    parts.push(locStr);
  }
  if (Number.isFinite(accountId)) {
    parts.push(encodeVarint((4 << 3) | 0));
    parts.push(encodeVarint(Number(accountId) >>> 0));
  }
  return parts.length ? Buffer.concat(parts) : Buffer.alloc(0);
}

function decodeSubmitPlaytestUserResponse(buffer) {
  if (!buffer || !buffer.length) return null;
  let offset = 0;
  while (offset < buffer.length) {
    let tag;
    try {
      ({ value: tag, nextOffset: offset } = decodeVarint(buffer, offset));
    } catch (err) {
      log('warn', 'Failed to decode playtest response varint', { error: err.message });
      return null;
    }
    const fieldNumber = tag >>> 3;
    const wireType = tag & 0x07;

    if (fieldNumber === 1 && wireType === 0) {
      try {
        const { value } = decodeVarint(buffer, offset);
        return value >>> 0;
      } catch (err) {
        log('warn', 'Failed to decode playtest response code', { error: err.message });
        return null;
      }
    }

    const next = skipField(buffer, offset, wireType);
    if (next < 0 || next > buffer.length) break;
    offset = next;
  }
  return null;
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

async function sendPlaytestInvite(accountId, location, timeoutMs = 10000) {
  await waitForDeadlockGcReady(timeoutMs);

  return new Promise((resolve, reject) => {
    const payload = encodeSubmitPlaytestUserPayload(accountId, location);
    const entry = {
      resolve: null,
      reject: null,
      timer: null,
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

    entry.timer = setTimeout(() => entry.reject(new Error('Timeout beim Warten auf GC-Antwort')), Math.max(3000, Number(timeoutMs) || 10000));

    pendingPlaytestInviteResponses.push(entry);

    try {
      client.sendToGC(DEADLOCK_APP_ID, PROTO_MASK + GC_MSG_SUBMIT_PLAYTEST_USER, {}, payload);
      log('info', 'Deadlock playtest invite requested', { accountId, location });
    } catch (err) {
      entry.reject(err);
    }
  });
}

function handlePlaytestInviteResponse(buffer) {
  if (!pendingPlaytestInviteResponses.length) {
    log('warn', 'Received unexpected playtest invite response');
    return;
  }

  const entry = pendingPlaytestInviteResponses.shift();
  if (entry && entry.timer) clearTimeout(entry.timer);

  const code = decodeSubmitPlaytestUserResponse(buffer);
  const mapping = Object.prototype.hasOwnProperty.call(PLAYTEST_RESPONSE_MAP, code || 0)
    ? PLAYTEST_RESPONSE_MAP[code || 0]
    : { key: 'unknown', message: 'Unbekannte Antwort des Game Coordinators.' };

  const response = {
    success: code === 0,
    code: code === null ? null : Number(code),
    key: mapping.key,
    message: mapping.message,
  };

  if (entry && entry.resolve) {
    try {
      entry.resolve(response);
    } catch (err) {
      log('warn', 'Failed to resolve playtest invite promise', { error: err.message });
    }
  }
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
          const relationshipRaw = client.myFriends ? client.myFriends[sid64] : undefined;
          const isFriend = Number(relationshipRaw) === Number((SteamUser.EFriendRelationship || {}).Friend);
          return {
            ok: true,
            data: {
              steam_id64: sid64,
              account_id: sid.accountid ?? null,
              friend: isFriend,
              relationship: relationshipRaw ?? null,
              relationship_name: relationshipName(relationshipRaw),
            },
          };
        })();
        finalizeTaskRun(task, promise);
        break;
      }

      case 'AUTH_SEND_PLAYTEST_INVITE': {
        const promise = (async () => {
          if (!runtimeState.logged_on) throw new Error('Not logged in');
          const raw = payload?.steam_id ?? payload?.steam_id64;
          const timeoutMs = payload?.timeout_ms ?? payload?.response_timeout_ms;
          const sid = raw ? parseSteamID(raw) : null;
          const accountId = payload?.account_id != null ? Number(payload.account_id) : (sid ? sid.accountid : null);
          if (!Number.isFinite(accountId) || accountId <= 0) throw new Error('account_id missing or invalid');
          const locationRaw = typeof payload?.location === 'string' ? payload.location.trim() : '';
          const location = locationRaw || 'discord-betainvite';
          const response = await sendPlaytestInvite(Number(accountId), location, Number(timeoutMs) || 10000);
          const sid64 = sid && typeof sid.getSteamID64 === 'function' ? sid.getSteamID64() : (sid ? String(sid) : null);
          return {
            ok: Boolean(response && response.success),
            data: {
              steam_id64: sid64,
              account_id: Number(accountId),
              location,
              response,
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
    client.setPersona(SteamUser.EPersonaState.Online);
  } catch (err) {
    log('warn', 'Failed to set persona online', { error: err.message });
  }
  ensureDeadlockGamePlaying(true);

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
client.on('appLaunched', (appId) => {
  if (Number(appId) !== Number(DEADLOCK_APP_ID)) return;
  deadlockAppActive = true;
  deadlockGcReady = false;
  lastGcHelloAttemptAt = 0;
  log('info', 'Deadlock app launched – GC session starting');
  sendDeadlockGcHello(true);
});
client.on('appQuit', (appId) => {
  if (Number(appId) !== Number(DEADLOCK_APP_ID)) return;
  deadlockAppActive = false;
  deadlockGcReady = false;
  flushDeadlockGcWaiters(new Error('Deadlock app quit'));
  flushPendingPlaytestInvites(new Error('Deadlock app quit'));
  log('info', 'Deadlock app quit – GC session ended');
});
client.on('receivedFromGC', (appid, msgType, payload) => {
  if (Number(appid) !== Number(DEADLOCK_APP_ID)) return;
  const baseMsg = msgType & ~PROTO_MASK;
  if (baseMsg === GC_MSG_CLIENT_WELCOME) {
    log('info', 'Received Deadlock GC welcome');
    notifyDeadlockGcReady();
    return;
  }
  if (baseMsg === GC_MSG_SUBMIT_PLAYTEST_USER_RESPONSE) {
    handlePlaytestInviteResponse(payload);
    return;
  }
  log('debug', 'Received GC message', { msgType, baseMsg });
});
client.on('disconnected', (eresult, msg) => {
  runtimeState.logged_on = false;
  runtimeState.logging_in = false;
  loginInProgress = false;
  runtimeState.last_disconnect_at = nowSeconds();
  runtimeState.last_disconnect_eresult = eresult;
  deadlockAppActive = false;
  deadlockGcReady = false;
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
  scheduleStatePublish({ reason: 'auto_login', started: result && result.started });
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
    presenceLogger.stop();
    flushPendingPlaytestInvites(new Error('Service shutting down'));
    flushDeadlockGcWaiters(new Error('Service shutting down'));
    client.logOff();
  } catch {}
  try { db.close(); } catch {}
  process.exit(code);
}
process.on('SIGINT', () => shutdown(0));
process.on('SIGTERM', () => shutdown(0));
process.on('uncaughtException', (err) => { log('error', 'Uncaught exception', { error: err && err.stack ? err.stack : err }); shutdown(1); });
process.on('unhandledRejection', (err) => { log('error', 'Unhandled rejection', { error: err && err.stack ? err.stack : err }); });
