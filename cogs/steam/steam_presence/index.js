#!/usr/bin/env node
/**
 * Steam Rich Presence bridge (safe login flow, v5 tokens)
 * - Auto-Login NUR wenn ./.steam-data/refresh.token existiert
 * - Sonst: KEIN Auto-Login, nur via "!sg <CODE>" oder "<CODE>" + Passwort/TOTP
 * - KEINE device-Approval-Wartepfade (kein Timeout). Bei device -> abbrechen & auf !sg warten.
 * - Bei ungültigem refresh.token: löschen, nicht automatisch neu versuchen.
 * - Machine-Auth-Token: wird gespeichert/geladen; reduziert erneute Guard-Prompts bei PW-Login.
 * - Restliche Funktionen (Presence, DB etc.) bleiben unverändert.
 */

const fs = require('fs');
const path = require('path');
const os = require('os');
const readline = require('readline');
const SteamUser = require('steam-user');
const SteamTotp = require('steam-totp');
const Database = require('better-sqlite3');

let SteamIDCtor = null;
try { SteamIDCtor = require('steamid'); } catch { SteamIDCtor = SteamUser.SteamID; }

// ---------------- Config ----------------
const APP_ID = parseInt(process.env.DEADLOCK_APP_ID || '1422450', 10);
const WATCH_REFRESH_MS = parseInt(process.env.RP_WATCH_REFRESH_SEC || '60', 10) * 1000;
const POLL_INTERVAL_MS  = parseInt(process.env.RP_POLL_INTERVAL_MS  || '30000', 10);

// Token-Bucket
const CHUNK_SIZE      = parseInt(process.env.RP_CHUNK_SIZE      || '20', 10);
const CHUNK_DELAY_MS  = parseInt(process.env.RP_CHUNK_DELAY_MS  || '500', 10);
const MAX_REQ_PER_MIN = parseInt(process.env.RP_MAX_REQ_PER_MIN || '120', 10);

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

// --------------- Single-instance lock ---------------
const LOCK_PATH = path.join(__dirname, 'presence.lock');
let lockFd;
try { lockFd = fs.openSync(LOCK_PATH, 'wx'); fs.writeFileSync(lockFd, String(process.pid)); }
catch { console.error('Another presence instance seems to be running. Exiting.'); process.exit(0); }
function cleanupLock(){ try{ if(lockFd) fs.closeSync(lockFd); fs.unlinkSync(LOCK_PATH); }catch{} }
process.on('exit', cleanupLock); process.on('SIGINT', ()=>{ cleanupLock(); process.exit(0); });
process.on('SIGTERM', ()=>{ cleanupLock(); process.exit(0); });

// --------------- DB init ---------------
function resolveDbPath() {
  if (process.env.DEADLOCK_DB_PATH) return path.resolve(process.env.DEADLOCK_DB_PATH);
  const baseDir = process.env.DEADLOCK_DB_DIR ? path.resolve(process.env.DEADLOCK_DB_DIR) : path.join(os.homedir(), 'Documents', 'Deadlock', 'service');
  return path.join(baseDir, 'deadlock.sqlite3');
}
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

// --------------- Steam client ---------------
// WICHTIG: v5 – wir nutzen Token-Flow
const client = new SteamUser({ renewRefreshTokens: true });
client.setOption('promptSteamGuardCode', false);
client.setOption('machineName', 'DeadlockPresence');

// Feste „Cookie“-Ablage
const DATA_DIR = path.join(__dirname, '.steam-data');
try { fs.mkdirSync(DATA_DIR, { recursive: true }); } catch {}
client.setOption('dataDirectory', DATA_DIR);

// Login creds (für Erstlogin ohne Token)
const loginAccount = process.env.STEAM_BOT_USERNAME || process.env.STEAM_LOGIN || process.env.STEAM_ACCOUNT;
const password   = process.env.STEAM_BOT_PASSWORD || process.env.STEAM_PASSWORD || '';
const totpSecret = process.env.STEAM_TOTP_SECRET || '';
let   guardCode  = ''; // nur via !sg

