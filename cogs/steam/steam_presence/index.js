#!/usr/bin/env node
'use strict';

/**
 * Steam bridge focused on authentication and task execution.
 *
 * Responsibilities:
 *   - Manage Steam login using refresh tokens or explicit login tasks.
 *   - Persist refresh- & machine-auth tokens on disk.
 *   - Poll the shared SQLite database for tasks and execute them.
 *   - Keep the connection alive (reconnect only when a refresh token exists).
 *
 * Non-goals:
 *   - Rich presence handling, friend management, snapshots, etc.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const SteamUser = require('steam-user');
const Database = require('better-sqlite3');

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

function resolveDbPath() {
  if (process.env.DEADLOCK_DB_PATH) {
    return path.resolve(process.env.DEADLOCK_DB_PATH);
  }
  const baseDir = process.env.DEADLOCK_DB_DIR
    ? path.resolve(process.env.DEADLOCK_DB_DIR)
    : path.join(os.homedir(), 'Documents', 'Deadlock', 'service');
  return path.join(baseDir, 'deadlock.sqlite3');
}

function ensureDir(dirPath) {
  try {
    fs.mkdirSync(dirPath, { recursive: true });
  } catch (err) {
    if (err && err.code !== 'EEXIST') throw err;
  }
}

function readToken(filePath) {
  try {
    if (!fs.existsSync(filePath)) return '';
    return fs.readFileSync(filePath, 'utf8').trim();
  } catch (err) {
    log('warn', 'Failed to read token file', { path: filePath, error: err.message });
    return '';
  }
}

function writeToken(filePath, value) {
  try {
    if (!value) {
      fs.rmSync(filePath, { force: true });
      return;
    }
    fs.writeFileSync(filePath, `${value}\n`, 'utf8');
  } catch (err) {
    log('warn', 'Failed to persist token', { path: filePath, error: err.message });
  }
}

function safeJsonStringify(value) {
  try {
    return JSON.stringify(value);
  } catch (err) {
    log('warn', 'Failed to stringify JSON', { error: err.message });
    return null;
  }
}

function safeJsonParse(value) {
  if (!value) return {};
  try {
    return JSON.parse(value);
  } catch (err) {
    throw new Error(`Invalid JSON payload: ${err.message}`);
  }
}

const DATA_DIR = path.resolve(
  process.env.STEAM_PRESENCE_DATA_DIR || path.join(__dirname, '.steam-data'),
);
ensureDir(DATA_DIR);
const REFRESH_TOKEN_PATH = path.join(DATA_DIR, 'refresh.token');
const MACHINE_TOKEN_PATH = path.join(DATA_DIR, 'machine_auth_token.txt');

const ACCOUNT_NAME =
  process.env.STEAM_BOT_USERNAME ||
  process.env.STEAM_LOGIN ||
  process.env.STEAM_ACCOUNT ||
  '';
const ACCOUNT_PASSWORD = process.env.STEAM_BOT_PASSWORD || process.env.STEAM_PASSWORD || '';

const TASK_POLL_INTERVAL_MS = parseInt(process.env.STEAM_TASK_POLL_MS || '2000', 10);
const RECONNECT_DELAY_MS = parseInt(process.env.STEAM_RECONNECT_DELAY_MS || '5000', 10);

const dbPath = resolveDbPath();
ensureDir(path.dirname(dbPath));
log('info', 'Using SQLite database', { dbPath });
const db = new Database(dbPath);
db.pragma('journal_mode = WAL');
db.pragma('synchronous = NORMAL');
db.pragma('busy_timeout = 5000');

db.prepare(
  `CREATE TABLE IF NOT EXISTS steam_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    result TEXT,
    error TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    started_at INTEGER,
    finished_at INTEGER
  )`,
).run();

db.prepare(
  `CREATE INDEX IF NOT EXISTS idx_steam_tasks_status ON steam_tasks(status, id)`,
).run();

db.prepare(
  `CREATE INDEX IF NOT EXISTS idx_steam_tasks_updated ON steam_tasks(updated_at)`,
).run();

const selectPendingTaskStmt = db.prepare(
  `SELECT id, type, payload FROM steam_tasks
   WHERE status = 'PENDING'
   ORDER BY id ASC
   LIMIT 1`,
);

const markTaskRunningStmt = db.prepare(
  `UPDATE steam_tasks
     SET status = 'RUNNING',
         started_at = ?,
         updated_at = ?
   WHERE id = ? AND status = 'PENDING'`,
);

const finishTaskStmt = db.prepare(
  `UPDATE steam_tasks
      SET status = ?,
          result = ?,
          error = ?,
          finished_at = ?,
          updated_at = ?
    WHERE id = ?`,
);

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

const client = new SteamUser();
client.setOption('autoRelogin', false);
client.setOption('machineName', process.env.STEAM_MACHINE_NAME || 'DeadlockBridge');

function updateRefreshToken(token) {
  refreshToken = token ? String(token).trim() : '';
  runtimeState.refresh_token_present = Boolean(refreshToken);
}

function updateMachineToken(token) {
  machineAuthToken = token ? String(token).trim() : '';
  runtimeState.machine_token_present = Boolean(machineAuthToken);
}

function clearReconnectTimer() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

function scheduleReconnect(reason, delayMs = RECONNECT_DELAY_MS) {
  if (!refreshToken) return;
  if (manualLogout) return;
  if (runtimeState.logged_on) return;
  if (loginInProgress) return;
  if (reconnectTimer) return;

  const delay = Math.max(1000, Number.isFinite(delayMs) ? delayMs : RECONNECT_DELAY_MS);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    try {
      const result = initiateLogin('auto-reconnect', {});
      log('info', 'Auto reconnect attempt', { reason, result });
    } catch (err) {
      log('warn', 'Auto reconnect failed to start', { error: err.message, reason });
    }
  }, delay);
}

function guardTypeFromDomain(domain) {
  const norm = String(domain || '').toLowerCase();
  if (norm.includes('email')) return 'email';
  if (norm.includes('two-factor') || norm.includes('authenticator') || norm.includes('mobile')) return 'totp';
  if (norm.includes('device')) return 'device';
  return norm || 'unknown';
}

function buildLoginOptions(overrides = {}) {
  if (overrides.refreshToken) {
    return { refreshToken: overrides.refreshToken };
  }

  if (refreshToken && !overrides.forceAccountCredentials) {
    return { refreshToken };
  }

  const accountName =
    overrides.accountName !== undefined && overrides.accountName !== null
      ? String(overrides.accountName)
      : ACCOUNT_NAME;
  const password =
    overrides.password !== undefined && overrides.password !== null
      ? String(overrides.password)
      : ACCOUNT_PASSWORD;

  if (!accountName) {
    throw new Error('Missing Steam account name');
  }
  if (!password) {
    throw new Error('Missing Steam account password');
  }

  const options = { accountName, password };

  if (overrides.twoFactorCode) {
    options.twoFactorCode = String(overrides.twoFactorCode);
  }
  if (overrides.authCode) {
    options.authCode = String(overrides.authCode);
  }
  if (Object.prototype.hasOwnProperty.call(overrides, 'rememberPassword')) {
    options.rememberPassword = Boolean(overrides.rememberPassword);
  }
  if (overrides.machineAuthToken) {
    options.machineAuthToken = String(overrides.machineAuthToken);
  } else if (machineAuthToken) {
    options.machineAuthToken = machineAuthToken;
  }

  return options;
}

function initiateLogin(source, payload) {
  if (client.steamID && client.steamID.isValid()) {
    const steamId64 = typeof client.steamID.getSteamID64 === 'function'
      ? client.steamID.getSteamID64()
      : String(client.steamID);
    return { started: false, reason: 'already_logged_on', steam_id64: steamId64 };
  }

  if (loginInProgress) {
    return { started: false, reason: 'login_in_progress' };
  }

  const overrides = {};
  if (payload) {
    if (Object.prototype.hasOwnProperty.call(payload, 'use_refresh_token') && !payload.use_refresh_token) {
      overrides.forceAccountCredentials = true;
    }
    if (Object.prototype.hasOwnProperty.call(payload, 'force_credentials') && payload.force_credentials) {
      overrides.forceAccountCredentials = true;
    }
    if (payload.account_name) overrides.accountName = payload.account_name;
    if (payload.password) overrides.password = payload.password;
    if (payload.refresh_token) overrides.refreshToken = payload.refresh_token;
    if (payload.two_factor_code) overrides.twoFactorCode = payload.two_factor_code;
    if (payload.auth_code) overrides.authCode = payload.auth_code;
    if (Object.prototype.hasOwnProperty.call(payload, 'remember_password')) {
      overrides.rememberPassword = Boolean(payload.remember_password);
    }
    if (payload.machine_auth_token) overrides.machineAuthToken = payload.machine_auth_token;
  }

  const options = buildLoginOptions(overrides);

  if (options.accountName) {
    runtimeState.account_name = options.accountName;
  }

  loginInProgress = true;
  runtimeState.logging_in = true;
  runtimeState.last_login_attempt_at = nowSeconds();
  runtimeState.last_login_source = source;
  runtimeState.last_error = null;
  pendingGuard = null;
  runtimeState.guard_required = null;
  manualLogout = false;
  clearReconnectTimer();

  log('info', 'Initiating Steam login', {
    using_refresh_token: Boolean(options.refreshToken),
    source,
  });

  try {
    client.logOn(options);
  } catch (err) {
    loginInProgress = false;
    runtimeState.logging_in = false;
    runtimeState.last_error = { message: err.message };
    throw err;
  }

  return {
    started: true,
    using_refresh_token: Boolean(options.refreshToken),
    source,
  };
}

function handleGuardCodeTask(payload) {
  if (!pendingGuard || !pendingGuard.callback) {
    throw new Error('No Steam Guard challenge is pending');
  }
  const code = payload && payload.code ? String(payload.code).trim() : '';
  if (!code) {
    throw new Error('Steam Guard code is required');
  }

  const callback = pendingGuard.callback;
  const domain = pendingGuard.domain;
  pendingGuard = null;
  runtimeState.guard_required = null;
  runtimeState.last_guard_submission_at = nowSeconds();

  try {
    callback(code);
    log('info', 'Submitted Steam Guard code', { domain: domain || null });
  } catch (err) {
    throw new Error(`Failed to submit guard code: ${err.message}`);
  }

  return {
    accepted: true,
    domain: domain || null,
    type: guardTypeFromDomain(domain),
  };
}

function handleLogoutTask() {
  manualLogout = true;
  clearReconnectTimer();
  runtimeState.logging_in = false;
  loginInProgress = false;
  pendingGuard = null;
  runtimeState.guard_required = null;
  runtimeState.last_error = null;

  try {
    client.logOff();
  } catch (err) {
    log('warn', 'logOff failed', { error: err.message });
  }

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

function completeTask(id, status, result = undefined, error = undefined) {
  const finishedAt = nowSeconds();
  const resultJson = result === undefined ? null : safeJsonStringify(result);
  const errorText = error ? String(error) : null;

  finishTaskStmt.run(status, resultJson, errorText, finishedAt, finishedAt, id);
}

let taskInProgress = false;

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
    let result;

    log('info', 'Executing steam task', { id: task.id, type: task.type });

    switch (task.type) {
      case 'AUTH_STATUS':
        result = getStatusPayload();
        completeTask(task.id, 'DONE', result, null);
        break;
      case 'AUTH_LOGIN':
        result = initiateLogin('task', payload);
        completeTask(task.id, 'DONE', result, null);
        break;
      case 'AUTH_GUARD_CODE':
        result = handleGuardCodeTask(payload);
        completeTask(task.id, 'DONE', result, null);
        break;
      case 'AUTH_LOGOUT':
        result = handleLogoutTask();
        completeTask(task.id, 'DONE', result, null);
        break;
      default:
        throw new Error(`Unsupported task type: ${task.type}`);
    }
  } catch (err) {
    log('error', 'Failed to process steam task', { error: err.message });
    if (task && task.id) {
      completeTask(task.id, 'FAILED', null, err.message);
    } else if (typeof err.taskId === 'number') {
      completeTask(err.taskId, 'FAILED', null, err.message);
    }
  } finally {
    taskInProgress = false;
  }
}

setInterval(() => {
  try {
    processNextTask();
  } catch (err) {
    log('error', 'Task polling loop failed', { error: err.message });
  }
}, Math.max(500, TASK_POLL_INTERVAL_MS));

function markLoggedOn(details) {
  runtimeState.logged_on = true;
  runtimeState.logging_in = false;
  loginInProgress = false;
  runtimeState.guard_required = null;
  pendingGuard = null;
  runtimeState.last_logged_on_at = nowSeconds();
  runtimeState.last_error = null;

  if (client.steamID && typeof client.steamID.getSteamID64 === 'function') {
    runtimeState.steam_id64 = client.steamID.getSteamID64();
  } else if (client.steamID) {
    runtimeState.steam_id64 = String(client.steamID);
  } else {
    runtimeState.steam_id64 = null;
  }

  log('info', 'Steam login successful', {
    country: details ? details.publicIPCountry : undefined,
    cellId: details ? details.cellID : undefined,
    steam_id64: runtimeState.steam_id64,
  });
}

client.on('loggedOn', (details) => {
  markLoggedOn(details);
});

client.on('webSession', () => {
  log('debug', 'Steam web session established');
});

client.on('steamGuard', (domain, callback, lastCodeWrong) => {
  pendingGuard = { domain, callback };
  runtimeState.guard_required = {
    domain: domain || null,
    type: guardTypeFromDomain(domain),
    last_code_wrong: Boolean(lastCodeWrong),
    requested_at: nowSeconds(),
  };
  runtimeState.logging_in = true;
  log('info', 'Steam Guard challenge received', {
    domain: domain || null,
    lastCodeWrong: Boolean(lastCodeWrong),
  });
});

client.on('refreshToken', (token) => {
  updateRefreshToken(token);
  writeToken(REFRESH_TOKEN_PATH, refreshToken);
  log('info', 'Stored refresh token', { path: REFRESH_TOKEN_PATH });
});

client.on('machineAuthToken', (token) => {
  updateMachineToken(token);
  writeToken(MACHINE_TOKEN_PATH, machineAuthToken);
  log('info', 'Stored machine auth token', { path: MACHINE_TOKEN_PATH });
});

client.on('disconnected', (eresult, msg) => {
  runtimeState.logged_on = false;
  runtimeState.logging_in = false;
  loginInProgress = false;
  runtimeState.last_disconnect_at = nowSeconds();
  runtimeState.last_disconnect_eresult = eresult;
  log('warn', 'Steam disconnected', { eresult, msg });
  scheduleReconnect('disconnect');
});

client.on('error', (err) => {
  runtimeState.last_error = {
    message: err && err.message ? err.message : String(err),
    eresult: err && typeof err.eresult === 'number' ? err.eresult : undefined,
  };
  runtimeState.logging_in = false;
  loginInProgress = false;

  const text = String(err && err.message ? err.message : '').toLowerCase();
  log('error', 'Steam client error', { error: runtimeState.last_error.message, eresult: runtimeState.last_error.eresult });

  if (text.includes('invalid refresh') || text.includes('expired') || text.includes('refresh token')) {
    if (refreshToken) {
      log('warn', 'Clearing refresh token after authentication failure');
      updateRefreshToken('');
      writeToken(REFRESH_TOKEN_PATH, '');
    }
    return;
  }

  if (text.includes('ratelimit') || text.includes('rate limit') || text.includes('throttle')) {
    log('warn', 'Rate limit encountered; waiting for explicit login task');
    return;
  }

  scheduleReconnect('error');
});

client.on('sessionExpired', () => {
  log('warn', 'Steam session expired');
  runtimeState.logged_on = false;
  scheduleReconnect('session-expired');
});

function autoLoginIfPossible() {
  if (!refreshToken) {
    log('info', 'Auto-login disabled (no refresh token). Waiting for tasks.');
    return;
  }

  const result = initiateLogin('auto-start', {});
  log('info', 'Auto-login kick-off', result);
}

autoLoginIfPossible();

function shutdown(code = 0) {
  try {
    log('info', 'Shutting down Steam bridge');
    clearReconnectTimer();
    client.logOff();
  } catch {}
  try {
    db.close();
  } catch {}
  process.exit(code);
}

process.on('SIGINT', () => shutdown(0));
process.on('SIGTERM', () => shutdown(0));
process.on('uncaughtException', (err) => {
  log('error', 'Uncaught exception', { error: err && err.stack ? err.stack : err });
  shutdown(1);
});
process.on('unhandledRejection', (err) => {
  log('error', 'Unhandled rejection', { error: err && err.stack ? err.stack : err });
});
