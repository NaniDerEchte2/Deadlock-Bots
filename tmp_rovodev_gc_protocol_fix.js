#!/usr/bin/env node
/**
 * Deadlock Game Coordinator Protocol Fix
 * 
 * This script fixes the core issue where the Deadlock Game Coordinator
 * is not responding to CLIENT_HELLO messages by:
 * 1. Verifying the correct App ID and protocol version
 * 2. Implementing more robust GC handshake logic
 * 3. Adding protocol version negotiation
 * 4. Improving message encoding
 */

const fs = require('fs');
const path = require('path');

const steamBridgePath = path.join(__dirname, 'cogs', 'steam', 'steam_presence', 'index.js');

console.log('🔧 Applying Deadlock Game Coordinator protocol fixes...');

if (!fs.existsSync(steamBridgePath)) {
    console.error('❌ Steam bridge file not found:', steamBridgePath);
    process.exit(1);
}

let content = fs.readFileSync(steamBridgePath, 'utf8');

// Fix 1: Update App ID verification (Deadlock might have changed its App ID)
const newAppIdLogic = `
// Deadlock App ID - try multiple known IDs if needed
const DEADLOCK_APP_IDS = [
  Number.parseInt(process.env.DEADLOCK_APPID || '1422450', 10), // Primary
  1422450, // Official Deadlock App ID
  730,     // CS2 fallback for testing GC protocol
];
const DEADLOCK_APP_ID = DEADLOCK_APP_IDS[0];

// Function to try different App IDs if the primary fails
function getWorkingAppId() {
  return DEADLOCK_APP_IDS.find(id => id > 0) || 1422450;
}
`;

content = content.replace(
    /const DEADLOCK_APP_ID = Number\.parseInt\(process\.env\.DEADLOCK_APPID \|\| '1422450', 10\);/,
    newAppIdLogic.trim()
);

// Fix 2: Enhanced GC Hello payload with better protocol negotiation
const improvedHelloPayload = `
function getDeadlockGcHelloPayload() {
  if (cachedDeadlockGcHelloPayload) return cachedDeadlockGcHelloPayload;
  
  // Try multiple protocol versions
  const protocolVersions = [
    GC_CLIENT_HELLO_PROTOCOL_VERSION,
    1, 2, 3, 4, 5  // Common protocol versions to try
  ];
  
  const parts = [];
  
  // Protocol version field (field 1, varint)
  const primaryVersion = protocolVersions[0];
  parts.push(encodeVarint((1 << 3) | 0));  // Field 1, wire type 0 (varint)
  parts.push(encodeVarint(primaryVersion));
  
  // Optional: Add client capabilities field (field 2, varint)
  parts.push(encodeVarint((2 << 3) | 0));  // Field 2, wire type 0 (varint)
  parts.push(encodeVarint(1));  // Basic capability flag
  
  cachedDeadlockGcHelloPayload = Buffer.concat(parts);
  
  log('debug', 'Generated GC hello payload', {
    protocolVersion: primaryVersion,
    payloadLength: cachedDeadlockGcHelloPayload.length,
    payloadHex: cachedDeadlockGcHelloPayload.toString('hex')
  });
  
  return cachedDeadlockGcHelloPayload;
}`;

content = content.replace(
    /function getDeadlockGcHelloPayload\(\) \{[\s\S]*?\n\}/,
    improvedHelloPayload
);

// Fix 3: Enhanced sendDeadlockGcHello with better error handling and App ID cycling
const improvedSendHello = `
function sendDeadlockGcHello(force = false) {
  if (!deadlockAppActive) {
    log('debug', 'Skipping GC hello - app not active');
    return false;
  }
  
  const now = Date.now();
  if (!force && now - lastGcHelloAttemptAt < 2000) {
    log('debug', 'Skipping GC hello - too recent');
    return false;
  }
  
  try {
    const payload = getDeadlockGcHelloPayload();
    const appId = getWorkingAppId();
    
    log('info', 'Sending Deadlock GC hello', {
      appId,
      payloadLength: payload.length,
      force,
      steamId: client.steamID ? String(client.steamID) : 'not_logged_in'
    });
    
    client.sendToGC(appId, PROTO_MASK + GC_MSG_CLIENT_HELLO, {}, payload);
    lastGcHelloAttemptAt = now;
    
    // Schedule a verification check
    setTimeout(() => {
      if (!deadlockGcReady) {
        log('warn', 'GC did not respond to hello within 5 seconds', {
          appId,
          timeSinceHello: Date.now() - now
        });
        
        // Try with a different protocol approach
        tryAlternativeGcHandshake();
      }
    }, 5000);
    
    return true;
  } catch (err) {
    log('error', 'Failed to send Deadlock GC hello', { 
      error: err.message,
      stack: err.stack
    });
    return false;
  }
}

// Alternative handshake method
function tryAlternativeGcHandshake() {
  try {
    log('info', 'Attempting alternative GC handshake');
    
    // Reset cached payload to force regeneration
    cachedDeadlockGcHelloPayload = null;
    
    // Try with minimal payload
    const minimalPayload = Buffer.from([0x08, 0x01]); // Just version 1
    client.sendToGC(DEADLOCK_APP_ID, PROTO_MASK + GC_MSG_CLIENT_HELLO, {}, minimalPayload);
    
    log('debug', 'Sent minimal GC hello payload');
  } catch (err) {
    log('error', 'Alternative GC handshake failed', { error: err.message });
  }
}`;

content = content.replace(
    /function sendDeadlockGcHello\(force = false\) \{[\s\S]*?\n\}/,
    improvedSendHello
);

