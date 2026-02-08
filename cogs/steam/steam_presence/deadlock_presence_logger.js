'use strict';

const SteamUser = require('steam-user');

class DeadlockPresenceLogger {
  constructor(client, log, options = {}) {
    this.client = client;
    this.log = typeof log === 'function' ? log : () => {};

    this.appId = Number.parseInt(options.appId || process.env.DEADLOCK_APPID || '1422450', 10);
    if (!Number.isFinite(this.appId)) {
      this.appId = 1422450;
    }

    this.language = options.language || process.env.STEAM_PRESENCE_LANGUAGE || 'german';
    if (this.language) {
      try {
        this.client.setOption('language', this.language);
      } catch (err) {
        this.log('warn', 'Failed to set Steam language option', { error: err.message || String(err) });
      }
    }

    this.sessionStart = new Map();
    this.friendIds = new Set();
    this.started = false;
    this.initialLoadComplete = false;

    // Rich presence batching config to avoid Steam client timeouts
    // Ultra-conservative defaults: 3 friends per batch, 1000ms delay between batches
    // Each batch takes ~3-4 seconds to process (getPersonas + richPresence + wait time)
    this.richPresenceBatchSize = Number.parseInt(options.richPresenceBatchSize || process.env.RICH_PRESENCE_BATCH_SIZE || '3', 10);
    this.richPresenceBatchDelayMs = Number.parseInt(options.richPresenceBatchDelayMs || process.env.RICH_PRESENCE_BATCH_DELAY_MS || '1000', 10);

    if (this.richPresenceBatchSize < 1) this.richPresenceBatchSize = 3;
    if (this.richPresenceBatchDelayMs < 100) this.richPresenceBatchDelayMs = 1000;

    this.handlers = {
      loggedOn: this.handleLoggedOn.bind(this),
      friendsList: this.handleFriendsList.bind(this),
      user: this.handleUser.bind(this),
      friendRelationship: this.handleRelationship.bind(this),
      richPresence: this.handleRichPresencePush.bind(this),
    };
  }

  start() {
    if (this.started) return;
    this.started = true;
    this.client.on('loggedOn', this.handlers.loggedOn);
    this.client.on('friendsList', this.handlers.friendsList);
    this.client.on('user', this.handlers.user);
    this.client.on('friendRelationship', this.handlers.friendRelationship);
    this.client.on('richPresence', this.handlers.richPresence);
  }

  stop() {
    if (!this.started) return;
    this.started = false;
    this.client.removeListener('loggedOn', this.handlers.loggedOn);
    this.client.removeListener('friendsList', this.handlers.friendsList);
    this.client.removeListener('user', this.handlers.user);
    this.client.removeListener('friendRelationship', this.handlers.friendRelationship);
    this.client.removeListener('richPresence', this.handlers.richPresence);
  }

  handleLoggedOn() {
    this.log('debug', 'Presence logger received loggedOn');
    this.sessionStart.clear();
    this.friendIds.clear();
    this.initialLoadComplete = false;
  }

  handleRelationship(steamID, relationship) {
    const sid = this.toSteamId(steamID);
    if (!sid) return;
    if (relationship === SteamUser.EFriendRelationship.Friend) {
      this.friendIds.add(sid);
      this.fetchPersonasAndRichPresence([sid]);
    } else {
      this.friendIds.delete(sid);
      this.sessionStart.delete(sid);
    }
  }

  handleFriendsList() {
    const allFriends = Object.keys(this.client.myFriends || {}).filter((sid) => {
      return this.client.myFriends[sid] === SteamUser.EFriendRelationship.Friend;
    });
    this.friendIds = new Set(allFriends);
    if (!allFriends.length) {
      this.initialLoadComplete = true;
      return;
    }

    // Batch friends to avoid Steam client timeouts with large friend lists
    this.log('info', 'Loading friend presence data', {
      total_friends: allFriends.length,
      batch_size: this.richPresenceBatchSize,
      batch_delay_ms: this.richPresenceBatchDelayMs,
      estimated_duration_sec: Math.ceil(allFriends.length / this.richPresenceBatchSize) * (this.richPresenceBatchDelayMs / 1000)
    });

    // Wait 15 seconds after friendsList event before starting batch processing
    // This gives the Steam client time to:
    // - Complete Friend/DB sync (~8-9 seconds)
    // - Establish stable GC connection
    // - Initialize internal request queues (~5-6 seconds additional)
    // Testing shows first batch had timeouts with 10s delay, but batch 2+ worked perfectly
    setTimeout(() => {
      this.log('info', 'Starting friend presence batch processing');
      this.fetchPersonasAndRichPresenceBatched(allFriends);
    }, 15000);
  }

  handleUser(steamID) {
    const sid = this.toSteamId(steamID);
    if (!sid) return;
    if (this.friendIds.size && !this.friendIds.has(sid)) return;

    // Skip individual fetches during initial batch load to prevent 109 simultaneous requests
    if (!this.initialLoadComplete) {
      this.log('debug', 'Skipping handleUser fetch during initial load', { steamId: sid });
      return;
    }

    this.fetchPersonasAndRichPresence([sid]);
  }

