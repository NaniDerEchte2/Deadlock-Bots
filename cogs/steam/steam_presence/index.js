#!/usr/bin/env node
/**
 * Steam Rich Presence bridge for Deadlock bots.
 * - Warten auf Mobile-Approval (domain === 'device') mit Watchdog/Timeout
 * - Optional: Code via stdin (!sg) — auch Umschalten von Device→Code
 * - Persistenter loginKey (Datei) -> spätere Logins ohne Guard
 * - Single-Login-Schutz, Backoff bei Disconnect/Fehler
 */

const fs = require('fs');
const path = require('path');
const os = require('os');
const readline = require('readline');
const SteamUser = require('steam-user');
const SteamTotp = require('steam-totp');
const Database = require('better-sqlite3');

// ---- SteamID ctor (prefer 'steamid') ----
let SteamIDCtor = null;
try { SteamIDCtor = require('steamid'); } catch { SteamIDCtor = SteamUser.SteamID; }

// ---- Config / Logging ----
const APP_ID = parseInt(process.env.DEADLOCK_APP_ID || '1422450', 10);
const WATCH_REFRESH_MS = parseInt(process.env.RP_WATCH_REFRESH_SEC || '30', 10) * 1000;
const POLL_INTERVAL_MS = parseInt(process.env.RP_POLL_INTERVAL_MS || '15000', 10);

const DEVICE_WAIT_TIMEOUT_MS = (parseInt(process.env.STEAM_DEVICE_WAIT_TIMEOUT_SEC || '300', 10) || 0) * 1000; // 0 = kein Timeout
const STATUS_LOG_EVERY_MS = parseInt(process.env.STEAM_DEVICE_STATUS_LOG_EVERY_MS || '15000', 10);

// Logging
const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
const LOG_LEVEL = (process.env.LOG_LEVEL || 'info').toLowerCase();
const LOG_THRESHOLD = Object.prototype.hasOwnProperty.call(LOG_LEVELS, LOG_LEVEL) ? LOG_LEVELS[LOG_LEVEL] : LOG_LEVELS.info;

function log(level, message, extra = undefined) {
  const lvl = LOG_LEVELS[level];
  if (lvl === undefined || lvl > LOG_THRESHOLD) return;
  const payload = { time: new Date().toISOString(), level, msg: message };
  if (extra && typeof extra === 'object') for (const [k, v] of Object.entries(extra)) if (v !== undefined) payload[k] = v;
  console.log(JSON.stringify(payload));
}

function resolveDbPath() {
  if (process.env.DEADLOCK_DB_PATH) return path.resolve(process.env.DEADLOCK_DB_PATH);
  const baseDir = process.env.DEADLOCK_DB_DIR ? path.resolve(process.env.DEADLOCK_DB_DIR) : path.join(os.homedir(), 'Documents', 'Deadlock', 'service');
  return path.join(baseDir, 'deadlock.sqlite3');
}

// ---- DB init ----
const dbPath = resolveDbPath();
log('info', 'Using SQLite database', { dbPath });
const db = new Database(dbPath);
db.pragma('journal_mode = WAL');
db.pragma('synchronous = NORMAL');

db.prepare(`
  CREATE TABLE IF NOT EXISTS steam_rich_presence (
    steam_id TEXT PRIMARY KEY,
    app_id INTEGER,
    status TEXT,
    display TEXT,
    player_group TEXT,
    player_group_size INTEGER,
    connect TEXT,
    raw_json TEXT,
    last_update INTEGER
  )
`).run();

db.prepare(`
  CREATE TABLE IF NOT EXISTS steam_presence_watchlist (
    steam_id TEXT PRIMARY KEY,
    note TEXT,
    added_at INTEGER DEFAULT (strftime('%s','now'))
  )
`).run();

const upsertPresence = db.prepare(`
  INSERT INTO steam_rich_presence(steam_id, app_id, status, display, player_group, player_group_size, connect, raw_json, last_update)
  VALUES (@steam_id, @app_id, @status, @display, @player_group, @player_group_size, @connect, @raw_json, @last_update)
  ON CONFLICT(steam_id) DO UPDATE SET
    app_id=excluded.app_id,
    status=excluded.status,
    display=excluded.display,
    player_group=excluded.player_group,
    player_group_size=excluded.player_group_size,
    connect=excluded.connect,
    raw_json=excluded.raw_json,
    last_update=excluded.last_update
`);

