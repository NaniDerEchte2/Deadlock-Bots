'use strict';

/**
 * Quick Invite Utility for Steam Bridge
 * - Erzeugt Quick-Invite-Links über steam-user.
 * - Verwaltet einen kleinen Pool in SQLite.
 * - Automatisches "Ensure": hält stets >= target (Default 1) Links 'available'.
 * - KEIN Fallback über Websession: wenn steam-user es nicht kann -> Fehler.
 *
 * Defaults:
 *   - inviteLimit: 1
 *   - inviteDuration: null (kein Ablauf)
 *   - poolTarget: 1 (mind. 1 verfügbar)
 *   - autoEnsure: true (Intervall gesteuert)
 *   - autoEnsureIntervalMs: 30000 ms (30s)
 */

const DEFAULT_INVITE_LIMIT = Number(process.env.STEAM_INVITE_LIMIT ?? 1);
const DEFAULT_INVITE_DURATION =
  process.env.STEAM_INVITE_DURATION === 'null'
    ? null
    : (process.env.STEAM_INVITE_DURATION ? Number(process.env.STEAM_INVITE_DURATION) : null); // null = kein Ablauf
const DEFAULT_POOL_MIN_AVAILABLE_RAW = process.env.STEAM_INVITE_POOL_MIN_AVAILABLE;
const DEFAULT_POOL_MIN_AVAILABLE = Math.max(
  1,
  Number.isFinite(Number(DEFAULT_POOL_MIN_AVAILABLE_RAW))
    ? Number(DEFAULT_POOL_MIN_AVAILABLE_RAW)
    : 2
);

const DEFAULT_POOL_REFILL_COUNT_RAW = process.env.STEAM_INVITE_POOL_REFILL_COUNT;
const DEFAULT_POOL_REFILL_COUNT = Math.max(
  0,
  Number.isFinite(Number(DEFAULT_POOL_REFILL_COUNT_RAW))
    ? Number(DEFAULT_POOL_REFILL_COUNT_RAW)
    : 5
);

const DEFAULT_POOL_TARGET_RAW = process.env.STEAM_INVITE_POOL_TARGET;
const DEFAULT_POOL_TARGET = Math.max(
  1,
  Number.isFinite(Number(DEFAULT_POOL_TARGET_RAW))
    ? Number(DEFAULT_POOL_TARGET_RAW)
    : DEFAULT_POOL_MIN_AVAILABLE
);

const DEFAULT_POOL_REFILL_TARGET_RAW = process.env.STEAM_INVITE_POOL_REFILL_TARGET;
const DEFAULT_POOL_REFILL_TARGET = Math.max(
  DEFAULT_POOL_TARGET,
  Number.isFinite(Number(DEFAULT_POOL_REFILL_TARGET_RAW))
    ? Number(DEFAULT_POOL_REFILL_TARGET_RAW)
    : (DEFAULT_POOL_MIN_AVAILABLE + DEFAULT_POOL_REFILL_COUNT)
);

const DEFAULT_AUTO_ENSURE = String(process.env.STEAM_INVITE_AUTO_ENSURE ?? 'true').toLowerCase() !== 'false';
const DEFAULT_AUTO_ENSURE_MS = Number(process.env.STEAM_INVITE_AUTO_ENSURE_MS ?? 30000);

const URL_REGEX = /^https?:\/\/\S+$/i;
const TIMEOUT_REGEX = /timed out/i;

const nowSec = () => Math.floor(Date.now() / 1000);

function parseInviteLink(link) {
  try {
    const url = new URL(String(link));
    // Erwartete Pfade: /p/<code>[/<token>]
    const parts = url.pathname.split('/').filter(Boolean); // ["p","<code>","<token>?"]
    const code = parts[1] || null;
    const token = parts.length >= 3 ? parts[2] : null;
    return { code, token };
  } catch {
    return { code: null, token: null };
  }
}

/**
 * Sucht rekursiv die erste HTTP/HTTPS-URL in einem beliebig strukturierten Objekt.
 */
