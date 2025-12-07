const Database = require('better-sqlite3');
const path = require('path');

const dbPath = path.join(__dirname, 'service', 'deadlock.sqlite3');
const db = new Database(dbPath, { readonly: true });

console.log('=== Latest Task Check ===\n');

// Get the very latest tasks (including pending/running)
const latestTasks = db.prepare(`
  SELECT id, type, status, error,
         datetime(created_at, 'unixepoch', 'localtime') as created,
         datetime(started_at, 'unixepoch', 'localtime') as started,
         datetime(finished_at, 'unixepoch', 'localtime') as finished
  FROM steam_tasks
  WHERE type = 'BUILD_PUBLISH'
  ORDER BY id DESC
  LIMIT 5
`).all();

const now = new Date();
console.log(`Current time: ${now.toLocaleString()}\n`);

console.log('Most Recent Tasks:');
console.log('─'.repeat(80));

if (latestTasks.length === 0) {
  console.log('No BUILD_PUBLISH tasks found in database');
} else {
  latestTasks.forEach(task => {
    const status = task.status === 'DONE' ? '✓' :
                   task.status === 'FAILED' ? '✗' :
                   task.status === 'RUNNING' ? '▶' :
                   task.status === 'PENDING' ? '◷' : '?';

    console.log(`${status} Task #${task.id} - ${task.status}`);
    console.log(`  Created:  ${task.created || 'N/A'}`);

    if (task.started) {
      console.log(`  Started:  ${task.started}`);
    }

    if (task.finished) {
      console.log(`  Finished: ${task.finished}`);
    }

    if (task.error) {
      const errorPreview = task.error.length > 100
        ? task.error.substring(0, 100) + '...'
        : task.error;
      console.log(`  Error: ${errorPreview}`);
    }
    console.log('');
  });

  const newest = latestTasks[0];
  const createdTime = new Date(newest.created);
  const ageMinutes = Math.floor((now - createdTime) / 1000 / 60);

  console.log(`\nNewest task (#${newest.id}) is ${ageMinutes} minutes old`);
}

// Check pending clones
const pendingClones = db.prepare(`
  SELECT COUNT(*) as count
  FROM hero_build_clones
  WHERE status = 'pending' AND attempts < 3
`).get();

console.log(`\nPending clones ready to publish: ${pendingClones.count}`);

// Check Steam bridge
const steamState = db.prepare(`
  SELECT payload
  FROM standalone_bot_state
  WHERE bot = 'steam'
`).get();

if (steamState) {
  const payload = JSON.parse(steamState.payload || '{}');
  const runtime = payload.runtime || {};

  console.log('\nSteam Bridge State:');
  console.log(`  Logged in: ${runtime.logged_on ? '✓ YES' : '✗ NO'}`);
  console.log(`  GC Ready:  ${runtime.deadlock_gc_ready ? '✓ YES' : '✗ NO'}`);

  if (!runtime.logged_on) {
    console.log('\n⚠ Steam not logged in - publisher will skip');
  } else if (!runtime.deadlock_gc_ready) {
    console.log('\n⚠ GC not ready - publisher will skip');
  } else {
    console.log('\n✓ All systems ready for publishing');
  }
}

db.close();
