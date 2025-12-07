# Database Architecture Refactoring Plan

**Status:** Planning Phase
**Created:** 2025-12-06
**Priority:** Medium (Technical Debt)
**Estimated Effort:** 8-12 hours

---

## Executive Summary

This document outlines the complete migration strategy to centralize all database access through `service/db.py`, eliminating the current multi-connection architecture that causes "Cannot operate on a closed database" errors and potential data corruption.

### Current State (After Quick Fix)
- ✅ Central DB (`service/db.py`) with async support
- ✅ One cog fully migrated: `deadlock_team_balancer.py`
- ✅ All PRAGMA conflicts removed (no more WAL re-initialization)
- ⚠️ Four cogs still create own connections: `tempvoice`, `deadlock_voice_status`, `dl_coaching`, `rank_voice_manager`

### Target State
- ✅ **Single Source of Truth**: Only `service/db.py` manages DB connections
- ✅ **No Connection Leaks**: Automatic connection lifecycle management
- ✅ **Consistent Transaction Handling**: Unified commit/rollback patterns
- ✅ **Better Performance**: Connection pooling, query optimization

---

## Problem Analysis

### Root Cause
Multiple cogs create independent `aiosqlite.Connection` instances to the same SQLite file:

```python
# ❌ CURRENT (4 cogs doing this)
self.db = await aiosqlite.connect(str(DB_PATH))
await self.db.execute("PRAGMA journal_mode=WAL")  # Race condition!
```

### Symptoms Fixed
- ✅ "Cannot operate on a closed database" errors (PRAGMA conflicts resolved)
- ⚠️ Still risk of connection leaks if cog crashes
- ⚠️ Inconsistent transaction boundaries across cogs

### Remaining Risks
1. **Connection Leaks**: If a cog crashes, its DB connection may not close properly
2. **No Connection Pooling**: Each cog opens its own connection (resource waste)
3. **Inconsistent Patterns**: Some use transactions, some use autocommit
4. **Maintenance Burden**: DB changes require updating 5 different locations

---

## Migration Strategy

### Phase 1: Preparation (✅ DONE)
- [x] Add async wrappers to `service/db.py`
- [x] Add transaction context manager
- [x] Remove all PRAGMA conflicts from cogs
- [x] Migrate `deadlock_team_balancer.py` as proof-of-concept

### Phase 2: Cog Refactoring (TODO)
Migrate each cog individually with full testing between migrations.

#### Priority Order (Easiest → Hardest)

##### 1. rank_voice_manager.py (Low Complexity - 2 hours)
**Complexity:** 🟢 Easy
**Estimated Effort:** 2 hours
**DB Usage:** Simple queries, no complex transactions

**Changes Required:**
- Remove `self.db` attribute
- Replace `await self.db.execute()` → `await db.execute_async()`
- Replace `await self.db.execute_fetchone()` → `await db.query_one_async()`
- Remove `cog_load()` DB connection setup
- Remove `cog_unload()` DB cleanup

**Testing Checklist:**
- [ ] Voice channel permissions update correctly
- [ ] Anchor channels persist across bot restarts
- [ ] Role-based access works

##### 2. deadlock_voice_status.py (Medium Complexity - 3 hours)
**Complexity:** 🟡 Medium
**Estimated Effort:** 3 hours
**DB Usage:** Uses transactions, batch updates

**Changes Required:**
- Remove `self.db` attribute and `_ensure_db()` method
- Replace transaction pattern:
  ```python
  # OLD
  async with self.db:
      await self.db.executemany(...)
      await self.db.commit()

  # NEW
  async with db.transaction():
      await db.executemany_async(...)
      # Auto-commits on success
  ```
- Update `_watch_list_sync()` to use `db.query_all_async()`
- Handle rollback scenarios with transaction context

**Testing Checklist:**
- [ ] Voice status updates appear in channels
- [ ] Batch updates complete atomically
- [ ] Error handling works (rollback on exception)

##### 3. dl_coaching.py (High Complexity - 4 hours) ⚠️ EXCLUDED COG
**Complexity:** 🔴 High
**Estimated Effort:** 4 hours (when enabled)
**DB Usage:** Complex state machine, many helper methods

**Current Status:** This cog is **EXCLUDED** from loading (see startup logs). Refactor only if re-enabled.

