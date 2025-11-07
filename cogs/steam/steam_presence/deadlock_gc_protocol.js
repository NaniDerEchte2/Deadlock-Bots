'use strict';

/**
 * Deadlock GC protocol override loader.
 *
 * Power users can drop a file named `.steam-data/deadlock_gc_protocol.js`
 * next to this module. That file may export any of the following hooks:
 *
 *   - `buildHelloPayload(context) -> Buffer|string|Uint8Array`
 *   - `getPlaytestConfig(context) -> { name?, exclusive?, messageIds?, buildPayload? }`
 *
 * The hooks are optional. Whenever the file changes we evict it from the
 * require cache so the running bridge can pick up adjustments without
 * restarts.
 */

const fs = require('fs');
const path = require('path');

const PROTOCOL_OVERRIDE_PATH = path.join(__dirname, '.steam-data', 'deadlock_gc_protocol.js');
let cachedModule = null;
let cachedMtimeMs = 0;
let lastErrorMessage = null;
let watcherInitialized = false;

function statsForOverride() {
  try {
    return fs.statSync(PROTOCOL_OVERRIDE_PATH);
  } catch (err) {
    if (err && err.code === 'ENOENT') return null;
    throw err;
  }
}

function loadOverrideModule(forceReload = false) {
  const stat = statsForOverride();
  if (!stat) {
    cachedModule = null;
    cachedMtimeMs = 0;
    lastErrorMessage = null;
    return null;
  }

  if (!forceReload && cachedModule && cachedMtimeMs === stat.mtimeMs) {
    return cachedModule;
  }

  try {
    const resolved = require.resolve(PROTOCOL_OVERRIDE_PATH);
    delete require.cache[resolved];
  } catch (_) {
    // Ignore cache delete errors, we'll attempt to require either way.
  }

  try {
    cachedModule = require(PROTOCOL_OVERRIDE_PATH);
    cachedMtimeMs = stat.mtimeMs;
    lastErrorMessage = null;
  } catch (err) {
    cachedModule = null;
    cachedMtimeMs = stat.mtimeMs;
    lastErrorMessage = err && err.message ? err.message : String(err);
  }

  return cachedModule;
}

function ensureWatcher() {
  if (watcherInitialized) return;
  watcherInitialized = true;

  try {
    fs.watchFile(PROTOCOL_OVERRIDE_PATH, { interval: 2000 }, () => loadOverrideModule(true));
  } catch (_) {
    // Optional convenience only – ignore if fs.watchFile is unavailable.
  }
}

function invokeHook(hookName, ...args) {
  ensureWatcher();
  const mod = loadOverrideModule();
  if (!mod || typeof mod[hookName] !== 'function') {
    return { ok: false, value: null };
  }

  try {
    return { ok: true, value: mod[hookName](...args) };
  } catch (err) {
    lastErrorMessage = err && err.message ? err.message : String(err);
    return { ok: false, value: null, error: err };
  }
}

function getHelloPayloadOverride(context) {
  const result = invokeHook('buildHelloPayload', context);
  return result.ok ? result.value : null;
}

function getPlaytestOverrides(context) {
  const result = invokeHook('getPlaytestConfig', context);
  return result.ok ? result.value : null;
}

function getOverrideInfo() {
  const stat = statsForOverride();
  return {
    path: PROTOCOL_OVERRIDE_PATH,
    exists: Boolean(stat),
    mtimeMs: stat ? stat.mtimeMs : null,
    lastError: lastErrorMessage,
    loaded: Boolean(cachedModule),
  };
}

module.exports = {
  DEADLOCK_GC_PROTOCOL_OVERRIDE_PATH: PROTOCOL_OVERRIDE_PATH,
  getHelloPayloadOverride,
  getPlaytestOverrides,
  getOverrideInfo,
  _forceReload: () => loadOverrideModule(true),
};
