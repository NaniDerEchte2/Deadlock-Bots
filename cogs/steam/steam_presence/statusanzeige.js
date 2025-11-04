'use strict';

const SteamUser = require('steam-user');
const { DeadlockPresenceLogger } = require('./deadlock_presence_logger');

const DEFAULT_INTERVAL_MS = 60000;
const MIN_INTERVAL_MS = 10000;
const PERSIST_ERROR_LOG_INTERVAL_MS = 60000;
const MAX_MATCH_MINUTES = 24 * 60;

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
    const friendIds = this.collectFriendIds();
    if (!friendIds.length) {
      this.log('debug', 'Statusanzeige snapshot skipped (no known friends)');
      return;
    }

    this.log('debug', 'Statusanzeige snapshot started', {
      friendCount: friendIds.length,
      intervalMs: this.pollIntervalMs,
    });
    this.fetchPersonasAndRichPresence(friendIds);
  }

  handleSnapshot(entry) {
    if (!entry || !entry.steamId) return;
    const steamId = String(entry.steamId);
    const stageInfo = this.deriveStage(entry);

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
      lastServerId: record.partyHint,
      lastSeenTs: unixSeconds,
      inDeadlockNow: record.inDeadlock ? 1 : 0,
      inMatchNowStrict: record.stage === 'match' ? 1 : 0,
      deadlockStage: record.stage,
      deadlockMinutes: minutesValue,
      deadlockLocalized: record.localized,
      deadlockHero: record.hero,
      deadlockPartyHint: record.partyHint,
      deadlockUpdatedAt: unixSeconds,
    };
  }

  deriveStage(entry) {
    if (!entry || !entry.inDeadlock) {
      return { stage: 'offline', minutes: null };
    }
    const localized = entry.localizedString ? String(entry.localizedString).toLowerCase() : '';
    const minutes = Number.isFinite(entry.minutes) ? entry.minutes : null;

    if (localized.includes('lobby')) {
      return { stage: 'lobby', minutes: minutes !== null ? minutes : 0 };
    }

    if (localized.includes('min')) {
      return { stage: 'match', minutes: minutes !== null ? minutes : 0 };
    }

    if (minutes !== null && minutes > 0) {
      return { stage: 'match', minutes };
    }

    return { stage: 'unknown', minutes };
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
