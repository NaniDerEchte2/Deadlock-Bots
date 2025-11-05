'use strict';

/**
 * Task Processing System
 * - Handles Steam task queue from database
 * - Implements retry logic and error handling
 * - Provides task execution statistics
 */

const { SmartLogger } = require('./logger');

class TaskProcessor {
  constructor(database, steamClient) {
    this.db = database;
    this.steamClient = steamClient;
    this.logger = new SmartLogger();
    
    // Task processing state
    this.processing = false;
    this.processingInterval = null;
    this.pollIntervalMs = parseInt(process.env.TASK_POLL_INTERVAL || '5000', 10);
    
    // Statistics
    this.stats = {
      processed: 0,
      successful: 0,
      failed: 0,
      lastProcessedAt: null,
      errorRate: 0
    };
    
    // Task handlers
    this.taskHandlers = new Map();
    this.setupDefaultHandlers();
    
    // Error tracking for circuit breaker
    this.recentErrors = [];
    this.maxRecentErrors = 10;
    this.circuitBreakerThreshold = 5; // Stop processing if 5+ errors in recent batch
  }

  setupDefaultHandlers() {
    this.taskHandlers.set('AUTH_STATUS', this.handleAuthStatus.bind(this));
    this.taskHandlers.set('AUTH_LOGIN', this.handleAuthLogin.bind(this));
    this.taskHandlers.set('AUTH_LOGOUT', this.handleAuthLogout.bind(this));
    this.taskHandlers.set('AUTH_GUARD_CODE', this.handleAuthGuardCode.bind(this));
    
    // NOTE: Custom task handlers (AUTH_SEND_PLAYTEST_INVITE, etc.) will be 
    // registered by the main SteamBridge class via addTaskHandler()
  }

  start() {
    if (this.processing) {
      this.logger.warn('Task processor already running');
      return;
    }
    
    this.processing = true;
    this.logger.info('Starting task processor', { 
      poll_interval_ms: this.pollIntervalMs 
    });
    
    this.processingInterval = setInterval(() => {
      this.processTasks().catch(err => {
        this.logger.error('Task processing error', { error: err.message });
      });
    }, this.pollIntervalMs);
  }

  stop() {
    if (!this.processing) return;
    
    this.processing = false;
    if (this.processingInterval) {
      clearInterval(this.processingInterval);
      this.processingInterval = null;
    }
    
    this.logger.info('Task processor stopped');
  }

  async processTasks() {
    if (!this.processing) return;
    
    // Circuit breaker check
    if (this.isCircuitBreakerOpen()) {
      this.logger.warn('Circuit breaker open - skipping task processing', {
        recent_errors: this.recentErrors.length,
        threshold: this.circuitBreakerThreshold
      });
      return;
    }
    
    try {
      const tasks = this.getPendingTasks();
      
      if (tasks.length === 0) {
        return; // No tasks to process
      }
      
      this.logger.debug(`Processing ${tasks.length} pending tasks`);
      
      let processed = 0;
      let successful = 0;
      
      for (const task of tasks) {
        try {
          await this.processTask(task);
          successful++;
        } catch (error) {
          this.logger.error(`Task ${task.id} failed`, {
            task_id: task.id,
            task_type: task.type,
            error: error.message
          });
          
          this.markTaskFailed(task.id, error.message);
          this.addRecentError(error);
        }
        processed++;
      }
      
      // Update statistics
      this.stats.processed += processed;
      this.stats.successful += successful;
      this.stats.failed += (processed - successful);
      this.stats.lastProcessedAt = Date.now();
      this.stats.errorRate = this.stats.failed / this.stats.processed;
      
      if (processed > 0) {
        this.logger.info('Task batch completed', {
          processed,
          successful,
          failed: processed - successful,
          total_error_rate: Math.round(this.stats.errorRate * 100) + '%'
        });
      }
      
    } catch (error) {
      this.logger.error('Failed to process task batch', { error: error.message });
    }
  }

  getPendingTasks() {
    try {
      const stmt = this.db.prepare(`
        SELECT id, type, created_at, payload, attempts
        FROM steam_tasks 
        WHERE status = 'pending' 
        ORDER BY created_at ASC 
        LIMIT 10
      `);
      
      return stmt.all();
    } catch (error) {
      this.logger.error('Failed to fetch pending tasks', { error: error.message });
      return [];
    }
  }

