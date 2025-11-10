'use strict';

const https = require('https');
const { URLSearchParams } = require('url');
const SteamUser = require('steam-user');
const { DeadlockPresenceLogger } = require('./deadlock_presence_logger');

const DEFAULT_INTERVAL_MS = 60000;
const MIN_INTERVAL_MS = 10000;
const PERSIST_ERROR_LOG_INTERVAL_MS = 60000;
const MAX_MATCH_MINUTES = 24 * 60;
const VOICE_WATCH_MAX_AGE_SEC = 180;
const PLAYER_SUMMARIES_ENDPOINT = 'https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/';
const WEB_SUMMARY_CHUNK = 100;
const WEB_SUMMARY_ERROR_LOG_INTERVAL_MS = 60000;
const DEFAULT_WEB_SUMMARY_TTL_MS = 60000;
const DEFAULT_WEB_SUMMARY_GRACE_MS = 15000;

class StatusAnzeige extends DeadlockPresenceLogger {
  constructor(client, log, options = {}) {
    super(client, log, options);
    this.pollIntervalMs = this.resolveInterval(options.pollIntervalMs);
    this.running = false;
    this.pollTimer = null;
    this.nextPollDueAt = null;

    this.db = options.db || null;
    this.persistenceEnabled = Boolean(this.db);
    this.upsertPresenceStmt = null;
    this.lastPersistErrorAt = 0;
    this.latestPresence = new Map();
    this.voiceWatchStmt = null;
    this.steamWebApiKey =
      (options.steamWebApiKey ||
        process.env.STEAM_WEB_API_KEY ||
        process.env.STEAM_API_KEY ||
        '') +
      '';
    this.steamWebApiKey = this.steamWebApiKey.trim() || null;
    this.webApiTimeoutMs = Math.max(
      3000,
      Number.isFinite(Number(options.webApiTimeoutMs))
        ? Number(options.webApiTimeoutMs)
        : Number(process.env.STEAM_WEBAPI_TIMEOUT_MS || 12000)
    );
    const summaryTtlCandidate =
      Number.isFinite(Number(options.webSummaryCacheTtlMs))
        ? Number(options.webSummaryCacheTtlMs)
        : DEFAULT_WEB_SUMMARY_TTL_MS;
    this.webSummaryCacheTtlMs = Math.max(this.pollIntervalMs, summaryTtlCandidate);
    this.webSummaryGraceMs =
      Number.isFinite(Number(options.webSummaryGraceMs)) && Number(options.webSummaryGraceMs) > 0
        ? Number(options.webSummaryGraceMs)
        : DEFAULT_WEB_SUMMARY_GRACE_MS;
    this.webSummaryCache = new Map();
    this.lastWebApiErrorAt = 0;

    if (this.db && typeof this.db.exec === 'function') {
      try {
        this.db.exec(`
          CREATE TABLE IF NOT EXISTS deadlock_voice_watch(
            steam_id TEXT PRIMARY KEY,
            guild_id INTEGER,
            channel_id INTEGER,
            updated_at INTEGER NOT NULL
          )
        `);
      } catch (err) {
        this.log('warn', 'Failed to ensure deadlock_voice_watch table exists', {
          error: err && err.message ? err.message : String(err),
        });
      }
    }

    if (this.persistenceEnabled) {
      try {
        this.preparePersistence();
        this.persistenceEnabled = Boolean(this.upsertPresenceStmt);
      } catch (err) {
        this.persistenceEnabled = false;
        this.upsertPresenceStmt = null;
        this.log('warn', 'Statusanzeige persistence initialisation failed', {
          error: err && err.message ? err.message : String(err),
        });
      }
    } else {
      this.log('debug', 'Statusanzeige running without persistence database reference');
    }

    if (this.db && typeof this.db.prepare === 'function') {
      try {
        this.voiceWatchStmt = this.db.prepare(
          'SELECT steam_id FROM deadlock_voice_watch WHERE updated_at >= ?'
        );
      } catch (err) {
        this.voiceWatchStmt = null;
        this.log('warn', 'Failed to prepare voice watch lookup statement', {
          error: err && err.message ? err.message : String(err),
        });
      }
    }

    this.boundHandleDisconnected = this.handleDisconnected.bind(this);
  }

