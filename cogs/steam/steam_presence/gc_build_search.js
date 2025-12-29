#!/usr/bin/env node
'use strict';

/**
 * GC Build Search Module
 *
 * Ermöglicht die Suche nach Builds direkt über den Deadlock Game Coordinator.
 * Dies bietet Zugang zu ALLEN In-Game Builds, nicht nur denen die auf der API verfügbar sind.
 *
 * Usage (in index.js):
 *   const { GcBuildSearch } = require('./gc_build_search');
 *   const gcSearch = new GcBuildSearch({ client, log, trace, db });
 *   const results = await gcSearch.findBuilds({ heroId: 1, searchText: 'damage' });
 */

const path = require('path');
const fs = require('fs');
const protobuf = require('protobufjs');

// GC Message IDs for Build Search
const GC_MSG_FIND_HERO_BUILDS = 9195;
const GC_MSG_FIND_HERO_BUILDS_RESPONSE = 9196;
const PROTO_MASK = 0x80000000;
const DEADLOCK_APP_ID = 1422450;

// Timeout for GC responses
const GC_RESPONSE_TIMEOUT_MS = 30000;

class GcBuildSearch {
  constructor({ client, log, trace, db, appId }) {
    this.client = client;
    this.log = typeof log === 'function' ? log : () => {};
    this.trace = typeof trace === 'function' ? trace : () => {};
    this.db = db;
    this.appId = appId || DEADLOCK_APP_ID;

    // Proto definitions
    this.protoRoot = null;
    this.FindHeroBuildsMsg = null;
    this.FindHeroBuildsResponseMsg = null;

    // Pending search requests (legacy - now using steam-user's job callback system)
    // Kept for backwards compatibility with handleGcMessage fallback
    this.pendingSearches = new Map();
    this.searchIdCounter = 0;
  }

  /**
   * Load protobuf definitions
   */
  async loadProto() {
    if (this.protoRoot) return;

    const protoPath = path.join(__dirname, 'protos', 'find_hero_builds.proto');

    if (!fs.existsSync(protoPath)) {
      throw new Error(`Proto file not found: ${protoPath}. Please ensure find_hero_builds.proto exists.`);
    }

    this.protoRoot = await protobuf.load(protoPath);
    this.FindHeroBuildsMsg = this.protoRoot.lookupType('CMsgClientToGCFindHeroBuilds');
    this.FindHeroBuildsResponseMsg = this.protoRoot.lookupType('CMsgClientToGCFindHeroBuildsResponse');

    this.log('info', 'GcBuildSearch: Proto definitions loaded');
  }

  /**
   * Handle incoming GC message (LEGACY FALLBACK - call this from the receivedFromGC handler)
   * NOTE: Primary response handling is now via sendToGC's job callback system.
   * This handler is kept as a fallback for responses that don't use job_id_target.
   * @returns {boolean} true if message was handled
   */
  handleGcMessage(appId, msgType, payload) {
    const messageId = msgType & ~PROTO_MASK;

    if (messageId !== GC_MSG_FIND_HERO_BUILDS_RESPONSE) {
      return false;
    }

    this.log('info', 'GcBuildSearch: Received FindHeroBuilds response', {
      appId,
      messageId,
      payloadLength: payload?.length || 0
    });

    // Resolve all pending searches (we don't have a request ID to match)
    // In practice, we should only have one pending search at a time
    if (this.pendingSearches.size === 0) {
      this.log('warn', 'GcBuildSearch: Received response but no pending searches');
      return true;
    }

    // Get the oldest pending search
    const [searchId, waiter] = this.pendingSearches.entries().next().value;
    this.pendingSearches.delete(searchId);

    try {
      if (!this.FindHeroBuildsResponseMsg) {
        waiter.reject(new Error('Proto not loaded'));
        return true;
      }

      const response = this.FindHeroBuildsResponseMsg.decode(payload);

      this.log('info', 'GcBuildSearch: Decoded response', {
        responseType: response.response,
        resultCount: response.results?.length || 0,
        buildWindowOverride: response.buildWindowStartTimeOverride
      });

      waiter.resolve(response);
    } catch (err) {
      this.log('error', 'GcBuildSearch: Failed to decode response', {
        error: err.message
      });
      waiter.reject(err);
    }

    return true;
  }

