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
const SteamID = require('steamid');
const SteamTotp = require('steam-totp');
const Database = require('better-sqlite3');

const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
const LOG_LEVEL = (process.env.LOG_LEVEL || 'info').toLowerCase();
const LOG_THRESHOLD = Object.prototype.hasOwnProperty.call(LOG_LEVELS, LOG_LEVEL)
  ? LOG_LEVELS[LOG_LEVEL]
  : LOG_LEVELS.info;

function intOption(envName, fallback) {
  const raw = process.env[envName];
  if (raw === undefined || raw === null || raw === '') {
    return fallback;
  }
  const parsed = parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

const APP_ID = intOption('DEADLOCK_APP_ID', 1422450);
const WATCH_REFRESH_MS = intOption('RP_WATCH_REFRESH_SEC', 30) * 1000;
const POLL_INTERVAL_MS = intOption('RP_POLL_INTERVAL_MS', 15000);
const FRIEND_REQUEST_INTERVAL_MS = Math.max(intOption('FRIEND_REQUEST_INTERVAL_MS', 15000), 1000);
const FRIEND_REQUEST_RETRY_SEC = Math.max(intOption('FRIEND_REQUEST_RETRY_SEC', 300), 30);
const FRIEND_REQUEST_BATCH_SIZE = Math.max(intOption('FRIEND_REQUEST_BATCH_SIZE', 20), 1);
const FRIEND_REQUEST_MAX_ATTEMPTS = intOption('FRIEND_REQUEST_MAX_ATTEMPTS', 5);
const QUICK_INVITE_POOL_SIZE = Math.max(intOption('QUICK_INVITE_POOL_SIZE', 10), 0);
const QUICK_INVITE_DURATION_SEC = Math.max(intOption('QUICK_INVITE_DURATION_SEC', 30 * 24 * 60 * 60), 0);
const QUICK_INVITE_REFRESH_MS = Math.max(intOption('QUICK_INVITE_REFRESH_MS', 15 * 60 * 1000), 60000);

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

function resolveLoginKeyPath(dbFilePath) {
  if (process.env.STEAM_LOGIN_KEY_PATH) {
    return path.resolve(process.env.STEAM_LOGIN_KEY_PATH);
  }
  const baseDir = process.env.STEAM_LOGIN_KEY_DIR
    ? path.resolve(process.env.STEAM_LOGIN_KEY_DIR)
    : (dbFilePath ? path.dirname(dbFilePath) : '');
  if (!baseDir) {
    return '';
  }
  return path.join(baseDir, 'steam_login.key');
}

const dbPath = resolveDbPath();
log('info', 'Using SQLite database', { dbPath });
const db = new Database(dbPath);

const loginKeyPath = resolveLoginKeyPath(dbPath);
if (loginKeyPath) {
  log('info', 'Steam login key persistence enabled', {
    loginKeyPath,
    source: process.env.STEAM_LOGIN_KEY_PATH ? 'env' : 'default',
  });
}

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

db.prepare(`
  CREATE TABLE IF NOT EXISTS steam_friend_requests (
    steam_id TEXT PRIMARY KEY,
    status TEXT DEFAULT 'pending',
    requested_at INTEGER DEFAULT (strftime('%s','now')),
    last_attempt INTEGER,
    attempts INTEGER DEFAULT 0,
    error TEXT
  )
`).run();

db.prepare(`
  CREATE TABLE IF NOT EXISTS steam_quick_invites (
    token TEXT PRIMARY KEY,
    invite_link TEXT NOT NULL,
    invite_limit INTEGER DEFAULT 1,
    invite_duration INTEGER,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    status TEXT DEFAULT 'available',
    reserved_by INTEGER,
    reserved_at INTEGER,
    last_seen INTEGER
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

let friendRequestQuerySql = `
  SELECT steam_id, attempts FROM steam_friend_requests
  WHERE status = 'pending'
    AND (last_attempt IS NULL OR last_attempt <= strftime('%s','now') - ?)
`;
if (FRIEND_REQUEST_MAX_ATTEMPTS > 0) {
  friendRequestQuerySql += ` AND attempts < ${FRIEND_REQUEST_MAX_ATTEMPTS}`;
}
friendRequestQuerySql += ' ORDER BY requested_at ASC LIMIT ?';
const friendRequestQuery = db.prepare(friendRequestQuerySql);
const markFriendRequestRetry = db.prepare(`
  UPDATE steam_friend_requests
  SET last_attempt = strftime('%s','now'),
      attempts = attempts + 1,
      error = @error
  WHERE steam_id = @steam_id
`);
const markFriendRequestSent = db.prepare(`
  UPDATE steam_friend_requests
  SET status = 'sent',
      last_attempt = strftime('%s','now'),
      attempts = CASE WHEN attempts < 1 THEN 1 ELSE attempts END,
      error = NULL
  WHERE steam_id = @steam_id
`);
const markFriendRequestFailed = db.prepare(`
  UPDATE steam_friend_requests
  SET status = 'failed',
      last_attempt = strftime('%s','now'),
      attempts = attempts + 1,
      error = @error
  WHERE steam_id = @steam_id
`);

const markQuickInvitesExpired = db.prepare(`
  UPDATE steam_quick_invites
  SET status = 'expired',
      last_seen = strftime('%s','now')
  WHERE status = 'available'
    AND expires_at IS NOT NULL
    AND expires_at <= strftime('%s','now')
`);

const countAvailableQuickInvites = db.prepare(`
  SELECT COUNT(1) AS count
  FROM steam_quick_invites
  WHERE status = 'available'
    AND (expires_at IS NULL OR expires_at > strftime('%s','now'))
`);

const upsertQuickInvite = db.prepare(`
  INSERT INTO steam_quick_invites(
    token, invite_link, invite_limit, invite_duration, created_at,
    expires_at, status, reserved_by, reserved_at, last_seen
  ) VALUES (
    @invite_token, @invite_link, @invite_limit, @invite_duration, @created_at,
    @expires_at, 'available', NULL, NULL, strftime('%s','now')
  )
  ON CONFLICT(token) DO UPDATE SET
    invite_link=excluded.invite_link,
    invite_limit=excluded.invite_limit,
    invite_duration=excluded.invite_duration,
    created_at=excluded.created_at,
    expires_at=excluded.expires_at,
    status=CASE
      WHEN steam_quick_invites.status = 'shared' THEN steam_quick_invites.status
      ELSE 'available'
    END,
    reserved_by=CASE
      WHEN steam_quick_invites.status = 'shared' THEN steam_quick_invites.reserved_by
      ELSE NULL
    END,
    reserved_at=CASE
      WHEN steam_quick_invites.status = 'shared' THEN steam_quick_invites.reserved_at
      ELSE NULL
    END,
    last_seen=strftime('%s','now')
`);

const client = new SteamUser();
client.setOption('promptSteamGuardCode', false);

let isLoggedOn = false;
let reconnectTimer = null;
const watchList = new Map();
let ensuringQuickInvites = false;
let quickInviteWarnedUnsupported = false;

const loginAccount = process.env.STEAM_BOT_USERNAME || process.env.STEAM_LOGIN || process.env.STEAM_ACCOUNT;
let loginKey = process.env.STEAM_LOGIN_KEY || '';
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
        const steamID = new SteamID(sid);
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

function processFriendRequests() {
  if (!isLoggedOn) {
    return;
  }
  let rows = [];
  try {
    rows = friendRequestQuery.all(FRIEND_REQUEST_RETRY_SEC, FRIEND_REQUEST_BATCH_SIZE);
  } catch (err) {
    log('error', 'Failed to read pending friend requests', { error: err.message });
    return;
  }
  if (!rows || rows.length === 0) {
    return;
  }

  for (const row of rows) {
    const sid = String(row.steam_id || '').trim();
    if (!sid) {
      continue;
    }

    let steamID;
    try {
      steamID = new SteamID(sid);
    } catch (err) {
      log('warn', 'Invalid SteamID in friend request queue', { steamId: sid, error: err.message });
      try {
        markFriendRequestFailed.run({ steam_id: sid, error: err.message });
      } catch (dbErr) {
        log('error', 'Failed to mark Steam friend request as failed', { steamId: sid, error: dbErr.message });
      }
      continue;
    }

    try {
      log('info', 'Sending Steam friend request', { steamId: sid });
      client.addFriend(steamID);
      try {
        markFriendRequestSent.run({ steam_id: sid });
      } catch (dbErr) {
        log('warn', 'Failed to persist friend request success state', { steamId: sid, error: dbErr.message });
      }
    } catch (err) {
      log('warn', 'Steam friend request failed', { steamId: sid, error: err.message });
      try {
        markFriendRequestRetry.run({ steam_id: sid, error: err.message });
      } catch (dbErr) {
        log('error', 'Failed to persist friend request retry state', { steamId: sid, error: dbErr.message });
      }
    }
  }
}

async function ensureQuickInvitePool() {
  if (!isLoggedOn || QUICK_INVITE_POOL_SIZE <= 0) {
    return;
  }
  if (typeof client.createQuickInviteLink !== 'function') {
    if (!quickInviteWarnedUnsupported) {
      log('warn', 'Quick invite API not supported by current steam-user version');
      quickInviteWarnedUnsupported = true;
    }
    return;
  }
  if (ensuringQuickInvites) {
    return;
  }
  ensuringQuickInvites = true;

  try {
    try {
      markQuickInvitesExpired.run();
    } catch (err) {
      log('warn', 'Failed to mark expired quick invites', { error: err.message });
    }

    let available = 0;
    try {
      const row = countAvailableQuickInvites.get();
      if (row && Object.prototype.hasOwnProperty.call(row, 'count')) {
        available = Number(row.count) || 0;
      }
    } catch (err) {
      log('error', 'Failed to count available quick invites', { error: err.message });
      return;
    }

    const needed = QUICK_INVITE_POOL_SIZE - available;
    if (needed <= 0) {
      return;
    }

    for (let i = 0; i < needed; i += 1) {
      try {
        const options = { inviteLimit: 1 };
        if (QUICK_INVITE_DURATION_SEC > 0) {
          options.inviteDuration = QUICK_INVITE_DURATION_SEC;
        }

        const response = await client.createQuickInviteLink(options);
        if (!response || !response.token) {
          log('warn', 'Quick invite creation returned empty response');
          continue;
        }

        const token = response.token;
        const createdAt = token.time_created instanceof Date
          ? Math.floor(token.time_created.getTime() / 1000)
          : Math.floor(Date.now() / 1000);
        const inviteDurationRaw = token.invite_duration;
        const inviteDuration = (inviteDurationRaw === undefined || inviteDurationRaw === null)
          ? null
          : Number(inviteDurationRaw);
        let expiresAt = null;
        if (inviteDuration && Number.isFinite(inviteDuration) && inviteDuration > 0) {
          expiresAt = createdAt + inviteDuration;
        } else if (QUICK_INVITE_DURATION_SEC > 0) {
          expiresAt = createdAt + QUICK_INVITE_DURATION_SEC;
        }

        upsertQuickInvite.run({
          invite_token: token.invite_token,
          invite_link: token.invite_link,
          invite_limit: Number(token.invite_limit || 0),
          invite_duration: inviteDuration,
          created_at: createdAt,
          expires_at: expiresAt,
        });

        log('info', 'Created Steam quick invite link', {
          inviteToken: token.invite_token,
          expiresAt,
        });
      } catch (err) {
        log('warn', 'Failed to create Steam quick invite link', { error: err.message });
        break;
      }
    }
  } finally {
    ensuringQuickInvites = false;
  }
}

client.on('loggedOn', async () => {
  isLoggedOn = true;
  log('info', 'Logged in to Steam', { account: loginAccount });
  client.setPersona(SteamUser.EPersonaState.Online);
  refreshWatchList();
  processFriendRequests();
  try {
    await ensureQuickInvitePool();
  } catch (err) {
    log('warn', 'Initial quick invite pool fill failed', { error: err.message });
  }
});

client.on('loginKey', (key) => {
  log('info', 'Received new login key');
  loginKey = key;
  if (loginKeyPath) {
    try {
      fs.mkdirSync(path.dirname(loginKeyPath), { recursive: true });
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
  const sid64 = steamID.getSteamID64();

  if (relationship === SteamUser.EFriendRelationship.RequestRecipient) {
    log('info', 'Accepting inbound friend request', { steamId: sid64 });
    try {
      client.addFriend(steamID);
      try {
        markFriendRequestSent.run({ steam_id: sid64 });
      } catch (dbErr) {
        log('debug', 'Failed to update friend request state after accepting inbound request', {
          steamId: sid64,
          error: dbErr.message,
        });
      }
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

  if (relationship === SteamUser.EFriendRelationship.Friend) {
    try {
      markFriendRequestSent.run({ steam_id: sid64 });
    } catch (err) {
      log('debug', 'Failed to update friend request state for friend relationship', {
        steamId: sid64,
        error: err.message,
      });
    }
  }

  if (relationship === SteamUser.EFriendRelationship.None) {
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
setInterval(processFriendRequests, FRIEND_REQUEST_INTERVAL_MS);
if (QUICK_INVITE_POOL_SIZE > 0) {
  setInterval(() => {
    ensureQuickInvitePool().catch((err) => {
      log('warn', 'Quick invite pool refresh failed', { error: err.message });
    });
  }, QUICK_INVITE_REFRESH_MS);
}

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