  resolveInterval(customInterval) {
    const envValue =
      optionsToNumber(customInterval) ??
      optionsToNumber(process.env.STEAM_STATUS_POLL_MS) ??
      optionsToNumber(process.env.STEAM_PRESENCE_POLL_MS) ??
      optionsToNumber(process.env.STEAM_STATUSANZEIGE_INTERVAL_MS);

    if (envValue === null || envValue === undefined) {
      return DEFAULT_INTERVAL_MS;
    }

    if (!Number.isFinite(envValue) || envValue < MIN_INTERVAL_MS) {
      return DEFAULT_INTERVAL_MS;
    }

    return envValue;
  }

  start() {
    if (this.running) return;
    this.running = true;
    super.start();
    this.client.on('disconnected', this.boundHandleDisconnected);
    this.scheduleNextPoll(true);
  }

  stop() {
    if (!this.running) return;
    this.running = false;
    this.client.removeListener('disconnected', this.boundHandleDisconnected);
    this.clearPollTimer();
    super.stop();
  }

  handleLoggedOn() {
    super.handleLoggedOn();
    this.scheduleNextPoll(true);
  }

  handleDisconnected() {
    this.clearPollTimer();
  }

  scheduleNextPoll(immediate = false) {
    this.clearPollTimer();
    if (!this.running) return;

    const delay = immediate ? Math.min(this.pollIntervalMs, 2000) : this.pollIntervalMs;
    this.nextPollDueAt = Date.now() + delay;

    this.pollTimer = setTimeout(() => {
      this.pollTimer = null;
      try {
        this.performSnapshot();
      } catch (err) {
        try {
          this.log('warn', 'Statusanzeige snapshot failed', {
            error: err && err.message ? err.message : String(err),
          });
        } catch (_) {}
      } finally {
        if (this.running) {
          this.scheduleNextPoll();
        }
      }
    }, delay);
  }

  clearPollTimer() {
    if (this.pollTimer) {
      clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }
    this.nextPollDueAt = null;
  }

  performSnapshot() {
    if (!this.running) return;
    const targetSteamIds = this.collectVoiceWatchSteamIds();
    if (!targetSteamIds.length) {
      this.log('debug', 'Statusanzeige snapshot skipped (no active voice members)');
      return;
    }

    this.log('debug', 'Statusanzeige snapshot started', {
      voiceCount: targetSteamIds.length,
      intervalMs: this.pollIntervalMs,
    });
    this.refreshServerSummaries(targetSteamIds);
    this.fetchPersonasAndRichPresence(targetSteamIds);
  }

  handleSnapshot(entry) {
    if (!entry || !entry.steamId) return;
    const steamId = String(entry.steamId);
    const summary = this.getServerSummary(steamId);
    const stageInfo = this.deriveStage(entry, summary);

    const record = {
      steamId,
      capturedAtMs: entry.capturedAtMs,
      inDeadlock: Boolean(entry.inDeadlock),
      playingAppID:
        typeof entry.playingAppID === 'number' && Number.isFinite(entry.playingAppID)
          ? entry.playingAppID
          : null,
      stage: stageInfo.stage,
      minutes: stageInfo.minutes,
      localized: this.normalizeLocalizedString(entry.localizedString),
      hero: this.normalizeHeroGuess(entry.heroGuess),
      partyHint: this.normalizePartyHint(entry.partyHint),
      serverId: summary && summary.serverId ? summary.serverId : null,
      lobbyId: summary && summary.lobbyId ? summary.lobbyId : null,
      serverIp: summary && summary.serverIp ? summary.serverIp : null,
    };

    this.latestPresence.set(steamId, record);

    if (!this.persistenceEnabled || !this.upsertPresenceStmt) {
      return;
    }

    try {
      const payload = this.buildDbPayload(record);
      this.upsertPresenceStmt.run(payload);
    } catch (err) {
      const now = Date.now();
      if (!this.lastPersistErrorAt || now - this.lastPersistErrorAt >= PERSIST_ERROR_LOG_INTERVAL_MS) {
        this.lastPersistErrorAt = now;
        this.log('warn', 'Statusanzeige failed to persist presence snapshot', {
          steamId,
          error: err && err.message ? err.message : String(err),
        });
      }
    }
  }

  collectFriendIds() {
    const ids = new Set();
    if (this.friendIds && this.friendIds.size) {
      this.friendIds.forEach((sid) => ids.add(sid));
    }
    if (this.client && this.client.myFriends) {
      Object.entries(this.client.myFriends).forEach(([sid, relation]) => {
        if (relation === SteamUser.EFriendRelationship.Friend) {
          ids.add(sid);
        }
      });
    }
    return Array.from(ids);
  }

