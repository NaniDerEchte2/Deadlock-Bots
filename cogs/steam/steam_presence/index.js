#!/usr/bin/env node
'use strict';

/**
 * Steam Bridge - Refactored and Optimized
 * 
 * Main orchestration module that coordinates:
 * - Steam client connection and authentication
 * - Task processing from database
 * - Rich presence tracking (optimized)
 * - Quick invite management
 * - Health monitoring and reporting
 * 
 * Key improvements:
 * - Modular architecture with separation of concerns
 * - Intelligent logging with rate limiting
 * - Better error handling and recovery
 * - Reduced API calls and log spam
 * - Circuit breaker pattern for reliability
 */

const fs = require('fs');
const path = require('path');

function parseBoolean(value, fallback = null) {
  if (value === undefined || value === null || value === '') {
    return fallback;
  }

  const normalized = String(value).trim().toLowerCase();

  if (['1', 'true', 'yes', 'on'].includes(normalized)) {
    return true;
  }

  if (['0', 'false', 'no', 'off'].includes(normalized)) {
    return false;
  }

  return fallback;
}

const DATA_DIRECTORY = process.env.STEAM_PRESENCE_DATA_DIR || path.join(__dirname, '.steam-data');

const TOKEN_PATHS = {
  refresh: path.join(DATA_DIRECTORY, 'refresh.token'),
  refreshLegacy: path.join(DATA_DIRECTORY, 'refresh_token.txt'),
  machine: path.join(DATA_DIRECTORY, 'machine_auth_token.txt')
};

const DEFAULT_CREDENTIALS = {
  accountName: process.env.STEAM_BOT_USERNAME || process.env.STEAM_LOGIN || process.env.STEAM_ACCOUNT || '',
  password: process.env.STEAM_BOT_PASSWORD || process.env.STEAM_PASSWORD || '',
  rememberPassword: parseBoolean(process.env.STEAM_REMEMBER_PASSWORD || process.env.STEAM_REMEMBER, null)
};

// Core modules
const { SmartLogger } = require('./core/logger');
const { SteamClientManager } = require('./core/steam-client');
const { DatabaseManager } = require('./core/database');
const { TaskProcessor } = require('./core/task-processor');

// Legacy modules (to be refactored)
const { QuickInvites } = require('./quick_invites');
const { StatusAnzeige } = require('./statusanzeige');

// Configuration
const CONFIG = {
  // Database
  dbPath: process.env.DEADLOCK_DB_PATH || path.join(__dirname, '../../../service/deadlock.sqlite3'),
  
  // Steam
  deadlockAppId: parseInt(process.env.DEADLOCK_APPID || '1422450', 10),
  dataDirectory: DATA_DIRECTORY,
  
  // Presence tracking
  presenceCheckInterval: parseInt(process.env.PRESENCE_CHECK_INTERVAL || '60000', 10), // 1 minute default
  maxPresenceRequests: parseInt(process.env.MAX_PRESENCE_REQUESTS || '50', 10),
  
  // Health monitoring
  healthCheckInterval: parseInt(process.env.HEALTH_CHECK_INTERVAL || '300000', 10), // 5 minutes
  heartbeatInterval: parseInt(process.env.HEARTBEAT_INTERVAL || '30000', 10), // 30 seconds
  
  // Performance
  batchSize: parseInt(process.env.BATCH_SIZE || '10', 10),
  maxRetries: parseInt(process.env.MAX_RETRIES || '3', 10),

  tokenPaths: TOKEN_PATHS,
  defaultCredentials: DEFAULT_CREDENTIALS
};

class SteamBridge {
  constructor() {
    this.logger = new SmartLogger({
      defaultRateLimit: 30000, // 30s rate limit
      batchTimeout: 5000 // 5s batch window
    });

    this.defaultCredentials = {
      accountName: CONFIG.defaultCredentials.accountName,
      password: CONFIG.defaultCredentials.password,
      rememberPassword: CONFIG.defaultCredentials.rememberPassword
    };

    this.refreshToken = null;
    this.machineAuthToken = null;

    this.ensureDataDirectory();
    this.refreshToken = this.loadRefreshToken();
    this.machineAuthToken = this.loadMachineAuthToken();

    // Initialize components
    this.database = null;
    this.steamClient = null;
    this.taskProcessor = null;
    this.quickInvites = null;
    this.statusAnzeige = null;
    
    // State management
    this.isRunning = false;
    this.startTime = Date.now();
    this.lastHeartbeat = null;
    
    // Performance monitoring
    this.stats = {
      uptime: 0,
      tasksProcessed: 0,
      presenceUpdates: 0,
      errors: 0,
      lastError: null
    };
    
    // Intervals
    this.heartbeatTimer = null;
    this.healthCheckTimer = null;
    this.presenceTimer = null;
    
    // Presence tracking state
    this.presenceState = {
      lastCheck: 0,
      activeUsers: new Set(),
      pendingRequests: 0,
      requestQueue: []
    };
    
    this.setupSignalHandlers();
  }

