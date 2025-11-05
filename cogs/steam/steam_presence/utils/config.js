'use strict';

/**
 * Configuration Management
 * - Centralized configuration with environment variable support
 * - Validation and default values
 * - Configuration profiles for different environments
 */

const path = require('path');

class ConfigManager {
  constructor() {
    this.env = process.env.NODE_ENV || 'production';
    this.config = this.loadConfiguration();
  }

  loadConfiguration() {
    const baseConfig = {
      // Database
      database: {
        path: process.env.DEADLOCK_DB_PATH || path.join(__dirname, '../../../../service/deadlock.sqlite3'),
        timeout: parseInt(process.env.DB_TIMEOUT || '30000', 10),
        verbose: process.env.DB_VERBOSE === '1'
      },

      // Steam
      steam: {
        dataDirectory: process.env.STEAM_PRESENCE_DATA_DIR || path.join(__dirname, '../.steam-data'),
        deadlockAppId: parseInt(process.env.DEADLOCK_APPID || '1422450', 10),
        maxReconnectAttempts: parseInt(process.env.STEAM_MAX_RECONNECT_ATTEMPTS || '5', 10),
        reconnectDelay: parseInt(process.env.STEAM_RECONNECT_DELAY || '5000', 10)
      },

      // Logging
      logging: {
        level: (process.env.LOG_LEVEL || 'info').toLowerCase(),
        quietMode: process.env.STEAM_QUIET_LOGS === '1',
        rateLimitMs: parseInt(process.env.LOG_RATE_LIMIT || '30000', 10),
        batchTimeoutMs: parseInt(process.env.LOG_BATCH_TIMEOUT || '5000', 10)
      },

      // Task Processing
      tasks: {
        pollIntervalMs: parseInt(process.env.TASK_POLL_INTERVAL || '5000', 10),
        maxConcurrent: parseInt(process.env.TASK_MAX_CONCURRENT || '3', 10),
        retryAttempts: parseInt(process.env.TASK_RETRY_ATTEMPTS || '3', 10),
        circuitBreakerThreshold: parseInt(process.env.TASK_CIRCUIT_BREAKER_THRESHOLD || '5', 10)
      },

      // Presence Tracking
      presence: {
        checkIntervalMs: parseInt(process.env.PRESENCE_CHECK_INTERVAL || '60000', 10),
        maxConcurrentRequests: parseInt(process.env.PRESENCE_MAX_REQUESTS || '50', 10),
        batchSize: parseInt(process.env.PRESENCE_BATCH_SIZE || '10', 10),
        cacheTimeoutMs: parseInt(process.env.PRESENCE_CACHE_TIMEOUT || '300000', 10)
      },

      // Health Monitoring
      health: {
        checkIntervalMs: parseInt(process.env.HEALTH_CHECK_INTERVAL || '300000', 10),
        heartbeatIntervalMs: parseInt(process.env.HEARTBEAT_INTERVAL || '30000', 10),
        memoryThresholdMb: parseInt(process.env.MEMORY_THRESHOLD_MB || '512', 10)
      },

      // Quick Invites
      quickInvites: {
        poolTarget: parseInt(process.env.QI_POOL_TARGET || '2', 10),
        poolMinAvailable: parseInt(process.env.QI_POOL_MIN || '1', 10),
        poolRefillTarget: parseInt(process.env.QI_POOL_REFILL_TARGET || '7', 10),
        ensureIntervalMs: parseInt(process.env.QI_ENSURE_INTERVAL || '30000', 10)
      }
    };

    // Apply environment-specific overrides
    if (this.env === 'development') {
      return this.applyDevelopmentOverrides(baseConfig);
    } else if (this.env === 'test') {
      return this.applyTestOverrides(baseConfig);
    }

    return baseConfig;
  }

  applyDevelopmentOverrides(config) {
    return {
      ...config,
      logging: {
        ...config.logging,
        level: 'debug',
        quietMode: false
      },
      tasks: {
        ...config.tasks,
        pollIntervalMs: 2000 // Faster polling in dev
      },
      presence: {
        ...config.presence,
        checkIntervalMs: 30000 // More frequent checks in dev
      }
    };
  }

  applyTestOverrides(config) {
    return {
      ...config,
      database: {
        ...config.database,
        path: ':memory:' // In-memory database for tests
      },
      logging: {
        ...config.logging,
        level: 'error' // Minimal logging in tests
      },
      tasks: {
        ...config.tasks,
        pollIntervalMs: 100 // Very fast polling for tests
      }
    };
  }

  get(path) {
    const keys = path.split('.');
    let value = this.config;
    
    for (const key of keys) {
      if (value && typeof value === 'object' && key in value) {
        value = value[key];
      } else {
        return undefined;
      }
    }
    
    return value;
  }

  set(path, value) {
    const keys = path.split('.');
    let current = this.config;
    
    for (let i = 0; i < keys.length - 1; i++) {
      const key = keys[i];
      if (!(key in current) || typeof current[key] !== 'object') {
        current[key] = {};
      }
      current = current[key];
    }
    
    current[keys[keys.length - 1]] = value;
  }

  validate() {
    const errors = [];
    
    // Validate required paths
    const requiredPaths = [
      'database.path',
      'steam.dataDirectory'
    ];
    
    for (const path of requiredPaths) {
      if (this.get(path) === undefined) {
        errors.push(`Missing required configuration: ${path}`);
      }
    }
    
    // Validate numeric ranges
    const numericValidations = [
      { path: 'tasks.pollIntervalMs', min: 1000, max: 60000 },
      { path: 'presence.checkIntervalMs', min: 10000, max: 600000 },
      { path: 'health.heartbeatIntervalMs', min: 5000, max: 300000 }
    ];
    
    for (const validation of numericValidations) {
      const value = this.get(validation.path);
      if (typeof value === 'number') {
        if (value < validation.min || value > validation.max) {
          errors.push(`${validation.path} must be between ${validation.min} and ${validation.max}`);
        }
      }
    }
    
    return errors;
  }

  // Get all configuration as object
  getAll() {
    return { ...this.config };
  }

  // Environment helpers
  isDevelopment() {
    return this.env === 'development';
  }

  isProduction() {
    return this.env === 'production';
  }

  isTest() {
    return this.env === 'test';
  }
}

// Export singleton instance
module.exports = new ConfigManager();