function findFirstUrlString(value, maxDepth = 4) {
  try {
    if (value == null) return null;

    if (typeof value === 'string') {
      const trimmed = value.trim();
      if (URL_REGEX.test(trimmed)) return trimmed;
      const m = trimmed.match(/https?:\/\/\S+/i);
      if (m) return m[0];
      return null;
    }

    if (typeof value === 'object' && typeof value.href === 'string' && URL_REGEX.test(value.href)) {
      return value.href;
    }

    if (maxDepth <= 0) return null;

    if (Array.isArray(value)) {
      for (const item of value) {
        const found = findFirstUrlString(item, maxDepth - 1);
        if (found) return found;
      }
      return null;
    }

    if (typeof value === 'object') {
      const prioritizedKeys = ['invite_url', 'inviteUrl', 'url', 'link', 'href'];
      for (const k of prioritizedKeys) {
        if (Object.prototype.hasOwnProperty.call(value, k)) {
          const found = findFirstUrlString(value[k], maxDepth - 1);
          if (found) return found;
        }
      }
      for (const [k, v] of Object.entries(value)) {
        if (prioritizedKeys.includes(k)) continue;
        const found = findFirstUrlString(v, maxDepth - 1);
        if (found) return found;
      }
    }

    return null;
  } catch {
    return null;
  }
}

function normalizeSteamToken(rawToken, fallbackTimestamp) {
  if (!rawToken) return null;

  const tokenData =
    rawToken && typeof rawToken === 'object' && rawToken.token && typeof rawToken.token === 'object'
      ? rawToken.token
      : rawToken;

  if (!tokenData || typeof tokenData !== 'object') return null;

  const inviteLink = typeof tokenData.invite_link === 'string' ? tokenData.invite_link.trim() : '';
  const inviteToken = tokenData.invite_token ?? tokenData.token;
  if (!inviteLink || !inviteToken) return null;

  const token = String(inviteToken).trim();
  if (!token) return null;

  const limitRaw = Number(tokenData.invite_limit);
  const invite_limit = Number.isFinite(limitRaw) && limitRaw > 0 ? Math.floor(limitRaw) : 1;

  let invite_duration = null;
  if (Object.prototype.hasOwnProperty.call(tokenData, 'invite_duration') && tokenData.invite_duration !== null) {
    const durationRaw = Number(tokenData.invite_duration);
    invite_duration = Number.isFinite(durationRaw) && durationRaw >= 0 ? Math.floor(durationRaw) : null;
  }

  let created_at = fallbackTimestamp;
  const timeCreated = tokenData.time_created;
  if (timeCreated instanceof Date && !Number.isNaN(timeCreated.getTime())) {
    created_at = Math.floor(timeCreated.getTime() / 1000);
  } else if (typeof timeCreated === 'number' && Number.isFinite(timeCreated)) {
    created_at = Math.floor(timeCreated);
  }

  let expires_at = null;
  if (invite_duration !== null) {
    if (timeCreated instanceof Date && !Number.isNaN(timeCreated.getTime())) {
      expires_at = Math.floor(timeCreated.getTime() / 1000) + invite_duration;
    } else {
      expires_at = fallbackTimestamp + invite_duration;
    }
  }

  return {
    token,
    invite_link: inviteLink,
    invite_limit,
    invite_duration,
    created_at,
    expires_at,
    valid: tokenData.valid !== false,
  };
}

class QuickInvites {
  /**
   * Synchronisiert den lokalen Pool mit dem aktuellen Zustand bei Steam.
   * @param {object} [opts]
   * @param {number} [opts.maxRevoke]
   * @returns {Promise<{tokens:number, inserted:number, updated:number, status_updated:number, revoked:number, error?:string}>}
   */
  async syncFromSteam(opts = {}) {
    if (this._syncPromise) {
      try {
        return await this._syncPromise;
      } catch {
        // ignore and proceed with fresh sync
      }
    }
    this._syncPromise = (async () => {
      try {
        return await this._syncFromSteamImpl(opts);
      } finally {
        this._syncPromise = null;
      }
    })();
    return this._syncPromise;
  }

  async _listQuickInviteTokens() {
    if (typeof this.client.listQuickInviteLinks !== 'function') {
      return [];
    }
    return new Promise((resolve, reject) => {
      this.client.listQuickInviteLinks((err, res) => {
        if (err) return reject(err);
        const tokens = res && Array.isArray(res.tokens) ? res.tokens : [];
        resolve(tokens);
      });
    });
  }

  async _revokeQuickInviteToken(token) {
    if (typeof this.client.revokeQuickInviteLink !== 'function') return false;
    return new Promise((resolve, reject) => {
      this.client.revokeQuickInviteLink(token, (err) => {
        if (err) return reject(err);
        resolve(true);
      });
    });
  }

