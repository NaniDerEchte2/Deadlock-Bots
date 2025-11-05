'use strict';

/**
 * Database Connection Manager
 * - Handles SQLite database operations
 * - Provides connection pooling and error handling
 * - Includes database health monitoring
 */

const Database = require('better-sqlite3');
const { SmartLogger } = require('./logger');

class DatabaseManager {
  constructor(dbPath) {
    this.dbPath = dbPath;
    this.db = null;
    this.logger = new SmartLogger();
    this.isConnected = false;
    
    // Connection options
    this.options = {
      readonly: false,
      fileMustExist: false,
      timeout: 30000,
      verbose: process.env.DB_VERBOSE === '1' ? this.logger.debug.bind(this.logger) : null
    };
    
    // Health monitoring
    this.lastHealthCheck = null;
    this.healthCheckInterval = 60000; // 1 minute
    this.operationStats = {
      queries: 0,
      errors: 0,
      avgExecutionTime: 0
    };
  }

  connect() {
    if (this.isConnected && this.db) {
      this.logger.debug('Database already connected');
      return this.db;
    }
    
    try {
      this.logger.info('Connecting to SQLite database', { 
        dbPath: this.dbPath 
      });
      
      this.db = new Database(this.dbPath, this.options);
      
      // Configure for better performance
      this.db.pragma('journal_mode = WAL');
      this.db.pragma('synchronous = NORMAL');
      this.db.pragma('cache_size = 1000');
      this.db.pragma('temp_store = MEMORY');
      
      this.isConnected = true;
      
      // Test connection
      this.testConnection();
      
      this.logger.info('Database connected successfully');
      
      return this.db;
      
    } catch (error) {
      this.logger.error('Failed to connect to database', { 
        error: error.message,
        dbPath: this.dbPath
      });
      throw error;
    }
  }

  testConnection() {
    try {
      const result = this.db.prepare('SELECT 1 as test').get();
      if (result?.test !== 1) {
        throw new Error('Connection test failed');
      }
    } catch (error) {
      this.isConnected = false;
      throw new Error(`Database connection test failed: ${error.message}`);
    }
  }

  disconnect() {
    if (!this.db || !this.isConnected) return;
    
    try {
      this.db.close();
      this.isConnected = false;
      this.logger.info('Database disconnected');
    } catch (error) {
      this.logger.error('Error disconnecting database', { error: error.message });
    }
  }

  // Prepared statement with error handling and timing
  executeQuery(sql, params = []) {
    if (!this.isConnected) {
      throw new Error('Database not connected');
    }
    
    const startTime = Date.now();
    
    try {
      const stmt = this.db.prepare(sql);
      let result;
      
      if (params.length > 0) {
        if (sql.trim().toLowerCase().startsWith('select')) {
          result = stmt.all(...params);
        } else {
          result = stmt.run(...params);
        }
      } else {
        if (sql.trim().toLowerCase().startsWith('select')) {
          result = stmt.all();
        } else {
          result = stmt.run();
        }
      }
      
      // Update stats
      const executionTime = Date.now() - startTime;
      this.updateOperationStats(executionTime, false);
      
      return result;
      
    } catch (error) {
      this.updateOperationStats(Date.now() - startTime, true);
      this.logger.error('Database query failed', {
        error: error.message,
        sql: sql.substring(0, 100) + (sql.length > 100 ? '...' : ''),
        params: params
      });
      throw error;
    }
  }

  // Convenience methods
  selectAll(sql, params = []) {
    return this.executeQuery(sql, params);
  }

  selectOne(sql, params = []) {
    const results = this.executeQuery(sql, params);
    return results.length > 0 ? results[0] : null;
  }

  insert(table, data) {
    const columns = Object.keys(data);
    const placeholders = columns.map(() => '?').join(', ');
    const values = columns.map(col => data[col]);
    
    const sql = `INSERT INTO ${table} (${columns.join(', ')}) VALUES (${placeholders})`;
    return this.executeQuery(sql, values);
  }

  update(table, data, whereClause, whereParams = []) {
    const columns = Object.keys(data);
    const setClause = columns.map(col => `${col} = ?`).join(', ');
    const values = columns.map(col => data[col]);
    
    const sql = `UPDATE ${table} SET ${setClause} WHERE ${whereClause}`;
    return this.executeQuery(sql, [...values, ...whereParams]);
  }

  // Transaction support
  transaction(callback) {
    if (!this.isConnected) {
      throw new Error('Database not connected');
    }
    
    const transaction = this.db.transaction(callback);
    return transaction;
  }

  // Health monitoring
  updateOperationStats(executionTime, isError) {
    this.operationStats.queries++;
    if (isError) {
      this.operationStats.errors++;
    }
    
    // Update average execution time (simple moving average)
    this.operationStats.avgExecutionTime = 
      (this.operationStats.avgExecutionTime + executionTime) / 2;
  }

  async performHealthCheck() {
    const now = Date.now();
    
    if (this.lastHealthCheck && (now - this.lastHealthCheck) < this.healthCheckInterval) {
      return; // Too soon for another health check
    }
    
    this.lastHealthCheck = now;
    
    try {
      // Test basic connectivity
      this.testConnection();
      
      // Check database integrity (light check)
      const integrity = this.db.prepare('PRAGMA quick_check').get();
      
      // Get database stats
      const stats = this.getHealthStats();
      
      this.logger.logHealth('Database', 'healthy', {
        integrity: integrity?.integrity_check || 'ok',
        ...stats
      });
      
    } catch (error) {
      this.logger.logHealth('Database', 'unhealthy', {
        error: error.message,
        last_successful_check: this.lastHealthCheck
      });
    }
  }

  getHealthStats() {
    try {
      const errorRate = this.operationStats.queries > 0 
        ? (this.operationStats.errors / this.operationStats.queries) * 100 
        : 0;
      
      return {
        is_connected: this.isConnected,
        total_queries: this.operationStats.queries,
        total_errors: this.operationStats.errors,
        error_rate_percent: Math.round(errorRate * 100) / 100,
        avg_execution_time_ms: Math.round(this.operationStats.avgExecutionTime * 100) / 100,
        db_path: this.dbPath
      };
    } catch (error) {
      return {
        is_connected: this.isConnected,
        error: error.message
      };
    }
  }

  // Get raw database instance (for advanced operations)
  getRawDatabase() {
    if (!this.isConnected) {
      throw new Error('Database not connected');
    }
    return this.db;
  }
}

module.exports = { DatabaseManager };