**Changes Required:**
- Refactor entire `TVDB` pattern (9 DB helper methods)
- Replace all `_db_upsert()`, `_db_get()`, `_db_delete()` calls
- Migrate schema creation to central `service/db.init_schema()`
- Replace transaction handling in timeout loop

**Helper Methods to Migrate:**
```python
# Current helpers (all need replacement)
_db_connect()      → Remove (use central DB)
_db_ensure_schema() → Move to service/db.py
_db_upsert()       → Use db.execute_async()
_db_get()          → Use db.query_one_async()
_db_get_by_thread() → Use db.query_one_async()
_db_delete()       → Use db.execute_async()
_db_get_active()   → Use db.query_all_async()
```

**Testing Checklist:**
- [ ] Coaching sessions create correctly
- [ ] Thread associations work
- [ ] Timeout cleanup runs
- [ ] State transitions persist

##### 4. tempvoice/core.py (Very High Complexity - 5 hours)
**Complexity:** 🔴 Very High
**Estimated Effort:** 5 hours
**DB Usage:** Custom `TVDB` class (1106 lines), complex queries

**Changes Required:**
- **Option A (Recommended):** Refactor `TVDB` class to use `service.db` internally
  ```python
  class TVDB:
      # Remove self.db attribute
      # All methods use db.query_one_async(), db.execute_async(), etc.

      async def get_lane(self, channel_id: int):
          return await db.query_one_async(
              "SELECT * FROM tempvoice_lanes WHERE channel_id=?",
              (channel_id,)
          )
  ```

- **Option B (Easier):** Keep `TVDB` interface, delegate to central DB
  ```python
  class TVDB:
      # Keep existing method signatures
      # Replace implementation to use service.db
  ```

**TVDB Methods to Migrate (18 methods):**
```python
connect()              → Remove
_create_tables()       → Move to service/db.init_schema()
_ensure_interface_table() → Move to service/db.init_schema()
get_lane()            → Use db.query_one_async()
create_lane()         → Use db.execute_async()
delete_lane()         → Use db.execute_async()
get_lanes_by_guild()  → Use db.query_all_async()
update_lane_name()    → Use db.execute_async()
get_ban()             → Use db.query_one_async()
add_ban()             → Use db.execute_async()
remove_ban()          → Use db.execute_async()
get_bans_by_owner()   → Use db.query_all_async()
get_owner_prefs()     → Use db.query_one_async()
set_owner_region()    → Use db.execute_async()
get_interface_channel() → Use db.query_one_async()
set_interface_channel() → Use db.execute_async()
get_user_lane_state() → Use db.query_one_async()
set_user_lane_state() → Use db.execute_async()
```

**Testing Checklist:**
- [ ] Auto-lanes create when users join
- [ ] Owner permissions work correctly
- [ ] Bans persist and work
- [ ] Interface panel updates
- [ ] Region preferences save
- [ ] Lane cleanup on empty

---

## Phase 3: Schema Migration (TODO - 2 hours)

### Consolidate Schema Definitions
Currently, schema creation is scattered across cogs. Centralize in `service/db.py`.

**Current Schema Locations:**
- `service/db.py` - Core tables (voice_stats, steam_links, etc.)
- `tempvoice/core.py` - TempVoice tables
- `dl_coaching.py` - Coaching tables
- `rank_voice_manager.py` - (Uses existing tables)

**Target:** Single `init_schema()` function with all tables

```python
# service/db.py
def init_schema(conn: Optional[sqlite3.Connection] = None) -> None:
    """Initialize complete database schema."""
    c = conn or connect()

    # Core tables (existing)
    c.executescript("""...""")

    # TempVoice tables (migrate from tempvoice/core.py)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS tempvoice_lanes (...);
        CREATE TABLE IF NOT EXISTS tempvoice_bans (...);
        ...
    """)

    # Coaching tables (migrate from dl_coaching.py)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS coaching_sessions (...);
        ...
    """)
```

---

## Phase 4: Testing & Validation (TODO - 2 hours)

### Test Plan for Each Cog

