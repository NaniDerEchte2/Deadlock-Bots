#!/usr/bin/env node
'use strict';

/**
 * Build Catalog Manager
 *
 * Verwaltet die Builds für die Deutsche Deadlock Community:
 * - Entdeckt Builds von watched authors via In-Game GC
 * - Wählt max 3 Builds pro Hero basierend auf Author-Priorität
 * - Erstellt/aktualisiert deutsche Klone der Builds
 *
 * Funktionalität (beibehalten):
 * - Author-Priorität aus watched_build_authors
 * - Max 3 Builds pro Hero
 * - Nur englische Originals klonen
 * - Builds der letzten 45 Tage berücksichtigen
 */

// Config
const MAX_BUILDS_PER_HERO = 3;
const BUILD_AGE_DAYS = 45;
const TARGET_LANGUAGE = 1; // German
const SOURCE_LANGUAGE = 0; // English
const GC_REQUEST_DELAY_MS = 3000; // Rate limiting zwischen GC-Requests

class BuildCatalogManager {
  constructor({ db, gcBuildSearch, log, trace, getPersonaName }) {
    this.db = db;
    this.gcBuildSearch = gcBuildSearch;
    this.log = typeof log === 'function' ? log : () => {};
    this.trace = typeof trace === 'function' ? trace : () => {};
    this.getPersonaName = typeof getPersonaName === 'function' ? getPersonaName : async (id) => String(id);

    // Prepared statements
    this._initStatements();
  }

  _initStatements() {
    this.selectWatchedAuthorsStmt = this.db.prepare(`
      SELECT author_account_id, notes, priority, is_active
      FROM watched_build_authors
      WHERE is_active = 1
      ORDER BY priority DESC, author_account_id
    `);

    this.updateWatchedAuthorMetadataStmt = this.db.prepare(`
      UPDATE watched_build_authors
         SET last_checked_at = ?,
             last_checked_status = ?,
             last_checked_message = ?
       WHERE author_account_id = ?
    `);

    this.selectSourceBuildsStmt = this.db.prepare(`
      SELECT hbs.*, wba.priority, wba.notes as author_name_override
      FROM hero_build_sources hbs
      INNER JOIN watched_build_authors wba ON hbs.author_account_id = wba.author_account_id
      WHERE hbs.publish_ts >= ?
        AND hbs.language = ?
        AND wba.is_active = 1
      ORDER BY hbs.hero_id, wba.priority DESC, hbs.publish_ts DESC
    `);

    this.selectExistingCloneStmt = this.db.prepare(`
      SELECT * FROM hero_build_clones
      WHERE origin_hero_build_id = ?
        AND target_language = ?
      LIMIT 1
    `);

    this.insertHeroBuildCloneStmt = this.db.prepare(`
      INSERT INTO hero_build_clones (
        origin_hero_build_id, origin_build_id, hero_id, author_account_id,
        source_language, source_version, source_last_updated_ts,
        target_language, target_name, target_description, status
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
    `);

    this.updateCloneMetadataStmt = this.db.prepare(`
      UPDATE hero_build_clones
         SET target_name = ?,
             target_description = ?,
             updated_at = ?
       WHERE id = ?
    `);

    this.insertTaskStmt = this.db.prepare(`
      INSERT INTO steam_tasks (type, payload, status, created_at, updated_at)
      VALUES (?, ?, 'PENDING', ?, ?)
    `);

    this.checkPendingTaskStmt = this.db.prepare(`
      SELECT id FROM steam_tasks
      WHERE type = ?
        AND status IN ('PENDING', 'RUNNING')
      LIMIT 1
    `);
  }