  /**
   * Search for hero builds via the Game Coordinator
   *
   * @param {Object} options Search options
   * @param {number} [options.authorAccountId] - Filter by author account ID
   * @param {number} [options.heroId] - Filter by hero ID
   * @param {string} [options.searchText] - Text search query
   * @param {number} [options.heroBuildId] - Get specific build by ID
   * @param {number[]} [options.languages] - Language filters (0=English, etc.)
   * @param {number[]} [options.tags] - Tag filters
   * @returns {Promise<Object>} Search results
   */
  async findBuilds(options = {}) {
    await this.loadProto();

    if (!this.client) {
      throw new Error('Steam client not available');
    }

    if (typeof this.client.sendToGC !== 'function') {
      throw new Error('Steam client does not support sendToGC');
    }

    // Build the request payload
    const payload = {};

    if (options.authorAccountId) {
      payload.authorAccountId = options.authorAccountId;
    }
    if (options.heroId) {
      payload.heroId = options.heroId;
    }
    if (options.searchText) {
      payload.searchText = options.searchText;
    }
    if (options.heroBuildId) {
      payload.heroBuildId = options.heroBuildId;
    }
    // IMPORTANT: Always send language array like the game does [0, 0]
    // The game sends language twice for hero searches
    if (options.languages && options.languages.length > 0) {
      payload.language = options.languages;
    } else {
      // Default to English [0, 0] like the game client
      payload.language = [0, 0];
    }
    if (options.tags && options.tags.length > 0) {
      payload.tags = options.tags;
    }

    this.log('info', 'GcBuildSearch: Sending FindHeroBuilds request', {
      payload,
      appId: this.appId
    });

    this.trace('gc_find_builds_request', payload);

    // Create and encode the message
    const message = this.FindHeroBuildsMsg.create(payload);
    const buffer = this.FindHeroBuildsMsg.encode(message).finish();

    // Create a promise for the response using steam-user's job callback system
    // This ensures proper job_id_source is sent, which the GC requires
    const responsePromise = new Promise((resolve, reject) => {
      const timeoutId = setTimeout(() => {
        reject(new Error('GC response timeout'));
      }, GC_RESPONSE_TIMEOUT_MS);

      // Use sendToGC with callback - steam-user will set job_id_source automatically
      // and route the response back via this callback
      this.client.sendToGC(
        this.appId,
        GC_MSG_FIND_HERO_BUILDS, // steam-user adds PROTO_MASK when protoBufHeader is provided
        {}, // protoBufHeader - steam-user will add job_id_source
        buffer,
        (appId, msgType, gcPayload) => {
          clearTimeout(timeoutId);

          this.log('info', 'GcBuildSearch: Received GC response via job callback', {
            appId,
            msgType,
            payloadLength: gcPayload?.length || 0
          });

          // Check if this is the expected response type
          if (msgType === GC_MSG_FIND_HERO_BUILDS_RESPONSE) {
            try {
              if (!this.FindHeroBuildsResponseMsg) {
                reject(new Error('Proto not loaded'));
                return;
              }

              const response = this.FindHeroBuildsResponseMsg.decode(gcPayload);

              this.log('info', 'GcBuildSearch: Decoded response', {
                responseType: response.response,
                resultCount: response.results?.length || 0
              });

              resolve(response);
            } catch (err) {
              this.log('error', 'GcBuildSearch: Failed to decode response', {
                error: err.message
              });
              reject(err);
            }
          } else {
            // Unexpected message type (e.g., 9025 for author searches)
            this.log('warn', 'GcBuildSearch: Received unexpected message type', {
              expected: GC_MSG_FIND_HERO_BUILDS_RESPONSE,
              received: msgType
            });
            reject(new Error(`Unexpected GC message type: ${msgType} (expected ${GC_MSG_FIND_HERO_BUILDS_RESPONSE})`));
          }
        }
      );
    });

    return responsePromise;
  }