// Token-Dateien (neuer Standard)
const REFRESH_TOKEN_PATH = path.join(DATA_DIR, 'refresh.token');
const MACHINE_TOKEN_PATH = path.join(DATA_DIR, 'machine_auth_token.txt');

let refreshToken = '';
try { if (fs.existsSync(REFRESH_TOKEN_PATH)) { refreshToken = fs.readFileSync(REFRESH_TOKEN_PATH, 'utf8').trim(); if (refreshToken) log('info', 'Loaded refresh token', { path: REFRESH_TOKEN_PATH }); } }
catch (err) { log('warn', 'Failed to read refresh token', { path: REFRESH_TOKEN_PATH, error: err.message }); }

let machineAuthToken = '';
try { if (fs.existsSync(MACHINE_TOKEN_PATH)) { machineAuthToken = fs.readFileSync(MACHINE_TOKEN_PATH, 'utf8').trim(); if (machineAuthToken) log('info', 'Loaded machine auth token', { path: MACHINE_TOKEN_PATH }); } }
catch (err) { log('warn', 'Failed to read machine auth token', { path: MACHINE_TOKEN_PATH, error: err.message }); }

// --------------- State ---------------
let isLoggedOn = false;
let isConnecting = false;

// Keine Device-Approval-Engine, keine Timer/Timeouts.
let reconnectTimer = null;
let backoffMs = 10_000;
const backoffMaxMs = 5 * 60_000;

// Token-Bucket
let tokens = MAX_REQ_PER_MIN;
setInterval(() => { tokens = MAX_REQ_PER_MIN; }, 60_000);

// --------------- Helpers ---------------
function tryRequestPresence(steamID) {
  if (tokens <= 0) return false;
  try { client.requestFriendRichPresence(steamID, APP_ID); tokens--; return true; }
  catch (err) { log('debug', 'requestFriendRichPresence failed', { steamId: String(steamID), error: err.message }); return false; }
}

function scheduleReconnect(opts = {}) {
  const { immediate = false } = opts;
  if (reconnectTimer || isConnecting) return;
  const delay = immediate ? 0 : backoffMs;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    backoffMs = Math.min(backoffMaxMs, Math.floor(backoffMs * 1.8));
    // Nur reconnecten, wenn wir Auto-Login dürfen (refresh token existiert)
    if (refreshToken) {
      log('info', 'Reconnecting to Steam…', { delayMs: delay, nextBackoffMs: backoffMs });
      logOn();
    } else {
      log('info', 'No refresh token present — staying idle (waiting for !sg).');
    }
  }, delay);
}
function resetBackoff() { backoffMs = 10_000; }

function buildLogonOptions() {
  // v5-Regeln:
  // - Mit refreshToken: KEIN accountName/password/machineAuthToken übergeben
  if (refreshToken) {
    return { refreshToken };
  }

  // Ohne refreshToken: Passwort-Login erforderlich (+ 2FA), optional machineAuthToken
  if (!loginAccount) {
    log('error', 'Missing STEAM_BOT_USERNAME/STEAM_LOGIN env variable'); throw new Error('No account');
  }
  if (!password) {
    log('error', 'Missing STEAM_BOT_PASSWORD and no refresh token — cannot login. Use !sg <CODE> after setting password env.');
    throw new Error('No password');
  }

  const opts = { accountName: loginAccount, password };

  // 2FA: bevorzugt TOTP, sonst Guard-Code via !sg
  if (totpSecret) {
    opts.twoFactorCode = SteamTotp.generateAuthCode(totpSecret);
  } else if (guardCode) {
    opts.twoFactorCode = guardCode.trim().toUpperCase();
    guardCode = '';
  } else {
    log('info', 'No refresh token and no guard/TOTP code — skipping login until !sg.');
    throw new Error('No code');
  }

  // machineAuthToken hilft, erneute device-Prompts zu vermeiden (entscheidet Steam)
  if (machineAuthToken) {
    opts.machineAuthToken = machineAuthToken;
  }
  return opts;
}

