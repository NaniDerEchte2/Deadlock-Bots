const path = require('path');
const os = require('os');

const Database = require('better-sqlite3');

const dbPath = path.join(os.homedir(), 'Documents', 'Deadlock', 'service', 'deadlock.sqlite3');
const db = new Database(dbPath);

const query = "SELECT origin_hero_build_id, uploaded_build_id, target_name, hero_id, status FROM hero_build_clones WHERE status = 'done' ORDER BY updated_at DESC LIMIT 5";
const rows = db.prepare(query).all();

console.log(JSON.stringify(rows, null, 2));
