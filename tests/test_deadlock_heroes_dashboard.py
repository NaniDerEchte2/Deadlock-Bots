from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web

from service import db
from service.dashboard import DashboardServer


class _DummyRequest:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        match_info: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload if payload is not None else {}
        self.match_info = match_info or {}

    async def json(self) -> dict[str, Any]:
        return self._payload


class DeadlockHeroesDashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmp.name) / "deadlock-dashboard-tests.sqlite3"
        self._old_db_path = os.environ.get("DEADLOCK_DB_PATH")
        os.environ["DEADLOCK_DB_PATH"] = str(self._db_path)

        db.close_connection()
        db._DB_PATH_CACHED = None  # type: ignore[attr-defined]
        db.connect()  # create base schema

        self.server = DashboardServer.__new__(DashboardServer)
        self.server._check_auth = lambda request, required=False: None  # type: ignore[method-assign]

    def tearDown(self) -> None:
        db.close_connection()
        db._DB_PATH_CACHED = None  # type: ignore[attr-defined]
        if self._old_db_path is None:
            os.environ.pop("DEADLOCK_DB_PATH", None)
        else:
            os.environ["DEADLOCK_DB_PATH"] = self._old_db_path
        self._tmp.cleanup()

    @staticmethod
    def _response_json(response: web.Response) -> dict[str, Any]:
        raw_text = response.text if response.text is not None else response.body.decode("utf-8")
        return json.loads(raw_text)

    @staticmethod
    def _create_sync_tables() -> None:
        conn = db.connect_proxy()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS hero_build_sources(
              hero_build_id INTEGER,
              origin_build_id INTEGER,
              author_account_id INTEGER NOT NULL,
              hero_id INTEGER NOT NULL,
              language INTEGER NOT NULL,
              version INTEGER NOT NULL,
              name TEXT NOT NULL,
              description TEXT,
              publish_ts INTEGER,
              last_updated_ts INTEGER,
              fetched_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
              last_seen_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            );

            CREATE TABLE IF NOT EXISTS hero_build_clones(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              origin_hero_build_id INTEGER NOT NULL,
              origin_build_id INTEGER,
              hero_id INTEGER NOT NULL,
              author_account_id INTEGER,
              source_language INTEGER,
              source_version INTEGER,
              source_last_updated_ts INTEGER,
              target_language INTEGER NOT NULL,
              target_name TEXT,
              target_description TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              status_info TEXT,
              uploaded_build_id INTEGER,
              uploaded_version INTEGER,
              created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
              updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
              last_attempt_at INTEGER,
              attempts INTEGER NOT NULL DEFAULT 0,
              UNIQUE(origin_hero_build_id, target_language)
            );
            """
        )

    async def test_upsert_hero_snapshot_and_sync_summary(self) -> None:
        self._create_sync_tables()
        now_ts = int(time.time())
        db.execute(
            """
            INSERT INTO hero_build_sources(
                hero_build_id, origin_build_id, author_account_id, hero_id, language,
                version, name, description, publish_ts, last_updated_ts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (101, 501, 9001, 7, 0, 1, "Source Build 101", "desc", now_ts, now_ts),
        )

        request = _DummyRequest(
            {
                "hero_id": 7,
                "name": "Seven",
                "is_active": True,
                "builds": [
                    {
                        "build_id": 101,
                        "build_name": "Aggro Core",
                        "author_name": "Alpha",
                        "is_active": True,
                        "sort_order": 10,
                    },
                    {
                        "build_id": 202,
                        "build_name": "Support Flex",
                        "author_name": "Bravo",
                        "is_active": True,
                        "sort_order": 20,
                    },
                ],
            }
        )

        response = await self.server._handle_deadlock_upsert_hero(request)
        payload = self._response_json(response)
        sync = payload["sync_summary"]

        self.assertEqual(payload["hero"]["hero_id"], 7)
        self.assertEqual(len(payload["hero"]["builds"]), 2)
        self.assertEqual(sync["queued"], 1)
        self.assertEqual(sync["skipped_missing_source"], 1)
        self.assertEqual(sync["errors"], 0)

        clone_rows = db.query_all(
            "SELECT origin_hero_build_id, hero_id, status FROM hero_build_clones ORDER BY origin_hero_build_id"
        )
        self.assertEqual(len(clone_rows), 1)
        self.assertEqual(int(clone_rows[0]["origin_hero_build_id"]), 101)
        self.assertEqual(int(clone_rows[0]["hero_id"]), 7)
        self.assertEqual(str(clone_rows[0]["status"]), "pending")

    async def test_duplicate_build_id_rejected(self) -> None:
        request = _DummyRequest(
            {
                "hero_id": 8,
                "name": "Eight",
                "builds": [
                    {"build_id": 11, "build_name": "One", "author_name": "A", "sort_order": 1},
                    {"build_id": 11, "build_name": "Two", "author_name": "B", "sort_order": 2},
                ],
            }
        )

        with self.assertRaises(web.HTTPBadRequest):
            await self.server._handle_deadlock_upsert_hero(request)

    async def test_delete_hero_also_deletes_build_snapshot(self) -> None:
        ts = int(time.time())
        db.execute(
            """
            INSERT INTO deadlock_heroes(hero_id, name, origin_build_id, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (12, "Twelve", None, 1, ts, ts),
        )
        db.execute(
            """
            INSERT INTO deadlock_hero_builds(hero_id, build_id, build_name, author_name, is_active, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (12, 1201, "Build X", "Author X", 1, 100, ts, ts),
        )

        response = await self.server._handle_deadlock_delete_hero(
            _DummyRequest(match_info={"hero_id": "12"})
        )
        payload = self._response_json(response)
        self.assertTrue(payload["deleted"])
        self.assertIsNone(db.query_one("SELECT 1 FROM deadlock_heroes WHERE hero_id = ?", (12,)))
        self.assertIsNone(
            db.query_one("SELECT 1 FROM deadlock_hero_builds WHERE hero_id = ?", (12,))
        )

    async def test_manual_sync_only_queues_active_builds(self) -> None:
        self._create_sync_tables()
        ts = int(time.time())
        db.execute(
            """
            INSERT INTO deadlock_heroes(hero_id, name, origin_build_id, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (99, "NinetyNine", None, 1, ts, ts),
        )
        db.execute(
            """
            INSERT INTO deadlock_hero_builds(hero_id, build_id, build_name, author_name, is_active, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (99, 9001, "Active Build", "Author A", 1, 10, ts, ts),
        )
        db.execute(
            """
            INSERT INTO deadlock_hero_builds(hero_id, build_id, build_name, author_name, is_active, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (99, 9002, "Inactive Build", "Author B", 0, 20, ts, ts),
        )
        db.execute(
            """
            INSERT INTO hero_build_sources(
                hero_build_id, origin_build_id, author_account_id, hero_id, language,
                version, name, description, publish_ts, last_updated_ts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9001, 19001, 777, 99, 0, 4, "S1", "desc", ts, ts),
        )
        db.execute(
            """
            INSERT INTO hero_build_sources(
                hero_build_id, origin_build_id, author_account_id, hero_id, language,
                version, name, description, publish_ts, last_updated_ts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9002, 19002, 888, 99, 0, 4, "S2", "desc", ts, ts),
        )

        response = await self.server._handle_deadlock_sync_hero(
            _DummyRequest(match_info={"hero_id": "99"})
        )
        payload = self._response_json(response)
        sync = payload["sync_summary"]

        self.assertEqual(sync["checked"], 1)
        self.assertEqual(sync["queued"], 1)
        self.assertEqual(sync["errors"], 0)
        self.assertEqual(sync["skipped_missing_source"], 0)

        clone_rows = db.query_all(
            "SELECT origin_hero_build_id FROM hero_build_clones ORDER BY origin_hero_build_id"
        )
        self.assertEqual([int(row["origin_hero_build_id"]) for row in clone_rows], [9001])


if __name__ == "__main__":
    unittest.main()