const watchlistQuery = db.prepare(`
  SELECT DISTINCT steam_id FROM (
    SELECT steam_id FROM steam_links
    UNION
    SELECT steam_id FROM steam_presence_watchlist
  )
  WHERE steam_id IS NOT NULL AND steam_id != ''
`);

// ---- Steam client ----
const client = new SteamUser();
client.setOption('promptSteamGuardCode', false);
client.setOption('machineName', process.env.STEAM_MACHINE_NAME || 'DeadlockPresence');

// Login creds
const loginAccount = process.env.STEAM_BOT_USERNAME || process.env.STEAM_LOGIN || process.env.STEAM_ACCOUNT;
let loginKey = process.env.STEAM_LOGIN_KEY || '';
const loginKeyPath = process.env.STEAM_LOGIN_KEY_PATH
  ? path.resolve(process.env.STEAM_LOGIN_KEY_PATH)
  : path.join(__dirname, 'steam_login.key'); // Standard-Datei im Service-Ordner
const password = process.env.STEAM_BOT_PASSWORD || process.env.STEAM_PASSWORD;
const totpSecret = process.env.STEAM_TOTP_SECRET || '';
let guardCode = process.env.STEAM_GUARD_CODE || '';

if (!loginAccount) {
  log('error', 'Missing STEAM_BOT_USERNAME/STEAM_LOGIN env variable');
  process.exit(1);
}

// Lade persistierten loginKey (falls ENV leer)
if (!loginKey && fs.existsSync(loginKeyPath)) {
  try {
    loginKey = fs.readFileSync(loginKeyPath, 'utf8').trim();
    if (loginKey) log('info', 'Loaded login key from file', { loginKeyPath });
  } catch (err) {
    log('warn', 'Failed to read login key file', { loginKeyPath, error: err.message });
  }
}

// ---- State / Helpers ----
let isLoggedOn = false;
let isConnecting = false;
let waitingDeviceApproval = false;
let deviceWaitStartedAt = 0;
let deviceStatusTicker = null;
let reconnectTimer = null;
let backoffMs = 10_000;      // Start-Backoff
const backoffMaxMs = 5 * 60_000;

function startDeviceWaitWatchdog() {
  stopDeviceWaitWatchdog();
  deviceWaitStartedAt = Date.now();
  if (STATUS_LOG_EVERY_MS > 0) {
    deviceStatusTicker = setInterval(() => {
      if (!waitingDeviceApproval) return;
      const waited = Math.floor((Date.now() - deviceWaitStartedAt) / 1000);
      log('info', 'Still waiting for approval in Steam Mobile app…', { waitedSec: waited });
      if (DEVICE_WAIT_TIMEOUT_MS > 0 && (Date.now() - deviceWaitStartedAt) >= DEVICE_WAIT_TIMEOUT_MS) {
        log('warn', 'Device approval timed out — resetting challenge and re-trying login');
        waitingDeviceApproval = false;
        try { client.logOff(); } catch {}
        scheduleReconnect({ immediate: true });
      }
    }, STATUS_LOG_EVERY_MS);
  }
}

function stopDeviceWaitWatchdog() {
  if (deviceStatusTicker) {
    clearInterval(deviceStatusTicker);
    deviceStatusTicker = null;
  }
  deviceWaitStartedAt = 0;
}

function scheduleReconnect(opts = {}) {
  const { immediate = false } = opts;
  if (reconnectTimer || isConnecting || waitingDeviceApproval) return;
  const delay = immediate ? 0 : backoffMs;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    backoffMs = Math.min(backoffMaxMs, Math.floor(backoffMs * 1.8));
    log('info', 'Reconnecting to Steam…', { delayMs: delay, nextBackoffMs: backoffMs });
    logOn();
  }, delay);
}

function resetBackoff() {
  backoffMs = 10_000;
}

function logOn() {
  if (isLoggedOn || isConnecting) return;
  if (waitingDeviceApproval) {
    log('info', 'Still waiting for mobile device approval; not starting a new login.');
    return;
  }

  const options = { accountName: loginAccount, rememberPassword: true };

  if (loginKey) {
    options.loginKey = loginKey;
  } else if (password) {
    options.password = password;
    if (totpSecret) {
      options.twoFactorCode = SteamTotp.generateAuthCode(totpSecret);
    } else if (guardCode) {
      options.twoFactorCode = guardCode.trim();
      guardCode = '';
    }
  } else {
    log('error', 'Missing STEAM_BOT_PASSWORD or STEAM_LOGIN_KEY');
    process.exit(1);
  }

  isConnecting = true;
  log('info', 'Logging in to Steam', { account: loginAccount });
  try {
    client.logOn(options);
  } catch (e) {
    isConnecting = false;
    log('error', 'client.logOn threw', { error: e.message || String(e) });
    scheduleReconnect();
  }
}