  /**
   * Discover builds from all watched authors via GC
   * Ersetzt die alte DISCOVER_WATCHED_BUILDS Logik die die externe API nutzte
   */
  async discoverWatchedBuilds() {
    this.log('info', 'BuildCatalogManager: Starting build discovery via GC');

    const authors = this.selectWatchedAuthorsStmt.all();
    if (!authors || authors.length === 0) {
      this.log('warn', 'BuildCatalogManager: No watched authors configured');
      return { success: true, message: 'No authors to watch', authorsChecked: 0, buildsDiscovered: 0 };
    }

    this.log('info', 'BuildCatalogManager: Processing watched authors', { count: authors.length });

    const now = Math.floor(Date.now() / 1000);
    let totalNewBuilds = 0;
    let totalUpdatedBuilds = 0;
    let authorsChecked = 0;
    const errors = [];

    for (const author of authors) {
      try {
        this.log('info', 'BuildCatalogManager: Searching builds for author', {
          authorAccountId: author.author_account_id,
          priority: author.priority,
          notes: author.notes
        });

        // Search via GC
        const result = await this.gcBuildSearch.discoverBuildsFromAuthor(author.author_account_id);

        if (result.success) {
          totalNewBuilds += result.newBuilds || 0;
          totalUpdatedBuilds += result.updatedBuilds || 0;

          this.updateWatchedAuthorMetadataStmt.run(
            now,
            'success',
            `GC: ${result.totalBuilds} builds (${result.newBuilds} new)`,
            author.author_account_id
          );

          this.log('info', 'BuildCatalogManager: Author processed', {
            authorAccountId: author.author_account_id,
            totalBuilds: result.totalBuilds,
            newBuilds: result.newBuilds
          });
        } else {
          this.updateWatchedAuthorMetadataStmt.run(
            now,
            'gc_error',
            result.error || 'Unknown error',
            author.author_account_id
          );

          errors.push({
            authorAccountId: author.author_account_id,
            error: result.error
          });
        }

        authorsChecked++;

        // Rate limiting
        await this._sleep(GC_REQUEST_DELAY_MS);

      } catch (err) {
        this.log('error', 'BuildCatalogManager: Failed to process author', {
          authorAccountId: author.author_account_id,
          error: err.message
        });

        this.updateWatchedAuthorMetadataStmt.run(
          now,
          'error',
          err.message,
          author.author_account_id
        );

        errors.push({
          authorAccountId: author.author_account_id,
          error: err.message
        });
      }
    }

    this.log('info', 'BuildCatalogManager: Discovery completed', {
      authorsChecked,
      totalNewBuilds,
      totalUpdatedBuilds,
      errors: errors.length
    });

    return {
      success: true,
      authorsChecked,
      totalNewBuilds,
      totalUpdatedBuilds,
      errors
    };
  }