  async initialize() {
    try {
      this.logger.info('ðŸš€ Initializing Steam Bridge', {
        version: '2.0.0-optimized',
        config: {
          db_path: CONFIG.dbPath,
          data_dir: CONFIG.dataDirectory,
          presence_interval: CONFIG.presenceCheckInterval
        }
      });
      
      // Initialize database
      await this.initializeDatabase();
      
      // Initialize Steam client
      await this.initializeSteamClient();
      
      // Initialize task processor
      await this.initializeTaskProcessor();
      
      // Initialize legacy modules (to be refactored)
      await this.initializeLegacyModules();
      
      this.logger.info('âœ… Steam Bridge initialization complete');
      return true;
      
    } catch (error) {
      this.logger.error('âŒ Initialization failed', { error: error.message });
      throw error;
    }
  }

  async initializeDatabase() {
    this.logger.info('ðŸ“Š Initializing database connection');

    this.database = new DatabaseManager(CONFIG.dbPath);
    await this.database.connect();

    try {
      const now = Math.floor(Date.now() / 1000);
      this.database.executeQuery(
        `INSERT INTO standalone_bot_state (bot, heartbeat, payload, updated_at)
         VALUES (?, ?, ?, ?)
         ON CONFLICT(bot) DO NOTHING`,
        ['steam', now, JSON.stringify({}), now]
      );
    } catch (error) {
      this.logger.warn('Failed to ensure standalone_bot_state row', { error: error.message });
    }

    this.logger.info('âœ… Database initialized', {
      path: CONFIG.dbPath
    });
  }

  async initializeSteamClient() {
    this.logger.info('ðŸŽ® Initializing Steam client');
    
    this.steamClient = new SteamClientManager({
      dataDirectory: CONFIG.dataDirectory
    });
    
    // Setup Steam client event handlers
    this.setupSteamEventHandlers();
    
    this.logger.info('âœ… Steam client initialized');
  }

  async initializeTaskProcessor() {
    this.logger.info('âš™ï¸ Initializing task processor');
    
    this.taskProcessor = new TaskProcessor(
      this.database.getRawDatabase(),
      this.steamClient,
      {
        buildLoginOptions: this.buildLoginOptions.bind(this)
      }
    );
    
    // Add custom task handlers
    this.setupCustomTaskHandlers();
    
    this.logger.info('âœ… Task processor initialized');
  }

  async initializeLegacyModules() {
    this.logger.info('🔧 Initializing legacy modules');
    
    try {
      // Initialize QuickInvites - correct parameter order: (db, client, log, opts)
      this.quickInvites = new QuickInvites(
        this.database.getRawDatabase(),
        this.steamClient.getClient(),
        this.createLegacyLogger()  // Pass proper logger function
      );
      
      // Initialize StatusAnzeige - correct parameter order: (client, log, options)
      this.statusAnzeige = new StatusAnzeige(
        this.steamClient.getClient(),
        this.createLegacyLogger(),  // Pass proper logger function
        {
          db: this.database.getRawDatabase()  // Pass db in options object
        }
      );
      
      this.logger.info('✅ Legacy modules initialized');
      
    } catch (error) {
      this.logger.warn('⚠️ Some legacy modules failed to initialize', {
        error: error.message
      });
    }
  }

  setupSteamEventHandlers() {
    const client = this.steamClient.getClient();
    
    // Game-specific events
    client.on('appLaunched', (appId) => {
      if (Number(appId) === CONFIG.deadlockAppId) {
        this.logger.info('ðŸŽ¯ Deadlock app launched - starting presence tracking');
        this.startPresenceTracking();
      }
    });
    
    client.on('appQuit', (appId) => {
      if (Number(appId) === CONFIG.deadlockAppId) {
        this.logger.info('ðŸŽ¯ Deadlock app quit - stopping presence tracking');
        this.stopPresenceTracking();
      }
    });
    
    // Rich presence events (optimized)
    client.on('user', (sid, user) => {
      this.handleUserUpdate(sid, user);
    });

    client.on('refreshToken', (token) => {
      this.logger.info('Received Steam refresh token update');
      this.saveRefreshToken(token);
    });

    client.on('machineAuthToken', (token) => {
      this.logger.info('Received Steam machine auth token update');
      this.saveMachineAuthToken(token);
    });
  }

  setupCustomTaskHandlers() {
    // Add quick invite task handlers
    this.taskProcessor.addTaskHandler('AUTH_QUICK_INVITE_CREATE', 
      this.handleQuickInviteCreate.bind(this));
    
    this.taskProcessor.addTaskHandler('AUTH_QUICK_INVITE_ENSURE_POOL', 
      this.handleQuickInviteEnsurePool.bind(this));
    
    this.taskProcessor.addTaskHandler('AUTH_SEND_PLAYTEST_INVITE',
      this.handlePlaytestInvite.bind(this));
    
    this.taskProcessor.addTaskHandler('AUTH_CHECK_FRIENDSHIP',
      this.handleCheckFriendship.bind(this));
    
    this.logger.info('✅ Custom task handlers registered', {
      handlers: ['AUTH_QUICK_INVITE_CREATE', 'AUTH_QUICK_INVITE_ENSURE_POOL', 'AUTH_SEND_PLAYTEST_INVITE', 'AUTH_CHECK_FRIENDSHIP']
    });
  }