  handleRichPresencePush(steamID, appID, richPresence) {
    const sid = this.toSteamId(steamID);
    if (!sid || Number(appID) !== this.appId) return;
    const persona = this.client.users && this.client.users[sid] ? this.client.users[sid] : null;
    const localizedString = persona && persona.rich_presence_string ? String(persona.rich_presence_string) : null;
    const pushRichObj = {
      richPresence: richPresence && typeof richPresence === 'object' ? richPresence : {},
      localizedString,
    };
    this.writeSnapshotForUser(sid, persona, pushRichObj);
    this.fetchAndWriteRichPresence([sid]);
  }

  async fetchPersonasAndRichPresenceBatched(ids) {
    const steamIds = Array.from(new Set(ids.map((sid) => this.toSteamId(sid)).filter(Boolean)));
    if (!steamIds.length) return;

    // Split into batches to avoid Steam client timeouts
    const batches = [];
    for (let i = 0; i < steamIds.length; i += this.richPresenceBatchSize) {
      batches.push(steamIds.slice(i, i + this.richPresenceBatchSize));
    }

    // Process batches sequentially - wait for each to complete before starting next
    for (let i = 0; i < batches.length; i++) {
      const batch = batches[i];
      this.log('info', 'Processing friend presence batch', {
        batch: i + 1,
        total_batches: batches.length,
        batch_size: batch.length
      });

      // Call and WAIT for completion before next batch
      await this.fetchPersonasAndRichPresenceAsync(batch);

      // Delay before next batch (except for last one)
      if (i < batches.length - 1) {
        await new Promise(resolve => setTimeout(resolve, this.richPresenceBatchDelayMs));
      }
    }

    this.log('info', 'Friend presence batch processing completed', {
      total_batches: batches.length,
      total_friends: steamIds.length
    });

    // Mark initial load as complete - now handleUser can respond to real-time updates
    this.initialLoadComplete = true;
    this.log('info', 'Initial friend load complete - real-time presence updates enabled');
  }

  fetchPersonasAndRichPresenceAsync(ids) {
    return new Promise((resolve) => {
      const steamIds = Array.from(new Set(ids.map((sid) => this.toSteamId(sid)).filter(Boolean)));
      if (!steamIds.length) {
        resolve();
        return;
      }

      if (!this.isClientReady()) {
        this.log('debug', 'Presence logger skipped fetch (client not ready)', { count: steamIds.length });
        resolve();
        return;
      }

      try {
        this.log('debug', 'Requesting personas for presence snapshot', { count: steamIds.length });
        this.client.getPersonas(steamIds, (err) => {
          if (err) {
            this.log('warn', 'getPersonas failed', { error: err.message || String(err) });
            // Wait a bit before continuing on error to avoid hammering the client
            setTimeout(resolve, 1000);
            return;
          }

          // Now request rich presence data
          this.fetchAndWriteRichPresence(steamIds);

          // Wait 3 seconds for rich presence requests to complete
          // Rich presence requests can take up to 10s to timeout, so we need adequate wait time
          setTimeout(resolve, 3000);
        });
      } catch (err) {
        this.log('warn', 'getPersonas threw', { error: err.message || String(err) });
        setTimeout(resolve, 1000);
      }
    });
  }

  fetchPersonasAndRichPresence(ids) {
    const steamIds = Array.from(new Set(ids.map((sid) => this.toSteamId(sid)).filter(Boolean)));
    if (!steamIds.length) return;
    if (!this.isClientReady()) {
      this.log('debug', 'Presence logger skipped fetch (client not ready)', { count: steamIds.length });
      return;
    }

    try {
      this.log('debug', 'Requesting personas for presence snapshot', { count: steamIds.length });
      this.client.getPersonas(steamIds, (err) => {
        if (err) {
          this.log('warn', 'getPersonas failed', { error: err.message || String(err) });
          return;
        }
        this.fetchAndWriteRichPresence(steamIds);
      });
    } catch (err) {
      this.log('warn', 'getPersonas threw', { error: err.message || String(err) });
    }
  }

  fetchAndWriteRichPresence(ids) {
    if (!ids.length) return;
    try {
      this.log('debug', 'Fetching Deadlock rich presence', { count: ids.length });
      const args = [this.appId, ids];
      if (this.language) {
        args.push(this.language);
      }
      args.push((err, resp) => {
        if (err) {
          this.log('warn', 'requestRichPresence failed', { error: err.message || String(err) });
          return;
        }
        const users = (resp && resp.users) ? resp.users : {};
        const received = Object.keys(users).length;
        if (!received) {
          const inDeadlockCount = ids.reduce((count, sid) => {
            const persona = (this.client.users && this.client.users[sid]) ? this.client.users[sid] : null;
            const playingAppId = this.toInt(persona && (persona.gameid || persona.game_id));
            return playingAppId === this.appId ? count + 1 : count;
          }, 0);
          this.log('debug', 'No Deadlock rich presence returned', {
            requested: ids.length,
            in_deadlock: inDeadlockCount,
          });
        }
        ids.forEach((sid) => {
          const persona = (this.client.users && this.client.users[sid]) ? this.client.users[sid] : null;
          const rich = users[sid] || null;
          this.writeSnapshotForUser(sid, persona, rich);
        });
      });
      this.client.requestRichPresence(...args);
    } catch (err) {
      this.log('warn', 'requestRichPresence threw', { error: err.message || String(err) });
    }
  }

