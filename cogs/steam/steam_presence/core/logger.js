'use strict';

/**
 * Optimiertes Logger-System für Steam Bridge
 * - Reduziert Log-Spam durch Rate Limiting
 * - Batch-Logging für ähnliche Events
 * - Intelligente Filterung von redundanten Logs
 */

class SmartLogger {
  constructor(options = {}) {
    this.logLevels = { error: 0, warn: 1, info: 2, debug: 3 };
    this.logLevel = (process.env.LOG_LEVEL || 'info').toLowerCase();
    this.logThreshold = this.logLevels[this.logLevel] ?? this.logLevels.info;
    
    // Rate Limiting für häufige Logs
    this.rateLimits = new Map();
    this.defaultRateLimit = options.defaultRateLimit || 30000; // 30s default
    
    // Batch Logging für ähnliche Events
    this.batches = new Map();
    this.batchTimeout = options.batchTimeout || 5000; // 5s batch window
    this.batchTimers = new Map();
    
    // Spezielle Rate Limits für bekannte Spam-Messages
    this.specialRateLimits = {
      'Requesting personas for presence snapshot': 60000, // 1 minute
      'Fetching Deadlock rich presence': 60000,
      'No Deadlock rich presence returned': 300000, // 5 minutes
      'getPersonas failed': 120000, // 2 minutes
    };
    
    // Quiet Mode für Production
    this.quietMode = process.env.STEAM_QUIET_LOGS === '1';
  }

  log(level, message, extra = {}) {
    const lvl = this.logLevels[level];
    if (lvl === undefined || lvl > this.logThreshold) return;
    
    // Rate Limiting Check
    if (this.isRateLimited(message, level)) {
      return;
    }
    
    // Batch Similar Messages
    if (this.shouldBatch(message, level)) {
      this.addToBatch(message, extra);
      return;
    }
    
    // Filter in Quiet Mode
    if (this.quietMode && this.shouldFilterInQuietMode(message, level)) {
      return;
    }
    
    this.outputLog(level, message, extra);
  }

  isRateLimited(message, level) {
    // Never rate limit errors
    if (level === 'error') return false;
    
    const now = Date.now();
    const key = this.getRateLimitKey(message);
    const limit = this.specialRateLimits[message] || this.defaultRateLimit;
    
    const lastLog = this.rateLimits.get(key);
    if (lastLog && (now - lastLog) < limit) {
      return true;
    }
    
    this.rateLimits.set(key, now);
    return false;
  }

  shouldBatch(message, level) {
    // Batch certain repetitive messages
    const batchablePatterns = [
      'Requesting personas for presence snapshot',
      'Fetching Deadlock rich presence',
      'No Deadlock rich presence returned'
    ];
    
    return level === 'info' && batchablePatterns.some(pattern => 
      message.includes(pattern)
    );
  }

  addToBatch(message, extra) {
    const batchKey = this.getBatchKey(message);
    
    if (!this.batches.has(batchKey)) {
      this.batches.set(batchKey, {
        count: 0,
        firstExtra: extra,
        lastExtra: extra,
        firstTime: Date.now()
      });
    }
    
    const batch = this.batches.get(batchKey);
    batch.count++;
    batch.lastExtra = extra;
    
    // Set/Reset timer
    if (this.batchTimers.has(batchKey)) {
      clearTimeout(this.batchTimers.get(batchKey));
    }
    
    const timer = setTimeout(() => {
      this.flushBatch(batchKey);
    }, this.batchTimeout);
    
    this.batchTimers.set(batchKey, timer);
  }

  flushBatch(batchKey) {
    const batch = this.batches.get(batchKey);
    if (!batch) return;
    
    const message = `${batchKey} (batched ${batch.count} occurrences)`;
    const extra = {
      ...batch.lastExtra,
      batch_count: batch.count,
      batch_duration_ms: Date.now() - batch.firstTime
    };
    
    this.outputLog('info', message, extra);
    
    // Cleanup
    this.batches.delete(batchKey);
    if (this.batchTimers.has(batchKey)) {
      clearTimeout(this.batchTimers.get(batchKey));
      this.batchTimers.delete(batchKey);
    }
  }

  shouldFilterInQuietMode(message, level) {
    if (level === 'error' || level === 'warn') return false;
    
    const quietFilters = [
      'Steam login successful',
      'Deadlock app launched',
      'Auto-login kick-off',
      'Using SQLite database',
      'Statusanzeige initialisiert'
    ];
    
    return !quietFilters.some(filter => message.includes(filter));
  }

  getRateLimitKey(message) {
    // Group similar messages for rate limiting
    if (message.includes('personas for presence')) return 'personas_request';
    if (message.includes('Deadlock rich presence')) return 'rich_presence';
    if (message.includes('No Deadlock rich presence')) return 'no_presence';
    return message;
  }

  getBatchKey(message) {
    if (message.includes('personas for presence')) return 'Requesting personas';
    if (message.includes('Fetching Deadlock rich presence')) return 'Fetching rich presence';
    if (message.includes('No Deadlock rich presence')) return 'No rich presence';
    return message;
  }

  outputLog(level, message, extra = {}) {
    const payload = { 
      time: new Date().toISOString(), 
      level, 
      msg: message 
    };
    
    if (extra && typeof extra === 'object') {
      for (const [key, value] of Object.entries(extra)) {
        if (value !== undefined) {
          payload[key] = value;
        }
      }
    }
    
    console.log(JSON.stringify(payload));
  }

  // Convenience methods
  error(message, extra) { this.log('error', message, extra); }
  warn(message, extra) { this.log('warn', message, extra); }
  info(message, extra) { this.log('info', message, extra); }
  debug(message, extra) { this.log('debug', message, extra); }

  // Summary logging for statistics
  logSummary(type, stats) {
    if (this.quietMode) return;
    
    this.info(`📊 ${type} Summary`, {
      ...stats,
      summary_type: type
    });
  }

  // Health status logging
  logHealth(component, status, details = {}) {
    const level = status === 'healthy' ? 'info' : 'warn';
    const emoji = status === 'healthy' ? '✅' : '⚠️';
    
    this.log(level, `${emoji} ${component} Health: ${status}`, {
      component,
      health_status: status,
      ...details
    });
  }
}

module.exports = { SmartLogger };