  async _syncFromSteamImpl(opts = {}) {
    let steamTokens;
    try {
      steamTokens = await this._listQuickInviteTokens();
    } catch (err) {
      this.log('warn', 'Failed to fetch quick invites from Steam', { error: err.message });
      return { tokens: 0, inserted: 0, updated: 0, status_updated: 0, revoked: 0, error: err.message };
    }

    const now = nowSec();
    const normalized = [];
    for (const token of steamTokens) {
      const normalizedToken = normalizeSteamToken(token, now);
      if (normalizedToken) normalized.push(normalizedToken);
    }

    const seenTokens = new Set();
    const existing = new Map();
    try {
      for (const row of this.selectTokenStatuses.all()) {
        if (!row || !row.token) continue;
        existing.set(String(row.token), { status: row.status || 'available' });
      }
    } catch (err) {
      this.log('warn', 'Failed to read existing quick invite snapshot', { error: err.message });
    }

    let inserted = 0;
    let metaUpdated = 0;
    let statusUpdated = 0;
    let revoked = 0;
    const revokeQueue = new Set();

    const tx = this.db.transaction((records) => {
      for (const entry of records) {
        const payload = {
          token: entry.token,
          invite_link: entry.invite_link,
          invite_limit: entry.invite_limit,
          invite_duration: entry.invite_duration,
          created_at: entry.created_at || now,
          expires_at: entry.expires_at,
          last_seen: now,
        };

        seenTokens.add(entry.token);
        const existingRow = existing.get(entry.token);

        if (!existingRow) {
          this.insertInvite.run({ ...payload, status: entry.valid ? 'available' : 'used' });
          inserted += 1;
          if (!entry.valid) revokeQueue.add(entry.token);
          continue;
        }

        this.updateInviteMeta.run(payload);
        metaUpdated += 1;

        if (!entry.valid && existingRow.status !== 'used' && existingRow.status !== 'revoked') {
          this.updateInviteStatus.run({ token: entry.token, status: 'used', last_seen: now });
          statusUpdated += 1;
          revokeQueue.add(entry.token);
        } else if (entry.valid && existingRow.status === 'invalid') {
          this.updateInviteStatus.run({ token: entry.token, status: 'available', last_seen: now });
          statusUpdated += 1;
        }
      }
    });

    try {
      tx(normalized);
    } catch (err) {
      this.log('warn', 'Quick invite sync transaction failed', { error: err.message });
    }

    for (const [token, info] of existing.entries()) {
      if (seenTokens.has(token)) continue;
      if (info.status === 'revoked') continue;
      try {
        this.updateInviteStatus.run({ token, status: 'revoked', last_seen: now });
        statusUpdated += 1;
      } catch (err) {
        this.log('debug', 'Failed to mark quick invite as revoked', { token, error: err.message });
      }
    }

    const maxRevoke = Math.max(0, Number(opts.maxRevoke ?? 5));
    if (maxRevoke > 0 && revokeQueue.size > 0) {
      let attempts = 0;
      for (const token of revokeQueue) {
        if (attempts >= maxRevoke) break;
        attempts += 1;
        try {
          await this._revokeQuickInviteToken(token);
          this.updateInviteStatus.run({ token, status: 'revoked', last_seen: now });
          revoked += 1;
        } catch (err) {
          this.log('debug', 'Failed to revoke consumed quick invite token', { token, error: err.message });
        }
      }
    }

    const summary = {
      tokens: normalized.length,
      inserted,
      updated: metaUpdated,
      status_updated: statusUpdated,
      revoked,
    };
    if (normalized.length || inserted || metaUpdated || statusUpdated || revoked) {
      this.log('debug', 'Quick invite sync summary', summary);
    }
    return summary;
  }

