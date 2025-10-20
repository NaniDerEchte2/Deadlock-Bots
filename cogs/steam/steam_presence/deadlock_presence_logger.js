'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
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

    const outputDir = options.outputDir
      ? path.resolve(options.outputDir)
      : path.resolve(process.env.STEAM_PRESENCE_LOG_DIR || path.join(os.homedir(), 'Documents', 'Deadlock', 'logs'));
    const fileName = options.fileName || 'deadlock_presence_log.csv';
    this.csvPath = options.csvPath ? path.resolve(options.csvPath) : path.join(outputDir, fileName);
    this.ensureDir(path.dirname(this.csvPath));

    this.sessionStart = new Map();
    this.friendIds = new Set();
    this.started = false;
    this.headerWritten = false;
    this.batchRows = [];
    this.batchTimer = null;

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
    this.flushBatch(true);
  }

  handleLoggedOn() {
    this.log('debug', 'Presence logger received loggedOn');
    this.sessionStart.clear();
    this.friendIds.clear();
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
    if (!allFriends.length) return;
    this.fetchPersonasAndRichPresence(allFriends);
  }

  handleUser(steamID) {
    const sid = this.toSteamId(steamID);
    if (!sid) return;
    if (this.friendIds.size && !this.friendIds.has(sid)) return;
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

  fetchPersonasAndRichPresence(ids) {
    const steamIds = Array.from(new Set(ids.map((sid) => this.toSteamId(sid)).filter(Boolean)));
    if (!steamIds.length) return;
    if (!this.isClientReady()) {
      this.log('debug', 'Presence logger skipped fetch (client not ready)', { count: steamIds.length });
      return;
    }

    try {
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

    const row = [
      capturedIso,
      steamId,
      this.toCsvSafe(name),
      Number.isFinite(playingAppID) ? playingAppID : '',
      inDeadlock ? 'true' : 'false',
      this.toCsvSafe(localizedString),
      this.toCsvSafe(heroGuess),
      Number.isFinite(minutes) ? minutes : '',
      this.toCsvSafe(partyHint),
      this.toCsvSafe(richPresence),
    ];

    this.pushCsvRow(row);
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
    const match = String(localizedString).match(/:\s*([A-Za-zÀ-ÿ0-9 _\-]+)\s*\(/);
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

  ensureDir(dirPath) {
    try {
      fs.mkdirSync(dirPath, { recursive: true });
    } catch (err) {
      if (!err || err.code !== 'EEXIST') {
        throw err;
      }
    }
  }

  ensureCsvHeader() {
    if (this.headerWritten && fs.existsSync(this.csvPath)) {
      return;
    }
    if (!fs.existsSync(this.csvPath)) {
      const header = [
        'timestamp_iso',
        'steamid64',
        'name',
        'playing_appid',
        'deadlock',
        'localized_string',
        'hero_guess',
        'minutes',
        'party_hint',
        'raw_rich_presence_json',
      ].join(',');
      fs.writeFileSync(this.csvPath, `${header}\n`, 'utf8');
    }
    this.headerWritten = true;
  }

  flushBatch(immediate = false) {
    if (this.batchRows.length === 0) {
      if (immediate && this.batchTimer) {
        clearTimeout(this.batchTimer);
        this.batchTimer = null;
      }
      return;
    }
    try {
      this.ensureCsvHeader();
      fs.appendFileSync(this.csvPath, `${this.batchRows.join('\n')}\n`, 'utf8');
    } catch (err) {
      this.log('warn', 'Failed to append presence rows to CSV', { error: err.message || String(err) });
    } finally {
      this.batchRows = [];
      if (this.batchTimer) {
        clearTimeout(this.batchTimer);
        this.batchTimer = null;
      }
    }
  }

  flushBatchSoon() {
    if (this.batchTimer) return;
    this.batchTimer = setTimeout(() => this.flushBatch(), 1500);
  }

  pushCsvRow(row) {
    this.ensureCsvHeader();
    this.batchRows.push(row.join(','));
    this.flushBatchSoon();
  }

  toCsvSafe(value) {
    if (value === null || value === undefined || value === '') {
      return '""';
    }
    const str = typeof value === 'string' ? value : JSON.stringify(value);
    const escaped = str.replace(/"/g, '""');
    return `"${escaped}"`;
  }
}

module.exports = { DeadlockPresenceLogger };