  /**
   * Alternative discovery: Search by HERO instead of by AUTHOR
   * This works when the GC doesn't respond to author-based searches
   * Iterates through all heroes, fetches all builds, filters by watched authors
   */
  async discoverWatchedBuildsViaHeroes() {
    this.log('info', 'BuildCatalogManager: Starting HERO-based build discovery via GC');

    // Get all watched author IDs
    const authors = this.selectWatchedAuthorsStmt.all();
    if (!authors || authors.length === 0) {
      this.log('warn', 'BuildCatalogManager: No watched authors configured');
      return { success: true, message: 'No authors to watch', heroesChecked: 0, buildsDiscovered: 0 };
    }

    const watchedAuthorIds = authors.map(a => a.author_account_id);
    this.log('info', 'BuildCatalogManager: Searching for builds from watched authors', {
      authorCount: watchedAuthorIds.length,
      authorIds: watchedAuthorIds
    });

    // Deadlock Hero IDs (1-25 as of current game state, some might not exist)
    // We'll try all IDs from 1-30 to cover future heroes
    const heroIds = Array.from({ length: 30 }, (_, i) => i + 1);

    const now = Math.floor(Date.now() / 1000);
    let totalNewBuilds = 0;
    let totalUpdatedBuilds = 0;
    let totalMatchedBuilds = 0;
    let heroesChecked = 0;
    let heroesWithBuilds = 0;
    const errors = [];

    for (const heroId of heroIds) {
      try {
        const result = await this.gcBuildSearch.discoverBuildsForHero(heroId, watchedAuthorIds);

        if (result.success) {
          heroesChecked++;
          if (result.totalBuilds > 0) {
            heroesWithBuilds++;
          }
          totalNewBuilds += result.newBuilds || 0;
          totalUpdatedBuilds += result.updatedBuilds || 0;
          totalMatchedBuilds += result.matchedBuilds || 0;

          if (result.matchedBuilds > 0) {
            this.log('info', 'BuildCatalogManager: Found builds from watched authors', {
              heroId,
              totalBuilds: result.totalBuilds,
              matchedBuilds: result.matchedBuilds,
              newBuilds: result.newBuilds
            });
          }
        } else {
          // Hero doesn't exist or GC error
          if (result.error && !result.error.includes('timeout')) {
            this.log('debug', 'BuildCatalogManager: Hero search failed (may not exist)', {
              heroId,
              error: result.error
            });
          } else {
            errors.push({ heroId, error: result.error });
          }
        }

        // Rate limiting between hero requests
        await this._sleep(GC_REQUEST_DELAY_MS);

      } catch (err) {
        this.log('error', 'BuildCatalogManager: Failed to process hero', {
          heroId,
          error: err.message
        });
        errors.push({ heroId, error: err.message });
      }
    }

    // Update watched authors metadata with summary
    const summaryMessage = `Hero-scan: ${totalMatchedBuilds} builds (${totalNewBuilds} new) from ${heroesWithBuilds} heroes`;
    for (const author of authors) {
      this.updateWatchedAuthorMetadataStmt.run(
        now,
        errors.length > 0 ? 'partial' : 'success',
        summaryMessage,
        author.author_account_id
      );
    }

    this.log('info', 'BuildCatalogManager: Hero-based discovery completed', {
      heroesChecked,
      heroesWithBuilds,
      totalMatchedBuilds,
      totalNewBuilds,
      totalUpdatedBuilds,
      errors: errors.length
    });

    return {
      success: true,
      heroesChecked,
      heroesWithBuilds,
      totalMatchedBuilds,
      totalNewBuilds,
      totalUpdatedBuilds,
      errors
    };
  }

  /**
   * Maintain the build catalog - select builds and create/update clones
   * Beibehalten: Max 3 Builds pro Hero, Author-Priorität, 45 Tage Fenster
   */
  async maintainCatalog() {
    this.log('info', 'BuildCatalogManager: Starting catalog maintenance');

    const stats = {
      buildsToClone: 0,
      buildsToUpdate: 0,
      tasksCreated: 0,
      skippedBuilds: 0
    };

    const now = Math.floor(Date.now() / 1000);
    const cutoffTime = now - (BUILD_AGE_DAYS * 24 * 60 * 60);

    // Get all source builds from watched authors
    const sourceBuilds = this.selectSourceBuildsStmt.all(cutoffTime, SOURCE_LANGUAGE);

    this.log('info', 'BuildCatalogManager: Found source builds', {
      count: sourceBuilds.length,
      cutoffDays: BUILD_AGE_DAYS
    });

    // Track builds per hero (max 3)
    const heroBuildCounts = {};

    for (const sourceBuild of sourceBuilds) {
      try {
        // Check hero limit
        const currentCount = heroBuildCounts[sourceBuild.hero_id] || 0;
        if (currentCount >= MAX_BUILDS_PER_HERO) {
          stats.skippedBuilds++;
          continue;
        }
        heroBuildCounts[sourceBuild.hero_id] = currentCount + 1;

        // Check if clone exists
        const existingClone = this.selectExistingCloneStmt.get(
          sourceBuild.hero_build_id,
          TARGET_LANGUAGE
        );

        // Build target metadata
        const targetMeta = await this._buildTargetMetadata(sourceBuild);

        if (existingClone) {
          // Check if update needed
          const needsUpdate = this._cloneNeedsUpdate(sourceBuild, existingClone, targetMeta);

          if (needsUpdate && existingClone.status !== 'processing') {
            stats.buildsToUpdate++;

            // Update clone metadata
            this.updateCloneMetadataStmt.run(
              targetMeta.name,
              targetMeta.description,
              now,
              existingClone.id
            );

            // Create BUILD_PUBLISH task
            this._createPublishTask({
              origin_hero_build_id: sourceBuild.hero_build_id,
              hero_build_clone_id: existingClone.id,
              minimal: false,
              update: true,
              target_language: TARGET_LANGUAGE,
              target_name: targetMeta.name,
              target_description: targetMeta.description
            });

            stats.tasksCreated++;
          }
        } else {
          // Create new clone
          stats.buildsToClone++;

          try {
            this.insertHeroBuildCloneStmt.run(
              sourceBuild.hero_build_id,
              sourceBuild.origin_build_id || 0,
              sourceBuild.hero_id,
              sourceBuild.author_account_id,
              sourceBuild.language,
              sourceBuild.version,
              sourceBuild.last_updated_ts,
              TARGET_LANGUAGE,
              targetMeta.name,
              targetMeta.description
            );

            // Create BUILD_PUBLISH task
            this._createPublishTask({
              origin_hero_build_id: sourceBuild.hero_build_id,
              minimal: false,
              target_language: TARGET_LANGUAGE
            });

            stats.tasksCreated++;

          } catch (err) {
            this.log('error', 'BuildCatalogManager: Failed to insert clone', {
              heroBuildId: sourceBuild.hero_build_id,
              error: err.message
            });
          }
        }

      } catch (err) {
        this.log('error', 'BuildCatalogManager: Failed to process build', {
          heroBuildId: sourceBuild.hero_build_id,
          error: err.message
        });
      }
    }

    this.log('info', 'BuildCatalogManager: Catalog maintenance completed', stats);

    return {
      success: true,
      ...stats
    };
  }

