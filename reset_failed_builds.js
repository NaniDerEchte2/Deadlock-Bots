const Database = require('better-sqlite3');
const path = require('path');

const dbPath = path.join(__dirname, 'service', 'deadlock.sqlite3');
const db = new Database(dbPath);

console.log('=== Resetting Failed Builds for Retry ===\n');

// Get failed builds count
const failedCount = db.prepare(`
  SELECT COUNT(*) as count FROM hero_build_clones WHERE status = 'failed'
`).get();

console.log(`Found ${failedCount.count} failed builds\n`);

if (failedCount.count === 0) {
  console.log('No failed builds to reset');
  db.close();
  process.exit(0);
}

// Reset failed builds to pending with 0 attempts
const result = db.prepare(`
  UPDATE hero_build_clones
  SET status = 'pending',
      attempts = 0,
      status_info = 'Reset after protobuf encoding fix',
      last_attempt_at = NULL
  WHERE status = 'failed'
`).run();

console.log(`✓ Reset ${result.changes} builds to pending status`);
console.log('✓ Attempts counter reset to 0');
console.log('✓ Builds will be picked up in next publisher run\n');

// Show what will be published
const nextBuilds = db.prepare(`
  SELECT c.origin_hero_build_id, c.target_language,
         s.hero_id, s.name
  FROM hero_build_clones c
  JOIN hero_build_sources s ON c.origin_hero_build_id = s.hero_build_id
  WHERE c.status = 'pending'
  ORDER BY c.created_at ASC
  LIMIT 10
`).all();

console.log('Next builds to be published:');
console.log('─'.repeat(80));
nextBuilds.forEach((build, idx) => {
  console.log(`${idx + 1}. Build #${build.origin_hero_build_id} | Hero ${build.hero_id} | ${build.name}`);
});

if (nextBuilds.length >= 10) {
  const totalPending = db.prepare(`
    SELECT COUNT(*) as count FROM hero_build_clones WHERE status = 'pending'
  `).get();
  console.log(`\n... and ${totalPending.count - 10} more`);
}

console.log('\n✓ Builds ready for next publisher run (in max 10 minutes)');

db.close();