  collectVoiceWatchSteamIds() {
    if (!this.voiceWatchStmt) {
      return [];
    }
    try {
      const cutoff = Math.floor(Date.now() / 1000) - VOICE_WATCH_MAX_AGE_SEC;
      const rows = this.voiceWatchStmt.all(cutoff);
      if (!rows || !rows.length) return [];
      const ids = new Set();
      rows.forEach((row) => {
        if (row && row.steam_id) {
          ids.add(String(row.steam_id));
        }
      });
      return Array.from(ids);
    } catch (err) {
      this.log('warn', 'Failed to load voice watch steam ids', {
        error: err && err.message ? err.message : String(err),
      });
      return [];
    }
  }

  refreshServerSummaries(steamIds) {
    if (!this.steamWebApiKey || !Array.isArray(steamIds) || !steamIds.length) {
      return;
    }
    const uniqueIds = Array.from(
      new Set(
        steamIds
          .map((sid) => (sid ? String(sid).trim() : ''))
          .filter((sid) => sid.length > 0)
      )
    );
    if (!uniqueIds.length) {
      return;
    }
    const now = Date.now();
    const stale = uniqueIds.filter((sid) => {
      const cached = this.webSummaryCache.get(sid);
      if (!cached) {
        return true;
      }
      return now - cached.cachedAt > this.webSummaryCacheTtlMs;
    });
    if (!stale.length) {
      return;
    }
    for (let idx = 0; idx < stale.length; idx += WEB_SUMMARY_CHUNK) {
      const chunk = stale.slice(idx, idx + WEB_SUMMARY_CHUNK);
      this.fetchSummaryChunk(chunk).catch((err) => {
        const shouldLog =
          !this.lastWebApiErrorAt ||
          Date.now() - this.lastWebApiErrorAt >= WEB_SUMMARY_ERROR_LOG_INTERVAL_MS;
        if (shouldLog) {
          this.lastWebApiErrorAt = Date.now();
          this.log('warn', 'Statusanzeige failed to refresh Steam Web summaries', {
            count: chunk.length,
            error: err && err.message ? err.message : String(err),
          });
        }
      });
    }
  }

  fetchSummaryChunk(ids) {
    if (!ids || !ids.length) {
      return Promise.resolve(0);
    }
    return new Promise((resolve, reject) => {
      const params = new URLSearchParams({
        key: this.steamWebApiKey,
        steamids: ids.join(','),
      });
      const url = `${PLAYER_SUMMARIES_ENDPOINT}?${params.toString()}`;
      const req = https.get(url, (res) => {
        let body = '';
        res.on('data', (chunk) => {
          body += chunk;
        });
        res.on('end', () => {
          if (res.statusCode !== 200) {
            reject(new Error(`Steam summaries HTTP ${res.statusCode}`));
            return;
          }
          try {
            const parsed = JSON.parse(body || '{}');
            const players =
              parsed && parsed.response && Array.isArray(parsed.response.players)
                ? parsed.response.players
                : [];
            players.forEach((player) => this.applyWebSummary(player));
            resolve(players.length);
          } catch (err) {
            reject(err);
          }
        });
      });
      req.on('error', reject);
      req.setTimeout(this.webApiTimeoutMs, () => {
        req.destroy(new Error('Steam summaries request timeout'));
      });
    });
  }

  applyWebSummary(row) {
    if (!row || !row.steamid) return;
    const steamId = String(row.steamid);
    this.webSummaryCache.set(steamId, {
      cachedAt: Date.now(),
      serverId: row.gameserversteamid ? String(row.gameserversteamid) : null,
      lobbyId: row.lobbysteamid ? String(row.lobbysteamid) : null,
      serverIp: row.gameserverip ? String(row.gameserverip) : null,
    });
  }

  getServerSummary(steamId) {
    if (!steamId) {
      return null;
    }
    const sid = String(steamId);
    const entry = this.webSummaryCache.get(sid);
    if (!entry) {
      return null;
    }
    const now = Date.now();
    if (now - entry.cachedAt > this.webSummaryCacheTtlMs + this.webSummaryGraceMs) {
      this.webSummaryCache.delete(sid);
      return null;
    }
    return entry;
  }

