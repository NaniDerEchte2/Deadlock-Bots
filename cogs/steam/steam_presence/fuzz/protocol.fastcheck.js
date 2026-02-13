'use strict';

const fs = require('fs');
const fc = require('fast-check');
const protocol = require('../deadlock_gc_protocol');

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
    numRuns: 1000,
    endOnFailure: true,
  }
);

fs.unwatchFile(protocol.DEADLOCK_GC_PROTOCOL_OVERRIDE_PATH);
console.log('fast-check protocol fuzzing passed');