  /**
   * @param {import('better-sqlite3').Database} db
   * @param {import('steam-user')} client
   * @param {(level:string,msg:string,extra?:object)=>void} log
   * @param {object} [opts]
   * @param {number} [opts.inviteLimit]
   * @param {number|null} [opts.inviteDuration]
   * @param {number} [opts.poolTarget]
   * @param {boolean} [opts.autoEnsure]
   * @param {number} [opts.autoEnsureIntervalMs]
   */
  constructor(db, client, log, opts = {}) {
    this.db = db;
    this.client = client;
    this.log = log;

    this.inviteLimit = Number(opts.inviteLimit ?? DEFAULT_INVITE_LIMIT);
    this.inviteDuration = (opts.inviteDuration === null) ? null : (opts.inviteDuration ?? DEFAULT_INVITE_DURATION);
    this.poolMinAvailable = Math.max(1, Number(opts.poolMinAvailable ?? DEFAULT_POOL_MIN_AVAILABLE));
    this.poolTarget = Math.max(this.poolMinAvailable, Number(opts.poolTarget ?? DEFAULT_POOL_TARGET));
    this.poolRefillTarget = Math.max(this.poolTarget, Number(opts.poolRefillTarget ?? DEFAULT_POOL_REFILL_TARGET));
    this.poolRefillCount = Math.max(0, Number(opts.poolRefillCount ?? DEFAULT_POOL_REFILL_COUNT));
    this.autoEnsure = (typeof opts.autoEnsure === 'boolean') ? opts.autoEnsure : DEFAULT_AUTO_ENSURE;
    this.autoEnsureIntervalMs = Math.max(1000, Number(opts.autoEnsureIntervalMs ?? DEFAULT_AUTO_ENSURE_MS));

    this._autoTimer = null;
    this._ensureInFlight = false;
    this._syncPromise = null;

    this._prepareSchema();
    this._prepareStatements();
  }

  _prepareSchema() {
    this.db.prepare(`
      CREATE TABLE IF NOT EXISTS steam_quick_invites (
        token           TEXT PRIMARY KEY,
        invite_link     TEXT NOT NULL,
        invite_limit    INTEGER,
        invite_duration INTEGER,
        created_at      INTEGER DEFAULT (strftime('%s','now')),
        expires_at      INTEGER,
        status          TEXT NOT NULL DEFAULT 'available', -- available|reserved|used|invalid|revoked
        reserved_by     INTEGER,
        reserved_at     INTEGER,
        last_seen       INTEGER
      )
    `).run();

    // FIX: einfache String-Literale statt backticks mit escapes
    this.db.prepare('CREATE INDEX IF NOT EXISTS idx_sqi_status_expires ON steam_quick_invites(status, expires_at)').run();
    this.db.prepare('CREATE INDEX IF NOT EXISTS idx_sqi_created ON steam_quick_invites(created_at)').run();
  }

  _prepareStatements() {
    this.insertInvite = this.db.prepare(`
      INSERT INTO steam_quick_invites
        (token, invite_link, invite_limit, invite_duration, created_at, expires_at, status, last_seen)
      VALUES
        (@token, @invite_link, @invite_limit, @invite_duration, @created_at, @expires_at, COALESCE(@status, 'available'), @last_seen)
      ON CONFLICT(token) DO NOTHING
    `);

    this.updateInviteMeta = this.db.prepare(`
      UPDATE steam_quick_invites
         SET invite_link=@invite_link,
             invite_limit=@invite_limit,
             invite_duration=@invite_duration,
             expires_at=@expires_at,
             created_at=COALESCE(created_at, @created_at),
             last_seen=@last_seen
       WHERE token=@token
    `);

    this.updateInviteStatus = this.db.prepare(`
      UPDATE steam_quick_invites
         SET status=@status,
             last_seen=@last_seen
       WHERE token=@token
    `);

    this.selectTokenStatuses = this.db.prepare(`
      SELECT token, status
        FROM steam_quick_invites
    `);

    this.countAvailable = this.db.prepare(`
      SELECT COUNT(*) AS c
      FROM steam_quick_invites
      WHERE status='available'
        AND (expires_at IS NULL OR expires_at > strftime('%s','now'))
    `);
  }

  getAvailableCount() {
    try {
      const row = this.countAvailable.get();
      return Number((row && row.c) || 0);
    } catch (e) {
      this.log('warn', 'quickInvites.getAvailableCount failed', { error: e.message });
      return 0;
    }
  }