  /**
   * Search builds by author and store results in database
   */
  async discoverBuildsFromAuthor(authorAccountId) {
    try {
      const response = await this.findBuilds({ authorAccountId });

      if (response.response !== 1) { // k_eSuccess
        this.log('warn', 'GcBuildSearch: Author search failed', {
          authorAccountId,
          responseCode: response.response
        });
        return { success: false, error: `Response code: ${response.response}` };
      }

      const builds = response.results || [];
      let newBuilds = 0;
      let updatedBuilds = 0;

      for (const result of builds) {
        const build = result.heroBuild;
        if (!build) continue;

        const stats = this.upsertBuild(build, {
          numFavorites: result.numFavorites,
          numWeeklyFavorites: result.numWeeklyFavorites,
          numDailyFavorites: result.numDailyFavorites,
          numIgnores: result.numIgnores,
          numReports: result.numReports,
          source: 'gc_author_search'
        });

        if (stats.inserted) newBuilds++;
        if (stats.updated) updatedBuilds++;
      }

      this.log('info', 'GcBuildSearch: Author discovery complete', {
        authorAccountId,
        totalBuilds: builds.length,
        newBuilds,
        updatedBuilds
      });

      return {
        success: true,
        totalBuilds: builds.length,
        newBuilds,
        updatedBuilds
      };

    } catch (err) {
      this.log('error', 'GcBuildSearch: Author discovery failed', {
        authorAccountId,
        error: err.message
      });
      return { success: false, error: err.message };
    }
  }

  /**
   * Search builds by hero ID and filter by watched authors
   * This approach works when author-based search doesn't get GC responses
   */
  async discoverBuildsForHero(heroId, watchedAuthorIds) {
    try {
      this.log('info', 'GcBuildSearch: Searching builds for hero', { heroId });

      // Search all builds for this hero (no author filter)
      const response = await this.findBuilds({ heroId });

      if (response.response !== 1) { // k_eSuccess
        this.log('warn', 'GcBuildSearch: Hero search failed', {
          heroId,
          responseCode: response.response
        });
        return { success: false, error: `Response code: ${response.response}` };
      }

      const builds = response.results || [];
      let matchedBuilds = 0;
      let newBuilds = 0;
      let updatedBuilds = 0;

      // Convert watchedAuthorIds to a Set for fast lookup
      const authorSet = new Set(watchedAuthorIds.map(id => Number(id)));

      for (const result of builds) {
        const build = result.heroBuild;
        if (!build) continue;

        // Get author ID from build (handle both camelCase and snake_case)
        const authorId = build.authorAccountId || build.author_account_id;

        // Only process builds from watched authors
        if (!authorSet.has(Number(authorId))) {
          continue;
        }

        matchedBuilds++;

        const stats = this.upsertBuild(build, {
          numFavorites: result.numFavorites,
          numWeeklyFavorites: result.numWeeklyFavorites,
          numDailyFavorites: result.numDailyFavorites,
          numIgnores: result.numIgnores,
          numReports: result.numReports,
          source: 'gc_hero_search'
        });

        if (stats.inserted) newBuilds++;
        if (stats.updated) updatedBuilds++;
      }

      this.log('info', 'GcBuildSearch: Hero discovery complete', {
        heroId,
        totalBuilds: builds.length,
        matchedBuilds,
        newBuilds,
        updatedBuilds
      });

      return {
        success: true,
        heroId,
        totalBuilds: builds.length,
        matchedBuilds,
        newBuilds,
        updatedBuilds
      };

    } catch (err) {
      this.log('error', 'GcBuildSearch: Hero discovery failed', {
        heroId,
        error: err.message
      });
      return { success: false, heroId, error: err.message };
    }
  }