function logOn() {
  if (isLoggedOn || isConnecting) return;

  // Regel: nur auto, wenn refreshToken existiert
  if (!refreshToken && !guardCode && !totpSecret) {
    log('info', 'Auto-login disabled (no refresh token). Waiting for !sg <CODE>.');
    return;
  }

  let options;
  try { options = buildLogonOptions(); }
  catch { return; }

  isConnecting = true;
  log('info', 'Logging in to Steam', { usingRefreshToken: Boolean(options.refreshToken), account: options.refreshToken ? undefined : loginAccount });
  try { client.logOn(options); }
  catch (e) { isConnecting = false; log('error', 'client.logOn threw', { error: e.message || String(e) }); }
}

// --------------- stdin (!sg / status) ---------------
const stdinRL = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
stdinRL.on('line', (line) => {
  let txt = (line || '').trim();
  if (!txt) return;

  // Plain Code: 5–7 alphanumerische Zeichen
  const plainCodeMatch = txt.match(/^[A-Z0-9]{5,7}$/i);

  // Varianten: "!sg CODE", "sg CODE", "/sg CODE"
  const cmdMatch = txt.match(/^(?:!|\/)?sg\s+([A-Z0-9]{5,7})$/i);

  if (/^status$/i.test(txt)) {
    log('info', 'Status', {
      isLoggedOn, isConnecting,
      hasRefreshToken: Boolean(refreshToken),
      hasMachineAuthToken: Boolean(machineAuthToken),
      dataDir: DATA_DIR
    });
    return;
  }

  if (/^cancel$/i.test(txt)) {
    try { client.logOff(); } catch {}
    isConnecting = false;
    log('info', 'Cancelled any ongoing login; waiting for !sg.');
    return;
  }

  const code = cmdMatch ? cmdMatch[1] : (plainCodeMatch ? plainCodeMatch[0] : null);
  if (code) {
    guardCode = code.toUpperCase();
    log('info', 'Guard code received via console; attempting login now.');
    logOn(); // unmittelbarer Versuch (durch User ausgelöst)
    return;
  }

  log('info', 'Unknown console input. Use "!sg <CODE>" or "STATUS".');
});

// --------------- Presence helpers ---------------
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
        if (isLoggedOn) tryRequestPresence(steamID);
      } catch (err) {
        log('warn', 'Ignoring invalid SteamID', { steamId: sid, error: err.message });
      }
    }
  }
  for (const sid of Array.from(watchList.keys())) {
    if (!next.has(sid)) { watchList.delete(sid); log('info', 'Removed SteamID from watch list', { steamId: sid }); }
  }
}

async function pollPresence() {
  if (!isLoggedOn || watchList.size === 0) return;
  const ids = Array.from(watchList.values());
  for (let i = 0; i < ids.length; i += CHUNK_SIZE) {
    const chunk = ids.slice(i, i + CHUNK_SIZE);
    for (const sid of chunk) {
      if (tokens <= 0) {
        log('warn', 'Presence request budget exhausted; pausing chunk loop until refill.');
        await new Promise(r => setTimeout(r, Math.max(CHUNK_DELAY_MS, 1000)));
      }
      tryRequestPresence(sid);
    }
    await new Promise(r => setTimeout(r, CHUNK_DELAY_MS));
  }
}

// --------------- Events ---------------
client.on('loggedOn', () => {
  isLoggedOn = true;
  isConnecting = false;
  resetBackoff();
  log('info', 'Logged in to Steam', { account: loginAccount, usingRefreshToken: Boolean(refreshToken) });
  client.setPersona(SteamUser.EPersonaState.Online);

  setTimeout(() => { refreshWatchList(); pollPresence().catch(() => {}); }, 5000);
});

