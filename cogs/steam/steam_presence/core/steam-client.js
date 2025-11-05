'use strict';

/**
 * Steam Client Management Module
 * - Handles Steam connection and authentication
 * - Manages login state and reconnection logic
 * - Provides clean interface for Steam operations
 */

const SteamUser = require('steam-user');
const { SmartLogger } = require('./logger');

class SteamClientManager {
  constructor(options = {}) {
    this.logger = new SmartLogger();
    this.client = new SteamUser({
      dataDirectory: options.dataDirectory,
      autoRelogin: false,
      enablePicsCache: false,
      changelistUpdateInterval: 0
    });
    
    this.state = {
      loggedOn: false,
      loggingIn: false,
      steamId64: null,
      lastError: null,
      loginAttempts: 0,
      maxLoginAttempts: 3,
      guardRequired: null
    };
    
    this.reconnectTimeout = null;
    this.reconnectDelay = 5000; // 5 seconds base delay
    this.maxReconnectDelay = 300000; // 5 minutes max delay
    
    this.setupEventHandlers();
  }

  setupEventHandlers() {
    this.client.on('loggedOn', (details) => {
      this.state.loggedOn = true;
      this.state.loggingIn = false;
      this.state.loginAttempts = 0;
      this.state.guardRequired = null;
      this.pendingGuardCallback = null;
      this.reconnectDelay = 5000; // Reset delay on successful login
      
      if (this.client.steamID && typeof this.client.steamID.getSteamID64 === 'function') {
        this.state.steamId64 = this.client.steamID.getSteamID64();
      }
      
      this.logger.info('Steam login successful', {
        steam_id64: this.state.steamId64,
        country: details?.publicIPCountry,
        cellId: details?.cellID
      });
      
      // Set away status to reduce API calls
      try {
        this.client.setPersona(SteamUser.EPersonaState.Away);
      } catch (err) {
        this.logger.warn('Failed to set persona away', { error: err.message });
      }
    });

    this.client.on('disconnected', (eresult, msg) => {
      this.state.loggedOn = false;
      this.state.loggingIn = false;
      
      this.logger.warn('Steam disconnected', { eresult, message: msg });
      this.scheduleReconnect('disconnect');
    });

    this.client.on('error', (err) => {
      this.state.lastError = {
        message: err?.message || String(err),
        eresult: err?.eresult
      };
      this.state.loggingIn = false;
      
      this.logger.error('Steam client error', {
        error: this.state.lastError.message,
        eresult: this.state.lastError.eresult
      });
      
      const errorMsg = String(err?.message || '').toLowerCase();
      
      // Handle specific error types
      if (errorMsg.includes('invalid refresh') || errorMsg.includes('expired')) {
        this.logger.warn('Authentication token expired - manual login required');
        return; // Don't auto-reconnect on auth failures
      }
      
      if (errorMsg.includes('ratelimit') || errorMsg.includes('rate limit')) {
        this.logger.warn('Rate limit encountered - delaying reconnect');
        this.scheduleReconnect('ratelimit', 60000); // 1 minute delay for rate limits
        return;
      }
      
      this.scheduleReconnect('error');
    });

    this.client.on('steamGuard', (domain, callback) => {
      this.logger.info('Steam Guard challenge required', {
        domain: domain || 'unknown',
        type: this.getGuardType(domain)
      });

      // Store guard callback for external handling
      this.pendingGuardCallback = callback;
      this.state.guardRequired = {
        domain: domain || null,
        type: this.getGuardType(domain)
      };
    });

    this.client.on('refreshToken', (token) => {
      this.logger.debug('Refresh token updated');
      // Token handling should be done by caller
    });
  }

  getGuardType(domain) {
    if (!domain) return 'unknown';
    const norm = String(domain).toLowerCase();
    
    if (norm.includes('email')) return 'email';
    if (norm.includes('two-factor') || norm.includes('authenticator') || norm.includes('mobile')) return 'totp';
    if (norm.includes('device')) return 'device';
    return 'unknown';
  }

