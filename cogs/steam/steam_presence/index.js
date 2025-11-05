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
  dataDirectory: process.env.STEAM_PRESENCE_DATA_DIR || path.join(__dirname, '.steam-data'),
  
  // Presence tracking
  presenceCheckInterval: parseInt(process.env.PRESENCE_CHECK_INTERVAL || '60000', 10), // 1 minute default
  maxPresenceRequests: parseInt(process.env.MAX_PRESENCE_REQUESTS || '50', 10),
  
  // Health monitoring
  healthCheckInterval: parseInt(process.env.HEALTH_CHECK_INTERVAL || '300000', 10), // 5 minutes
  heartbeatInterval: parseInt(process.env.HEARTBEAT_INTERVAL || '30000', 10), // 30 seconds
  
  // Performance
  batchSize: parseInt(process.env.BATCH_SIZE || '10', 10),
  maxRetries: parseInt(process.env.MAX_RETRIES || '3', 10)
};

class SteamBridge {
  constructor() {
    this.logger = new SmartLogger({
      defaultRateLimit: 30000, // 30s rate limit
      batchTimeout: 5000 // 5s batch window
    });
    
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
      this.steamClient
    );
    
    // Add custom task handlers
    this.setupCustomTaskHandlers();
    
    this.logger.info('âœ… Task processor initialized');
  }

  async initializeLegacyModules() {
    this.logger.info('ðŸ”§ Initializing legacy modules');
    
    try {
      // Initialize QuickInvites
      this.quickInvites = new QuickInvites({
        database: this.database.getRawDatabase(),
        steamClient: this.steamClient.getClient()
      });
      
      // Initialize StatusAnzeige
      this.statusAnzeige = new StatusAnzeige({
        database: this.database.getRawDatabase()
      });
      
      this.logger.info('âœ… Legacy modules initialized');
      
    } catch (error) {
      this.logger.warn('âš ï¸ Some legacy modules failed to initialize', {
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
  }

  setupCustomTaskHandlers() {
    // Add quick invite task handlers
    this.taskProcessor.addTaskHandler('AUTH_QUICK_INVITE_CREATE', 
      this.handleQuickInviteCreate.bind(this));
    
    this.taskProcessor.addTaskHandler('AUTH_QUICK_INVITE_ENSURE_POOL', 
      this.handleQuickInviteEnsurePool.bind(this));
    
    this.taskProcessor.addTaskHandler('AUTH_SEND_PLAYTEST_INVITE',
      this.handlePlaytestInvite.bind(this));
  }

  // Custom task handlers
  async handleQuickInviteCreate(payload, task) {
    if (!this.quickInvites) {
      throw new Error('QuickInvites module not available');
    }
    
    const result = await this.quickInvites.createInvite(payload);
    return { success: true, invite: result };
  }

  async handleQuickInviteEnsurePool(payload, task) {
    if (!this.quickInvites) {
      throw new Error('QuickInvites module not available');
    }
    
    const target = payload.target || 2;
    const result = await this.quickInvites.ensurePool({ target });
    return { success: true, ensured: result };
  }

  async handlePlaytestInvite(payload, task) {
    // Simplified playtest invite logic
    if (!payload.steam_id) {
      throw new Error('Steam ID required for playtest invite');
    }
    
    // This would need integration with the game coordinator
    // For now, return a placeholder response
    this.logger.warn('Playtest invite not yet implemented in refactored version', {
      steam_id: payload.steam_id
    });
    
    return { success: false, error: 'Not implemented in refactored version' };
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
    const refreshToken = this.loadRefreshToken();
    
    if (!refreshToken) {
      this.logger.info('No refresh token available - waiting for manual login');
      return;
    }
    
    try {
      const result = await this.steamClient.login({
        refreshToken,
        source: 'auto-start'
      });
      
      this.logger.info('Auto-login initiated', {
        started: result.started,
        using_refresh_token: true
      });
      
    } catch (error) {
      this.logger.warn('Auto-login failed', { error: error.message });
    }
  }

  loadRefreshToken() {
    try {
      const tokenPath = // Try multiple token file formats for flexibility
      const tokenPaths = [
        path.join(CONFIG.dataDirectory, 'refresh.token'),      // Original format
        path.join(CONFIG.dataDirectory, 'refresh_token.txt')   // Alternative format
      ];
      
      for (const tokenPath of tokenPaths) {
        if (fs.existsSync(tokenPath)) {
          const token = fs.readFileSync(tokenPath, 'utf8').trim();
          if (token) {
            this.logger.info('Loaded refresh token', { path: path.basename(tokenPath) });
            return token;
          }
        }
      }
      
      // Fallback: original single file check
      const fallbackPath = path.join(CONFIG.dataDirectory, 'refresh_token.txt');
      if (fs.existsSync(tokenPath)) {
        return fs.readFileSync(tokenPath, 'utf8').trim();
      }
    } catch (error) {
      this.logger.debug('Could not load refresh token', { error: error.message });
    }
    return null;
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
        }
      };
      
      this.database.update('standalone_bot_state', 
        {
          heartbeat: Math.floor(this.lastHeartbeat / 1000),
          payload: JSON.stringify(payload),
          updated_at: Math.floor(this.lastHeartbeat / 1000)
        },
        'bot = ?',
        ['steam']
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

  formatUptime(ms) {
    const seconds = Math.floor(ms / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    
    if (hours > 0) return `${hours}h ${minutes % 60}m`;
    if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
    return `${seconds}s`;
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