  /**
   * Erzeugt EINEN Quick-Invite-Link über steam-user.
   * @param {object} [opts]
   * @param {number} [opts.inviteLimit]
   * @param {number|null} [opts.inviteDuration]
   * @returns {Promise<{invite_link:string, token:string|null, invite_limit:number, invite_duration:number|null, expires_at:number|null}>}
   */
  async createOne(opts = {}) {
    if (typeof this.client.createQuickInviteLink !== 'function') {
      throw new Error('steam-user createQuickInviteLink() not available');
    }

    const inviteLimit = Number(opts.inviteLimit ?? this.inviteLimit);
    const inviteDuration = (opts.inviteDuration === null) ? null : (opts.inviteDuration ?? this.inviteDuration);

    return new Promise((resolve, reject) => {
      const args = { inviteLimit: Number.isFinite(inviteLimit) ? inviteLimit : 1 };
      if (inviteDuration !== null) args.inviteDuration = Number(inviteDuration);

      this.client.createQuickInviteLink(args, (err, linkObj) => {
        if (err) return reject(err);
        if (!linkObj) return reject(new Error('No link returned by steam-user'));

        // Robust: egal wie verschachtelt – finde URL
        const urlStr =
          (typeof linkObj === 'string' && linkObj) ||
          findFirstUrlString(linkObj);

        if (!urlStr) {
          try {
            this.log('warn', 'createQuickInviteLink returned non-URL object', {
              sample: JSON.stringify(linkObj, (_, v) => (typeof v === 'object' ? v : String(v))).slice(0, 500)
            });
          } catch {}
          return reject(new Error('Steam returned malformed quick-invite object without a usable URL'));
        }

        const linkStr = String(urlStr).trim();
        const { token } = parseInviteLink(linkStr);
        const expires_at = (inviteDuration !== null) ? (nowSec() + Number(inviteDuration)) : null;

        resolve({
          invite_link: linkStr,
          token: token ? String(token) : null,
          invite_limit: Number.isFinite(inviteLimit) ? inviteLimit : 1,
          invite_duration: (inviteDuration === null) ? null : Number(inviteDuration),
          expires_at: (expires_at === null ? null : Number(expires_at))
        });
      });
    });
  }

  /**
   * Sichert, dass mind. `target` 'available' Einträge existieren.
   */
  async ensurePool(opts = {}) {
    const inviteLimit = Number(opts.inviteLimit ?? this.inviteLimit);
    const inviteDuration = (opts.inviteDuration === null) ? null : (opts.inviteDuration ?? this.inviteDuration);

    const hasExplicitTarget = Object.prototype.hasOwnProperty.call(opts, 'target');
    const hasExplicitRefillTarget = Object.prototype.hasOwnProperty.call(opts, 'refillTarget');
    const hasExplicitRefillIncrement = Object.prototype.hasOwnProperty.call(opts, 'refillIncrement');

    const minAvailable = Math.max(1, Number(opts.minAvailable ?? this.poolMinAvailable));

    const rawTarget = hasExplicitTarget ? Number(opts.target) : Number(this.poolTarget);
    let target = Number.isFinite(rawTarget) ? Math.floor(rawTarget) : minAvailable;
    target = Math.max(minAvailable, target);

    let refillTarget;
    if (hasExplicitRefillTarget) {
      const parsed = Number(opts.refillTarget);
      refillTarget = Number.isFinite(parsed) ? Math.floor(parsed) : target;
    } else if (hasExplicitRefillIncrement) {
      const parsed = Number(opts.refillIncrement);
      refillTarget = target + (Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : 0);
    } else if (!hasExplicitTarget && Number.isFinite(this.poolRefillTarget) && this.poolRefillTarget > target) {
      refillTarget = Math.floor(this.poolRefillTarget);
    } else if (!hasExplicitTarget && this.poolRefillCount > 0) {
      refillTarget = target + this.poolRefillCount;
    } else {
      refillTarget = target;
    }
    refillTarget = Math.max(target, refillTarget);

    const available_before = this.getAvailableCount();
    const summary = {
      created: 0,
      available_before,
      available_after: available_before,
      min_available: minAvailable,
      target,
      refill_target: refillTarget,
    };

    if (available_before >= minAvailable && available_before >= target) {
      summary.minimum_satisfied = true;
      summary.target_satisfied = true;
      return summary;
    }

    try {
      summary.synced = await this.syncFromSteam();
    } catch (err) {
      this.log('warn', 'Quick invite sync before ensure failed', { error: err.message });
      summary.synced_error = err.message;
    }

    const afterSync = this.getAvailableCount();
    summary.available_after = afterSync;
    if (afterSync >= minAvailable && afterSync >= target) {
      summary.minimum_satisfied = true;
      summary.target_satisfied = true;
      this.log('info', 'Quick invite pool satisfied after Steam sync', {
        minAvailable,
        target,
        refillTarget,
        available_before,
        available_after: afterSync,
        created: 0,
      });
      return summary;
    }

    const toCreate = Math.max(0, refillTarget - afterSync);
    for (let i = 0; i < toCreate; i++) {
      let rec;
      try {
        // eslint-disable-next-line no-await-in-loop
        rec = await this.createOne({ inviteLimit, inviteDuration });
      } catch (err) {
        if (err && TIMEOUT_REGEX.test(String(err.message || err))) {
          this.log('warn', 'Quick invite creation timed out, attempting Steam sync fallback', { error: err.message });
          try {
            summary.retry_sync = await this.syncFromSteam({ maxRevoke: 10 });
          } catch (syncErr) {
            this.log('warn', 'Retry sync failed after quick invite timeout', { error: syncErr.message });
            summary.retry_sync_error = syncErr.message;
          }
          const availableAfterRetry = this.getAvailableCount();
          summary.available_after = availableAfterRetry;
          if (availableAfterRetry >= minAvailable && availableAfterRetry >= target) {
            this.log('info', 'Quick invite pool satisfied after timeout fallback sync', {
              minAvailable,
              target,
              refillTarget,
              available_before,
              available_after: availableAfterRetry,
              created: summary.created,
            });
            return summary;
          }
          throw new Error(`Quick invite creation timed out and no reusable tokens available (target=${target}, refillTarget=${refillTarget}, available=${availableAfterRetry})`);
        }
        throw err;
      }

      const timestamp = nowSec();
      const row = {
        token: String(rec.token || `no-token-${Date.now()}-${i}`),
        invite_link: String(rec.invite_link),
        invite_limit: Number.isFinite(rec.invite_limit) ? Number(rec.invite_limit) : 1,
        invite_duration: (rec.invite_duration === null ? null : Number(rec.invite_duration)),
        created_at: Number(timestamp),
        expires_at: (rec.expires_at === null ? null : Number(rec.expires_at)),
        last_seen: Number(timestamp),
        status: 'available',
      };

      try {
        this.insertInvite.run(row);
        summary.created += 1;
      } catch (err) {
        this.log('warn', 'Failed to persist quick invite', { token: row.token, error: err.message });
      }
    }

    const available_after = this.getAvailableCount();
    summary.available_after = available_after;
    summary.minimum_satisfied = available_after >= minAvailable;
    summary.target_satisfied = available_after >= target;
    this.log('info', 'Quick invite pool ensured', {
      created: summary.created,
      available_before,
      available_after,
      minAvailable,
      target,
      refillTarget,
    });
    return summary;
  }

