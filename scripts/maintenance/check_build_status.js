const Database = require('better-sqlite3');
const path = require('path');

const dbPath = path.join(__dirname, 'service', 'deadlock.sqlite3');
const db = new Database(dbPath, { readonly: true });

console.log('=== Build System Status After Fix ===\n');

// Check recent tasks
const recentTasks = db.prepare(`
  SELECT id, type, status, error,
         datetime(created_at, 'unixepoch') as created,
         datetime(finished_at, 'unixepoch') as finished
  FROM steam_tasks
  WHERE type = 'BUILD_PUBLISH'
  ORDER BY id DESC
  LIMIT 10
`).all();

console.log('Recent BUILD_PUBLISH Tasks:');
console.log('─'.repeat(80));
recentTasks.forEach(task => {
  const status = task.status === 'DONE' ? '✓' : task.status === 'FAILED' ? '✗' : '◷';
  console.log(`${status} Task #${task.id} - ${task.status}`);
  console.log(`  Created: ${task.created || 'N/A'}`);
  console.log(`  Finished: ${task.finished || 'N/A'}`);
  if (task.error) {
    console.log(`  Error: ${task.error.substring(0, 80)}`);
  }
  console.log('');
});

// Count by status
const statusCounts = db.prepare(`
  SELECT status, COUNT(*) as count
  FROM steam_tasks
  WHERE type = 'BUILD_PUBLISH'
  GROUP BY status
`).all();

console.log('\nTask Status Summary:');
console.log('─'.repeat(80));
statusCounts.forEach(row => {
  console.log(`  ${row.status}: ${row.count}`);
});

// Check clone status
const cloneCounts = db.prepare(`
  SELECT status, COUNT(*) as count
  FROM hero_build_clones
  GROUP BY status
`).all();

console.log('\nBuild Clone Status Summary:');
console.log('─'.repeat(80));
cloneCounts.forEach(row => {
  console.log(`  ${row.status}: ${row.count}`);
});

// Check most recent successful upload
const recentSuccess = db.prepare(`
  SELECT c.origin_hero_build_id, c.uploaded_build_id, c.uploaded_version,
         datetime(c.updated_at, 'unixepoch') as updated,
         s.hero_id, s.name
  FROM hero_build_clones c
  JOIN hero_build_sources s ON c.origin_hero_build_id = s.hero_build_id
  WHERE c.status = 'uploaded'
  ORDER BY c.updated_at DESC
  LIMIT 5
`).all();

if (recentSuccess.length > 0) {
  console.log('\nRecent Successful Uploads:');
  console.log('─'.repeat(80));
  recentSuccess.forEach(row => {
    console.log(`✓ Build #${row.origin_hero_build_id} → #${row.uploaded_build_id} v${row.uploaded_version}`);
    console.log(`  Hero: ${row.hero_id} | Name: ${row.name}`);
    console.log(`  Uploaded: ${row.updated}`);
    console.log('');
  });
} else {
  console.log('\n⚠ No successful uploads yet');
}

// Check Steam bridge status
const steamStatus = db.prepare(`
  SELECT payload,
         datetime(last_heartbeat, 'unixepoch') as last_heartbeat
  FROM standalone_bot_state
  WHERE bot = 'steam'
  LIMIT 1
`).get();

if (steamStatus) {
  const payload = JSON.parse(steamStatus.payload || '{}');
  const runtime = payload.runtime || {};

  console.log('\nSteam Bridge Status:');
  console.log('─'.repeat(80));
  console.log(`  Last heartbeat: ${steamStatus.last_heartbeat}`);
  console.log(`  Logged in: ${runtime.logged_on ? '✓' : '✗'}`);
  console.log(`  GC Ready: ${runtime.deadlock_gc_ready ? '✓' : '✗'}`);
}

db.close();