  // Custom task handlers
  async handleQuickInviteCreate(payload, task) {
    if (!this.quickInvites) {
      throw new Error('QuickInvites module not available');
    }
    
    // Check if logged in
    const steamStatus = this.steamClient.getStatus();
    if (!steamStatus.logged_on) {
      throw new Error('Not logged in to Steam');
    }
    
    const options = {
      inviteLimit: payload?.invite_limit ?? payload?.inviteLimit ?? 1,
      inviteDuration: payload?.invite_duration ?? payload?.inviteDuration ?? null
    };
    
    const result = await this.quickInvites.createOne(options);
    return { 
      ok: true, 
      data: result
    };
  }

  async handleQuickInviteEnsurePool(payload, task) {
    if (!this.quickInvites) {
      throw new Error('QuickInvites module not available');
    }
    
    // Check if logged in
    const steamStatus = this.steamClient.getStatus();
    if (!steamStatus.logged_on) {
      throw new Error('Not logged in to Steam');
    }
    
    const options = {
      target: payload?.target ?? 2,
      inviteLimit: payload?.invite_limit ?? payload?.inviteLimit ?? 1,
      inviteDuration: payload?.invite_duration ?? payload?.inviteDuration ?? null
    };
    
    const result = await this.quickInvites.ensurePool(options);
    return { 
      ok: true, 
      data: result
    };
  }

  async handlePlaytestInvite(payload, task) {
    // Check if logged in
    const steamStatus = this.steamClient.getStatus();
    if (!steamStatus.logged_on) {
      throw new Error('Not logged in to Steam');
    }
    
    // Parse Steam ID and account ID
    const raw = payload?.steam_id ?? payload?.steam_id64;
    const timeoutMs = payload?.timeout_ms ?? payload?.response_timeout_ms;
    
    if (!raw) {
      throw new Error('Steam ID required for playtest invite');
    }
    
    // Convert Steam ID to account ID using proper Steam ID parsing
    let accountId;
    if (payload?.account_id != null) {
      accountId = Number(payload.account_id);
    } else {
      try {
        // Use Steam ID library for proper conversion
        const SteamID = require('steamid');
        const sid = new SteamID(String(raw));
        if (!sid.isValid()) {
          throw new Error('Invalid Steam ID format');
        }
        accountId = sid.accountid;
      } catch (e) {
        // Fallback to simple conversion
        const steamId64 = typeof raw === 'string' ? BigInt(raw) : BigInt(String(raw));
        accountId = Number(steamId64 - BigInt('76561197960265728'));
      }
    }
    
    if (!Number.isFinite(accountId) || accountId <= 0) {
      throw new Error('Invalid account ID derived from Steam ID');
    }
    
    const location = (typeof payload?.location === 'string' ? payload.location.trim() : '') || 'discord-betainvite';
    const inviteTimeout = Number.isFinite(Number(timeoutMs)) ? Number(timeoutMs) : 15000; // 15s default
    
    try {
      // Ensure Deadlock game is active and GC is ready
      await this.ensureDeadlockGameActive();
      await this.waitForDeadlockGC(inviteTimeout);
      
      // Send the actual playtest invite
      const response = await this.sendPlaytestInviteToGC(accountId, location, inviteTimeout);
      
      this.logger.info('Playtest invite completed', {
        steam_id: raw,
        account_id: accountId,
        location,
        success: response.success,
        response_code: response.code
      });
      
      return {
        ok: Boolean(response && response.success),
        data: {
          steam_id64: String(raw),
          account_id: accountId,
          location,
          response,
        },
      };
      
    } catch (error) {
      this.logger.error('Playtest invite failed', {
        steam_id: raw,
        account_id: accountId,
        location,
        error: error.message
      });
      
      // Return failure but don't throw - let task processor handle it
      return {
        ok: false,
        error: error.message,
        data: {
          steam_id64: String(raw),
          account_id: accountId,
          location,
        }
      };
    }
  }

  async handleCheckFriendship(payload, task) {
    // Check if logged in
    const steamStatus = this.steamClient.getStatus();
    if (!steamStatus.logged_on) {
      throw new Error('Not logged in to Steam');
    }
    
    const steamId = payload?.steam_id ?? payload?.steam_id64;
    if (!steamId) {
      throw new Error('Steam ID required for friendship check');
    }
    
    try {
      const client = this.steamClient.getClient();
      
      // Check if user is in friends list
      const friends = client.myFriends || {};
      const isFriend = Object.prototype.hasOwnProperty.call(friends, steamId);
      
      this.logger.debug('Friendship check completed', {
        steam_id: steamId,
        is_friend: isFriend
      });
      
      return {
        ok: true,
        data: {
          steam_id64: String(steamId),
          is_friend: isFriend,
          checked_at: Date.now()
        }
      };
      
    } catch (error) {
      this.logger.error('Friendship check failed', {
        steam_id: steamId,
        error: error.message
      });
      
      return {
        ok: false,
        error: error.message,
        data: {
          steam_id64: String(steamId)
        }
      };
    }
  }