// Neuer Standard: Refresh-Token speichern
client.on('refreshToken', (token) => {
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(REFRESH_TOKEN_PATH, token, 'utf8');
    refreshToken = token;
    log('info', 'Stored refresh token', { path: REFRESH_TOKEN_PATH });
  } catch (err) {
    log('warn', 'Failed to persist refresh token', { path: REFRESH_TOKEN_PATH, error: err.message });
  }
});

// Neuer Standard: Machine-Auth-Token speichern
client.on('machineAuthToken', (token) => {
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(MACHINE_TOKEN_PATH, token, 'utf8');
    machineAuthToken = token;
    log('info', 'Stored machine auth token', { path: MACHINE_TOKEN_PATH });
  } catch (err) {
    log('warn', 'Failed to write machine auth token', { path: MACHINE_TOKEN_PATH, error: err.message });
  }
});

// SteamGuard-Flow: keine device-Wartepfade
client.on('steamGuard', (domain, callback) => {
  const d = (domain || 'device').toLowerCase();
  if (d === 'device') {
    log('warn', 'Steam Guard (device) requested — blocked by config. Use !sg <CODE> instead.');
    try { client.logOff(); } catch {}
    isConnecting = false;
    return; // kein callback()-Loop
  }
  // Codebasierte Varianten: akzeptieren, wenn vorhanden
  if (totpSecret) {
    const code = SteamTotp.generateAuthCode(totpSecret);
    log('info', 'Supplying TOTP Steam Guard code');
    return void callback(code);
  }
  if (guardCode) {
    const code = guardCode.trim().toUpperCase(); guardCode = '';
    log('info', 'Supplying guard code from console (!sg)');
    return void callback(code);
  }
  // Kein Code vorhanden: abbrechen, auf !sg warten
  log('info', 'Steam Guard required but no code present — waiting for !sg.');
  try { client.logOff(); } catch {}
  isConnecting = false;
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
  log('warn', 'Steam disconnected', { eresult, msg });
  // Nur reconnecten, wenn wir einen Refresh-Token haben (Auto-Login erlaubt)
  if (refreshToken) scheduleReconnect();
});

client.on('error', (err) => {
  log('error', 'Steam client error', { error: err.message });
  const text = (err && err.message) ? err.message.toLowerCase() : '';

  // Offensichtliche Token-Probleme -> Token löschen, NICHT automatisch neu versuchen
  if (text.includes('invalid refresh') || text.includes('expired') || text.includes('refresh token')) {
    if (refreshToken) {
      log('warn', 'Refresh token likely invalid — clearing token and waiting for !sg');
      try { fs.unlinkSync(REFRESH_TOKEN_PATH); } catch {}
      refreshToken = '';
    }
    isConnecting = false;
    return;
  }

  // RateLimit o.ä. -> kein Autosturm; nur bei vorhandenem Token später reconnecten
  if (text.includes('ratelimit') || text.includes('rate limit') || text.includes('throttle')) {
    isConnecting = false;
    log('warn', 'Server throttle — no auto retry without refresh token. Wait and use !sg if needed.');
    return;
  }

  // Standard: bei sonstigen Errors nur reconnecten, wenn Token existiert
  isConnecting = false;
  if (refreshToken) scheduleReconnect();
});

client.on('webSession', () => log('debug', 'Web session established'));

// --------------- Kickoff / Shutdown ---------------
refreshWatchList();
setInterval(refreshWatchList, WATCH_REFRESH_MS);
setInterval(() => { pollPresence().catch(() => {}); }, POLL_INTERVAL_MS);

// Startregel: nur wenn refresh.token existiert
if (refreshToken) {
  log('info', 'Auto-login enabled (refresh.token present).');
  logOn();
} else {
  log('info', 'Auto-login disabled (no refresh token). Waiting for !sg <CODE>.');
}

function shutdown() {
  log('info', 'Shutting down presence service');
  try { client.logOff(); } catch {}
  try { db.close(); } catch {}
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
process.on('uncaughtException', (err) => { log('error', 'Uncaught exception', { error: err.stack || err.message }); shutdown(); });
