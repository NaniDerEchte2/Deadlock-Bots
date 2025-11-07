'use strict';

/**
 * Deadlock GC helper.
 *
 * Encapsulates the real Game Coordinator handshake payload (based on the
 * captured NetHook dumps) and the pared down playtest invite encoder/decoder.
 * The module does not talk to Steam directly – index.js still acts as the
 * bridge and calls into this helper whenever it needs a payload.
 */

const DEADLOCK_SESSION_NEED = 5972;
const DEADLOCK_HELLO_ENTRY_FLAG = 5;
const DEADLOCK_HELLO_SECONDARY_ENTRY_FLAG = 1;
const DEADLOCK_HELLO_TIMINGS = [
  { field: 11, value: 21 },
  { field: 12, value: 2 },
  { field: 13, value: 2 },
  { field: 14, value: 2560 },
  { field: 15, value: 1440 },
  { field: 16, value: 180 },
  { field: 17, value: 2560 },
  { field: 18, value: 1440 },
  { field: 19, value: 2560 },
  { field: 20, value: 1440 },
];

function encodeVarint(value) {
  let n = BigInt(value >>> 0);
  if (typeof value === 'bigint') n = value;
  const chunks = [];
  do {
    let byte = Number(n & 0x7fn);
    n >>= 7n;
    if (n !== 0n) byte |= 0x80;
    chunks.push(byte);
  } while (n !== 0n);
  return Buffer.from(chunks);
}

function encodeFixed64LE(value) {
  if (Buffer.isBuffer(value)) {
    if (value.length === 8) return value;
    if (value.length > 8) return value.slice(0, 8);
    const out = Buffer.alloc(8);
    value.copy(out);
    return out;
  }
  let big = BigInt(value);
  const out = Buffer.alloc(8);
  out.writeBigUInt64LE(big, 0);
  return out;
}

function encodeFieldVarint(field, value) {
  const tag = (field << 3) | 0;
  return Buffer.concat([encodeVarint(tag), encodeVarint(value)]);
}

function encodeFieldFixed64(field, value) {
  const tag = (field << 3) | 1;
  return Buffer.concat([encodeVarint(tag), encodeFixed64LE(value)]);
}

function encodeFieldBytes(field, buffer) {
  const tag = (field << 3) | 2;
  const payload = Buffer.isBuffer(buffer) ? buffer : Buffer.from(buffer || '');
  return Buffer.concat([encodeVarint(tag), encodeVarint(payload.length), payload]);
}

function parseVarint(buffer, offset) {
  let res = 0n;
  let shift = 0n;
  let pos = offset;
  while (pos < buffer.length) {
    const byte = buffer[pos];
    res |= BigInt(byte & 0x7f) << shift;
    pos += 1;
    if ((byte & 0x80) === 0) break;
    shift += 7n;
  }
  return { value: Number(res), next: pos };
}

class DeadlockGcBot {
  constructor({ client, log, trace }) {
    this.client = client;
    this.log = typeof log === 'function' ? log : () => {};
    this.trace = typeof trace === 'function' ? trace : () => {};
    this.cachedHello = null;
    this.cachedLegacyHello = null;
  }

  get steamID64() {
    if (this.client && this.client.steamID && typeof this.client.steamID.getSteamID64 === 'function') {
      return BigInt(this.client.steamID.getSteamID64());
    }
    return 0n;
  }

  get accountId() {
    if (this.client && this.client.steamID && Number.isFinite(this.client.steamID.accountid)) {
      return Number(this.client.steamID.accountid) >>> 0;
    }
    return null;
  }

  /**
   * Return up to two GC tokens (without consuming them) so we can mirror the real client hello.
   */
  peekGcTokens(max = 2) {
    const list = Array.isArray(this.client?._gcTokens) ? this.client._gcTokens : [];
    if (!list.length) return [];
    return list.slice(0, max).map((token) => {
      if (Buffer.isBuffer(token)) return token;
      if (typeof token === 'string') return Buffer.from(token, 'hex');
      return Buffer.from(token || []);
    });
  }