  /**
   * Search builds by hero and keyword
   */
  async discoverBuildsBySearch(heroId, searchText) {
    try {
      const response = await this.findBuilds({ heroId, searchText });

      if (response.response !== 1) {
        return { success: false, error: `Response code: ${response.response}` };
      }

      const builds = response.results || [];
      let newBuilds = 0;

      for (const result of builds) {
        const build = result.heroBuild;
        if (!build) continue;

        const stats = this.upsertBuild(build, {
          numFavorites: result.numFavorites,
          numWeeklyFavorites: result.numWeeklyFavorites,
          source: 'gc_keyword_search',
          searchQuery: searchText
        });

        if (stats.inserted) newBuilds++;
      }

      return {
        success: true,
        totalBuilds: builds.length,
        newBuilds,
        heroId,
        searchText
      };

    } catch (err) {
      return { success: false, error: err.message };
    }
  }

  /**
   * Insert or update a build in the database
   * Uses only the existing schema columns from hero_build_sources
   */
  upsertBuild(build, meta = {}) {
    if (!this.db) {
      this.log('warn', 'GcBuildSearch: No database connection for upsert');
      return { inserted: false, updated: false };
    }

    try {
      const now = Math.floor(Date.now() / 1000);

      // Check if build exists
      const existing = this.db.prepare(`
        SELECT hero_build_id FROM hero_build_sources WHERE hero_build_id = ?
      `).get(build.heroBuildId || build.hero_build_id);

      const isUpdate = !!existing;

      // Prepare data - handle both camelCase and snake_case from GC response
      const heroBuildId = build.heroBuildId || build.hero_build_id;
      const originBuildId = build.originBuildId || build.origin_build_id || null;
      const authorAccountId = build.authorAccountId || build.author_account_id;
      const heroId = build.heroId || build.hero_id;
      const language = build.language || 0;
      const version = build.version || 1;
      const name = build.name || '';
      const description = build.description || '';
      const tagsJson = JSON.stringify(build.tags || []);
      const detailsJson = JSON.stringify(build.details || {});
      const publishTs = build.publishTimestamp || build.publish_timestamp || now;
      const lastUpdatedTs = build.lastUpdatedTimestamp || build.last_updated_timestamp || now;

      // Upsert using only existing schema columns
      this.db.prepare(`
        INSERT INTO hero_build_sources (
          hero_build_id, origin_build_id, author_account_id, hero_id, language, version,
          name, description, tags_json, details_json, publish_ts, last_updated_ts,
          fetched_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hero_build_id) DO UPDATE SET
          origin_build_id = excluded.origin_build_id,
          version = excluded.version,
          name = excluded.name,
          description = excluded.description,
          tags_json = excluded.tags_json,
          details_json = excluded.details_json,
          last_updated_ts = excluded.last_updated_ts,
          last_seen_at = excluded.last_seen_at
      `).run(
        heroBuildId,
        originBuildId,
        authorAccountId,
        heroId,
        language,
        version,
        name,
        description,
        tagsJson,
        detailsJson,
        publishTs,
        lastUpdatedTs,
        now, // fetched_at
        now  // last_seen_at
      );

      // Log stats from GC for debugging (not stored in DB yet)
      if (meta.numFavorites || meta.numWeeklyFavorites) {
        this.log('debug', 'GcBuildSearch: Build stats from GC', {
          buildId: heroBuildId,
          favorites: meta.numFavorites,
          weeklyFavorites: meta.numWeeklyFavorites,
          source: meta.source
        });
      }

      return { inserted: !isUpdate, updated: isUpdate };

    } catch (err) {
      this.log('error', 'GcBuildSearch: Failed to upsert build', {
        buildId: build.heroBuildId || build.hero_build_id,
        error: err.message
      });
      return { inserted: false, updated: false, error: err.message };
    }
  }

  /**
   * Clear pending searches (e.g., on disconnect)
   */
  flushPendingSearches(error) {
    for (const [searchId, waiter] of this.pendingSearches) {
      waiter.reject(error || new Error('Search cancelled'));
    }
    this.pendingSearches.clear();
  }
}

module.exports = { GcBuildSearch, GC_MSG_FIND_HERO_BUILDS, GC_MSG_FIND_HERO_BUILDS_RESPONSE };