  async processTask(task) {
    const handler = this.taskHandlers.get(task.type);
    
    if (!handler) {
      throw new Error(`Unknown task type: ${task.type}`);
    }
    
    this.logger.debug(`Executing task ${task.id}`, {
      task_id: task.id,
      task_type: task.type,
      attempts: task.attempts || 0
    });
    
    // Mark as running
    this.markTaskRunning(task.id);
    
    try {
      let payload = {};
      if (task.payload) {
        try {
          payload = JSON.parse(task.payload);
        } catch (e) {
          this.logger.warn(`Invalid JSON payload for task ${task.id}`);
        }
      }
      
      const result = await handler(payload, task);
      
      // Mark as done
      this.markTaskDone(task.id, result);
      
      return result;
      
    } catch (error) {
      // Increment attempts and re-throw
      this.incrementTaskAttempts(task.id);
      throw error;
    }
  }

  // Task Handlers
  async handleAuthStatus(payload, task) {
    const status = this.steamClient.getStatus();
    
    this.logger.debug('Auth status requested', status);
    
    return {
      success: true,
      status: status
    };
  }

  async handleAuthLogin(payload, task) {
    const credentials = {
      refreshToken: payload.refresh_token,
      source: 'task'
    };
    
    const result = await this.steamClient.login(credentials);
    
    return {
      success: result.success,
      started: result.started,
      already_logged_in: result.alreadyLoggedIn
    };
  }

  async handleAuthLogout(payload, task) {
    const result = await this.steamClient.logout();
    
    return {
      success: result.success,
      already_logged_out: result.alreadyLoggedOut
    };
  }

  async handleAuthGuardCode(payload, task) {
    if (!payload.code) {
      throw new Error('Guard code not provided');
    }
    
    const result = this.steamClient.submitGuardCode(payload.code);
    
    return {
      success: result.success
    };
  }


  // Database operations
  markTaskRunning(taskId) {
    try {
      const stmt = this.db.prepare(`
        UPDATE steam_tasks 
        SET status = 'running', updated_at = ?
        WHERE id = ?
      `);
      stmt.run(Math.floor(Date.now() / 1000), taskId);
    } catch (error) {
      this.logger.error('Failed to mark task as running', { 
        task_id: taskId, 
        error: error.message 
      });
    }
  }

  markTaskDone(taskId, result) {
    try {
      const stmt = this.db.prepare(`
        UPDATE steam_tasks 
        SET status = 'done', finished_at = ?, result = ?
        WHERE id = ?
      `);
      stmt.run(
        Math.floor(Date.now() / 1000), 
        JSON.stringify(result),
        taskId
      );
    } catch (error) {
      this.logger.error('Failed to mark task as done', { 
        task_id: taskId, 
        error: error.message 
      });
    }
  }

  markTaskFailed(taskId, errorMessage) {
    try {
      const stmt = this.db.prepare(`
        UPDATE steam_tasks 
        SET status = 'failed', finished_at = ?, error = ?
        WHERE id = ?
      `);
      stmt.run(
        Math.floor(Date.now() / 1000), 
        errorMessage,
        taskId
      );
    } catch (error) {
      this.logger.error('Failed to mark task as failed', { 
        task_id: taskId, 
        error: error.message 
      });
    }
  }

  incrementTaskAttempts(taskId) {
    try {
      const stmt = this.db.prepare(`
        UPDATE steam_tasks 
        SET attempts = COALESCE(attempts, 0) + 1
        WHERE id = ?
      `);
      stmt.run(taskId);
    } catch (error) {
      this.logger.error('Failed to increment task attempts', { 
        task_id: taskId, 
        error: error.message 
      });
    }
  }

  // Error tracking and circuit breaker
  addRecentError(error) {
    this.recentErrors.push({
      timestamp: Date.now(),
      message: error.message
    });
    
    // Keep only recent errors (last 5 minutes)
    const fiveMinutesAgo = Date.now() - 300000;
    this.recentErrors = this.recentErrors.filter(e => e.timestamp > fiveMinutesAgo);
    
    // Limit array size
    if (this.recentErrors.length > this.maxRecentErrors) {
      this.recentErrors.shift();
    }
  }

  isCircuitBreakerOpen() {
    // Open circuit breaker if too many recent errors
    return this.recentErrors.length >= this.circuitBreakerThreshold;
  }

  getStatistics() {
    return {
      ...this.stats,
      is_processing: this.processing,
      recent_errors: this.recentErrors.length,
      circuit_breaker_open: this.isCircuitBreakerOpen(),
      poll_interval_ms: this.pollIntervalMs
    };
  }

  // Add custom task handler
  addTaskHandler(taskType, handler) {
    this.taskHandlers.set(taskType, handler);
    this.logger.debug(`Added task handler for ${taskType}`);
  }
}

module.exports = { TaskProcessor };