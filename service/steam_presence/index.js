#!/usr/bin/env node
/**
 * Steam Rich Presence bridge for Deadlock bots.
 * Logs into Steam via steam-user and persists rich presence snapshots
 * into the shared SQLite database so the Python bot can evaluate lobby/match states.
 */

require('dotenv').config();

const fs = require('fs');
const path = require('path');
const os = require('os');
const SteamUser = require('steam-user');
const SteamTotp = require('steam-totp');
const Database = require('better-sqlite3');

const APP_ID = parseInt(process.env.DEADLOCK_APP_ID || '1422450', 10);
const WATCH_REFRESH_MS = parseInt(process.env.RP_WATCH_REFRESH_SEC || '30', 10) * 1000;
const POLL_INTERVAL_MS = parseInt(process.env.RP_POLL_INTERVAL_MS || '15000', 10);
const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
const LOG_LEVEL = (process.env.LOG_LEVEL || 'info').toLowerCase();
const LOG_THRESHOLD = Object.prototype.hasOwnProperty.call(LOG_LEVELS, LOG_LEVEL)
  ? LOG_LEVELS[LOG_LEVEL]
  : LOG_LEVELS.info;

function log(level, message, extra = undefined) {
  const lvl = LOG_LEVELS[level];
  if (lvl === undefined || lvl > LOG_THRESHOLD) {
    return;
  }
  const payload = {
    time: new Date().toISOString(),
    level,
    msg: message,
  };
  if (extra && typeof extra === 'object') {
    for (const [k, v] of Object.entries(extra)) {
      if (v !== undefined) {
        payload[k] = v;
      }
    }
  }
  console.log(JSON.stringify(payload));
}

function resolveDbPath() {
  if (process.env.DEADLOCK_DB_PATH) {
    return path.resolve(process.env.DEADLOCK_DB_PATH);
  }
  const baseDir = process.env.DEADLOCK_DB_DIR
    ? path.resolve(process.env.DEADLOCK_DB_DIR)
    : path.join(os.homedir(), 'Documents', 'Deadlock', 'service');
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

const client = new SteamUser();
client.setOption('promptSteamGuardCode', false);

let isLoggedOn = false;
let reconnectTimer = null;
const watchList = new Map();

const loginAccount = process.env.STEAM_BOT_USERNAME || process.env.STEAM_LOGIN || process.env.STEAM_ACCOUNT;
let loginKey = process.env.STEAM_LOGIN_KEY || '';
const loginKeyPath = process.env.STEAM_LOGIN_KEY_PATH ? path.resolve(process.env.STEAM_LOGIN_KEY_PATH) : '';
const password = process.env.STEAM_BOT_PASSWORD || process.env.STEAM_PASSWORD;
const totpSecret = process.env.STEAM_TOTP_SECRET || '';
let guardCode = process.env.STEAM_GUARD_CODE || '';

if (!loginAccount) {
  log('error', 'Missing STEAM_BOT_USERNAME/STEAM_LOGIN env variable');
  process.exit(1);
}

if (!loginKey && loginKeyPath && fs.existsSync(loginKeyPath)) {
  try {
    loginKey = fs.readFileSync(loginKeyPath, 'utf8').trim();
    if (loginKey) {
      log('info', 'Loaded login key from file', { loginKeyPath });
    }
  } catch (err) {
    log('warn', 'Failed to read login key file', { loginKeyPath, error: err.message });
  }
}

function scheduleReconnect(delayMs = 10000) {
  if (reconnectTimer) {
    return;
  }
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    logOn();
  }, delayMs);
}

function logOn() {
  if (isLoggedOn) {
    return;
  }
  const options = {
    accountName: loginAccount,
    rememberPassword: true,
  };

  if (loginKey) {
    options.loginKey = loginKey;
  } else if (password) {
    options.password = password;
    if (totpSecret) {
      options.twoFactorCode = SteamTotp.generateAuthCode(totpSecret);
    } else if (guardCode) {
      options.twoFactorCode = guardCode;
      guardCode = '';
    }
  } else {
    log('error', 'Missing STEAM_BOT_PASSWORD or STEAM_LOGIN_KEY');
    process.exit(1);
  }

  log('info', 'Logging in to Steam', { account: loginAccount });
  client.logOn(options);
}

function safeRequestRichPresence(steamID) {
  try {
    client.requestFriendRichPresence(steamID, APP_ID);
  } catch (err) {
    log('debug', 'requestFriendRichPresence failed', { steamId: steamID.toString(), error: err.message });
  }
}