  buildRealHelloPayload(force = false) {
    if (!force && this.cachedHello) return this.cachedHello;

    const tokens = this.peekGcTokens();
    if (!tokens.length || !this.steamID64) {
      this.log('warn', 'Deadlock GC hello fallback: no GC tokens or SteamID available');
      return null;
    }

    const parts = [];
    parts.push(encodeFieldVarint(1, DEADLOCK_SESSION_NEED));

    tokens.forEach((token, index) => {
      const entryParts = [];

      const actor = Buffer.concat([
        encodeFieldVarint(1, 1),
        encodeFieldVarint(2, this.steamID64),
      ]);
      entryParts.push(encodeFieldBytes(1, actor));
      entryParts.push(encodeFieldFixed64(2, token));
      if (index > 0) {
        entryParts.push(encodeFieldVarint(3, DEADLOCK_HELLO_SECONDARY_ENTRY_FLAG));
      }
      entryParts.push(encodeFieldVarint(4, DEADLOCK_HELLO_ENTRY_FLAG));
      parts.push(encodeFieldBytes(2, Buffer.concat(entryParts)));
    });

    parts.push(encodeFieldVarint(3, 0));
    parts.push(encodeFieldVarint(6, 0));
    parts.push(encodeFieldVarint(7, 1));
    parts.push(encodeFieldVarint(9, 1));
    parts.push(encodeFieldBytes(10, Buffer.alloc(0)));
    DEADLOCK_HELLO_TIMINGS.forEach(({ field, value }) => {
      parts.push(encodeFieldVarint(field, value));
    });

    this.cachedHello = Buffer.concat(parts);
    return this.cachedHello;
  }

  buildLegacyHelloPayload(force = false) {
    if (!force && this.cachedLegacyHello) return this.cachedLegacyHello;
    // This mirrors the small payload we used before we reverse engineered the real hello.
    const actorParts = [];
    actorParts.push(encodeFieldVarint(1, 1));
    if (this.accountId !== null) {
      actorParts.push(encodeFieldVarint(2, this.accountId));
    }
    const payload = Buffer.concat([
      encodeFieldVarint(1, 1),
      encodeFieldVarint(2, this.accountId || 0),
    ]);

    const parts = [
      encodeFieldVarint(1, 1),
      encodeFieldBytes(2, payload),
    ];
    this.cachedLegacyHello = Buffer.concat(parts);
    return this.cachedLegacyHello;
  }

  getHelloPayload(force = false) {
    const real = this.buildRealHelloPayload(force);
    if (real && real.length) return real;
    return this.buildLegacyHelloPayload(force);
  }

  encodePlaytestInvitePayload(accountId, location) {
    const parts = [];
    if (location) {
      const locBuf = Buffer.from(String(location), 'utf8');
      parts.push(encodeFieldBytes(3, locBuf));
    }
    if (Number.isFinite(accountId)) {
      parts.push(encodeFieldVarint(4, Number(accountId) >>> 0));
    }
    return parts.length ? Buffer.concat(parts) : Buffer.alloc(0);
  }

  decodePlaytestInviteResponse(buffer) {
    if (!buffer || !buffer.length) return { code: null, success: false };
    let offset = 0;
    while (offset < buffer.length) {
      const { value: tag, next } = parseVarint(buffer, offset);
      offset = next;
      const field = tag >>> 3;
      const wire = tag & 0x07;
      if (field === 1 && wire === 0) {
        const { value: code } = parseVarint(buffer, offset);
        return { code, success: Number(code) === 0 };
      }
      if (wire === 0) {
        const { next: skip } = parseVarint(buffer, offset);
        offset = skip;
      } else if (wire === 1) {
        offset += 8;
      } else if (wire === 2) {
        const { value: len, next: n2 } = parseVarint(buffer, offset);
        offset = n2 + Number(len);
      } else if (wire === 5) {
        offset += 4;
      } else {
        break;
      }
    }
    return { code: null, success: false };
  }
}

module.exports = { DeadlockGcBot };