#### Unit Tests
```python
# test_db_migration.py
import pytest
from service import db

@pytest.mark.asyncio
async def test_tempvoice_lane_crud():
    """Test TempVoice lane create/read/update/delete."""
    # Create lane
    await db.execute_async(
        "INSERT INTO tempvoice_lanes (channel_id, guild_id, owner_id, base_name, category_id) VALUES (?, ?, ?, ?, ?)",
        (123, 456, 789, "Test Lane", 111)
    )

    # Read lane
    lane = await db.query_one_async("SELECT * FROM tempvoice_lanes WHERE channel_id=?", (123,))
    assert lane["owner_id"] == 789

    # Update lane
    await db.execute_async("UPDATE tempvoice_lanes SET base_name=? WHERE channel_id=?", ("Updated", 123))

    # Delete lane
    await db.execute_async("DELETE FROM tempvoice_lanes WHERE channel_id=?", (123,))
```

#### Integration Tests
- [ ] Run bot in test guild
- [ ] Trigger all cog functionality
- [ ] Verify no "Cannot operate on a closed database" errors
- [ ] Check DB file size (should be stable, no leaks)
- [ ] Monitor connection count (`lsof` on Linux, Process Explorer on Windows)

#### Stress Tests
- [ ] Simulate 100 concurrent voice joins (TempVoice)
- [ ] Create/balance 50 matches rapidly (TeamBalancer)
- [ ] Monitor for connection exhaustion
- [ ] Check for memory leaks over 24h

---

## Migration Checklist Per Cog

Use this checklist when migrating each cog:

### Before Migration
- [ ] Read entire cog source code
- [ ] Document all DB methods used
- [ ] Identify transaction patterns
- [ ] Create test cases for critical paths
- [ ] Backup current working code

### During Migration
- [ ] Create feature branch: `refactor/db-{cog-name}`
- [ ] Remove `import aiosqlite`
- [ ] Add `from service import db`
- [ ] Remove `self.db` attribute
- [ ] Replace all `self.db.execute()` → `db.execute_async()`
- [ ] Replace all `self.db.execute_fetchone()` → `db.query_one_async()`
- [ ] Replace all `self.db.execute_fetchall()` → `db.query_all_async()`
- [ ] Replace all `self.db.executemany()` → `db.executemany_async()`
- [ ] Wrap transactions in `async with db.transaction():`
- [ ] Remove `await self.db.commit()` (auto-commit or in transaction)
- [ ] Remove `await self.db.rollback()` (auto-rollback on exception)
- [ ] Remove `cog_load()` DB connection code
- [ ] Remove `cog_unload()` DB cleanup code
- [ ] Update docstrings to reflect changes

### After Migration
- [ ] Run unit tests
- [ ] Run integration tests
- [ ] Test in development environment
- [ ] Code review
- [ ] Test in staging (if available)
- [ ] Deploy to production
- [ ] Monitor for 24h
- [ ] Merge feature branch

---

## Rollback Plan

If migration causes issues:

1. **Immediate Rollback:**
   ```bash
   git revert {commit-hash}
   python main_bot.py  # Restart with old code
   ```

2. **Partial Rollback:**
   - Keep `service/db.py` improvements
   - Revert specific cog changes only
   - Document lessons learned

3. **Data Recovery:**
   - SQLite WAL files auto-recover
   - Backup DB before each migration: `cp deadlock.sqlite3 deadlock.sqlite3.backup-{cog-name}`

---

## Performance Considerations

### Expected Improvements
- **Connection Overhead**: Reduce from 5 connections → 1 connection (~80% reduction)
- **Memory Usage**: ~5MB saved (connections + buffers)
- **Startup Time**: Faster (one connection init instead of five)

### Potential Regressions
- **Thread Executor Overhead**: Async wrapper adds ~0.1ms per query
  - **Mitigation**: Use connection pooling if needed
- **Lock Contention**: Single connection = more lock waiting
  - **Mitigation**: SQLite WAL mode handles this well (multiple readers, one writer)

### Monitoring Metrics
```python
# Add to service/db.py
import time

_query_count = 0
_total_query_time = 0.0

def query_one_async_instrumented(sql, params=()):
    global _query_count, _total_query_time
    start = time.perf_counter()
    result = await query_one_async(sql, params)
    elapsed = time.perf_counter() - start
    _query_count += 1
    _total_query_time += elapsed
    return result

# Log metrics every hour
print(f"DB Stats: {_query_count} queries, avg {_total_query_time/_query_count*1000:.2f}ms")
```

