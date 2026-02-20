from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List

try:
    from service.standalone_manager import StandaloneBotConfig, StandaloneBotManager
except Exception as _standalone_import_error:
    StandaloneBotConfig = None  # type: ignore[assignment]
    StandaloneBotManager = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning(
        "Standalone manager unavailable: %s", _standalone_import_error
    )
    # TYPE_CHECKING import removed to avoid module-level cycles; use forward refs instead.


class StandaloneMixin:
    """Setup und Metriken für Standalone-Manager."""

    def setup_standalone_manager(self: "MasterBot") -> None:
        self.standalone_manager = None
        if StandaloneBotManager is None or StandaloneBotConfig is None:
            logging.getLogger(__name__).info(
                "Standalone manager Modul nicht verfügbar - überspringe"
            )
            return

        try:
            repo_root = self.root_dir
            standalone_dir = repo_root / "standalone"
            rank_script = standalone_dir / "rank_bot.py"
            custom_env: Dict[str, str] = {}
            pythonpath_entries: List[str] = []
            existing_pythonpath = os.environ.get("PYTHONPATH")
            if existing_pythonpath:
                pythonpath_entries.append(existing_pythonpath)
            pythonpath_entries.append(str(repo_root))
            custom_env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
            custom_env["PYTHONUNBUFFERED"] = "1"

            self.standalone_manager = StandaloneBotManager()
            manager = self.standalone_manager
            manager.register(
                StandaloneBotConfig(
                    key="rank",
                    name="Rank Bot",
                    script=rank_script,
                    workdir=repo_root,
                    description="Standalone Deadlock Rank Bot (eigener Discord-Token).",
                    autostart=True,
                    env=custom_env,
                    tags=["discord", "ranks", "dm"],
                    command_namespace="rank",
                    max_log_lines=400,
                    metrics_provider=self._collect_rank_bot_metrics,
                )
            )
            logging.info("Standalone manager initialisiert (Rank Bot registriert)")

            steam_dir = repo_root / "cogs" / "steam" / "steam_presence"
            steam_script = steam_dir / "index.js"
            if steam_script.exists():
                steam_env: Dict[str, str] = {}
                try:
                    from service import db as _db

                    steam_env["DEADLOCK_DB_PATH"] = str(_db.db_path())
                except Exception as db_exc:  # defensive logging
                    logging.getLogger(__name__).warning(
                        "Konnte DEADLOCK_DB_PATH für Steam Bridge nicht bestimmen: %s",
                        db_exc,
                    )

                default_data_dir = steam_dir / ".steam-data"
                if os.getenv("STEAM_PRESENCE_DATA_DIR"):
                    steam_env["STEAM_PRESENCE_DATA_DIR"] = os.getenv(
                        "STEAM_PRESENCE_DATA_DIR", ""
                    )
                else:
                    steam_env["STEAM_PRESENCE_DATA_DIR"] = str(default_data_dir)

                steam_env["NODE_ENV"] = os.getenv("STEAM_BRIDGE_NODE_ENV", "production")

                # Pass Steam API key to the bridge
                if os.getenv("STEAM_API_KEY"):
                    steam_env["STEAM_API_KEY"] = os.getenv("STEAM_API_KEY")

                node_executable = os.getenv("STEAM_BRIDGE_NODE") or "node"

                manager.register(
                    StandaloneBotConfig(
                        key="steam",
                        name="Steam Bridge",
                        script=steam_script,
                        workdir=steam_dir,
                        description="Node.js Steam Presence Bridge (Quick Invites, Auth Tasks).",
                        executable=node_executable,
                        env=steam_env,
                        autostart=True,
                        restart_on_crash=True,
                        daily_restart_at="05:00",
                        max_uptime_seconds=24 * 3600,
                        tags=["steam", "node", "presence"],
                        command_namespace="steam",
                        max_log_lines=400,
                        metrics_provider=self._collect_steam_bridge_metrics,
                    )
                )
                logging.info("Standalone manager: Steam Bridge registriert")
            else:
                logging.warning(
                    "Steam Bridge Script %s nicht gefunden – Registrierung übersprungen",
                    steam_script,
                )
        except Exception as exc:
            logging.getLogger(__name__).error(
                "Standalone manager konnte nicht initialisiert werden: %s",
                exc,
                exc_info=True,
            )
            self.standalone_manager = None

    async def _bootstrap_standalone_autostart(self) -> None:
        if not self.standalone_manager:
            return
        try:
            await self.standalone_manager.ensure_autostart()
        except Exception as exc:
            logging.getLogger(__name__).error(
                "Standalone Manager Autostart fehlgeschlagen: %s", exc
            )

    async def _collect_rank_bot_metrics(self) -> Dict[str, Any]:
        try:
            from service import db
        except Exception as exc:  # defensive import
            logging.getLogger(__name__).warning(
                "DB module unavailable for rank metrics: %s", exc
            )
            return {}

        def _query() -> Dict[str, Any]:
            meta_row = db.query_one(
                "SELECT heartbeat, payload, updated_at FROM standalone_bot_state WHERE bot=?",
                ("rank",),
            )
            payload: Dict[str, Any] = {}
            if meta_row and meta_row["payload"]:
                try:
                    payload = json.loads(meta_row["payload"])
                except Exception as decode_exc:
                    logging.getLogger(__name__).warning(
                        "Rank state payload decode failed: %s", decode_exc
                    )
                    payload = {}

            pending_rows = db.query_all(
                """
                SELECT id, command, status, created_at
                  FROM standalone_commands
                 WHERE bot=? AND status='pending'
              ORDER BY id ASC
                 LIMIT 20
                """,
                ("rank",),
            )
            recent_rows = db.query_all(
                """
                SELECT id, command, status, created_at, finished_at, error
                  FROM standalone_commands
                 WHERE bot=?
              ORDER BY id DESC
                 LIMIT 20
                """,
                ("rank",),
            )

            pending = [
                {
                    "id": int(row["id"]),
                    "command": row["command"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
                for row in pending_rows
            ]
            recent = [
                {
                    "id": int(row["id"]),
                    "command": row["command"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "finished_at": row["finished_at"],
                    "error": row["error"],
                }
                for row in recent_rows
            ]

            return {
                "state": payload,
                "pending_commands": pending,
                "recent_commands": recent,
                "heartbeat": meta_row["heartbeat"] if meta_row else None,
                "updated_at": meta_row["updated_at"] if meta_row else None,
            }

        return await asyncio.to_thread(_query)

    async def _collect_steam_bridge_metrics(self) -> Dict[str, Any]:
        try:
            from service import db
        except Exception as exc:  # defensive import
            logging.getLogger(__name__).warning(
                "DB module unavailable for steam metrics: %s", exc
            )
            return {}

        def _query() -> Dict[str, Any]:
            state_row = db.query_one(
                "SELECT heartbeat, payload, updated_at FROM standalone_bot_state WHERE bot=?",
                ("steam",),
            )
            payload: Dict[str, Any] = {}
            if state_row and state_row["payload"]:
                try:
                    payload = json.loads(state_row["payload"])
                except Exception as decode_exc:
                    logging.getLogger(__name__).warning(
                        "Steam bridge payload decode failed: %s",
                        decode_exc,
                    )
                    payload = {}

            pending_rows = db.query_all(
                """
                SELECT id, command, status, created_at
                  FROM standalone_commands
                 WHERE bot=? AND status='pending'
              ORDER BY id ASC
                 LIMIT 20
                """,
                ("steam",),
            )
            recent_rows = db.query_all(
                """
                SELECT id, command, status, created_at, finished_at, error
                  FROM standalone_commands
                 WHERE bot=?
              ORDER BY id DESC
                 LIMIT 20
                """,
                ("steam",),
            )

            task_counts_rows = db.query_all(
                """
                SELECT status, COUNT(*) AS count
                  FROM steam_tasks
              GROUP BY status
                """,
            )
            recent_tasks = db.query_all(
                """
                SELECT id, type, status, updated_at, finished_at
                  FROM steam_tasks
              ORDER BY updated_at DESC
                 LIMIT 10
                """,
            )
            quick_counts_rows = db.query_all(
                """
                SELECT status, COUNT(*) AS count
                  FROM steam_quick_invites
              GROUP BY status
                """,
            )
            quick_recent_rows = db.query_all(
                """
                SELECT invite_link, status, created_at
                  FROM steam_quick_invites
              ORDER BY created_at DESC
                 LIMIT 5
                """,
            )
            quick_available_row = db.query_one(
                """
                SELECT COUNT(*) AS count
                  FROM steam_quick_invites
                 WHERE status='available'
                   AND (expires_at IS NULL OR expires_at > strftime('%s','now'))
                """,
            )

            def _format_command_rows(
                rows: List[Any], *, include_finished: bool
            ) -> List[Dict[str, Any]]:
                formatted: List[Dict[str, Any]] = []
                for row in rows:
                    keys = set(row.keys()) if hasattr(row, "keys") else set()
                    formatted.append(
                        {
                            "id": int(row["id"]),
                            "command": row["command"],
                            "status": row["status"],
                            "created_at": row["created_at"],
                            "finished_at": row["finished_at"]
                            if include_finished and "finished_at" in keys
                            else None,
                            "error": row["error"]
                            if include_finished and "error" in keys
                            else None,
                        }
                    )
                return formatted

            def _format_task_counts(rows: List[Any]) -> Dict[str, int]:
                counts: Dict[str, int] = {}
                for row in rows:
                    status = str(row["status"] or "").upper()
                    try:
                        counts[status] = int(row["count"] or 0)
                    except (TypeError, ValueError):
                        counts[status] = 0
                return counts

            def _format_recent_tasks(rows: List[Any]) -> List[Dict[str, Any]]:
                recent: List[Dict[str, Any]] = []
                for row in rows:
                    recent.append(
                        {
                            "id": int(row["id"]),
                            "type": row["type"],
                            "status": row["status"],
                            "updated_at": row["updated_at"],
                            "finished_at": row["finished_at"],
                        }
                    )
                return recent

            def _format_quick_counts(rows: List[Any]) -> Dict[str, int]:
                result: Dict[str, int] = {}
                for row in rows:
                    status = str(row["status"] or "unknown")
                    try:
                        result[status] = int(row["count"] or 0)
                    except (TypeError, ValueError):
                        result[status] = 0
                return result

            quick_recent = [
                {
                    "invite_link": row["invite_link"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
                for row in quick_recent_rows
            ]

            task_counts = _format_task_counts(task_counts_rows)
            quick_counts = _format_quick_counts(quick_counts_rows)

            return {
                "state": payload,
                "runtime": payload.get("runtime", {}),
                "pending_commands": _format_command_rows(
                    pending_rows, include_finished=False
                ),
                "recent_commands": _format_command_rows(
                    recent_rows, include_finished=True
                ),
                "tasks": {
                    "counts": task_counts,
                    "recent": _format_recent_tasks(recent_tasks),
                },
                "quick_invites": {
                    "counts": quick_counts,
                    "recent": quick_recent,
                    "available": int(quick_available_row["count"])
                    if quick_available_row and quick_available_row["count"] is not None
                    else 0,
                    "total": sum(quick_counts.values()),
                },
                "heartbeat": int(state_row["heartbeat"])
                if state_row and state_row["heartbeat"] is not None
                else None,
                "updated_at": state_row["updated_at"] if state_row else None,
            }

        return await asyncio.to_thread(_query)