function refreshWatchList() {
  let rows = [];
  try {
    rows = watchlistQuery.all();
  } catch (err) {
    log('error', 'Failed to read watchlist from DB', { error: err.message });
    return;
  }
  const next = new Set();
  for (const row of rows) {
    const sid = String(row.steam_id || '').trim();
    if (!sid) {
      continue;
    }
    next.add(sid);
    if (!watchList.has(sid)) {
      try {
        const steamID = new SteamUser.SteamID(sid);
        watchList.set(sid, steamID);
        log('info', 'Added SteamID to watch list', { steamId: sid });
        if (isLoggedOn) {
          safeRequestRichPresence(steamID);
        }
      } catch (err) {
        log('warn', 'Ignoring invalid SteamID', { steamId: sid, error: err.message });
      }
    }
  }
  for (const sid of Array.from(watchList.keys())) {
    if (!next.has(sid)) {
      watchList.delete(sid);
      log('info', 'Removed SteamID from watch list', { steamId: sid });
    }
  }
}

function pollPresence() {
  if (!isLoggedOn || watchList.size === 0) {
    return;
  }
  const ids = Array.from(watchList.values());
  const chunkSize = 25;
  for (let i = 0; i < ids.length; i += chunkSize) {
    const chunk = ids.slice(i, i + chunkSize);
    for (const sid of chunk) {
      safeRequestRichPresence(sid);
    }
  }
}

client.on('loggedOn', () => {
  isLoggedOn = true;
  log('info', 'Logged in to Steam', { account: loginAccount });
  client.setPersona(SteamUser.EPersonaState.Online);
  refreshWatchList();
});

client.on('loginKey', (key) => {
  log('info', 'Received new login key');
  loginKey = key;
  if (loginKeyPath) {
    try {
      fs.writeFileSync(loginKeyPath, key, 'utf8');
      log('info', 'Stored login key to file', { loginKeyPath });
    } catch (err) {
      log('warn', 'Failed to persist login key to file', { loginKeyPath, error: err.message });
    }
  }
});

client.on('steamGuard', (domain, callback, lastCodeWrong) => {
  log('warn', 'Steam Guard required', { domain: domain || 'device', lastCodeWrong: Boolean(lastCodeWrong) });
  if (totpSecret) {
    const code = SteamTotp.generateAuthCode(totpSecret);
    log('info', 'Supplying TOTP Steam Guard code');
    callback(code);
  } else if (guardCode) {
    const code = guardCode;
    guardCode = '';
    log('info', 'Supplying static Steam Guard code');
    callback(code);
  } else {
    log('error', 'No Steam Guard code available. Set STEAM_TOTP_SECRET or STEAM_GUARD_CODE.');
  }
});

client.on('friendRichPresence', (steamID, appID) => {
  const sid64 = steamID.getSteamID64();
  const presence = client.getFriendRichPresence(steamID) || {};
  const normalized = {};
  for (const [key, value] of Object.entries(presence)) {
    if (value === undefined || value === null) {
      continue;
    }
    normalized[key] = typeof value === 'string' ? value : String(value);
  }

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

  try {
    upsertPresence.run(entry);
    log('debug', 'Stored rich presence update', { steamId: sid64, appId: entry.app_id, status: entry.status, display: entry.display });
  } catch (err) {
    log('error', 'Failed to persist rich presence', { steamId: sid64, error: err.message });
  }
});

client.on('friendRelationship', (steamID, relationship) => {
  if (relationship === SteamUser.EFriendRelationship.RequestRecipient) {
    const sid64 = steamID.getSteamID64();
    log('info', 'Accepting inbound friend request', { steamId: sid64 });
    try {
      client.addFriend(steamID);
    } catch (err) {
      log('warn', 'Failed to accept friend request', { steamId: sid64, error: err.message });
      return;
    }
    const cached = watchList.get(sid64);
    if (cached) {
      safeRequestRichPresence(cached);
    }
    return;
  }

  if (relationship === SteamUser.EFriendRelationship.None) {
    const sid64 = steamID.getSteamID64();
    if (watchList.delete(sid64)) {
      log('info', 'Friend relationship removed, deleting from watch list', { steamId: sid64 });
    }
  }
});

client.on('disconnected', (eresult, msg) => {
  isLoggedOn = false;
  log('warn', 'Steam disconnected', { eresult, msg });
  scheduleReconnect();
});

client.on('error', (err) => {
  log('error', 'Steam client error', { error: err.message });
});

client.on('webSession', () => {
  log('debug', 'Web session established');
});

refreshWatchList();
setInterval(refreshWatchList, WATCH_REFRESH_MS);
setInterval(pollPresence, POLL_INTERVAL_MS);

logOn();

function shutdown() {
  log('info', 'Shutting down presence service');
  try {
    client.logOff();
  } catch (err) {
    log('debug', 'logOff error', { error: err.message });
  }
  try {
    db.close();
  } catch (err) {
    log('debug', 'DB close error', { error: err.message });
  }
  process.exit(0);
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
process.on('uncaughtException', (err) => {
  log('error', 'Uncaught exception', { error: err.stack || err.message });
  shutdown();
});