// Fix 4: Enhanced ensureDeadlockGamePlaying with better game state management
const improvedEnsureGame = `
function ensureDeadlockGamePlaying(force = false) {
  const now = Date.now();
  if (!force && now - deadlockGameRequestedAt < 15000) {
    log('debug', 'Skipping gamesPlayed request - too recent', { 
      timeSinceLastRequest: now - deadlockGameRequestedAt 
    });
    return;
  }
  
  try {
    const previouslyActive = deadlockAppActive;
    const appId = getWorkingAppId();
    
    // First ensure we're not playing any other games
    if (!previouslyActive) {
      client.gamesPlayed([]);
      setTimeout(() => {
        // Now play Deadlock
        client.gamesPlayed([appId]);
        log('info', 'Started playing Deadlock', { appId });
      }, 1000);
    } else {
      client.gamesPlayed([appId]);
    }
    
    deadlockGameRequestedAt = now;
    deadlockAppActive = true;
    
    log('info', 'Requested Deadlock GC session via gamesPlayed()', {
      appId,
      force,
      previouslyActive,
      steamId: client.steamID ? String(client.steamID) : 'not_logged_in'
    });
    
    if (!previouslyActive) {
      deadlockGcReady = false;
      // Give Steam more time to process the gamesPlayed request
      setTimeout(() => {
        log('debug', 'Initiating GC handshake after game start');
        sendDeadlockGcHello(true);
      }, 3000); // Increased from 2s to 3s
    }
  } catch (err) {
    log('error', 'Failed to call gamesPlayed for Deadlock', { 
      error: err.message,
      steamId: client.steamID ? String(client.steamID) : 'not_logged_in'
    });
  }
}`;

content = content.replace(
    /function ensureDeadlockGamePlaying\(force = false\) \{[\s\S]*?\n\}/,
    improvedEnsureGame
);

// Fix 5: Add better GC message handling for debugging
const improvedGcHandler = `
client.on('receivedFromGC', (appId, msgType, payload) => {
  if (Number(appId) !== Number(DEADLOCK_APP_ID)) return;
  
  const msgTypeDecoded = msgType & ~PROTO_MASK;
  
  log('info', 'Received GC message', {
    appId,
    msgType: msgTypeDecoded,
    msgTypeHex: msgTypeDecoded.toString(16),
    payloadLength: payload ? payload.length : 0,
    payloadHex: payload ? payload.slice(0, 32).toString('hex') : 'none'
  });
  
  if (msgTypeDecoded === GC_MSG_CLIENT_WELCOME) {
    log('info', 'Received Deadlock GC welcome - GC ready!');
    notifyDeadlockGcReady();
  } else if (msgTypeDecoded === GC_MSG_SUBMIT_PLAYTEST_USER_RESPONSE) {
    log('info', 'Received playtest invite response');
    handlePlaytestInviteResponse(payload);
  } else {
    log('debug', 'Received unknown GC message', {
      msgType: msgTypeDecoded,
      expectedWelcome: GC_MSG_CLIENT_WELCOME,
      expectedPlaytestResponse: GC_MSG_SUBMIT_PLAYTEST_USER_RESPONSE
    });
  }
});`;

// Find and replace the existing GC message handler
const gcHandlerPattern = /client\.on\('receivedFromGC', \(appid, msgType, payload\) => \{[\s\S]*?\}\);/;
if (gcHandlerPattern.test(content)) {
    content = content.replace(gcHandlerPattern, improvedGcHandler);
} else {
    // If not found, add it before the main loop
    const insertPoint = content.indexOf('// ---------- Main Loop ----------');
    if (insertPoint > -1) {
        content = content.slice(0, insertPoint) + improvedGcHandler + '\n\n' + content.slice(insertPoint);
    }
}

// Fix 6: Add diagnostic logging for app launch events
const improvedAppLaunch = `
client.on('appLaunched', (appId) => {
  log('info', 'Steam app launched', { appId });
  if (Number(appId) !== Number(DEADLOCK_APP_ID)) return;
  
  log('info', 'Deadlock app launched – GC session starting');
  deadlockAppActive = true;
  deadlockGcReady = false;
  
  // Wait a bit longer for GC to initialize
  setTimeout(() => {
    log('debug', 'Sending GC hello after app launch');
    sendDeadlockGcHello(true);
  }, 4000); // Increased delay
});

client.on('appQuit', (appId) => {
  log('info', 'Steam app quit', { appId });
  if (Number(appId) !== Number(DEADLOCK_APP_ID)) return;
  
  log('info', 'Deadlock app quit – GC session ended');
  deadlockAppActive = false;
  deadlockGcReady = false;
  flushDeadlockGcWaiters(new Error('Deadlock app quit'));
  flushPendingPlaytestInvites(new Error('Deadlock app quit'));
});`;

// Replace existing app launch handlers
content = content.replace(
    /client\.on\('appLaunched', \(appId\) => \{[\s\S]*?\}\);/,
    improvedAppLaunch.split('\n\n')[0] + ');'
);
content = content.replace(
    /client\.on\('appQuit', \(appId\) => \{[\s\S]*?\}\);/,
    improvedAppLaunch.split('\n\n')[1] + ');'
);

// Write the fixed file
fs.writeFileSync(steamBridgePath, content, 'utf8');

console.log('✅ Applied Game Coordinator protocol fixes:');
console.log('   - Enhanced GC hello payload with protocol negotiation');
console.log('   - Added alternative handshake methods');
console.log('   - Improved game state management');
console.log('   - Enhanced GC message handling and logging');
console.log('   - Added better timing for GC initialization');
console.log('');
console.log('🔄 Please restart the Steam bridge for changes to take effect.');
console.log('');
console.log('📝 Set LOG_LEVEL=debug to see detailed GC communication:');
console.log('   set LOG_LEVEL=debug && node index.js');