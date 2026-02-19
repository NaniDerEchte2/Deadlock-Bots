"""Dashboard helpers for the Twitch cog."""

from __future__ import annotations

import os
import sqlite3
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import discord

from aiohttp import web

from .. import storage
from .server_v2 import build_v2_app
from ..constants import log
from ..constants import TWITCH_TARGET_GAME_NAME
from ..analytics.backend_extended import AnalyticsBackendExtended


def _parse_env_int(var_name: str, default: int = 0) -> int:
    raw = os.getenv(var_name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid integer for %s=%r – falling back to %s", var_name, raw, default)
        return default


STREAMER_ROLE_ID = _parse_env_int("STREAMER_ROLE_ID", 1313624729466441769)
STREAMER_GUILD_ID = _parse_env_int("STREAMER_GUILD_ID", 0)
FALLBACK_MAIN_GUILD_ID = _parse_env_int("MAIN_GUILD_ID", 0)


VERIFICATION_SUCCESS_DM_MESSAGE = (
    "🎉 Glückwunsch! Du wurdest erfolgreich als **Streamer-Partner** verifiziert und bist jetzt offiziell Teil des "
    "Streamer-Teams. Wir melden uns, falls wir noch Fragen haben – ansonsten schauen wir uns deine Angaben kurz an. "
    "Bei Fragen kannst du dich gerne hier melden: https://discord.com/channels/1289721245281292288/1428062025145385111"
)


class TwitchDashboardMixin:
    """Expose the aiohttp dashboard endpoints."""

    async def _dashboard_add(self, login: str, require_link: bool) -> str:
        return await self._cmd_add(login, require_link)

    async def _dashboard_remove(self, login: str) -> str:
        return await self._cmd_remove(login)

    async def _dashboard_list(self):
        # kleine Retry-Logik gegen gelegentliche "database is locked" Antworten
        for attempt in range(3):
            try:
                target_game = (os.getenv("TWITCH_TARGET_GAME_NAME") or TWITCH_TARGET_GAME_NAME or "").strip()
                with storage.get_conn() as c:
                    c.execute(
                        """
                        UPDATE twitch_streamers
                           SET is_on_discord=1
                         WHERE is_on_discord=0
                           AND EXISTS (
                               SELECT 1
                                 FROM twitch_streamers_partner_state ps
                                WHERE LOWER(ps.twitch_login) = LOWER(twitch_streamers.twitch_login)
                                  AND ps.is_partner = 1
                           )
                        """
                    )
                    rows = c.execute(
                        """
                        SELECT s.twitch_login,
                               s.manual_verified_permanent,
                               s.manual_verified_until,
                               s.manual_verified_at,
                               s.manual_partner_opt_out,
                               s.archived_at,
                               s.is_on_discord,
                               s.discord_user_id,
                               s.discord_display_name,
                               s.raid_bot_enabled,
                               a.raid_enabled AS raid_auth_enabled,
                               a.authorized_at AS raid_authorized_at,
                               a.token_expires_at AS raid_token_expires_at,
                               sess.last_deadlock_stream_at
                          FROM twitch_streamers s
                          LEFT JOIN twitch_raid_auth a
                            ON (
                                 s.twitch_user_id IS NOT NULL
                                 AND s.twitch_user_id = a.twitch_user_id
                               )
                            OR (
                                 s.twitch_user_id IS NULL
                                 AND LOWER(s.twitch_login) = LOWER(a.twitch_login)
                               )
                          LEFT JOIN (
                               SELECT LOWER(streamer_login) AS streamer_login,
                                      MAX(CASE
                                            WHEN had_deadlock_in_session = 1
                                                 OR LOWER(COALESCE(game_name,'')) = LOWER(?)
                                            THEN COALESCE(ended_at, started_at)
                                          END) AS last_deadlock_stream_at
                                 FROM twitch_stream_sessions
                                GROUP BY LOWER(streamer_login)
                          ) AS sess
                            ON sess.streamer_login = LOWER(s.twitch_login)
                         WHERE COALESCE(s.is_monitored_only, 0) = 0
                         ORDER BY s.twitch_login
                        """,
                        (target_game,),
                    ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 2:
                    raise
                await asyncio.sleep(0.3 * (attempt + 1))
        return []

    async def _dashboard_analytics_suggestions(
        self,
        include_non_partners: bool = True,
        *,
        days: int = 90,
        limit: int = 120,
    ) -> dict:
        """Partner- und optionale Non-Partner-Vorschläge für das Analytics-Dashboard."""
        partners = await self._dashboard_list()
        extras: List[dict] = []

        if include_non_partners:
            partner_logins = {
                (row.get("twitch_login") or row.get("streamer") or "").strip().lower()
                for row in partners
                if isinstance(row, dict)
            }
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
            try:
                with storage.get_conn() as c:
                    rows = c.execute(
                        """
                        SELECT streamer,
                               COUNT(*) AS samples,
                               MAX(ts_utc) AS last_seen,
                               AVG(viewer_count) AS avg_viewers
                          FROM twitch_stats_category
                         WHERE ts_utc >= ?
                         GROUP BY streamer
                         ORDER BY samples DESC, last_seen DESC
                         LIMIT ?
                        """,
                        (cutoff, limit * 2),
                    ).fetchall()
                for row in rows:
                    login = str(row["streamer"] if hasattr(row, "keys") else row[0] or "").strip()
                    if not login:
                        continue
                    lower = login.lower()
                    if lower in partner_logins:
                        continue
                    extras.append(
                        {
                            "twitch_login": login,
                            "avg_viewers": float(row["avg_viewers"] if hasattr(row, "keys") else row[3] or 0.0),
                            "samples": int(row["samples"] if hasattr(row, "keys") else row[1] or 0),
                            "last_seen": str(row["last_seen"] if hasattr(row, "keys") else row[2] or ""),
                        }
                    )
                    if len(extras) >= limit:
                        break
            except Exception:
                log.debug("Konnte Non-Partner-Suggestions fuer Analytics nicht laden", exc_info=True)

        return {"partners": partners, "extras": extras}

    async def _dashboard_set_discord_flag(self, login: str, is_on_discord: bool) -> str:
        normalized = self._normalize_login(login)
        if not normalized:
            raise ValueError("Ungültiger Login")

        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT twitch_login FROM twitch_streamers WHERE twitch_login=?",
                (normalized,),
            ).fetchone()
            if not row:
                raise ValueError(f"{normalized} ist nicht gespeichert")

            conn.execute(
                "UPDATE twitch_streamers SET is_on_discord=? WHERE twitch_login=?",
                (1 if is_on_discord else 0, normalized),
            )

        if is_on_discord:
            return f"{normalized} als Discord-Mitglied markiert"
        return f"Discord-Markierung für {normalized} entfernt"

    async def _dashboard_archive(self, login: str, mode: str) -> str:
        """
        Archiviert oder ent-archiviert einen Streamer.

        mode: 'archive'/'on' -> setzt archived_at=now, 'unarchive'/'off' -> NULL, 'toggle' -> flip.
        """
        normalized = self._normalize_login(login)
        if not normalized:
            raise ValueError("Ungültiger Login")

        mode_clean = (mode or "").strip().lower()
        if mode_clean in {"archive", "on", "set"}:
            desired = "archive"
        elif mode_clean in {"unarchive", "off", "unset", "restore"}:
            desired = "unarchive"
        else:
            desired = "toggle"

        now_iso = datetime.utcnow().isoformat(timespec="seconds")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT archived_at FROM twitch_streamers WHERE twitch_login = ?",
                (normalized,),
            ).fetchone()
            if not row:
                raise ValueError(f"{normalized} ist nicht gespeichert")
            current = row[0] if hasattr(row, "keys") else row[0]

            if desired == "archive":
                if current:
                    return f"{normalized} ist bereits archiviert (seit {current})"
                conn.execute(
                    "UPDATE twitch_streamers SET archived_at = ? WHERE twitch_login = ?",
                    (now_iso, normalized),
                )
                return f"{normalized} archiviert"

            if desired == "unarchive":
                if not current:
                    return f"{normalized} ist nicht archiviert"
                conn.execute(
                    "UPDATE twitch_streamers SET archived_at = NULL WHERE twitch_login = ?",
                    (normalized,),
                )
                return f"{normalized} ent-archiviert"

            # toggle
            new_value = None if current else now_iso
            conn.execute(
                "UPDATE twitch_streamers SET archived_at = ? WHERE twitch_login = ?",
                (new_value, normalized),
            )
            return f"{normalized} {'archiviert' if new_value else 'reaktiviert'}"

    async def _dashboard_save_discord_profile(
        self,
        login: str,
        *,
        discord_user_id: Optional[str],
        discord_display_name: Optional[str],
        mark_member: bool,
    ) -> str:
        normalized = self._normalize_login(login)
        if not normalized:
            raise ValueError("Ungültiger Login")

        discord_id_clean = (discord_user_id or "").strip()
        if discord_id_clean and not discord_id_clean.isdigit():
            raise ValueError("Discord-ID muss eine Zahl sein")

        display_name_clean = (discord_display_name or "").strip()
        if len(display_name_clean) > 120:
            display_name_clean = display_name_clean[:120]

        # Versuche twitch_user_id zu ermitteln
        twitch_user_id: Optional[str] = None
        
        # 1. Versuche aus raid_auth zu laden
        try:
            with storage.get_conn() as conn:
                raid_row = conn.execute(
                    "SELECT twitch_user_id FROM twitch_raid_auth WHERE LOWER(twitch_login)=LOWER(?)",
                    (normalized,),
                ).fetchone()
                if raid_row:
                    twitch_user_id = raid_row[0]
        except Exception:
            log.debug("Konnte user_id nicht aus raid_auth laden für %s", normalized, exc_info=True)
        
        # 2. Falls nicht in raid_auth: API-Call
        if not twitch_user_id and self.api:
            try:
                users = await self.api.get_users([normalized])
                user = users.get(normalized)
                if user:
                    twitch_user_id = user.get("id")
                    log.info("Fetched twitch_user_id %s for %s from API", twitch_user_id, normalized)
            except Exception:
                log.warning("Konnte user_id nicht von API holen für %s", normalized, exc_info=True)

        try:
            with storage.get_conn() as conn:
                row = conn.execute(
                    "SELECT twitch_login FROM twitch_streamers WHERE twitch_login=?",
                    (normalized,),
                ).fetchone()

                if row:
                    # UPDATE: Setze auch twitch_user_id falls verfügbar
                    if twitch_user_id:
                        conn.execute(
                            "UPDATE twitch_streamers "
                            "SET discord_user_id=?, discord_display_name=?, is_on_discord=?, twitch_user_id=? "
                            "WHERE twitch_login=?",
                            (
                                discord_id_clean or None,
                                display_name_clean or None,
                                1 if mark_member else 0,
                                twitch_user_id,
                                normalized,
                            ),
                        )
                    else:
                        conn.execute(
                            "UPDATE twitch_streamers "
                            "SET discord_user_id=?, discord_display_name=?, is_on_discord=? "
                            "WHERE twitch_login=?",
                            (
                                discord_id_clean or None,
                                display_name_clean or None,
                                1 if mark_member else 0,
                                normalized,
                            ),
                        )
                else:
                    # INSERT: Mit user_id falls verfügbar
                    conn.execute(
                        "INSERT INTO twitch_streamers "
                        "(twitch_login, twitch_user_id, discord_user_id, discord_display_name, is_on_discord) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            normalized,
                            twitch_user_id,
                            discord_id_clean or None,
                            display_name_clean or None,
                            1 if mark_member else 0,
                        ),
                    )
        except sqlite3.IntegrityError:
            raise ValueError("Discord-ID wird bereits verwendet")

        return f"Discord-Daten für {normalized} aktualisiert"

    async def _get_monetization_stats(self) -> dict:
        """Aggregate monetization & hype train data for the last 30 days."""
        cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        ads: dict = {
            "total": 0,
            "auto": 0,
            "manual": 0,
            "sessions_with_ads": 0,
            "avg_duration_s": 0.0,
            "avg_viewer_drop_pct": None,
            "worst_ads": [],
        }
        hype_train: dict = {"total": 0, "avg_level": 0.0, "max_level": 0, "avg_duration_s": 0.0}
        bits: dict = {"total": 0, "cheer_events": 0}
        subs: dict = {"total_events": 0, "gifted": 0}

        with storage.get_conn() as c:
            # 1a. Ad Break overview
            ad_agg = c.execute(
                """
                SELECT COUNT(*) AS total_ads,
                       SUM(CASE WHEN is_automatic=1 THEN 1 ELSE 0 END) AS auto_ads,
                       AVG(duration_seconds) AS avg_duration,
                       COUNT(DISTINCT session_id) AS sessions_with_ads
                  FROM twitch_ad_break_events
                 WHERE started_at >= ?
                """,
                (cutoff_30d,),
            ).fetchone()
            if ad_agg:
                total = int(ad_agg["total_ads"] or 0)
                auto = int(ad_agg["auto_ads"] or 0)
                ads["total"] = total
                ads["auto"] = auto
                ads["manual"] = total - auto
                ads["sessions_with_ads"] = int(ad_agg["sessions_with_ads"] or 0)
                ads["avg_duration_s"] = float(ad_agg["avg_duration"] or 0.0)

            # 1b. Viewer-impact analysis
            ad_rows = c.execute(
                """
                SELECT a.id, a.session_id, a.started_at, a.duration_seconds, a.is_automatic,
                       s.started_at AS session_start
                  FROM twitch_ad_break_events a
                  JOIN twitch_stream_sessions s ON s.id = a.session_id
                 WHERE a.started_at >= ?
                   AND a.session_id IS NOT NULL
                 ORDER BY a.started_at DESC
                 LIMIT 200
                """,
                (cutoff_30d,),
            ).fetchall()

            timeline_map: dict = {}
            if ad_rows:
                session_ids = list({int(r["session_id"]) for r in ad_rows if r["session_id"]})
                if session_ids:
                    session_ids_json = json.dumps(session_ids)
                    viewer_rows = c.execute(
                        """
                        SELECT session_id, minutes_from_start, viewer_count
                          FROM twitch_session_viewers
                         WHERE session_id IN (
                            SELECT CAST(value AS INTEGER) FROM json_each(?)
                         )
                         ORDER BY session_id, minutes_from_start
                        """,
                        (session_ids_json,),
                    ).fetchall()
                    for vr in viewer_rows:
                        sid = int(vr["session_id"])
                        timeline_map.setdefault(sid, []).append(
                            (float(vr["minutes_from_start"] or 0), int(vr["viewer_count"] or 0))
                        )

            drop_pcts: List[float] = []
            worst_ads: List[dict] = []
            for ad in ad_rows:
                session_id = int(ad["session_id"] or 0)
                ad_started = ad["started_at"]
                session_start = ad["session_start"]
                duration_s = float(ad["duration_seconds"] or 30)
                try:
                    ad_dt = datetime.fromisoformat(str(ad_started).replace("Z", "+00:00"))
                    sess_dt = datetime.fromisoformat(str(session_start).replace("Z", "+00:00"))
                    minutes_into = (ad_dt - sess_dt).total_seconds() / 60.0
                except Exception:
                    continue
                timeline = timeline_map.get(session_id, [])
                if not timeline:
                    continue
                duration_min = duration_s / 60.0
                pre_vals = [v for m, v in timeline if (minutes_into - 5) <= m < minutes_into]
                post_start = minutes_into + duration_min
                post_vals = [v for m, v in timeline if post_start <= m < (post_start + 5)]
                if not pre_vals or not post_vals:
                    continue
                pre_avg = sum(pre_vals) / len(pre_vals)
                if pre_avg <= 0:
                    continue
                post_avg = sum(post_vals) / len(post_vals)
                drop_pct = (post_avg - pre_avg) / pre_avg * 100.0
                drop_pcts.append(drop_pct)
                worst_ads.append({
                    "started_at": str(ad_started or "")[:16],
                    "duration_s": int(duration_s),
                    "drop_pct": round(drop_pct, 1),
                    "is_automatic": bool(ad["is_automatic"]),
                })

            if drop_pcts:
                ads["avg_viewer_drop_pct"] = round(sum(drop_pcts) / len(drop_pcts), 1)
            worst_ads.sort(key=lambda x: x["drop_pct"])
            ads["worst_ads"] = worst_ads[:5]

            # 1c. Hype Train overview
            try:
                ht_row = c.execute(
                    """
                    SELECT COUNT(*) AS total_trains,
                           AVG(level) AS avg_level,
                           MAX(level) AS max_level,
                           AVG(duration_seconds) AS avg_duration
                      FROM twitch_hype_train_events
                     WHERE started_at >= ?
                       AND ended_at IS NOT NULL
                    """,
                    (cutoff_30d,),
                ).fetchone()
                if ht_row:
                    hype_train["total"] = int(ht_row["total_trains"] or 0)
                    hype_train["avg_level"] = round(float(ht_row["avg_level"] or 0.0), 1)
                    hype_train["max_level"] = int(ht_row["max_level"] or 0)
                    hype_train["avg_duration_s"] = round(float(ht_row["avg_duration"] or 0.0), 0)
            except Exception:
                log.debug("Hype Train query fehlgeschlagen", exc_info=True)

            # 1d. Bits
            try:
                bits_row = c.execute(
                    "SELECT SUM(amount) AS total_bits, COUNT(*) AS cheer_events FROM twitch_bits_events WHERE received_at >= ?",
                    (cutoff_30d,),
                ).fetchone()
                if bits_row:
                    bits["total"] = int(bits_row["total_bits"] or 0)
                    bits["cheer_events"] = int(bits_row["cheer_events"] or 0)
            except Exception:
                log.debug("Bits query fehlgeschlagen", exc_info=True)

            # 1d. Subs
            try:
                subs_row = c.execute(
                    """
                    SELECT COUNT(*) AS total_events,
                           SUM(CASE WHEN is_gift=1 THEN 1 ELSE 0 END) AS gifted
                      FROM twitch_subscription_events
                     WHERE received_at >= ?
                    """,
                    (cutoff_30d,),
                ).fetchone()
                if subs_row:
                    subs["total_events"] = int(subs_row["total_events"] or 0)
                    subs["gifted"] = int(subs_row["gifted"] or 0)
            except Exception:
                log.debug("Subs query fehlgeschlagen", exc_info=True)

        return {
            "ads": ads,
            "hype_train": hype_train,
            "bits": bits,
            "subs": subs,
            "window_days": 30,
        }

    async def _dashboard_stats(
        self,
        *,
        hour_from: Optional[int] = None,
        hour_to: Optional[int] = None,
        streamer: Optional[str] = None,
    ) -> dict:
        stats = await self._compute_stats(
            hour_from=hour_from,
            hour_to=hour_to,
            streamer=streamer,
        )
        tracked_top = stats.get("tracked", {}).get("top", []) or []
        category_top = stats.get("category", {}).get("top", []) or []

        def _agg(items: List[dict]):
            samples = sum(int(d.get("samples") or 0) for d in items)
            uniq = len(items)
            avg_over_streamers = (
                sum(float(d.get("avg_viewers") or 0.0) for d in items) / float(uniq)
            ) if uniq else 0.0
            return samples, uniq, avg_over_streamers

        cat_samples, cat_uniq, cat_avg = _agg(category_top)
        tr_samples, tr_uniq, tr_avg = _agg(tracked_top)

        stats.setdefault("tracked", {})["samples"] = tr_samples
        stats["tracked"]["unique_streamers"] = tr_uniq
        stats.setdefault("category", {})["samples"] = cat_samples
        stats["category"]["unique_streamers"] = cat_uniq
        stats["avg_viewers_all"] = cat_avg
        stats["avg_viewers_tracked"] = tr_avg

        try:
            eventsub_fetcher = getattr(self, "_get_eventsub_capacity_overview", None)
            if callable(eventsub_fetcher):
                stats["eventsub"] = await eventsub_fetcher(hours=24)
        except Exception:
            log.debug("Konnte EventSub-Capacity-Overview nicht laden", exc_info=True)

        try:
            stats["monetization"] = await self._get_monetization_stats()
        except Exception:
            log.debug("Konnte Monetization-Stats nicht laden", exc_info=True)

        return stats

    async def _dashboard_streamer_analytics_data_old(self, streamer_login: str, days: int = 30) -> dict:
        """
        Comprehensive Analytics Data Aggregation.
        Calculates Channel Health Score and benchmarks against Deadlock category.
        """
        from datetime import datetime, timedelta
        import math

        def _pct(val: Optional[float]) -> float:
            if val is None: return 0.0
            # Heuristic: if <= 1.0 assume ratio, else percent
            if 0 <= val <= 1: return float(val) * 100.0
            return float(val)

        def _percentile_rank(val: float, population: List[float]) -> float:
            if not population: return 50.0
            population.sort()
            import bisect
            idx = bisect.bisect_left(population, val)
            return (idx / len(population)) * 100.0

        def _norm(val: float, target: float) -> float:
            if target <= 0: return 0.0
            return min(100.0, (val / target) * 100.0)

        login = self._normalize_login(streamer_login) if streamer_login else ""
        now = datetime.utcnow()
        cutoff = now - timedelta(days=days)
        cutoff_iso = cutoff.isoformat()

        # --- Data Containers ---
        sessions_data: List[dict] = []
        drops: List[dict] = []
        
        # --- Accumulators ---
        total_sessions = 0
        total_duration_h = 0.0
        total_watch_time_h = 0.0
        
        sum_avg_viewers = 0.0
        sum_peak_viewers = 0.0
        sum_ret10 = 0.0
        sum_dropoff = 0.0
        sum_unique_chatters = 0
        sum_chat_msgs = 0
        sum_followers = 0
        sum_returning_chatters = 0
        sum_first_chatters = 0

        with storage.get_conn() as conn:
            # 1. Sessions Data
            # ----------------
            session_rows = conn.execute(
                """
                SELECT id, streamer_login, started_at, duration_seconds, 
                       avg_viewers, peak_viewers, 
                       retention_10m, dropoff_pct, dropoff_label,
                       unique_chatters, returning_chatters, first_time_chatters,
                       follower_delta, stream_title, game_name
                  FROM twitch_stream_sessions
                 WHERE started_at >= ?
                   AND (streamer_login = ? OR ? = '')
                 ORDER BY started_at DESC
                """, (cutoff_iso, login, login)
            ).fetchall()

            # Chat Messages Map (Session ID -> Count)
            # Optimization: Pre-fetch counts
            msg_counts = {}
            if session_rows:
                ids = [str(r["id"]) for r in session_rows]
                # Note: This might be heavy if >1000 sessions, but for 30d single streamer it's fine.
                # If global view, limit strictly.
                if len(ids) < 900:
                    ids_json = json.dumps([int(session_id) for session_id in ids])
                    mc_rows = conn.execute(
                        """
                        SELECT session_id, COUNT(*) as c
                        FROM twitch_chat_messages
                        WHERE session_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
                        GROUP BY session_id
                        """,
                        (ids_json,),
                    ).fetchall()
                    msg_counts = {r["session_id"]: r["c"] for r in mc_rows}

            for s in session_rows:
                dur_sec = s["duration_seconds"] or 0
                dur_h = dur_sec / 3600.0
                
                avg_v = float(s["avg_viewers"] or 0)
                peak_v = int(s["peak_viewers"] or 0)
                ret10 = _pct(s["retention_10m"])
                drop_pct = _pct(s["dropoff_pct"])
                
                u_chat = s["unique_chatters"] or 0
                r_chat = s["returning_chatters"] or 0
                f_chat = s["first_time_chatters"] or 0
                
                msgs = msg_counts.get(s["id"], 0)
                # Fallback estimate if msg table empty but stats exist
                if msgs == 0 and u_chat > 0:
                    msgs = u_chat * 5 # Approximation

                f_delta = s["follower_delta"] or 0
                
                # Entry
                sess = {
                    "id": s["id"],
                    "date": s["started_at"][:10],
                    "startTime": s["started_at"][11:16],
                    "duration": dur_sec,
                    "avgViewers": avg_v,
                    "peakViewers": peak_v,
                    "retention10m": ret10,
                    "dropoff": drop_pct,
                    "chatters": u_chat,
                    "messages": msgs,
                    "followers": f_delta,
                    "title": s["stream_title"] or "",
                    "rpm": round(msgs / (dur_sec/60), 1) if dur_sec > 0 else 0
                }
                sessions_data.append(sess)
                
                # Drops for Insights
                if drop_pct > 15:
                    drops.append({
                        "date": sess["date"],
                        "pct": drop_pct,
                        "label": s["dropoff_label"] or "?"
                    })

                # Aggregates
                total_sessions += 1
                total_duration_h += dur_h
                total_watch_time_h += (avg_v * dur_h)
                
                sum_avg_viewers += avg_v
                sum_peak_viewers += peak_v
                sum_ret10 += ret10
                sum_dropoff += drop_pct
                sum_unique_chatters += u_chat
                sum_chat_msgs += msgs
                sum_followers += f_delta
                sum_returning_chatters += r_chat
                sum_first_chatters += f_chat

            # 2. Raid History (Network)
            # -------------------------
            raids_sent_row = conn.execute(
                "SELECT COUNT(*) as c, SUM(viewer_count) as v FROM twitch_raid_history WHERE from_broadcaster_login=? AND executed_at >= ?",
                (login, cutoff_iso)
            ).fetchone()
            raids_recv_row = conn.execute(
                "SELECT COUNT(*) as c, SUM(viewer_count) as v FROM twitch_raid_history WHERE to_broadcaster_login=? AND executed_at >= ?",
                (login, cutoff_iso)
            ).fetchone()
            
            raids_sent = raids_sent_row["c"] or 0
            raids_sent_viewers = raids_sent_row["v"] or 0
            raids_recv = raids_recv_row["c"] or 0

            # 3. Monetization (Subs)
            # ----------------------
            sub_row = conn.execute(
                "SELECT total, points FROM twitch_subscriptions_snapshot WHERE twitch_login=? ORDER BY snapshot_at DESC LIMIT 1",
                (login,)
            ).fetchone()
            curr_subs = sub_row["total"] or 0 if sub_row else 0
            curr_points = sub_row["points"] or 0 if sub_row else 0

            # 4. Engagement (Link Clicks)
            # ---------------------------
            clicks_row = conn.execute(
                "SELECT COUNT(*) as c FROM twitch_link_clicks WHERE streamer_login=? AND clicked_at >= ?",
                (login, cutoff_iso)
            ).fetchone()
            link_clicks = clicks_row["c"] or 0 if clicks_row else 0

            # 5. Benchmarking / Population Data
            # ---------------------------------
            # We need population distributions for percentiles
            
            # Category Avg Viewers Distribution
            pop_cat_rows = conn.execute(
                "SELECT AVG(viewer_count) as v FROM twitch_stats_category WHERE ts_utc >= ? GROUP BY streamer",
                (cutoff_iso,)
            ).fetchall()
            pop_avg_viewers = [r["v"] for r in pop_cat_rows if r["v"] is not None]

            # Internal Cohort (Partners/Tracked) for deeper stats
            # We use 'twitch_stream_sessions' aggregations for other metrics
            cohort_rows = conn.execute(
                """
                SELECT AVG(avg_viewers) as avg_v,
                       AVG(retention_10m) as r10, 
                       AVG(dropoff_pct) as avg_drop, 
                       SUM(follower_delta) as growth,
                       SUM(unique_chatters) as chat
                  FROM twitch_stream_sessions 
                 WHERE started_at >= ? 
                 GROUP BY streamer_login
                """, (cutoff_iso,)
            ).fetchall()
            
            cohort_avg = [float(r["avg_v"] or 0) for r in cohort_rows]
            cohort_ret10 = [_pct(r["r10"]) for r in cohort_rows]
            cohort_growth = [int(r["growth"] or 0) for r in cohort_rows]
            cohort_chat = [int(r["chat"] or 0) for r in cohort_rows]


        # --- Metric Calculations ---
        
        # Averages
        avg_v = sum_avg_viewers / max(total_sessions, 1)
        peak_v = sum_peak_viewers / max(total_sessions, 1) # Avg Peak
        avg_ret10 = sum_ret10 / max(total_sessions, 1)
        avg_drop = sum_dropoff / max(total_sessions, 1)
        
        # Rates
        chat_rate = (sum_unique_chatters / max(total_sessions, 1)) / max(avg_v, 1) * 100 # Chatters per 100 viewers
        returning_rate = (sum_returning_chatters / max(sum_unique_chatters, 1)) * 100
        conversion_rate = (sum_followers / max(sum_unique_chatters, 1)) * 100 # Follows per unique chatter (proxy for unique viewer)
        
        monetization_efficiency = curr_points / max(avg_v, 1) # Points per Viewer
        
        network_ratio = raids_sent / max(total_sessions, 1) # Raids sent per session
        
        # --- SCORING ENGINE (Improved) ---
        import math
        
        # Logarithmic scale for Reach to be fair to small streamers
        def _score_log(val):
            if val <= 1: return 0
            return min(100, math.log10(val) * 33) # 10->33, 100->66, 1000->99

        # 1. Reach (25%)
        # Mix of Percentile (vs Peers) and Absolute Log Scale (Progress)
        s_reach_avg = _percentile_rank(avg_v, cohort_avg if cohort_avg else pop_avg_viewers)
        s_reach_abs = _score_log(avg_v)
        score_reach = (s_reach_avg * 0.5) + (s_reach_abs * 0.5)
        
        # 2. Retention (25%)
        # Benchmarks: 30% is low, 70% is high
        s_ret_raw = _norm(avg_ret10 - 20, 50) # 20% -> 0, 70% -> 100
        s_ret_cohort = _percentile_rank(avg_ret10, cohort_ret10)
        s_drop = _norm(50 - avg_drop, 40) # 50% drop -> 0, 10% drop -> 100
        score_retention = (s_ret_raw * 0.4) + (s_ret_cohort * 0.3) + (s_drop * 0.3)
        
        # 3. Engagement (20%)
        # Benchmarks: 5 chatters/100v is low, 25 is high
        s_chat_density = _norm(chat_rate, 25)
        s_returning = _norm(returning_rate, 60) # 60% returning is very loyal
        s_clicks = _norm(link_clicks / max(total_duration_h, 1), 0.5)
        s_chat_cohort = _percentile_rank(sum_unique_chatters / max(total_sessions, 1), cohort_chat)
        score_engagement = (s_chat_density * 0.4) + (s_returning * 0.2) + (s_clicks * 0.2) + (s_chat_cohort * 0.2)
        
        # 4. Growth (15%)
        s_growth_cohort = _percentile_rank(sum_followers, cohort_growth)
        s_conversion = _norm(conversion_rate, 5) # 5% conversion is good
        score_growth = (s_growth_cohort * 0.6) + (s_conversion * 0.4)
        
        # 5. Monetization (10%)
        # Target: 2 points per viewer
        score_money = _norm(monetization_efficiency, 2.5)
        
        # 6. Network (5%)
        s_raid_sent = _norm(network_ratio, 0.5)
        s_raid_recv = _norm(raids_recv, 5) # 5 incoming raids in period
        score_network = (s_raid_sent * 0.6) + (s_raid_recv * 0.4)
        
        # Total
        total_score = (
            score_reach * 0.25 +
            score_retention * 0.25 +
            score_engagement * 0.20 +
            score_growth * 0.15 +
            score_money * 0.10 +
            score_network * 0.05
        )
        
        # --- Deep Insights & Correlations ---
        
        findings = []
        actions = []
        
        # Correlation Helper
        def _correlate(list_a, list_b):
            if len(list_a) != len(list_b) or len(list_a) < 3: return 0
            avg_a = sum(list_a) / len(list_a)
            avg_b = sum(list_b) / len(list_b)
            num = sum((a - avg_a) * (b - avg_b) for a, b in zip(list_a, list_b))
            den = math.sqrt(sum((a - avg_a)**2 for a in list_a) * sum((b - avg_b)**2 for b in list_b))
            return num / den if den != 0 else 0

        # Extract vectors for correlation
        vec_dur = [s["duration"] for s in sessions_data]
        vec_viewers = [s["avgViewers"] for s in sessions_data]
        vec_chat = [s["messages"] for s in sessions_data]
        vec_ret = [s["retention10m"] for s in sessions_data]
        
        corr_dur_view = _correlate(vec_dur, vec_viewers)
        corr_chat_ret = _correlate(vec_chat, vec_ret)
        
        # 1. Retention Analysis
        if avg_ret10 < 35:
            findings.append({"type": "neg", "title": "Kritischer Viewer-Verlust", "text": f"Nur {avg_ret10:.1f}% deiner Zuschauer bleiben länger als 10 Minuten. Dies ist der Hauptgrund für stagnierendes Wachstum."})
            actions.append({"tag": "Content", "text": "Strukturiere deine ersten 15 Minuten neu: Kein 'Warten auf Zuschauer', starte sofort mit Content/Gameplay."})
        elif avg_ret10 > 60:
            findings.append({"type": "pos", "title": "Starke Bindung", "text": "Deine Zuschauer bleiben überdurchschnittlich lange (Top-Tier Retention). Das Fundament für Wachstum steht."})

        # 2. Chat & Community
        if chat_rate < 5:
            findings.append({"type": "neg", "title": "Stiller Chat", "text": "Weniger als 5% deiner Zuschauer chatten. Das schadet der Discovery und Bindung."})
            actions.append({"tag": "Engagement", "text": "Nutze 'Call-to-Action': Stelle alle 15 Minuten eine offene Frage an den Chat oder nutze Predictions."})
        elif corr_chat_ret > 0.4:
            findings.append({"type": "pos", "title": "Chat treibt Retention", "text": "Daten zeigen: Wenn dein Chat aktiv ist, bleiben die Leute deutlich länger. Interaktion ist dein Schlüssel zum Erfolg."})

        # 3. Schedule & Duration
        if corr_dur_view < -0.3:
            findings.append({"type": "warn", "title": "Zu lange Streams?", "text": "Deine Zuschauerzahlen sinken bei längeren Streams deutlich. Deine Audience ermüdet."})
            actions.append({"tag": "Schedule", "text": "Versuche, deine Streams um 30-60 Minuten zu kürzen und die Energie zu komprimieren."})
        
        # 4. Networking
        if network_ratio < 0.1:
            findings.append({"type": "neg", "title": "Isolierte Insel", "text": "Du raidest fast nie. Twitch ist ein Geben und Nehmen."})
            actions.append({"tag": "Network", "text": "Suche dir 2-3 Deadlock-Partner ähnlicher Größe und raide sie konsequent nach jedem Stream."})
        elif raids_recv > 5:
            findings.append({"type": "pos", "title": "Guter Netzwerk-Hub", "text": "Du wirst oft geraidet. Deine Networking-Strategie funktioniert."})

        # 5. Growth
        if conversion_rate < 1.0 and avg_v > 10:
             findings.append({"type": "neg", "title": "Niedrige Conversion", "text": "Zuschauer schauen zu, folgen aber nicht. Der 'Reason to Follow' fehlt."})
             actions.append({"tag": "Growth", "text": "Erinnere an Follows in High-Hype-Momenten (nicht am Anfang). Definiere ein Follow-Goal im Overlay."})

        # --- Benchmark Comparison ---
        avg_pop_viewers = sum(pop_avg_viewers)/len(pop_avg_viewers) if pop_avg_viewers else 0
        avg_pop_ret = sum(cohort_ret10)/len(cohort_ret10) if cohort_ret10 else 0
        
        benchmarks = {
            "avgViewers": {"you": round(avg_v, 1), "avg": round(avg_pop_viewers, 1), "top10": round(_percentile_rank(90, pop_avg_viewers), 1)},
            "retention": {"you": round(avg_ret10, 1), "avg": round(avg_pop_ret, 1)},
        }

        scores = {
            "total": round(total_score),
            "reach": round(score_reach),
            "retention": round(score_retention),
            "engagement": round(score_engagement),
            "growth": round(score_growth),
            "monetization": round(score_money),
            "network": round(score_network)
        }
        
        summary = {
            "avgViewers": round(avg_v, 1),
            "peakViewers": peak_v,
            "hoursStreamed": round(total_duration_h, 1),
            "hoursWatched": round(total_watch_time_h, 0),
            "followersDelta": sum_followers,
            "subsTotal": curr_subs,
            "subPoints": curr_points,
            "raidsSent": raids_sent,
            "raidsRecv": raids_recv,
            "linkClicks": link_clicks,
            "uniqueChatters": sum_unique_chatters
        }

        return {
            "meta": {"streamer": login, "days": days, "generated": now.isoformat()},
            "scores": scores,
            "summary": summary,
            "benchmarks": benchmarks,
            "sessions": sessions_data,
            "findings": findings,
            "actions": actions,
            "correlations": {
                "durationVsViewers": round(corr_dur_view, 2),
                "chatVsRetention": round(corr_chat_ret, 2)
            },
            "retention": {"avg10m": round(avg_ret10, 1), "avgDrop": round(avg_drop, 1), "drops": drops},
            "network": {"sent": raids_sent, "received": raids_recv, "sentViewers": raids_sent_viewers},
        }

    async def _dashboard_streamer_analytics_data(self, streamer_login: str, days: int = 30) -> dict:
        """
        New comprehensive analytics using AnalyticsBackendExtended.
        Returns data structure compatible with the new React dashboard.
        """
        return await AnalyticsBackendExtended.get_comprehensive_analytics(
            streamer_login=streamer_login,
            days=days
        )

    async def _dashboard_streamer_overview(self, login: str) -> dict:
        """Fetch comprehensive stats for a single streamer."""
        login = self._normalize_login(login)
        if not login:
            return {}

        data = {"login": login}
        with storage.get_conn() as c:
            # 1. Stammdaten
            row = c.execute(
                "SELECT * FROM twitch_streamers WHERE twitch_login=?", (login,)
            ).fetchone()
            if not row:
                return {}
            data["meta"] = dict(row)

            # 2. Aggregated Session Stats (Last 30 days)
            agg = c.execute(
                """
                SELECT COUNT(*) as total_streams,
                       SUM(duration_seconds) as total_duration,
                       AVG(avg_viewers) as avg_avg_viewers,
                       MAX(peak_viewers) as max_peak,
                       SUM(follower_delta) as total_follower_delta,
                       SUM(unique_chatters) as total_unique_chatters
                  FROM twitch_stream_sessions
                 WHERE streamer_login=?
                   AND started_at > datetime('now', '-30 days')
                """,
                (login,),
            ).fetchone()
            data["stats_30d"] = dict(agg) if agg else {}

            # 3. Recent Sessions
            sessions = c.execute(
                """
                SELECT id, stream_id, started_at, duration_seconds, 
                       avg_viewers, peak_viewers, follower_delta, stream_title
                  FROM twitch_stream_sessions
                 WHERE streamer_login=?
                 ORDER BY started_at DESC
                 LIMIT 20
                """,
                (login,),
            ).fetchall()
            data["recent_sessions"] = [dict(s) for s in sessions]

        return data

    async def _dashboard_session_detail(self, session_id: int) -> dict:
        """Fetch deep-dive data for a single session."""
        data = {}
        with storage.get_conn() as c:
            # 1. Session Meta
            row = c.execute(
                "SELECT * FROM twitch_stream_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if not row:
                return {}
            data["session"] = dict(row)

            # 2. Viewer Timeline (Chart data)
            timeline = c.execute(
                """
                SELECT minutes_from_start, viewer_count 
                  FROM twitch_session_viewers 
                 WHERE session_id=? 
                 ORDER BY minutes_from_start ASC
                """,
                (session_id,),
            ).fetchall()
            data["timeline"] = [dict(t) for t in timeline]

            # 3. Chat Stats (if needed separately, though rolled up in session)
            # potentially fetch top chatters here
            top_chatters = c.execute(
                """
                SELECT chatter_login, messages 
                  FROM twitch_session_chatters
                 WHERE session_id=?
                 ORDER BY messages DESC
                 LIMIT 10
                """,
                (session_id,),
            ).fetchall()
            data["top_chatters"] = [dict(tc) for tc in top_chatters]

        return data

    async def _dashboard_comparison_stats(self, days: int = 30) -> dict:
        """Fetch comparative stats: Me vs Category vs Top."""
        data = {}
        with storage.get_conn() as c:
            # Global Category Stats (Deadlock)
            cat_stats = c.execute(
                """
                SELECT AVG(viewer_count) as avg_viewers, MAX(viewer_count) as peak_viewers
                  FROM twitch_stats_category
                 WHERE ts_utc > datetime('now', ?)
                """,
                (f"-{days} days",),
            ).fetchone()
            data["category"] = dict(cat_stats) if cat_stats else {}

            # Tracked Partner Stats
            track_stats = c.execute(
                """
                SELECT AVG(viewer_count) as avg_viewers, MAX(viewer_count) as peak_viewers
                  FROM twitch_stats_tracked
                 WHERE ts_utc > datetime('now', ?)
                """,
                (f"-{days} days",),
            ).fetchone()
            data["tracked_avg"] = dict(track_stats) if track_stats else {}

            # Top 5 Streamers by Avg Viewers (Local Data)
            top_streamers = c.execute(
                """
                SELECT streamer_login, AVG(avg_viewers) as val
                  FROM twitch_stream_sessions
                 WHERE started_at > datetime('now', ?)
                 GROUP BY streamer_login
                 ORDER BY val DESC
                 LIMIT 5
                """,
                (f"-{days} days",),
            ).fetchall()
            data["top_streamers"] = [dict(r) for r in top_streamers]

        return data

    async def _ensure_streamer_role(self, row_data: Optional[dict]) -> str:
        """Assign the streamer role when available; return a short status hint."""
        if STREAMER_ROLE_ID <= 0:
            return ""
        if not row_data:
            return ""

        user_id_raw = row_data.get("discord_user_id")
        if not user_id_raw:
            log.info("Streamer verification: no Discord ID stored for %s", row_data.get("discord_display_name"))
            return ""

        try:
            user_id = int(str(user_id_raw))
        except (TypeError, ValueError):
            log.warning("Streamer verification: invalid Discord ID %r", user_id_raw)
            return "(Streamer-Rolle konnte nicht vergeben werden – ungültige Discord-ID)"

        guild_candidates: List[discord.Guild] = []
        seen: set[int] = set()

        for guild_id in (STREAMER_GUILD_ID, FALLBACK_MAIN_GUILD_ID):
            if guild_id and guild_id not in seen:
                seen.add(guild_id)
                guild = self.bot.get_guild(guild_id)
                if guild:
                    guild_candidates.append(guild)

        if not guild_candidates:
            guild_candidates.extend(self.bot.guilds)

        for guild in guild_candidates:
            role = guild.get_role(STREAMER_ROLE_ID)
            if role is None:
                continue

            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    member = None
                except discord.HTTPException as exc:
                    log.warning("Streamer verification: fetch_member failed in guild %s: %s", guild.id, exc)
                    member = None

            if member is None:
                continue

            if role in member.roles:
                return ""

            try:
                await member.add_roles(role, reason="Streamer-Verifizierung über Dashboard bestätigt")
                log.info(
                    "Streamer verification: assigned role %s to %s in guild %s",
                    STREAMER_ROLE_ID,
                    user_id,
                    guild.id,
                )
                return "(Streamer-Rolle vergeben)"
            except discord.Forbidden:
                log.warning(
                    "Streamer verification: missing permissions to add role %s in guild %s",
                    STREAMER_ROLE_ID,
                    guild.id,
                )
                return "(Streamer-Rolle konnte nicht vergeben werden – fehlende Berechtigung)"
            except discord.HTTPException as exc:
                log.warning(
                    "Streamer verification: failed to add role %s in guild %s: %s",
                    STREAMER_ROLE_ID,
                    guild.id,
                    exc,
                )
                return "(Streamer-Rolle konnte nicht vergeben werden)"

        log.info(
            "Streamer verification: role %s or member %s not found in available guilds",
            STREAMER_ROLE_ID,
            user_id,
        )
        return "(Streamer-Rolle konnte nicht vergeben werden – Mitglied/Rolle nicht gefunden)"

    async def _notify_verification_success(self, login: str, row_data: Optional[dict]) -> str:
        if not row_data:
            log.info("Keine Discord-Daten für %s zum Versenden der Erfolgsnachricht gefunden", login)
            return ""

        user_id_raw = row_data.get("discord_user_id")
        if not user_id_raw:
            log.info("Keine Discord-ID für %s hinterlegt – überspringe Erfolgsnachricht", login)
            return ""

        try:
            user_id_int = int(str(user_id_raw))
        except (TypeError, ValueError):
            log.warning("Ungültige Discord-ID %r für %s – keine Erfolgsnachricht", user_id_raw, login)
            return "(Discord-DM konnte nicht zugestellt werden)"

        user = self.bot.get_user(user_id_int)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id_int)
            except discord.NotFound:
                user = None
            except discord.HTTPException:
                log.exception("Konnte Discord-User %s nicht abrufen", user_id_int)
                user = None

        if user is None:
            log.warning("Discord-User %s (%s) konnte nicht gefunden werden", user_id_int, login)
            return "(Discord-DM konnte nicht zugestellt werden)"

        try:
            await user.send(VERIFICATION_SUCCESS_DM_MESSAGE)
        except discord.Forbidden:
            log.warning(
                "DM an %s (%s) wegen erfolgreicher Verifizierung blockiert", user_id_int, login
            )
            return "(Discord-DM konnte nicht zugestellt werden)"
        except discord.HTTPException:
            log.exception(
                "Konnte Erfolgsnachricht nach Verifizierung nicht an %s senden", user_id_int
            )
            return "(Discord-DM konnte nicht zugestellt werden)"

        log.info(
            "Verifizierungs-Erfolgsnachricht an %s (%s) gesendet", user_id_int, login
        )
        return ""

    async def _dashboard_verify(self, login: str, mode: str) -> str:
        login = self._normalize_login(login)
        if not login:
            return "Ungültiger Login"

        if mode in {"permanent", "temp"}:
            row_data = None
            should_notify = False
            copied = 0
            with storage.get_conn() as c:
                row = c.execute(
                    (
                        "SELECT discord_user_id, discord_display_name, manual_verified_at "
                        "FROM twitch_streamers WHERE twitch_login=?"
                    ),
                    (login,),
                ).fetchone()
                if row:
                    row_data = dict(row)
                    should_notify = row_data.get("manual_verified_at") is None

                if mode == "permanent":
                    c.execute(
                        "UPDATE twitch_streamers "
                        "SET manual_verified_permanent=1, manual_verified_until=NULL, manual_verified_at=datetime('now'), "
                        "    manual_partner_opt_out=0, is_monitored_only=0, "
                        "    is_on_discord=1 "
                        "WHERE twitch_login=?",
                        (login,),
                    )
                    base_msg = f"{login} dauerhaft verifiziert"
                else:
                    c.execute(
                        "UPDATE twitch_streamers "
                        "SET manual_verified_permanent=0, manual_verified_until=datetime('now','+30 days'), "
                        "    manual_verified_at=datetime('now'), manual_partner_opt_out=0, is_monitored_only=0, is_on_discord=1 "
                        "WHERE twitch_login=?",
                        (login,),
                    )
                    base_msg = f"{login} für 30 Tage verifiziert"
                copied = storage.backfill_tracked_stats_from_category(c, login)

            notes: List[str] = []
            if copied:
                notes.append(f"({copied} historische Datenpunkte übernommen)")
            if should_notify:
                dm_note = await self._notify_verification_success(login, row_data)
                if dm_note:
                    notes.append(dm_note)
            role_note = await self._ensure_streamer_role(row_data)
            if role_note:
                notes.append(role_note)
            merged = " ".join(notes).strip()
            return f"{base_msg} {merged}".strip()

        if mode == "clear":
            with storage.get_conn() as c:
                c.execute(
                    "UPDATE twitch_streamers "
                    "SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL, "
                    "    manual_partner_opt_out=1, is_monitored_only=0 "
                    "WHERE twitch_login=?",
                    (login,),
                )

            # "Kein Partner" ist eine rein interne Markierung – es sollen hierbei keine DMs
            # ausgelöst werden. Wir geben daher eine entsprechend klare Rückmeldung aus,
            # damit Dashboard-Nutzer:innen wissen, dass keine Nachricht verschickt wurde.
            return f"Verifizierung für {login} zurückgesetzt (keine DM versendet)"

        if mode == "failed":
            row_data = None
            with storage.get_conn() as c:
                row = c.execute(
                    "SELECT discord_user_id, discord_display_name FROM twitch_streamers WHERE twitch_login=?",
                    (login,),
                ).fetchone()
                if row:
                    row_data = dict(row)
                    c.execute(
                        "UPDATE twitch_streamers "
                        "SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL, "
                        "    manual_partner_opt_out=0, is_monitored_only=0 "
                        "WHERE twitch_login=?",
                        (login,),
                    )

            if not row_data:
                return f"{login} ist nicht gespeichert"

            user_id_raw = row_data.get("discord_user_id")
            if not user_id_raw:
                return f"Keine Discord-ID für {login} hinterlegt"

            try:
                user_id_int = int(str(user_id_raw))
            except (TypeError, ValueError):
                return f"Ungültige Discord-ID für {login}"

            user = self.bot.get_user(user_id_int)
            if user is None:
                try:
                    user = await self.bot.fetch_user(user_id_int)
                except discord.NotFound:
                    user = None
                except discord.HTTPException:
                    log.exception("Konnte Discord-User %s nicht abrufen", user_id_int)
                    user = None

            if user is None:
                return f"Discord-User {user_id_int} konnte nicht gefunden werden"

            message = (
                "Hey! Deine Deadlock-Streamer-Verifizierung konnte leider nicht abgeschlossen werden. "
                "Du erfüllst aktuell nicht alle Voraussetzungen. Bitte prüfe die Anforderungen erneut "
                "und starte die Verifizierung anschließend mit /streamer noch einmal."
            )

            try:
                await user.send(message)
            except discord.Forbidden:
                log.warning("DM an %s (%s) wegen fehlgeschlagener Verifizierung blockiert", user_id_int, login)
                return (
                    f"Konnte {row_data.get('discord_display_name') or user.name} nicht per DM erreichen."
                )
            except discord.HTTPException:
                log.exception("Konnte Verifizierungsfehler-Nachricht nicht senden an %s", user_id_int)
                return "Nachricht konnte nicht gesendet werden"

            log.info("Verifizierungsfehler-Benachrichtigung an %s (%s) gesendet", user_id_int, login)
            return (
                f"{login}: Discord-User wurde über die fehlgeschlagene Verifizierung informiert"
            )
        return "Unbekannter Modus"

    async def _reload_twitch_cog(self) -> str:
        """Hot reload the entire Twitch cog.

        Verwendet explizites unload → load statt reload_extension(),
        damit bei einem fehlgeschlagenen vorherigen Reload der Cog
        nicht in einem inkonsistenten "already loaded" Zustand bleibt.
        
        Wartet nach dem Unload explizit auf Port-Freigabe, damit
        der neue Cog sauber starten kann.
        """
        try:
            # 1) Sicher unloaden (ignoriere Fehler wenn nicht geladen)
            try:
                await self.bot.unload_extension("cogs.twitch")
                log.info("Twitch cog unloaded for reload")
                
                # Warte explizit darauf, dass alle Ressourcen freigegeben wurden
                # (besonders wichtig: Ports 4343 und 8765)
                log.info("Warte 3 Sekunden auf vollständige Ressourcen-Freigabe...")
                await asyncio.sleep(3.0)
                
            except Exception as unload_err:
                log.warning("Twitch cog unload before reload: %s", unload_err)
                # Auch bei Fehler kurz warten, damit teilweise Cleanups Zeit haben
                await asyncio.sleep(2.0)

            # 2) Neu laden
            await self.bot.load_extension("cogs.twitch")
            log.info("Twitch cog hot reloaded via dashboard")
            return "Twitch-Modul erfolgreich neu geladen"
        except Exception as e:
            log.exception("Twitch cog hot reload failed")
            return f"Fehler beim Neuladen: {e}"

    async def _start_dashboard(self):
        if not getattr(self, "_dashboard_embedded", True):
            log.debug("Twitch dashboard embedded server disabled; skipping _start_dashboard")
            return
        
        # Retry logic for port availability during reloads
        max_retries = 5
        retry_delay = 0.5
        app = None
        runner = None
        
        for attempt in range(max_retries):
            try:
                app = build_v2_app(
                    noauth=self._dashboard_noauth,
                    token=self._dashboard_token,
                    partner_token=self._partner_dashboard_token,
                    oauth_client_id=self.client_id or None,
                    oauth_client_secret=self.client_secret or None,
                    oauth_redirect_uri=getattr(self, "_dashboard_auth_redirect_uri", None),
                    session_ttl_seconds=getattr(self, "_dashboard_session_ttl", 12 * 3600),
                    legacy_stats_url=getattr(self, "_legacy_stats_url", None),
                    add_cb=self._dashboard_add,
                    remove_cb=self._dashboard_remove,
                    list_cb=self._dashboard_list,
                    stats_cb=self._dashboard_stats,
                    verify_cb=self._dashboard_verify,
                    archive_cb=self._dashboard_archive,
                    discord_flag_cb=self._dashboard_set_discord_flag,
                    discord_profile_cb=self._dashboard_save_discord_profile,
                    raid_history_cb=getattr(self, "_dashboard_raid_history", None),
                    raid_bot=getattr(self, "_raid_bot", None),
                    reload_cb=self._reload_twitch_cog,
                    eventsub_webhook_handler=getattr(self, "_eventsub_webhook_handler", None),
                )
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, host=self._dashboard_host, port=self._dashboard_port)
                await site.start()
                self._web = runner
                self._web_app = app
                log.debug("Twitch dashboard running on http://%s:%s/twitch", self._dashboard_host, self._dashboard_port)
                return
            except OSError as e:
                if runner:
                    await runner.cleanup()
                
                # Check for address in use (WinError 10048 on Windows, EADDRINUSE=98 on Linux)
                import errno as _errno
                is_addr_in_use = e.errno in (10048, getattr(_errno, "EADDRINUSE", 98))
                
                if is_addr_in_use and attempt < max_retries - 1:
                    log.debug("Twitch dashboard port %s belegt, versuche es erneut in %ss... (Versuch %s/%s)", 
                              self._dashboard_port, retry_delay, attempt + 1, max_retries)
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                log.exception("Konnte Dashboard nicht starten (Port belegt oder anderer Fehler)")
                break
            except Exception:
                if runner:
                    await runner.cleanup()
                log.exception("Konnte Dashboard nicht starten")
                break

    async def _stop_dashboard(self):
        if self._web:
            await self._web.cleanup()
            self._web = None
            self._web_app = None