  async ensureAtLeastOne() {
    try {
      const res = await this.ensurePool({ target: 1 });
      if (res.created > 0) {
        this.log('info', 'ensureAtLeastOne created new invite(s)', res);
      }
    } catch (e) {
      this.log('warn', 'ensureAtLeastOne failed', { error: e.message });
    }
  }

  startAutoEnsure() {
    if (!this.autoEnsure) {
      this.log('info', 'QuickInvites autoEnsure disabled by config');
      return;
    }
    if (this._autoTimer) return;

    this._autoEnsureOnce();
    this._autoTimer = setInterval(() => this._autoEnsureOnce(), this.autoEnsureIntervalMs);
    this._autoTimer.unref?.();
    this.log('info', 'QuickInvites autoEnsure started', {
      poolTarget: this.poolTarget,
      poolMinAvailable: this.poolMinAvailable,
      poolRefillTarget: this.poolRefillTarget,
      poolRefillCount: this.poolRefillCount,
      inviteLimit: this.inviteLimit,
      inviteDuration: this.inviteDuration,
      intervalMs: this.autoEnsureIntervalMs
    });
  }

  stopAutoEnsure() {
    if (this._autoTimer) {
      clearInterval(this._autoTimer);
      this._autoTimer = null;
      this.log('info', 'QuickInvites autoEnsure stopped');
    }
  }

  async _autoEnsureOnce() {
    if (this._ensureInFlight) return;
    if (!this.client || !this.client.steamID) return; // erst loslegen, wenn eingeloggt

    this._ensureInFlight = true;
    try {
      await this.ensurePool({ target: this.poolTarget });
    } catch (e) {
      this.log('warn', 'autoEnsure iteration failed', { error: e.message });
    } finally {
      this._ensureInFlight = false;
    }
  }
}

module.exports = { QuickInvites };
