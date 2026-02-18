"use strict";
/**
 * Property-based fuzz tests for the Steam Bridge protocol helpers.
 *
 * Tests the core data-handling utilities used when processing steam_tasks
 * DB rows: JSON parsing, camelCase key conversion, and safeNumber.
 *
 * Run via:  npm run fuzz:protocol
 */

const fc = require("fast-check");

// ── Re-implementations of the helpers from index.js (pure, no DB/Steam deps) ──

function parsePayload(value) {
  if (!value) return {};
  try {
    return JSON.parse(value);
  } catch (err) {
    throw new Error(`Invalid JSON payload: ${err.message}`);
  }
}

function safeNumber(value) {
  return Number(value);
}

function convertKeysToCamelCase(obj) {
  if (Array.isArray(obj)) {
    return obj.map((v) => convertKeysToCamelCase(v));
  } else if (obj !== null && typeof obj === "object" && obj.constructor === Object) {
    return Object.keys(obj).reduce((result, key) => {
      const camelCaseKey = key.replace(/_([a-z])/g, (g) => g[1].toUpperCase());
      result[camelCaseKey] = convertKeysToCamelCase(obj[key]);
      return result;
    }, {});
  }
  return obj;
}

const KNOWN_TASK_TYPES = [
  "AUTH_STATUS", "AUTH_LOGIN", "AUTH_GUARD_CODE", "AUTH_LOGOUT",
  "AUTH_REFRESH_GAME_VERSION", "AUTH_SEND_FRIEND_REQUEST",
  "AUTH_CHECK_FRIENDSHIP", "BUILD_PUBLISH", "BUILD_CATALOG_CYCLE",
  "GC_SEARCH_BUILDS",
];

// ── Arbitraries ────────────────────────────────────────────────────────────────

const payloadObj = fc.dictionary(
  fc.string({ minLength: 1, maxLength: 32 }),
  fc.oneof(fc.string(), fc.integer(), fc.boolean(), fc.constant(null))
);

// ── Test runner ────────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;
const NUM_RUNS = parseInt(process.env.FUZZ_NUM_RUNS || "2000", 10);

function runProperty(label, arb, predicate) {
  try {
    fc.assert(fc.property(arb, predicate), { numRuns: NUM_RUNS });
    console.log("  OK  " + label);
    passed++;
  } catch (err) {
    console.error("  FAIL  " + label);
    console.error("        " + (err.message || err));
    failed++;
  }
}

console.log("Steam Bridge Protocol – property-based fuzz tests (" + NUM_RUNS + " runs each)\n");

// 1. JSON roundtrip is lossless
runProperty(
  "parsePayload: JSON roundtrip is lossless",
  payloadObj,
  (obj) => {
    const s = JSON.stringify(obj);
    return JSON.stringify(parsePayload(s)) === s;
  }
);

// 2. Falsy input returns {}
runProperty(
  "parsePayload: falsy input returns {}",
  fc.oneof(fc.constant(""), fc.constant(null), fc.constant(undefined), fc.constant(0)),
  (v) => {
    const r = parsePayload(v);
    return typeof r === "object" && r !== null && Object.keys(r).length === 0;
  }
);

// 3. Non-empty invalid JSON always throws (empty string is falsy → returns {})
runProperty(
  "parsePayload: non-empty invalid JSON always throws",
  fc.string({ minLength: 1 }).filter((s) => { try { JSON.parse(s); return false; } catch { return true; } }),
  (s) => {
    try { parsePayload(s); return false; } catch { return true; }
  }
);

// 4. convertKeysToCamelCase is idempotent
runProperty(
  "convertKeysToCamelCase: idempotent (f(f(x)) === f(x))",
  payloadObj,
  (obj) => {
    const once = convertKeysToCamelCase(obj);
    return JSON.stringify(convertKeysToCamelCase(once)) === JSON.stringify(once);
  }
);

// 5. convertKeysToCamelCase preserves key count
runProperty(
  "convertKeysToCamelCase: key count preserved",
  payloadObj,
  (obj) => Object.keys(convertKeysToCamelCase(obj)).length === Object.keys(obj).length
);

// 6. Primitives pass through unchanged
runProperty(
  "convertKeysToCamelCase: primitives pass through unchanged",
  fc.oneof(fc.string(), fc.integer(), fc.boolean(), fc.constant(null)),
  (p) => convertKeysToCamelCase(p) === p
);

// 7. safeNumber: integer strings roundtrip
runProperty(
  "safeNumber: integer strings roundtrip",
  fc.integer({ min: -1e9, max: 1e9 }),
  (n) => safeNumber(String(n)) === n
);

// 8. safeNumber never throws
runProperty(
  "safeNumber: never throws for any input",
  fc.anything(),
  (v) => { try { safeNumber(v); return true; } catch { return false; } }
);

// 9. Known task types are uppercase strings
runProperty(
  "Task types: all known types are non-empty uppercase strings",
  fc.constantFrom(...KNOWN_TASK_TYPES),
  (t) => typeof t === "string" && t.length > 0 && t === t.toUpperCase()
);

// 10. Task result envelope {ok, data} is always serialisable
runProperty(
  "Task result envelope: {ok, data} is serialisable",
  fc.record({
    ok: fc.boolean(),
    data: fc.oneof(payloadObj, fc.string(), fc.integer(), fc.constant(null)),
  }),
  (envelope) => typeof JSON.parse(JSON.stringify(envelope)).ok === "boolean"
);

// ── Summary ────────────────────────────────────────────────────────────────────

console.log("\nResults: " + passed + " passed, " + failed + " failed");
if (failed > 0) {
  console.error(failed + " property test(s) failed.");
  process.exit(1);
}
console.log("All protocol properties hold.");