// ---- stdin: global, non-blocking ----
const stdinRL = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
stdinRL.on('line', (line) => {
  const txt = (line || '').trim();
  if (!txt) return;

  // Steuerkommandos (optional)
  if (txt.toUpperCase() === 'STATUS') {
    log('info', 'Status', {
      isLoggedOn, isConnecting, waitingDeviceApproval,
      hasLoginKey: Boolean(loginKey), backoffMs,
    });
    return;
  }
  if (txt.toUpperCase() === 'CANCEL') {
    if (waitingDeviceApproval) {
      log('warn', 'Cancelling device approval wait (manual command).');
      waitingDeviceApproval = false;
      stopDeviceWaitWatchdog();
      try { client.logOff(); } catch {}
      scheduleReconnect({ immediate: true });
    } else {
      log('info', 'No device wait to cancel.');
    }
    return;
  }

  // Annahme: 5-stellige Codes (oder 2FA) -> „umschalten“ wenn wir gerade auf Device warten
  if (waitingDeviceApproval) {
    log('info', 'Received code while waiting for device approval — switching to code path.');
    stopDeviceWaitWatchdog();
    waitingDeviceApproval = false;
    guardCode = txt; // nächste logOn()-Runde nutzt diesen Code
    try { client.logOff(); } catch {}
    scheduleReconnect({ immediate: true });
  } else {
    // Kein Device-Wait → falls wir im steamGuard-Code-Zweig sind, wird der Code dort abgeholt,
    // ansonsten speichern wir ihn für den nächsten Logon.
    guardCode = txt;
    log('info', 'Stored guard code from stdin for next login attempt.');
    if (!isLoggedOn && !isConnecting) {
      scheduleReconnect({ immediate: true });
    }
  }
});

// ---- Presence helpers ----
function safeRequestRichPresence(steamID) {
  try { client.requestFriendRichPresence(steamID, APP_ID); }
  catch (err) { log('debug', 'requestFriendRichPresence failed', { steamId: steamID.toString(), error: err.message }); }
}

const watchList = new Map();
function refreshWatchList() {
  let rows = [];
  try { rows = watchlistQuery.all(); }
  catch (err) { log('error', 'Failed to read watchlist from DB', { error: err.message }); return; }
  const next = new Set();
  for (const row of rows) {
    const sid = String(row.steam_id || '').trim();
    if (!sid) continue;
    next.add(sid);
    if (!watchList.has(sid)) {
      try {
        const steamID = new SteamIDCtor(sid);
        watchList.set(sid, steamID);
        log('info', 'Added SteamID to watch list', { steamId: sid });
        if (isLoggedOn) safeRequestRichPresence(steamID);
      } catch (err) {
        log('warn', 'Ignoring invalid SteamID', { steamId: sid, error: err.message });
      }
    }
  }
  for (const sid of Array.from(watchList.keys())) {
    if (!next.has(sid)) { watchList.delete(sid); log('info', 'Removed SteamID from watch list', { steamId: sid }); }
  }
}

function pollPresence() {
  if (!isLoggedOn || watchList.size === 0) return;
  const ids = Array.from(watchList.values());
  const chunkSize = 25;
  for (let i = 0; i < ids.length; i += chunkSize) {
    for (const sid of ids.slice(i, i + chunkSize)) safeRequestRichPresence(sid);
  }
}

// ---- Events ----
client.on('loggedOn', () => {
  isLoggedOn = true;
  isConnecting = false;
  waitingDeviceApproval = false;
  stopDeviceWaitWatchdog();
  resetBackoff();
  log('info', 'Logged in to Steam', { account: loginAccount });
  client.setPersona(SteamUser.EPersonaState.Online);
  refreshWatchList();
});

client.on('loginKey', (key) => {
  log('info', 'Received new login key');
  loginKey = key;
  try {
    fs.writeFileSync(loginKeyPath, key, 'utf8');
    log('info', 'Stored login key to file', { loginKeyPath });
  } catch (err) {
    log('warn', 'Failed to persist login key to file', { loginKeyPath, error: err.message });
  }
});