  /**
   * Full catalog cycle: discover + maintain
   */
  async runFullCycle() {
    this.log('info', 'BuildCatalogManager: Starting full catalog cycle');

    // Step 1: Discover builds from watched authors
    const discoveryResult = await this.discoverWatchedBuilds();

    // Step 2: Maintain catalog (create/update clones)
    const maintenanceResult = await this.maintainCatalog();

    return {
      success: true,
      discovery: discoveryResult,
      maintenance: maintenanceResult
    };
  }

  /**
   * Build target metadata for German clone
   */
  async _buildTargetMetadata(sourceBuild) {
    // Get author display name
    let authorDisplayName = sourceBuild.author_name_override;
    if (!authorDisplayName) {
      try {
        authorDisplayName = await this.getPersonaName(sourceBuild.author_account_id);
      } catch (err) {
        authorDisplayName = String(sourceBuild.author_account_id);
      }
    }
    if (!authorDisplayName) {
      authorDisplayName = String(sourceBuild.author_account_id);
    }

    // Build description
    const descLines = [
      "Deutsche Deadlock Community",
      "",
      "Discord Server beitreten:",
      "  - Scrolle in Discord links ganz nach unten",
      "  - Klicke auf das + Symbol",
      "  - Waehle 'Server beitreten'",
      "  - Gib den Code ein: XmnqMbUZ7Z",
      "",
      "Twitch: twitch.tv/EarlySalty",
      "Kostenlos: Coaching, Patchnotes, Leaks & Events",
      "",
      "---",
      `Original Author: ${authorDisplayName}`,
      `Original Build: ${sourceBuild.hero_build_id}`
    ];

    return {
      name: 'EarlySalty - Deutsche Deadlock Community (Discord)',
      description: descLines.join('\n')
    };
  }

  /**
   * Check if clone needs update
   */
  _cloneNeedsUpdate(sourceBuild, existingClone, targetMeta) {
    return (
      sourceBuild.version > (existingClone.source_version || 0) ||
      sourceBuild.last_updated_ts > existingClone.updated_at ||
      existingClone.target_name !== targetMeta.name ||
      existingClone.target_description !== targetMeta.description
    );
  }

  /**
   * Create a BUILD_PUBLISH task
   */
  _createPublishTask(payload) {
    const now = Math.floor(Date.now() / 1000);
    this.insertTaskStmt.run(
      'BUILD_PUBLISH',
      JSON.stringify(payload),
      now,
      now
    );
  }

  /**
   * Sleep helper
   */
  _sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}

module.exports = { BuildCatalogManager, MAX_BUILDS_PER_HERO, BUILD_AGE_DAYS };