---

## Common Patterns & Solutions

### Pattern 1: Simple Query
```python
# OLD
row = await self.db.execute_fetchone("SELECT * FROM users WHERE id=?", (user_id,))

# NEW
row = await db.query_one_async("SELECT * FROM users WHERE id=?", (user_id,))
```

### Pattern 2: Batch Insert
```python
# OLD
await self.db.executemany("INSERT INTO logs VALUES (?, ?)", rows)
await self.db.commit()

# NEW
await db.executemany_async("INSERT INTO logs VALUES (?, ?)", rows)
# Auto-commits in autocommit mode, or wrap in transaction() if needed
```

### Pattern 3: Transaction
```python
# OLD
try:
    await self.db.execute("DELETE FROM table WHERE id=?", (id,))
    await self.db.execute("INSERT INTO log VALUES (?)", ("deleted",))
    await self.db.commit()
except:
    await self.db.rollback()
    raise

# NEW
async with db.transaction():
    await db.execute_async("DELETE FROM table WHERE id=?", (id,))
    await db.execute_async("INSERT INTO log VALUES (?)", ("deleted",))
    # Auto-commits on success, auto-rollbacks on exception
```

### Pattern 4: Cursor Iteration
```python
# OLD
cursor = await self.db.execute("SELECT * FROM users")
async for row in cursor:
    process(row)

# NEW
rows = await db.query_all_async("SELECT * FROM users")
for row in rows:
    process(row)
# Note: Loads all rows into memory. For huge result sets, consider pagination.
```

---

## Success Criteria

The refactoring is considered successful when:

- [ ] ✅ All cogs use only `service.db` for DB access
- [ ] ✅ No "Cannot operate on a closed database" errors for 7 days
- [ ] ✅ All integration tests pass
- [ ] ✅ Performance metrics stable or improved
- [ ] ✅ Code coverage >80% for DB layer
- [ ] ✅ Documentation updated
- [ ] ✅ Team trained on new patterns

---

## Timeline Estimate

| Phase | Cog | Hours | Dependencies |
|-------|-----|-------|-------------|
| ✅ Phase 1 | Preparation | 2h | None |
| ⏸️ Phase 2.1 | rank_voice_manager | 2h | Phase 1 |
| ⏸️ Phase 2.2 | deadlock_voice_status | 3h | Phase 2.1 |
| ⏸️ Phase 2.3 | tempvoice/core | 5h | Phase 2.2 |
| ⏸️ Phase 2.4 | dl_coaching (if enabled) | 4h | Phase 2.3 |
| ⏸️ Phase 3 | Schema Migration | 2h | Phase 2.* |
| ⏸️ Phase 4 | Testing & Validation | 2h | Phase 3 |
| **Total** | | **20h** | |

**Recommended Schedule:**
- Week 1: rank_voice_manager + deadlock_voice_status (5h)
- Week 2: tempvoice/core (5h)
- Week 3: Schema consolidation + Testing (4h)
- Week 4: dl_coaching if needed + Buffer (6h)

---

## Open Questions

1. **Connection Pooling:** Do we need connection pooling for SQLite?
   - **Answer:** Probably not - single WAL connection handles concurrency well
   - **Action:** Monitor performance, add pooling only if needed

2. **Read-Only Connections:** Should we support read-only connections for queries?
   - **Answer:** Consider for reporting/analytics
   - **Action:** Add `query_readonly_async()` if needed

3. **Migration Tool:** Do we need a migration framework (e.g., Alembic)?
   - **Answer:** Current schema is simple, manual migrations OK
   - **Action:** Revisit if schema changes become frequent

---

## References

- SQLite WAL Mode: https://www.sqlite.org/wal.html
- Python `sqlite3` Thread Safety: https://docs.python.org/3/library/sqlite3.html#sqlite3.threadsafety
- Discord.py Best Practices: https://discordpy.readthedocs.io/en/stable/faq.html#database

---

## Change Log

| Date | Author | Change |
|------|--------|--------|
| 2025-12-06 | Claude | Initial plan created |
| 2025-12-06 | Claude | Phase 1 completed (async support + quick fix) |

---

**Next Steps:** Pick a migration window and start with `rank_voice_manager.py` (lowest risk, 2h effort).
