#!/usr/bin/env node
'use strict';

/**
 * Deadlock GC Profile Card helper.
 *
 * Fetches the profile card of a Deadlock account directly from the Game Coordinator.
 * The payload and response are handled with a lightweight protobuf wire parser so the
 * feature can run independently from large generated proto bundles.
 */

const PROTO_MASK = 0x80000000;
const DEFAULT_DEADLOCK_APP_ID = 1422450;
const DEFAULT_GC_MSG_GET_PROFILE_CARD = 9024;
const DEFAULT_GC_MSG_GET_PROFILE_CARD_RESPONSE = 9025;
const DEFAULT_TIMEOUT_MS = 12000;
const REQUEST_FIELD_ACCOUNT_ID = 1;
const REQUEST_FIELD_DEV_ACCESS_HINT = 2;
const REQUEST_FIELD_FRIEND_ACCESS_HINT = 3;
const RESPONSE_FIELD_ACCOUNT_ID = 1;
const RESPONSE_FIELD_RANKED_BADGE_LEVEL = 2;

function sanitizeNumber(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function asPositiveInt(value, fieldName) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${fieldName} missing or invalid`);
  }
  return Math.floor(parsed);
}

function encodeVarint(rawValue) {
  let value = BigInt(rawValue);
  if (value < 0n) {
    throw new Error('encodeVarint only supports unsigned values');
  }
  const bytes = [];
  while (value >= 0x80n) {
    bytes.push(Number((value & 0x7Fn) | 0x80n));
    value >>= 7n;
  }
  bytes.push(Number(value));
  return Buffer.from(bytes);
}

function encodeFieldVarint(fieldNumber, value) {
  const safeField = asPositiveInt(fieldNumber, 'field number');
  const key = (BigInt(safeField) << 3n) | 0n;
  return Buffer.concat([encodeVarint(key), encodeVarint(value)]);
}

function decodeVarint(buffer, offset) {
  let result = 0n;
  let shift = 0n;
  let cursor = offset;

  while (cursor < buffer.length) {
    const byte = BigInt(buffer[cursor]);
    result |= (byte & 0x7Fn) << shift;
    cursor += 1;
    if ((byte & 0x80n) === 0n) {
      if (result <= BigInt(Number.MAX_SAFE_INTEGER)) {
        return { value: Number(result), nextOffset: cursor };
      }
      return { value: Number.MAX_SAFE_INTEGER, nextOffset: cursor };
    }
    shift += 7n;
    if (shift > 63n) {
      throw new Error('Varint too large');
    }
  }

  throw new Error('Unexpected end of buffer while decoding varint');
}

function parseTopLevelWireFields(buffer) {
  const varints = new Map();
  let offset = 0;

  const pushVarint = (field, value) => {
    if (!varints.has(field)) {
      varints.set(field, []);
    }
    varints.get(field).push(value);
  };

  while (offset < buffer.length) {
    const key = decodeVarint(buffer, offset);
    offset = key.nextOffset;
    const tag = key.value;
    const fieldNumber = tag >> 3;
    const wireType = tag & 0x07;

    if (fieldNumber <= 0) {
      throw new Error(`Invalid protobuf field number: ${fieldNumber}`);
    }

    if (wireType === 0) {
      const value = decodeVarint(buffer, offset);
      offset = value.nextOffset;
      pushVarint(fieldNumber, value.value);
      continue;
    }

    if (wireType === 1) {
      offset += 8;
      if (offset > buffer.length) {
        throw new Error('Invalid fixed64 field length');
      }
      continue;
    }

    if (wireType === 2) {
      const len = decodeVarint(buffer, offset);
      offset = len.nextOffset + len.value;
      if (offset > buffer.length) {
        throw new Error('Invalid length-delimited field length');
      }
      continue;
    }

    if (wireType === 5) {
      offset += 4;
      if (offset > buffer.length) {
        throw new Error('Invalid fixed32 field length');
      }
      continue;
    }

    throw new Error(`Unsupported wire type: ${wireType}`);
  }

  return varints;
}

function getFirstValue(map, field) {
  const values = map.get(field);
  if (!values || !values.length) return null;
  return values[0];
}

function inferBadgeLevel(varints, accountId) {
  const candidates = [];
  for (const [field, values] of varints.entries()) {
    for (const value of values) {
      if (!Number.isFinite(value)) continue;
      if (value <= 0) continue;
      if (value === accountId) continue;
      if (value > 500) continue;
      candidates.push({ field, value });
    }
  }

  if (!candidates.length) return null;

  candidates.sort((a, b) => {
    if (a.field !== b.field) return a.field - b.field;
    return b.value - a.value;
  });
  return candidates[0].value;
}

function buildProfileCardPayload(accountId, options = {}) {
  const friendAccessHint = options.friendAccessHint !== false;
  const devAccessHint = options.devAccessHint;
  const parts = [encodeFieldVarint(REQUEST_FIELD_ACCOUNT_ID, accountId)];

  if (devAccessHint !== undefined && devAccessHint !== null) {
    parts.push(encodeFieldVarint(REQUEST_FIELD_DEV_ACCESS_HINT, devAccessHint ? 1 : 0));
  }
  parts.push(encodeFieldVarint(REQUEST_FIELD_FRIEND_ACCESS_HINT, friendAccessHint ? 1 : 0));

  return Buffer.concat(parts);
}

function decodeProfileCardPayload(buffer, requestedAccountId) {
  const varints = parseTopLevelWireFields(buffer);
  let accountId = getFirstValue(varints, RESPONSE_FIELD_ACCOUNT_ID);
  if (!Number.isFinite(accountId) || accountId <= 0) {
    accountId = requestedAccountId;
  }

  let badgeLevel = getFirstValue(varints, RESPONSE_FIELD_RANKED_BADGE_LEVEL);
  if (!Number.isFinite(badgeLevel) || badgeLevel < 0) {
    badgeLevel = inferBadgeLevel(varints, accountId);
  }

  if (!Number.isFinite(badgeLevel) || badgeLevel < 0) {
    badgeLevel = null;
  }

  const rankedRank = badgeLevel === null ? null : Math.floor(badgeLevel / 10);
  const rankedSubrank = badgeLevel === null ? null : badgeLevel % 10;

  return {
    account_id: accountId,
    ranked_badge_level: badgeLevel,
    ranked_rank: rankedRank,
    ranked_subrank: rankedSubrank,
  };
}

class GcProfileCard {
  constructor({ client, log, trace, appId } = {}) {
    this.client = client;
    this.log = typeof log === 'function' ? log : () => {};
    this.trace = typeof trace === 'function' ? trace : () => {};
    this.appId = sanitizeNumber(appId, DEFAULT_DEADLOCK_APP_ID);
    this.msgRequest = DEFAULT_GC_MSG_GET_PROFILE_CARD;
    this.msgResponse = DEFAULT_GC_MSG_GET_PROFILE_CARD_RESPONSE;
    this.pendingRequests = new Map();
    this.requestCounter = 0;
  }

  _nextRequestId() {
    this.requestCounter += 1;
    return this.requestCounter;
  }

  _dequeueOldestRequest() {
    const iter = this.pendingRequests.entries().next();
    if (iter.done) return null;
    const [requestId, entry] = iter.value;
    return { requestId, entry };
  }

  _resolveRequest(requestId, responsePayload, metadata = {}) {
    const entry = this.pendingRequests.get(requestId);
    if (!entry) return false;
    this.pendingRequests.delete(requestId);
    if (entry.timer) clearTimeout(entry.timer);

    try {
      const decoded = decodeProfileCardPayload(responsePayload, entry.accountId);
      const result = {
        ...decoded,
        gc_app_id: metadata.appId,
        gc_msg_type: metadata.messageId,
      };
      entry.resolve(result);
    } catch (err) {
      entry.reject(err);
    }
    return true;
  }

  _rejectRequest(requestId, error) {
    const entry = this.pendingRequests.get(requestId);
    if (!entry) return false;
    this.pendingRequests.delete(requestId);
    if (entry.timer) clearTimeout(entry.timer);
    entry.reject(error);
    return true;
  }

  handleGcMessage(appId, msgType, payload) {
    const messageId = (Number(msgType) & ~PROTO_MASK);
    if (messageId !== this.msgResponse) {
      return false;
    }

    const oldest = this._dequeueOldestRequest();
    if (!oldest) {
      return false;
    }

    const payloadBuffer = Buffer.isBuffer(payload)
      ? payload
      : Buffer.from(payload || []);

    this.trace('received_profile_card_response_fallback', {
      requestId: oldest.requestId,
      appId,
      messageId,
      payloadHex: payloadBuffer.toString('hex').slice(0, 200),
    });

    this._resolveRequest(oldest.requestId, payloadBuffer, {
      appId: Number(appId),
      messageId,
    });
    return true;
  }

  flushPending(error) {
    const pendingIds = Array.from(this.pendingRequests.keys());
    for (const requestId of pendingIds) {
      this._rejectRequest(requestId, error || new Error('GC profile-card request cancelled'));
    }
  }

  async fetchPlayerCard(options = {}) {
    if (!this.client || typeof this.client.sendToGC !== 'function') {
      throw new Error('Steam client does not support sendToGC');
    }

    const accountId = asPositiveInt(options.accountId, 'account_id');
    const timeoutMs = Math.max(
      3000,
      asPositiveInt(options.timeoutMs || DEFAULT_TIMEOUT_MS, 'timeout_ms')
    );
    const payload = buildProfileCardPayload(accountId, options);
    const requestId = this._nextRequestId();

    this.trace('send_profile_card_request', {
      requestId,
      accountId,
      appId: this.appId,
      messageId: this.msgRequest,
      payloadHex: payload.toString('hex'),
    });

    this.log('info', 'GC_PROFILE_CARD: sending request', {
      requestId,
      accountId,
      appId: this.appId,
      messageId: this.msgRequest,
      timeoutMs,
    });

    return await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this._rejectRequest(requestId, new Error('Timeout waiting for profile card response'));
      }, timeoutMs);

      this.pendingRequests.set(requestId, {
        accountId,
        timer: timeout,
        resolve,
        reject,
      });

      this.client.sendToGC(
        this.appId,
        this.msgRequest,
        {},
        payload,
        (appId, msgType, gcPayload) => {
          try {
            const normalized = (Number(msgType) & ~PROTO_MASK);
            if (normalized !== this.msgResponse) {
              this._rejectRequest(
                requestId,
                new Error(
                  `Unexpected GC message type: ${normalized} (expected ${this.msgResponse})`
                )
              );
              return;
            }

            const payloadBuffer = Buffer.isBuffer(gcPayload)
              ? gcPayload
              : Buffer.from(gcPayload || []);
            const handled = this._resolveRequest(requestId, payloadBuffer, {
              appId: Number(appId),
              messageId: normalized,
            });
            if (!handled) {
              return;
            }

            this.trace('received_profile_card_response', {
              requestId,
              accountId,
              appId,
              messageId: normalized,
              payloadHex: payloadBuffer.toString('hex').slice(0, 200),
            });

            this.log('info', 'GC_PROFILE_CARD: response received', {
              requestId,
              accountId,
              messageId: normalized,
            });
          } catch (err) {
            this._rejectRequest(requestId, err);
          }
        }
      );
    });
  }
}

module.exports = {
  GcProfileCard,
  GC_MSG_GET_PROFILE_CARD: DEFAULT_GC_MSG_GET_PROFILE_CARD,
  GC_MSG_GET_PROFILE_CARD_RESPONSE: DEFAULT_GC_MSG_GET_PROFILE_CARD_RESPONSE,
};
