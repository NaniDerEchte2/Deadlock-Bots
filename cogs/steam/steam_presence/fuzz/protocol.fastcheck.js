'use strict';

const fs = require('fs');
const fc = require('fast-check');
const protocol = require('../deadlock_gc_protocol');
const runsFromEnv = Number.parseInt(process.env.FUZZ_NUM_RUNS || '1000', 10);
const fuzzRuns = Number.isFinite(runsFromEnv) && runsFromEnv > 0 ? runsFromEnv : 1000;

/**
 * Property-based fuzz test:
 * Hook entry-points must never throw on arbitrary JSON-like inputs.
 */
fc.assert(
  fc.property(fc.jsonValue(), (context) => {
    protocol.getHelloPayloadOverride(context);
    protocol.getPlaytestOverrides(context);

    const info = protocol.getOverrideInfo();
    return (
      info &&
      typeof info === 'object' &&
      typeof info.path === 'string' &&
      typeof info.exists === 'boolean' &&
      Object.prototype.hasOwnProperty.call(info, 'loaded')
    );
  }),
  {
    numRuns: fuzzRuns,
    endOnFailure: true,
  }
);

fs.unwatchFile(protocol.DEADLOCK_GC_PROTOCOL_OVERRIDE_PATH);
console.log(`fast-check protocol fuzzing passed (${fuzzRuns} runs)`);