  // Deadlock Game Coordinator helpers
  async ensureDeadlockGameActive() {
    const client = this.steamClient.getClient();
    if (!client) {
      throw new Error('Steam client not available');
    }
    
    try {
      // Request Deadlock game session
      client.gamesPlayed([CONFIG.deadlockAppId]);
      this.logger.debug('Requested Deadlock game session', { 
        app_id: CONFIG.deadlockAppId 
      });
    } catch (error) {
      throw new Error(`Failed to start Deadlock game session: ${error.message}`);
    }
  }

  async waitForDeadlockGC(timeoutMs = 20000) {
    const client = this.steamClient.getClient();
    
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error('Timeout waiting for Deadlock Game Coordinator'));
      }, timeoutMs);
      
      // Check if GC is already ready
      if (client.gc && client.gc.Deadlock) {
        clearTimeout(timer);
        resolve(true);
        return;
      }
      
      // Send GC hello message
      const hello = this.createGCHelloMessage();
      try {
        client.sendToGC(CONFIG.deadlockAppId, 0x80000000 + 4004, {}, hello);
        
        // Wait for GC welcome response
        const welcomeHandler = () => {
          clearTimeout(timer);
          client.removeListener('receivedFromGC', welcomeHandler);
          resolve(true);
        };
        
        client.on('receivedFromGC', (appid, msgType, payload) => {
          if (appid === CONFIG.deadlockAppId && msgType === (0x80000000 + 4005)) {
            welcomeHandler();
          }
        });
        
      } catch (error) {
        clearTimeout(timer);
        reject(new Error(`Failed to communicate with Deadlock GC: ${error.message}`));
      }
    });
  }

  createGCHelloMessage() {
    // Create protobuf message for GC hello
    // Field 1 (protocol version): varint
    const protocolVersion = 1;
    const tag = this.encodeVarint((1 << 3) | 0); // Field 1, wire type 0 (varint)
    const version = this.encodeVarint(protocolVersion);
    return Buffer.concat([tag, version]);
  }

  async sendPlaytestInviteToGC(accountId, location, timeoutMs) {
    const client = this.steamClient.getClient();
    
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error('Timeout waiting for playtest invite response'));
      }, timeoutMs);
      
      // Create the playtest invite message
      const payload = this.encodePlaytestInviteMessage(accountId, location);
      
      // Set up response handler
      const responseHandler = (appid, msgType, responsePayload) => {
        if (appid === CONFIG.deadlockAppId && msgType === (0x80000000 + 9190)) {
          clearTimeout(timer);
          client.removeListener('receivedFromGC', responseHandler);
          
          try {
            const response = this.decodePlaytestInviteResponse(responsePayload);
            resolve(response);
          } catch (error) {
            reject(new Error(`Failed to decode playtest response: ${error.message}`));
          }
        }
      };
      
      client.on('receivedFromGC', responseHandler);
      
      try {
        // Send the playtest invite message (GC_MSG_SUBMIT_PLAYTEST_USER = 9189)
        client.sendToGC(CONFIG.deadlockAppId, 0x80000000 + 9189, {}, payload);
        this.logger.debug('Sent playtest invite to GC', { 
          account_id: accountId, 
          location 
        });
      } catch (error) {
        clearTimeout(timer);
        client.removeListener('receivedFromGC', responseHandler);
        reject(error);
      }
    });
  }

  encodePlaytestInviteMessage(accountId, location) {
    const parts = [];
    
    // Add location if provided (field 3, string)
    if (location) {
      const locStr = Buffer.from(String(location), 'utf8');
      parts.push(this.encodeVarint((3 << 3) | 2)); // Field 3, wire type 2 (length-delimited)
      parts.push(this.encodeVarint(locStr.length));
      parts.push(locStr);
    }
    
    // Add account ID (field 4, varint)
    if (Number.isFinite(accountId)) {
      parts.push(this.encodeVarint((4 << 3) | 0)); // Field 4, wire type 0 (varint)
      parts.push(this.encodeVarint(Number(accountId) >>> 0));
    }
    
    return parts.length ? Buffer.concat(parts) : Buffer.alloc(0);
  }

  decodePlaytestInviteResponse(buffer) {
    if (!buffer || !buffer.length) {
      return { success: false, code: null, message: 'Empty response' };
    }
    
    let offset = 0;
    let responseCode = null;
    
    while (offset < buffer.length) {
      try {
        const { value: tag, nextOffset } = this.decodeVarint(buffer, offset);
        offset = nextOffset;
        
        const fieldNumber = tag >>> 3;
        const wireType = tag & 0x07;
        
        if (fieldNumber === 1 && wireType === 0) {
          // Response code field
          const { value } = this.decodeVarint(buffer, offset);
          responseCode = value >>> 0;
          break;
        }
        
        // Skip unknown fields
        offset = this.skipField(buffer, offset, wireType);
        if (offset < 0) break;
        
      } catch (error) {
        this.logger.warn('Failed to decode playtest response', { error: error.message });
        break;
      }
    }
    
    // Map response codes to messages
    const responseMap = {
      0: { success: true, message: 'Invite sent successfully' },
      1: { success: false, message: 'Internal error' },
      3: { success: false, message: 'Target is not a confirmed Steam friend' },
      4: { success: false, message: 'Friendship exists less than 30 days' },
      5: { success: false, message: 'Account already owns Deadlock' },
      6: { success: false, message: 'Target account is limited' },
      7: { success: false, message: 'Invite limit reached' }
    };
    
    const result = responseMap[responseCode] || { 
      success: false, 
      message: 'Unknown response from Game Coordinator' 
    };
    
    return {
      ...result,
      code: responseCode
    };
  }

  encodeVarint(value) {
    let v = Number(value >>> 0);
    const bytes = [];
    while (v >= 0x80) {
      bytes.push((v & 0x7f) | 0x80);
      v >>>= 7;
    }
    bytes.push(v);
    return Buffer.from(bytes);
  }

  decodeVarint(buffer, offset = 0) {
    let result = 0;
    let shift = 0;
    let position = offset;
    
    while (position < buffer.length) {
      const byte = buffer[position++];
      result |= (byte & 0x7f) << shift;
      if ((byte & 0x80) === 0) {
        return { value: result >>> 0, nextOffset: position };
      }
      shift += 7;
      if (shift > 35) break;
    }
    
    throw new Error('Truncated varint');
  }

  skipField(buffer, offset, wireType) {
    switch (wireType) {
      case 0: {
        const { nextOffset } = this.decodeVarint(buffer, offset);
        return nextOffset;
      }
      case 1:
        return offset + 8;
      case 2: {
        const { value: length, nextOffset } = this.decodeVarint(buffer, offset);
        return nextOffset + length;
      }
      case 5:
        return offset + 4;
      default:
        return -1;
    }
  }

  // Optimized presence tracking
  startPresenceTracking() {
    if (this.presenceTimer) {
      clearInterval(this.presenceTimer);
    }
    
    this.presenceTimer = setInterval(() => {
      this.checkPresence().catch(error => {
        this.logger.warn('Presence check failed', { error: error.message });
      });
    }, CONFIG.presenceCheckInterval);
    
    this.logger.info('â–¶ï¸ Presence tracking started', {
      interval_ms: CONFIG.presenceCheckInterval
    });
  }

  stopPresenceTracking() {
    if (this.presenceTimer) {
      clearInterval(this.presenceTimer);
      this.presenceTimer = null;
    }
    
    this.logger.info('â¹ï¸ Presence tracking stopped');
  }

  async checkPresence() {
    const now = Date.now();
    
    // Rate limiting check
    if (now - this.presenceState.lastCheck < CONFIG.presenceCheckInterval) {
      return; // Too soon
    }
    
    // Don't overwhelm with requests
    if (this.presenceState.pendingRequests >= CONFIG.maxPresenceRequests) {
      this.logger.warn('Too many pending presence requests - skipping check');
      return;
    }
    
    this.presenceState.lastCheck = now;
    
    try {
      // Get users that need presence checking (from database)
      const users = this.getUsersForPresenceCheck();
      
      if (users.length === 0) {
        return; // No users to check
      }
      
      // Batch process users instead of individual requests
      await this.batchProcessPresence(users);
      
      this.stats.presenceUpdates++;
      
      // Only log summary, not individual requests
      this.logger.logSummary('Presence Check', {
        users_checked: users.length,
        pending_requests: this.presenceState.pendingRequests,
        active_users: this.presenceState.activeUsers.size
      });
      
    } catch (error) {
      this.stats.errors++;
      this.stats.lastError = error.message;
      this.logger.error('Presence check batch failed', { error: error.message });
    }
  }

  getUsersForPresenceCheck() {
    try {
      // Get users that need presence updates (implement your logic)
      const users = this.database.selectAll(`
        SELECT DISTINCT steam_id64 
        FROM some_presence_table 
        WHERE last_check < ? 
        LIMIT ?
      `, [Date.now() - CONFIG.presenceCheckInterval, CONFIG.batchSize]);
      
      return users.map(u => u.steam_id64).filter(Boolean);
    } catch (error) {
      this.logger.error('Failed to get users for presence check', { error: error.message });
      return [];
    }
  }

  async batchProcessPresence(users) {
    // This is where the optimized batch presence checking would go
    // Instead of individual API calls for each user, batch them
    
    this.presenceState.pendingRequests++;
    
    try {
      // Simplified batch processing placeholder
      // In real implementation, this would use Steam API efficiently
      
      for (const userId of users) {
        this.presenceState.activeUsers.add(userId);
      }
      
    } finally {
      this.presenceState.pendingRequests--;
    }
  }

  handleUserUpdate(steamId, user) {
    // Handle user presence updates efficiently
    // Only log significant changes, not every update
    
    if (user.gameextrainfo && user.gameextrainfo.includes('Deadlock')) {
      if (!this.presenceState.activeUsers.has(steamId)) {
        this.presenceState.activeUsers.add(steamId);
        this.logger.debug('User entered Deadlock', { steam_id: steamId });
      }
    } else {
      if (this.presenceState.activeUsers.has(steamId)) {
        this.presenceState.activeUsers.delete(steamId);
        this.logger.debug('User left Deadlock', { steam_id: steamId });
      }
    }
  }

  // Lifecycle management
  async start() {
    if (this.isRunning) {
      this.logger.warn('Steam Bridge already running');
      return;
    }
    
    this.logger.info('ðŸš€ Starting Steam Bridge');
    
    try {
      // Start task processor
      this.taskProcessor.start();
      
      // Start status display
      if (this.statusAnzeige) {
        this.statusAnzeige.start();
      }
      
      // Start quick invites auto-ensure
      if (this.quickInvites && typeof this.quickInvites.startAutoEnsure === 'function') {
        this.quickInvites.startAutoEnsure();
      }
      
      // Start monitoring
      this.startHeartbeat();
      this.startHealthChecks();
      
      // Attempt auto-login
      await this.attemptAutoLogin();
      
      this.isRunning = true;
      this.logger.info('âœ… Steam Bridge started successfully');
      
    } catch (error) {
      this.logger.error('âŒ Failed to start Steam Bridge', { error: error.message });
      throw error;
    }
  }

  async stop() {
    if (!this.isRunning) return;
    
    this.logger.info('ðŸ›‘ Stopping Steam Bridge');
    
    try {
      // Stop all intervals
      this.stopHeartbeat();
      this.stopHealthChecks();
      this.stopPresenceTracking();
      
      // Stop components
      if (this.taskProcessor) {
        this.taskProcessor.stop();
      }
      
      if (this.statusAnzeige) {
        this.statusAnzeige.stop();
      }
      
      if (this.quickInvites && typeof this.quickInvites.stopAutoEnsure === 'function') {
        this.quickInvites.stopAutoEnsure();
      }
      
      // Logout from Steam
      if (this.steamClient) {
        await this.steamClient.logout();
      }
      
      // Close database
      if (this.database) {
        this.database.disconnect();
      }
      
      this.isRunning = false;
      this.logger.info('âœ… Steam Bridge stopped');
      
    } catch (error) {
      this.logger.error('Error during shutdown', { error: error.message });
    }
  }

  async attemptAutoLogin() {
    const refreshToken = this.refreshToken || this.loadRefreshToken();

    if (!refreshToken) {
      this.logger.info('No refresh token available - waiting for manual login');
      return;
    }

    try {
      const loginOptions = {
        refreshToken,
        source: 'auto-start'
      };

      const machineAuthToken = this.getMachineAuthToken();
      if (machineAuthToken) {
        loginOptions.machineAuthToken = machineAuthToken;
      }

      const result = await this.steamClient.login(loginOptions);

      this.logger.info('Auto-login initiated', {
        started: result.started,
        using_refresh_token: true
      });

    } catch (error) {
      this.logger.warn('Auto-login failed', { error: error.message });
    }
  }

  ensureDataDirectory() {
    try {
      fs.mkdirSync(CONFIG.dataDirectory, { recursive: true });
    } catch (error) {
      this.logger.warn('Failed to ensure data directory', { error: error.message });
    }
  }

  loadRefreshToken() {
    let token = null;

    try {
      for (const tokenPath of [CONFIG.tokenPaths.refresh, CONFIG.tokenPaths.refreshLegacy]) {
        if (!tokenPath) continue;
        if (fs.existsSync(tokenPath)) {
          const candidate = fs.readFileSync(tokenPath, 'utf8').trim();
          if (candidate) {
            token = candidate;
            this.logger.info('Loaded refresh token', { path: path.basename(tokenPath) });
            break;
          }
        }
      }
    } catch (error) {
      this.logger.debug('Could not load refresh token', { error: error.message });
    }

    this.refreshToken = token || null;
    return this.refreshToken;
  }

  saveRefreshToken(token) {
    const normalized = typeof token === 'string' ? token.trim() : '';
    this.refreshToken = normalized || null;

    let persisted = false;

    for (const tokenPath of [CONFIG.tokenPaths.refresh, CONFIG.tokenPaths.refreshLegacy]) {
      if (!tokenPath) continue;

      try {
        if (!this.refreshToken) {
          fs.rmSync(tokenPath, { force: true });
        } else {
          fs.writeFileSync(tokenPath, `${this.refreshToken}\n`, 'utf8');
        }
        persisted = true;
      } catch (error) {
        this.logger.warn('Failed to persist refresh token', {
          path: path.basename(tokenPath),
          error: error.message
        });
      }
    }

    if (!persisted) {
      return;
    }

    if (this.refreshToken) {
      this.logger.info('Stored refresh token', { path: path.basename(CONFIG.tokenPaths.refresh) });
    } else {
      this.logger.info('Cleared stored refresh token');
    }
  }

  loadMachineAuthToken() {
    let token = null;

    try {
      const machinePath = CONFIG.tokenPaths.machine;
      if (machinePath && fs.existsSync(machinePath)) {
        const candidate = fs.readFileSync(machinePath, 'utf8').trim();
        if (candidate) {
          token = candidate;
          this.logger.info('Loaded machine auth token', { path: path.basename(machinePath) });
        }
      }
    } catch (error) {
      this.logger.debug('Could not load machine auth token', { error: error.message });
    }

    this.machineAuthToken = token || null;
    return this.machineAuthToken;
  }

  saveMachineAuthToken(token) {
    const normalized = typeof token === 'string' ? token.trim() : '';
    this.machineAuthToken = normalized || null;

    const machinePath = CONFIG.tokenPaths.machine;
    if (!machinePath) {
      return;
    }

    let persisted = false;

    try {
      if (!this.machineAuthToken) {
        fs.rmSync(machinePath, { force: true });
      } else {
        fs.writeFileSync(machinePath, `${this.machineAuthToken}\n`, 'utf8');
      }
      persisted = true;
    } catch (error) {
      this.logger.warn('Failed to persist machine auth token', {
        path: path.basename(machinePath),
        error: error.message
      });
    }

    if (!persisted) {
      return;
    }

    if (this.machineAuthToken) {
      this.logger.info('Stored machine auth token', { path: path.basename(machinePath) });
    } else {
      this.logger.info('Cleared stored machine auth token');
    }
  }

  getMachineAuthToken() {
    if (this.machineAuthToken) {
      return this.machineAuthToken;
    }

    return this.loadMachineAuthToken();
  }

  buildLoginOptions(payload = {}) {
    const loginPayload = payload || {};

    const providedRefreshToken = loginPayload.refresh_token || loginPayload.refreshToken;
    const forceCredentials = Boolean(
      loginPayload.force_credentials ||
      loginPayload.forceCredentials ||
      (Object.prototype.hasOwnProperty.call(loginPayload, 'use_refresh_token') && !loginPayload.use_refresh_token)
    );

    if (providedRefreshToken && !forceCredentials) {
      const normalized = typeof providedRefreshToken === 'string'
        ? providedRefreshToken.trim()
        : providedRefreshToken;

      if (normalized) {
        this.refreshToken = normalized;
        return { refreshToken: normalized };
      }
    }

    const accountName = loginPayload.account_name ||
      loginPayload.accountName ||
      this.defaultCredentials.accountName;
    const password = loginPayload.password ||
      loginPayload.account_password ||
      this.defaultCredentials.password;

    if (!accountName || !password) {
      throw new Error('Missing Steam account credentials');
    }

    const options = {
      accountName,
      password
    };

    const twoFactor = loginPayload.two_factor_code || loginPayload.twoFactorCode;
    if (twoFactor) {
      options.twoFactorCode = String(twoFactor).trim();
    }

    const authCode = loginPayload.auth_code || loginPayload.authCode;
    if (authCode) {
      options.authCode = String(authCode).trim();
    }

    if (Object.prototype.hasOwnProperty.call(loginPayload, 'remember_password')) {
      options.rememberPassword = Boolean(loginPayload.remember_password);
    } else if (this.defaultCredentials.rememberPassword !== null) {
      options.rememberPassword = this.defaultCredentials.rememberPassword;
    }

    const providedMachineToken = loginPayload.machine_auth_token || loginPayload.machineAuthToken;
    if (providedMachineToken) {
      options.machineAuthToken = String(providedMachineToken).trim();
      this.machineAuthToken = options.machineAuthToken;
    } else {
      const storedMachineToken = this.getMachineAuthToken();
      if (storedMachineToken) {
        options.machineAuthToken = storedMachineToken;
      }
    }

    if (loginPayload.account_name) {
      this.defaultCredentials.accountName = loginPayload.account_name;
    }

    if (loginPayload.password) {
      this.defaultCredentials.password = loginPayload.password;
    }

    return options;
  }

  // Monitoring
  startHeartbeat() {
    this.heartbeatTimer = setInterval(() => {
      this.publishHeartbeat();
    }, CONFIG.heartbeatInterval);
  }

  stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  startHealthChecks() {
    this.healthCheckTimer = setInterval(() => {
      this.performHealthCheck();
    }, CONFIG.healthCheckInterval);
  }

  stopHealthChecks() {
    if (this.healthCheckTimer) {
      clearInterval(this.healthCheckTimer);
      this.healthCheckTimer = null;
    }
  }

  publishHeartbeat() {
    this.lastHeartbeat = Date.now();
    this.stats.uptime = this.lastHeartbeat - this.startTime;
    
    // Update database with heartbeat (simplified)
    try {
      const payload = {
        runtime: this.getSystemStats(),
        steam: this.steamClient.getStatus(),
        tasks: this.taskProcessor.getStatistics(),
        presence: {
          active_users: this.presenceState.activeUsers.size,
          pending_requests: this.presenceState.pendingRequests
        },
        components: this.getComponentStatuses()
      };

      const heartbeat = Math.floor(this.lastHeartbeat / 1000);
      const payloadJson = JSON.stringify(payload);

      this.database.executeQuery(
        `INSERT INTO standalone_bot_state (bot, heartbeat, payload, updated_at)
         VALUES (?, ?, ?, ?)
         ON CONFLICT(bot) DO UPDATE SET
           heartbeat = excluded.heartbeat,
           payload = excluded.payload,
           updated_at = excluded.updated_at`,
        ['steam', heartbeat, payloadJson, heartbeat]
      );

    } catch (error) {
      this.logger.error('Failed to publish heartbeat', { error: error.message });
    }
  }

  async performHealthCheck() {
    try {
      // Database health
      await this.database.performHealthCheck();
      
      // Steam client health
      const steamStatus = this.steamClient.getStatus();
      this.logger.logHealth('Steam Client', 
        steamStatus.logged_on ? 'healthy' : 'disconnected',
        steamStatus
      );
      
      // Task processor health
      const taskStats = this.taskProcessor.getStatistics();
      this.logger.logHealth('Task Processor',
        taskStats.is_processing ? 'healthy' : 'stopped',
        taskStats
      );
      
      // Overall system health
      this.logger.logSummary('System Health', this.getSystemStats());
      
    } catch (error) {
      this.logger.error('Health check failed', { error: error.message });
    }
  }

  getSystemStats() {
    return {
      uptime_ms: this.stats.uptime,
      uptime_human: this.formatUptime(this.stats.uptime),
      tasks_processed: this.stats.tasksProcessed,
      presence_updates: this.stats.presenceUpdates,
      total_errors: this.stats.errors,
      last_error: this.stats.lastError,
      memory_usage_mb: Math.round(process.memoryUsage().rss / 1024 / 1024),
      is_running: this.isRunning
    };
  }

  getComponentStatuses() {
    const steamStatus = this.steamClient ? { ...this.steamClient.getStatus() } : null;
    const taskStatus = this.taskProcessor ? { ...this.taskProcessor.getStatistics() } : null;
    const quickInvitesStatus =
      this.quickInvites && typeof this.quickInvites.getStatus === 'function'
        ? this.quickInvites.getStatus()
        : null;
    const statusAnzeigeStatus =
      this.statusAnzeige && typeof this.statusAnzeige.getStatus === 'function'
        ? this.statusAnzeige.getStatus()
        : null;

    return {
      steam_client: steamStatus,
      task_processor: taskStatus,
      quick_invites: quickInvitesStatus,
      statusanzeige: statusAnzeigeStatus,
      presence_tracker: {
        timer_active: Boolean(this.presenceTimer),
        pending_requests: this.presenceState.pendingRequests,
        active_users: this.presenceState.activeUsers.size,
        last_check_at: this.presenceState.lastCheck || null,
        interval_ms: CONFIG.presenceCheckInterval
      }
    };
  }

  formatUptime(ms) {
    const seconds = Math.floor(ms / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    
    if (hours > 0) return `${hours}h ${minutes % 60}m`;
    if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
    return `${seconds}s`;
  }

  // Create legacy logger format that legacy modules expect
  createLegacyLogger() {
    return (level, message, extra = {}) => {
      switch (level) {
        case 'debug':
          this.logger.debug(message, extra);
          break;
        case 'info':
          this.logger.info(message, extra);
          break;
        case 'warn':
          this.logger.warn(message, extra);
          break;
        case 'error':
          this.logger.error(message, extra);
          break;
        default:
          this.logger.info(message, extra);
      }
    };
  }

  setupSignalHandlers() {
    process.on('SIGINT', () => this.gracefulShutdown(0));
    process.on('SIGTERM', () => this.gracefulShutdown(0));
    process.on('uncaughtException', (err) => {
      this.logger.error('Uncaught exception', { error: err.stack });
      this.gracefulShutdown(1);
    });
    process.on('unhandledRejection', (err) => {
      this.logger.error('Unhandled rejection', { error: err.stack });
    });
  }

  async gracefulShutdown(code = 0) {
    this.logger.info('ðŸ”„ Graceful shutdown initiated');
    
    try {
      await this.stop();
      process.exit(code);
    } catch (error) {
      this.logger.error('Error during graceful shutdown', { error: error.message });
      process.exit(1);
    }
  }
}

// Main execution
async function main() {
  const bridge = new SteamBridge();
  
  try {
    await bridge.initialize();
    await bridge.start();
  } catch (error) {
    console.error('Failed to start Steam Bridge:', error.message);
    process.exit(1);
  }
}

// Start if this is the main module
if (require.main === module) {
  main();
}

module.exports = { SteamBridge };