  writeSnapshotForUser(steamId, persona, richObj) {
    const capturedAtMs = Date.now();
    const capturedIso = new Date(capturedAtMs).toISOString();

    const name = persona && (persona.player_name || persona.name || persona.persona_name || persona.personaName) || null;
    const playingAppID = this.toInt(persona && (persona.gameid || persona.game_id));
    const inDeadlock = playingAppID === this.appId;

    const localizedString = richObj && richObj.localizedString
      ? String(richObj.localizedString)
      : (persona && persona.rich_presence_string ? String(persona.rich_presence_string) : null);

    if (inDeadlock && !this.sessionStart.has(steamId)) {
      this.sessionStart.set(steamId, capturedAtMs);
    } else if (!inDeadlock) {
      this.sessionStart.delete(steamId);
    }

    const richPresence = (richObj && typeof richObj.richPresence === 'object' && richObj.richPresence) ? richObj.richPresence : {};
    const hasRichPresenceData = richPresence && Object.keys(richPresence).length > 0;
    const hasLocalizedString = typeof localizedString === 'string' && localizedString.length > 0;

    if (!inDeadlock && !hasRichPresenceData && !hasLocalizedString) {
      return;
    }

    const heroGuess = this.guessHero(localizedString);
    const minutes = this.computeMinutes(richPresence, steamId, capturedAtMs);
    const partyHint = this.extractPartyHint(richPresence);

    const entry = {
      capturedAtMs,
      capturedIso,
      steamId,
      name: name || null,
      playingAppID: Number.isFinite(playingAppID) ? playingAppID : null,
      inDeadlock,
      localizedString: localizedString || null,
      heroGuess: heroGuess || null,
      minutes: Number.isFinite(minutes) ? minutes : null,
      partyHint: partyHint || null,
      richPresence,
    };

    try {
      this.handleSnapshot(entry);
    } catch (err) {
      this.log('warn', 'Deadlock snapshot handler threw', {
        steamId,
        error: err && err.message ? err.message : String(err),
      });
    }

    return entry;
  }

  computeMinutes(rp, steamId, capturedAtMs) {
    if (rp && typeof rp.time === 'string') {
      const num = Number.parseFloat(rp.time);
      if (Number.isFinite(num)) {
        const minutes = Math.round(num / 60);
        if (minutes >= 0 && minutes < 24 * 60) {
          return minutes;
        }
      }
    }
    if (this.sessionStart.has(steamId)) {
      const startMs = this.sessionStart.get(steamId);
      const diffMin = Math.floor((capturedAtMs - startMs) / 60000);
      return diffMin >= 0 ? diffMin : 0;
    }
    return null;
  }

  guessHero(localizedString) {
    if (!localizedString) return null;
    const str = String(localizedString);
    const braceMatches = Array.from(str.matchAll(/\{([^{}]+)\}/g))
      .map((m) => (m[1] ? m[1].trim() : ''))
      .filter((token) => token.length > 0);
    if (braceMatches.length >= 2 && /deadlock/i.test(braceMatches[0])) {
      return braceMatches[1];
    }
    const heroColonMatch = str.match(/deadlock[^:]*:\s*([A-Za-z\u00C0-\u024F0-9 _\-]+)\s*\(/i);
    if (heroColonMatch) {
      return heroColonMatch[1].trim();
    }
    const match = str.match(/:\s*([A-Za-z\u00C0-\u024F0-9 _\-]+)\s*\(/);
    return match ? match[1].trim() : null;
  }

  extractPartyHint(rp) {
    if (!rp || typeof rp !== 'object') return null;
    const candidate = rp.party_id || rp.party || rp.lobby || rp.connect || null;
    return candidate ? String(candidate) : null;
  }

  toSteamId(id) {
    if (!id) return null;
    if (typeof id === 'string') return id;
    try {
      if (typeof id.getSteamID64 === 'function') {
        return id.getSteamID64();
      }
    } catch {}
    return String(id);
  }

  toInt(value) {
    if (value === null || value === undefined || value === '') return null;
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  isClientReady() {
    if (!this.client) return false;
    try {
      return Boolean(this.client.steamID && typeof this.client.steamID.isValid === 'function' && this.client.steamID.isValid());
    } catch (err) {
      return false;
    }
  }

  handleSnapshot(/* entry */) {
    // Intentionally empty – subclasses can persist or forward snapshots.
  }
}

module.exports = { DeadlockPresenceLogger };