  scheduleReconnect(reason, customDelay = null) {
    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout);
    }
    
    const delay = customDelay || this.getReconnectDelay();
    
    this.logger.info(`Scheduling reconnect in ${delay}ms`, { reason });
    
    this.reconnectTimeout = setTimeout(() => {
      this.attemptReconnect();
    }, delay);
  }

  getReconnectDelay() {
    // Exponential backoff with jitter
    const baseDelay = Math.min(this.reconnectDelay, this.maxReconnectDelay);
    const jitter = Math.random() * 1000; // 0-1s jitter
    
    this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, this.maxReconnectDelay);
    
    return baseDelay + jitter;
  }

  async attemptReconnect() {
    if (this.state.loggedOn || this.state.loggingIn) {
      this.logger.debug('Skipping reconnect - already connected/connecting');
      return;
    }
    
    this.state.loginAttempts++;
    
    if (this.state.loginAttempts > this.state.maxLoginAttempts) {
      this.logger.error('Max login attempts reached - stopping auto-reconnect');
      return;
    }
    
    this.logger.info('Attempting reconnect', { 
      attempt: this.state.loginAttempts,
      maxAttempts: this.state.maxLoginAttempts
    });
    
    // Reconnect logic should be handled by external token management
    // This just provides the interface
  }

  async login(credentials = {}) {
    if (this.state.loggingIn) {
      throw new Error('Login already in progress');
    }

    if (this.state.loggedOn) {
      this.logger.debug('Already logged in');
      return { success: true, alreadyLoggedIn: true };
    }

    this.state.loggingIn = true;
    this.state.lastError = null;

    try {
      const { source, ...loginOptions } = credentials;

      if (!loginOptions.refreshToken && !loginOptions.accountName) {
        throw new Error('No login credentials provided');
      }

      this.logger.info('Initiating Steam login', {
        using_refresh_token: Boolean(loginOptions.refreshToken),
        source: source || 'manual'
      });

      await this.client.logOn(loginOptions);
      return { success: true, started: true };

    } catch (error) {
      this.state.loggingIn = false;
      this.logger.error('Login failed', { error: error.message });
      throw error;
    }
  }

  async logout() {
    if (!this.state.loggedOn) {
      return { success: true, alreadyLoggedOut: true };
    }
    
    this.logger.info('Logging out from Steam');
    
    try {
      this.client.logOff();
      this.state.loggedOn = false;
      this.state.steamId64 = null;
      this.state.guardRequired = null;
      this.pendingGuardCallback = null;

      // Clear reconnect timer
      if (this.reconnectTimeout) {
        clearTimeout(this.reconnectTimeout);
        this.reconnectTimeout = null;
      }
      
      return { success: true };
    } catch (error) {
      this.logger.error('Logout failed', { error: error.message });
      throw error;
    }
  }

  submitGuardCode(code) {
    if (!this.pendingGuardCallback) {
      throw new Error('No pending Steam Guard challenge');
    }

    this.logger.info('Submitting Steam Guard code');

    try {
      this.pendingGuardCallback(code);
      this.pendingGuardCallback = null;
      this.state.guardRequired = null;
      return { success: true };
    } catch (error) {
      this.logger.error('Failed to submit guard code', { error: error.message });
      throw error;
    }
  }

  getStatus() {
    return {
      logged_on: this.state.loggedOn,
      logging_in: this.state.loggingIn,
      steam_id64: this.state.steamId64,
      last_error: this.state.lastError,
      login_attempts: this.state.loginAttempts,
      has_pending_guard: Boolean(this.pendingGuardCallback),
      guard_required: this.state.guardRequired
    };
  }

  // Get the underlying client for advanced operations
  getClient() {
    return this.client;
  }
}

module.exports = { SteamClientManager };