'use strict';

const SteamUser = require('steam-user');
const { DeadlockPresenceLogger } = require('./deadlock_presence_logger');

const DEFAULT_INTERVAL_MS = 60000;
const MIN_INTERVAL_MS = 10000;

class StatusAnzeige extends DeadlockPresenceLogger {
  constructor(client, log, options = {}) {
    super(client, log, options);
    this.pollIntervalMs = this.resolveInterval(options.pollIntervalMs);
    this.running = false;
    this.pollTimer = null;
    this.nextPollDueAt = null;

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