client.on('steamGuard', async (domain, callback, lastCodeWrong) => {
  const d = domain || 'device';
  log('warn', 'Steam Guard required', { domain: d, lastCodeWrong: Boolean(lastCodeWrong) });

  try {
    if (d === 'device') {
      if (!waitingDeviceApproval) {
        waitingDeviceApproval = true;
        isConnecting = false; // blocke neue logOn() Versuche
        log('info', 'Waiting for approval in Steam Mobile app…');
        startDeviceWaitWatchdog();
        return void callback(); // kein Code nötig – Steam wartet serverseitig auf Approve
      } else {
        log('debug', 'Already waiting for mobile approval; ignoring duplicate steamGuard event.');
        return;
      }
    }

    // Code-basierte Varianten (email/TOTP/Code)
    if (totpSecret) {
      const code = SteamTotp.generateAuthCode(totpSecret);
      log('info', 'Supplying TOTP Steam Guard code');
      return void callback(code);
    }
    if (guardCode) {
      const code = guardCode.trim(); guardCode = '';
      log('info', 'Supplying guard code from buffer/env');
      return void callback(code);
    }

    // Kein Code vorhanden → kurze, non-blocking Wartezeit auf stdin (optional)
    const code = await new Promise((resolve) => {
      let settled = false;
      const timer = setTimeout(() => { if (!settled) { settled = true; resolve(''); } }, 30_000);
      const onLine = (line) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        stdinRL.removeListener('line', onLine);
        resolve((line || '').trim());
      };
      stdinRL.on('line', onLine);
    });

    if (code) {
      log('info', 'Supplying Steam Guard code from stdin (inline wait)');
      return void callback(code);
    } else {
      log('warn', 'No Steam Guard code supplied in time; will retry later.');
      try { client.logOff(); } catch {}
      scheduleReconnect();
    }
  } catch (e) {
    log('error', 'Failed during steamGuard handling', { error: e.message || String(e) });
  }
});

client.on('friendRichPresence', (steamID, appID) => {
  const sid64 = typeof steamID.getSteamID64 === 'function' ? steamID.getSteamID64() : String(steamID);
  const presence = client.getFriendRichPresence(steamID) || {};
  const normalized = {};
  for (const [k, v] of Object.entries(presence)) if (v !== undefined && v !== null) normalized[k] = typeof v === 'string' ? v : String(v);
  const entry = {
    steam_id: sid64,
    app_id: Number(appID) || null,
    status: normalized.status || null,
    display: normalized.steam_display || normalized.display || null,
    player_group: normalized.steam_player_group || null,
    player_group_size: normalized.steam_player_group_size ? Number(normalized.steam_player_group_size) || null : null,
    connect: normalized.connect || null,
    raw_json: JSON.stringify(normalized),
    last_update: Math.floor(Date.now() / 1000),
  };
  try { upsertPresence.run(entry); log('debug', 'Stored rich presence update', { steamId: sid64, appId: entry.app_id, status: entry.status, display: entry.display }); }
  catch (err) { log('error', 'Failed to persist rich presence', { steamId: sid64, error: err.message }); }
});

client.on('friendRelationship', (steamID, relationship) => {
  if (relationship === SteamUser.EFriendRelationship.None) {
    const sid64 = typeof steamID.getSteamID64 === 'function' ? steamID.getSteamID64() : String(steamID);
    if (watchList.delete(sid64)) log('info', 'Friend relationship removed, deleting from watch list', { steamId: sid64 });
  }
});

client.on('disconnected', (eresult, msg) => {
  isLoggedOn = false;
  isConnecting = false;
  log('warn', 'Steam disconnected', { eresult, msg, waitingDeviceApproval });
  // Während Device-Wait nicht auto-reconnecten; sonst Backoff
  if (!waitingDeviceApproval) scheduleReconnect();
  else startDeviceWaitWatchdog(); // sicherstellen, dass Statuslogs weiterlaufen
});

client.on('error', (err) => {
  log('error', 'Steam client error', { error: err.message });
  const text = (err && err.message) ? err.message.toLowerCase() : '';
  if (!waitingDeviceApproval && (text.includes('ratelimit') || text.includes('rate limit'))) {
    scheduleReconnect();
  }
});

client.on('webSession', () => log('debug', 'Web session established'));

// ---- Kickoff ----
refreshWatchList();
setInterval(refreshWatchList, WATCH_REFRESH_MS);
setInterval(pollPresence, POLL_INTERVAL_MS);
logOn();

function shutdown() {
  log('info', 'Shutting down presence service');
  stopDeviceWaitWatchdog();
  try { client.logOff(); } catch {}
  try { db.close(); } catch {}
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
process.on('uncaughtException', (err) => { log('error', 'Uncaught exception', { error: err.stack || err.message }); shutdown(); });