  preparePersistence() {
    if (!this.db || typeof this.db.prepare !== 'function') {
      throw new Error('Statusanzeige persistence requires a valid better-sqlite3 connection');
    }
    this.upsertPresenceStmt = this.db.prepare(`
      INSERT INTO live_player_state (
        steam_id,
        last_gameid,
        last_server_id,
        last_seen_ts,
        in_deadlock_now,
        in_match_now_strict,
        deadlock_stage,
        deadlock_minutes,
        deadlock_localized,
        deadlock_hero,
        deadlock_party_hint,
        deadlock_updated_at
      ) VALUES (
        @steamId,
        @lastGameId,
        @lastServerId,
        @lastSeenTs,
        @inDeadlockNow,
        @inMatchNowStrict,
        @deadlockStage,
        @deadlockMinutes,
        @deadlockLocalized,
        @deadlockHero,
        @deadlockPartyHint,
        @deadlockUpdatedAt
      )
      ON CONFLICT(steam_id) DO UPDATE SET
        last_gameid = excluded.last_gameid,
        last_server_id = excluded.last_server_id,
        last_seen_ts = excluded.last_seen_ts,
        in_deadlock_now = excluded.in_deadlock_now,
        in_match_now_strict = excluded.in_match_now_strict,
        deadlock_stage = excluded.deadlock_stage,
        deadlock_minutes = excluded.deadlock_minutes,
        deadlock_localized = excluded.deadlock_localized,
        deadlock_hero = excluded.deadlock_hero,
        deadlock_party_hint = excluded.deadlock_party_hint,
        deadlock_updated_at = excluded.deadlock_updated_at
    `);
  }

  buildDbPayload(record) {
    const unixSeconds = Math.floor(record.capturedAtMs / 1000);
    let minutesValue = null;
    if (Number.isFinite(record.minutes)) {
      const bounded = Math.max(0, Math.min(MAX_MATCH_MINUTES, Math.round(record.minutes)));
      minutesValue = bounded;
    }

    return {
      steamId: record.steamId,
      lastGameId:
        record.playingAppID !== null && record.playingAppID !== undefined
          ? String(record.playingAppID)
          : null,
      lastServerId: record.serverId || record.partyHint,
      lastSeenTs: unixSeconds,
      inDeadlockNow: record.inDeadlock ? 1 : 0,
      inMatchNowStrict: record.stage === 'match' ? 1 : 0,
      deadlockStage: record.stage,
      deadlockMinutes: minutesValue,
      deadlockLocalized: record.localized,
      deadlockHero: record.hero,
      deadlockPartyHint: record.partyHint || record.lobbyId || null,
      deadlockUpdatedAt: unixSeconds,
    };
  }

  deriveStage(entry, summary = null) {
    if (!entry) {
      return { stage: 'offline', minutes: null };
    }
    const hasServerId = Boolean(summary && summary.serverId);
    const inDeadlock = Boolean(entry.inDeadlock || hasServerId);
    if (!inDeadlock) {
      return { stage: 'offline', minutes: null };
    }
    const localizedRaw = entry.localizedString ? String(entry.localizedString) : '';
    const localized = localizedRaw.toLowerCase();
    const minutes = Number.isFinite(entry.minutes) ? entry.minutes : null;
    const normalizedMinutes =
      minutes !== null ? Math.max(0, Math.min(MAX_MATCH_MINUTES, Math.round(minutes))) : null;

    const hero = entry.heroGuess ? String(entry.heroGuess).trim() : '';
    const hasDeadlockToken =
      localized.includes('{deadlock:}') || /\{deadlock[^}]*\}/i.test(localizedRaw);
    const hasBraceHero = /\{deadlock[^}]*\}\s*\{[^{}]+\}/i.test(localizedRaw);
    const matchIndicators = hasServerId || (hasDeadlockToken && (hero.length > 0 || hasBraceHero));

    if (matchIndicators) {
      return { stage: 'match', minutes: normalizedMinutes ?? 0 };
    }

    return { stage: 'lobby', minutes: normalizedMinutes };
  }

  normalizeLocalizedString(value) {
    if (!value) return null;
    const str = String(value).replace(/\s+/g, ' ').trim();
    return str.length ? str : null;
  }

  normalizeHeroGuess(value) {
    if (!value) return null;
    const str = String(value).trim();
    return str.length ? str : null;
  }

  normalizePartyHint(value) {
    if (!value) return null;
    const str = String(value).trim();
    return str.length ? str : null;
  }

  getPresenceSummary(steamId) {
    if (!steamId) return null;
    return this.latestPresence.get(String(steamId)) || null;
  }
}

function optionsToNumber(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number') return value;
  if (typeof value === 'string' && value.trim().length > 0) {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

module.exports = { StatusAnzeige };
