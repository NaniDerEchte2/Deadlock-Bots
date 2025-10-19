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
const DEFAULT_POOL_TARGET = Number(process.env.STEAM_INVITE_POOL_TARGET ?? 1);
const DEFAULT_AUTO_ENSURE = String(process.env.STEAM_INVITE_AUTO_ENSURE ?? 'true').toLowerCase() !== 'false';
const DEFAULT_AUTO_ENSURE_MS = Number(process.env.STEAM_INVITE_AUTO_ENSURE_MS ?? 30000);

const URL_REGEX = /^https?:\/\/\S+$/i;

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

class QuickInvites {
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
    this.poolTarget = Math.max(1, Number(opts.poolTarget ?? DEFAULT_POOL_TARGET));
    this.autoEnsure = (typeof opts.autoEnsure === 'boolean') ? opts.autoEnsure : DEFAULT_AUTO_ENSURE;
    this.autoEnsureIntervalMs = Math.max(1000, Number(opts.autoEnsureIntervalMs ?? DEFAULT_AUTO_ENSURE_MS));

    this._autoTimer = null;
    this._ensureInFlight = false;

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
      INSERT OR REPLACE INTO steam_quick_invites
        (token, invite_link, invite_limit, invite_duration, created_at, expires_at, status)
      VALUES
        (@token, @invite_link, @invite_limit, @invite_duration, @created_at, @expires_at, 'available')
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
    const target = Math.max(1, Number(opts.target ?? this.poolTarget));
    const inviteLimit = Number(opts.inviteLimit ?? this.inviteLimit);
    const inviteDuration = (opts.inviteDuration === null) ? null : (opts.inviteDuration ?? this.inviteDuration);

    const available_before = this.getAvailableCount();
    const toCreate = Math.max(0, target - available_before);
    if (toCreate <= 0) {
      return { created: 0, available_before, available_after: available_before };
    }

    const rows = [];
    for (let i = 0; i < toCreate; i++) {
      // eslint-disable-next-line no-await-in-loop
      const rec = await this.createOne({ inviteLimit, inviteDuration });

      const row = {
        token: String(rec.token || `no-token-${Date.now()}-${i}`),
        invite_link: String(rec.invite_link),
        invite_limit: Number.isFinite(rec.invite_limit) ? Number(rec.invite_limit) : 1,
        invite_duration: (rec.invite_duration === null ? null : Number(rec.invite_duration)),
        created_at: Number(nowSec()),
        expires_at: (rec.expires_at === null ? null : Number(rec.expires_at))
      };

      rows.push(row);
    }

    const tx = this.db.transaction((list) => {
      for (const r of list) this.insertInvite.run(r);
    });
    tx(rows);

    const available_after = available_before + rows.length;
    this.log('info', 'Quick invite pool ensured', { created: rows.length, available_before, available_after, target });
    return { created: rows.length, available_before, available_after };
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
