"""Dashboard helpers for the Twitch cog."""

from __future__ import annotations

import os
import sqlite3
import asyncio
from typing import List, Optional

import discord

from aiohttp import web

from . import storage
from .dashboard import Dashboard
from .logger import log


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
                with storage.get_conn() as c:
                    c.execute(
                        """
                        UPDATE twitch_streamers
                           SET is_on_discord=1
                         WHERE is_on_discord=0
                           AND (
                                manual_verified_permanent=1
                             OR manual_verified_until IS NOT NULL
                             OR manual_verified_at IS NOT NULL
                           )
                        """
                    )
                    rows = c.execute(
                        """
                        SELECT twitch_login,
                               manual_verified_permanent,
                               manual_verified_until,
                               manual_verified_at,
                               manual_partner_opt_out,
                               is_on_discord,
                               discord_user_id,
                               discord_display_name
                          FROM twitch_streamers
                         ORDER BY twitch_login
                        """
                    ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 2:
                    raise
                await asyncio.sleep(0.3 * (attempt + 1))
        return []

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

        try:
            with storage.get_conn() as conn:
                row = conn.execute(
                    "SELECT twitch_login FROM twitch_streamers WHERE twitch_login=?",
                    (normalized,),
                ).fetchone()

                if row:
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
                    conn.execute(
                        "INSERT INTO twitch_streamers "
                        "(twitch_login, discord_user_id, discord_display_name, is_on_discord) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            normalized,
                            discord_id_clean or None,
                            display_name_clean or None,
                            1 if mark_member else 0,
                        ),
                    )
        except sqlite3.IntegrityError:
            raise ValueError("Discord-ID wird bereits verwendet")

        return f"Discord-Daten für {normalized} aktualisiert"

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
        return stats

    async def _dashboard_streamer_analytics_data(self, streamer_login: str, days: int = 30) -> dict:
        """Return the full analytics payload used by /twitch/analytics."""
        from datetime import datetime, timedelta
        import math

        def _parse_dt(raw: object) -> Optional[datetime]:
            if not raw:
                return None
            text = str(raw).replace("Z", "+00:00")
            for fmt in (None, "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
                except Exception:
                    continue
            return None

        def _percentile(values: List[float], pct: float) -> Optional[float]:
            if not values:
                return None
            ordered = sorted(values)
            k = (len(ordered) - 1) * pct
            f = math.floor(k)
            c = math.ceil(k)
            if f == c:
                return ordered[int(k)]
            return ordered[f] * (c - k) + ordered[c] * (k - f)

        login = self._normalize_login(streamer_login) if streamer_login else ""
        now = datetime.utcnow()
        cutoff = now - timedelta(days=days)
        prev_cutoff = cutoff - timedelta(days=days)
        cutoff_iso = cutoff.isoformat()
        prev_cutoff_iso = prev_cutoff.isoformat()

        sessions_data: List[dict] = []
        drops: List[dict] = []
        hourly_self: dict[int, List[float]] = {}
        weekday_hour: dict[int, dict[int, List[float]]] = {}

        sum_peak = sum_ret5 = sum_ret10 = sum_ret20 = sum_drop = 0.0
        sum_unique = sum_first = sum_returning = 0
        sum_unique_viewers_est = 0.0
        sum_messages = 0
        sum_avg_viewers = 0.0
        sum_follower_delta = 0
        sum_chat_health_ratio = 0.0
        total_sessions = 0
        total_duration_hours = 0.0
        total_watch_time_hours = 0.0
        categories: dict[str, dict[str, float]] = {}

        session_sql = """
            SELECT id,
                   streamer_login,
                   stream_id,
                   started_at,
                   ended_at,
                   duration_seconds,
                   start_viewers,
                   peak_viewers,
                   end_viewers,
                   avg_viewers,
                   retention_5m,
                   retention_10m,
                   retention_20m,
                   dropoff_pct,
                   dropoff_label,
                   unique_chatters,
                   first_time_chatters,
                   returning_chatters,
                   followers_start,
                   followers_end,
                   follower_delta,
                   stream_title,
                   tags,
                   language,
                   notes,
                   game_name
              FROM twitch_stream_sessions
             WHERE started_at >= ?
        """
        params: List[object] = [cutoff_iso]
        if login:
            session_sql += " AND streamer_login = ?"
            params.append(login)
        session_sql += " ORDER BY started_at DESC"

        with storage.get_conn() as conn:
            rows = conn.execute(session_sql, params).fetchall()

            def _pct(val: Optional[float]) -> float:
                if val is None:
                    return 0.0
                if 0 <= val <= 1:
                    return float(val) * 100.0
                return float(val)

            def _estimate_unique_viewers(avg_viewers: float, unique_chatters: int, peak_viewers: int) -> float:
                # Use chat engagement as a proxy for reach; clamp engagement to avoid runaway values
                chat_per_100 = (unique_chatters * 100.0 / max(avg_viewers, 1)) if avg_viewers > 0 else 0.0
                engagement = min(0.6, max(chat_per_100 / 100.0, 0.02))  # assume 2%..60% active chatters
                est = unique_chatters / engagement if engagement > 0 else float(avg_viewers)
                est = max(est, avg_viewers)
                if peak_viewers:
                    est = min(est, peak_viewers * 4)  # soft upper bound
                return round(est, 1)

            for s in rows:
                started_dt = _parse_dt(s["started_at"])
                started_label = started_dt.strftime("%Y-%m-%d") if started_dt else "-"
                start_time_label = started_dt.strftime("%H:%M") if started_dt else "--:--"
                duration_seconds = int(s["duration_seconds"] or 0)
                duration_hours = duration_seconds / 3600 if duration_seconds > 0 else 0.0
                follower_delta = s["follower_delta"]
                if follower_delta is None:
                    try:
                        follower_delta = (s["followers_end"] or 0) - (s["followers_start"] or 0)
                    except Exception:
                        follower_delta = 0

                ret5 = _pct(s["retention_5m"])
                ret10 = _pct(s["retention_10m"])
                ret20 = _pct(s["retention_20m"])

                # Chat messages per session
                msg_row = conn.execute(
                    "SELECT SUM(messages) AS msg FROM twitch_session_chatters WHERE session_id=?",
                    (s["id"],),
                ).fetchone()
                total_messages = int(msg_row["msg"] or 0) if msg_row else 0
                rpm = total_messages / max(duration_seconds / 60, 1)

                unique_viewers_est = _estimate_unique_viewers(float(s["avg_viewers"] or 0.0), s["unique_chatters"] or 0, s["peak_viewers"] or 0)
                chat_per_viewer = (float(s["unique_chatters"] or 0) * 100.0) / max(float(s["avg_viewers"] or 0.0), 1.0) if (s["avg_viewers"] or 0) else 0.0
                first_rate = ((s["first_time_chatters"] or 0) / max((s["unique_chatters"] or 0), 1)) * 100 if (s["unique_chatters"] or 0) else 0.0
                returning_rate = ((s["returning_chatters"] or 0) / max((s["unique_chatters"] or 0), 1)) * 100 if (s["unique_chatters"] or 0) else 0.0

                session_entry = {
                    "id": s["id"],
                    "streamId": s["stream_id"],
                    "date": started_label,
                    "startTime": start_time_label,
                    "duration": duration_seconds,
                    "startViewers": s["start_viewers"] or 0,
                    "peakViewers": s["peak_viewers"] or 0,
                    "endViewers": s["end_viewers"] or 0,
                    "avgViewers": s["avg_viewers"] or 0,
                    "uniqueViewersEst": unique_viewers_est,
                    "retention5m": round(ret5, 1),
                    "retention10m": round(ret10, 1),
                    "retention20m": round(ret20, 1),
                    "dropoffPct": round((s["dropoff_pct"] or 0) * 100, 1) if (s["dropoff_pct"] and s["dropoff_pct"] <= 1) else round(s["dropoff_pct"] or 0, 1),
                    "dropoffLabel": s["dropoff_label"] or "",
                    "uniqueChatters": s["unique_chatters"] or 0,
                    "firstTimeChatters": s["first_time_chatters"] or 0,
                    "returningChatters": s["returning_chatters"] or 0,
                    "messages": total_messages,
                    "rpm": round(rpm, 2),
                    "followersStart": s["followers_start"] or 0,
                    "followersEnd": s["followers_end"] or 0,
                    "followerDelta": follower_delta or 0,
                    "followersPerHour": round((follower_delta or 0) / max(duration_hours, 0.01), 2) if duration_hours else 0.0,
                    "title": s["stream_title"] or "",
                    "tags": s["tags"] or "",
                    "language": s["language"] or "",
                    "notes": s["notes"] or "",
                    "game": s["game_name"] or "",
                    "weekday": started_dt.weekday() if started_dt else None,
                    "hour": started_dt.hour if started_dt else None,
                    "firstRate": round(first_rate, 1),
                    "returningRate": round(returning_rate, 1),
                    "chatPerViewer": round(chat_per_viewer, 1),
                }

                # derive drop minute if encoded as t=XXm (...)
                drop_label = session_entry["dropoffLabel"]
                drop_minute: Optional[int] = None
                if drop_label.startswith("t="):
                    try:
                        minute_part = drop_label.split("m", 1)[0].replace("t=", "")
                        drop_minute = int(minute_part)
                    except Exception:
                        drop_minute = None
                session_entry["dropMinute"] = drop_minute

                sessions_data.append(session_entry)

                total_sessions += 1
                sum_peak += session_entry["peakViewers"]
                sum_ret5 += session_entry["retention5m"]
                sum_ret10 += session_entry["retention10m"]
                sum_ret20 += session_entry["retention20m"]
                sum_drop += session_entry["dropoffPct"] or 0.0
                sum_unique += session_entry["uniqueChatters"]
                sum_first += session_entry["firstTimeChatters"]
                sum_returning += session_entry["returningChatters"]
                sum_follower_delta += session_entry["followerDelta"]
                sum_avg_viewers += float(session_entry["avgViewers"] or 0.0)
                sum_unique_viewers_est += float(unique_viewers_est)
                sum_messages += total_messages
                total_duration_hours += duration_hours
                total_watch_time_hours += (float(session_entry["avgViewers"] or 0.0) * duration_hours)

                avg_viewers = float(session_entry["avgViewers"] or 0.0)
                chat_ratio = (session_entry["uniqueChatters"] / max(avg_viewers, 1)) * 100 if avg_viewers > 0 else 0.0
                sum_chat_health_ratio += chat_ratio

                # drops
                drops.append(
                    {
                        "streamer": session_entry.get("streamId") or login or session_entry.get("streamer", ""),
                        "start": started_label,
                        "dropPct": session_entry["dropoffPct"],
                        "dropLabel": session_entry["dropoffLabel"],
                        "minute": drop_minute,
                        "title": session_entry["title"],
                    }
                )

                if started_dt:
                    hour = started_dt.hour
                    hourly_self.setdefault(hour, []).append(avg_viewers)
                    weekday = started_dt.weekday()
                    weekday_hour.setdefault(weekday, {}).setdefault(hour, []).append(avg_viewers)

                # categories/games
                game_name = (session_entry.get("game") or "").strip() or "Unbekannt"
                cat = categories.setdefault(game_name, {"sessions": 0, "avg_sum": 0.0, "peak_sum": 0.0, "followers_sum": 0.0, "unique_sum": 0, "duration_h": 0.0})
                cat["sessions"] += 1
                cat["avg_sum"] += float(session_entry["avgViewers"] or 0.0)
                cat["peak_sum"] += float(session_entry["peakViewers"] or 0.0)
                cat["followers_sum"] += float(session_entry["followerDelta"] or 0.0)
                cat["unique_sum"] += int(session_entry["uniqueChatters"] or 0)
                cat["duration_h"] += duration_hours

            # Chatters rollup for returning viewers
            returning_7d = 0
            returning_30d = 0
            base_rollup_sql = """
                SELECT COUNT(DISTINCT COALESCE(chatter_id, chatter_login)) as cnt
                  FROM twitch_chatter_rollup
                 WHERE last_seen_at >= ?
                   AND first_seen_at < ?
            """
            rollup_params = [ (now - timedelta(days=7)).isoformat(), (now - timedelta(days=7)).isoformat() ]
            if login:
                base_rollup_sql += " AND streamer_login = ?"
                rollup_params.append(login)
            row_7d = conn.execute(base_rollup_sql, rollup_params).fetchone()
            returning_7d = int(row_7d["cnt"] or 0) if row_7d else 0

            rollup_params_30 = [cutoff_iso, cutoff_iso]
            rollup_sql_30 = """
                SELECT COUNT(DISTINCT COALESCE(chatter_id, chatter_login)) as cnt
                  FROM twitch_chatter_rollup
                 WHERE last_seen_at >= ?
                   AND first_seen_at < ?
            """
            if login:
                rollup_sql_30 += " AND streamer_login = ?"
                rollup_params_30.append(login)
            row_30d = conn.execute(rollup_sql_30, rollup_params_30).fetchone()
            returning_30d = int(row_30d["cnt"] or 0) if row_30d else 0

            # total unique chatters in window
            unique_30d_sql = """
                SELECT COUNT(DISTINCT chatter_login) as cnt
                  FROM twitch_session_chatters
                 WHERE first_message_at >= ?
            """
            unique_params = [cutoff_iso]
            if login:
                unique_30d_sql += " AND streamer_login = ?"
                unique_params.append(login)
            uniq_row = conn.execute(unique_30d_sql, unique_params).fetchone()
            total_unique_chatters_30d = int(uniq_row["cnt"] or 0) if uniq_row else 0

            unique_7d_sql = """
                SELECT COUNT(DISTINCT chatter_login) as cnt
                  FROM twitch_session_chatters
                 WHERE first_message_at >= ?
            """
            unique_7d_params = [(now - timedelta(days=7)).isoformat()]
            if login:
                unique_7d_sql += " AND streamer_login = ?"
                unique_7d_params.append(login)
            uniq_7d_row = conn.execute(unique_7d_sql, unique_7d_params).fetchone()
            total_unique_chatters_7d = int(uniq_7d_row["cnt"] or 0) if uniq_7d_row else 0

            # previous-period unique chatters for trend
            prev_unique_sql = """
                SELECT COUNT(DISTINCT chatter_login) as cnt
                  FROM twitch_session_chatters
                 WHERE first_message_at >= ?
                   AND first_message_at < ?
            """
            prev_params = [prev_cutoff_iso, cutoff_iso]
            if login:
                prev_unique_sql += " AND streamer_login = ?"
                prev_params.append(login)
            prev_row = conn.execute(prev_unique_sql, prev_params).fetchone()
            prev_unique_chatters = int(prev_row["cnt"] or 0) if prev_row else 0

            # hourly category baseline
            hourly_cat: dict[int, List[float]] = {}
            cat_rows = conn.execute(
                """
                SELECT CAST(strftime('%H', ts_utc) AS INTEGER) as hour,
                       AVG(viewer_count) as avg_viewers,
                       COUNT(*) as samples
                  FROM twitch_stats_category
                 WHERE ts_utc >= ?
                 GROUP BY hour
                """,
                (cutoff_iso,),
            ).fetchall()
            for r in cat_rows:
                hour = int(r["hour"] or 0)
                hourly_cat.setdefault(hour, []).append(float(r["avg_viewers"] or 0.0))

            # tracked partners baseline
            hourly_tracked: dict[int, List[float]] = {}
            tracked_rows = conn.execute(
                """
                SELECT CAST(strftime('%H', ts_utc) AS INTEGER) as hour,
                       AVG(viewer_count) as avg_viewers,
                       COUNT(*) as samples
                  FROM twitch_stats_tracked
                 WHERE ts_utc >= ?
                 GROUP BY hour
                """,
                (cutoff_iso,),
            ).fetchall()
            for r in tracked_rows:
                hour = int(r["hour"] or 0)
                hourly_tracked.setdefault(hour, []).append(float(r["avg_viewers"] or 0.0))

            # benchmark tables (top 10)
            def _load_top(table: str) -> List[dict]:
                sql = f"""
                    WITH agg AS (
                        SELECT streamer,
                               COUNT(*) as samples,
                               AVG(viewer_count) as avg_viewers,
                               MAX(viewer_count) as peak_viewers,
                               MAX(is_partner) as is_partner
                          FROM {table}
                         WHERE ts_utc >= ?
                         GROUP BY streamer
                    )
                    SELECT agg.streamer,
                           agg.samples,
                           agg.avg_viewers,
                           agg.peak_viewers,
                           agg.is_partner,
                           COALESCE(ts.is_on_discord, 0) AS is_on_discord,
                           ts.discord_display_name,
                           ts.discord_user_id
                      FROM agg
                      LEFT JOIN twitch_streamers ts ON LOWER(ts.twitch_login) = LOWER(agg.streamer)
                     WHERE agg.samples >= 5
                     ORDER BY agg.avg_viewers DESC
                     LIMIT 10
                """
                bench_rows = conn.execute(sql, (cutoff_iso,)).fetchall()
                return [
                    {
                        "streamer": r["streamer"],
                        "samples": r["samples"],
                        "avgViewers": round(r["avg_viewers"] or 0, 1),
                        "peakViewers": r["peak_viewers"] or 0,
                        "isPartner": bool(r["is_partner"]),
                        "onDiscord": bool(r["is_on_discord"]),
                        "discordDisplay": r["discord_display_name"] or "",
                        "discordId": r["discord_user_id"] or "",
                    }
                    for r in bench_rows
                ]

            top_tracked = _load_top("twitch_stats_tracked")
            top_category = _load_top("twitch_stats_category")

            # quantiles for viewer baseline (category)
        cat_all_rows = conn.execute(
            "SELECT viewer_count FROM twitch_stats_category WHERE ts_utc >= ?",
            (cutoff_iso,),
        ).fetchall()
        cat_values = [float(r["viewer_count"] or 0.0) for r in cat_all_rows if r["viewer_count"] is not None]
        q25 = _percentile(cat_values, 0.25)
        q50 = _percentile(cat_values, 0.50)
        q75 = _percentile(cat_values, 0.75)

        def _compute_prev_window(window_start: str, window_end: str) -> dict:
            """Aggregate a comparison window for deltas (previous period)."""
            prev_sql = """
                SELECT id,
                       started_at,
                       duration_seconds,
                       peak_viewers,
                       avg_viewers,
                       retention_5m,
                       retention_10m,
                       retention_20m,
                       dropoff_pct,
                       unique_chatters,
                       first_time_chatters,
                       returning_chatters,
                       follower_delta,
                       followers_start,
                       followers_end
                  FROM twitch_stream_sessions
                 WHERE started_at >= ?
                   AND started_at < ?
            """
            prev_params: List[object] = [window_start, window_end]
            if login:
                prev_sql += " AND streamer_login = ?"
                prev_params.append(login)
            prev_rows = conn.execute(prev_sql, prev_params).fetchall()
            if not prev_rows:
                return {}

            prev_sum_avg_viewers = prev_sum_ret10 = prev_sum_unique = prev_sum_first = prev_sum_returning = 0.0
            prev_sum_peak = prev_sum_watch_time_h = prev_sum_unique_est = 0.0
            prev_sum_follower_delta = 0.0
            prev_total_duration_h = 0.0
            prev_sessions = 0
            for r in prev_rows:
                prev_sessions += 1
                duration_seconds = int(r["duration_seconds"] or 0)
                duration_hours = duration_seconds / 3600 if duration_seconds > 0 else 0.0
                follower_delta = r["follower_delta"]
                if follower_delta is None:
                    try:
                        follower_delta = (r["followers_end"] or 0) - (r["followers_start"] or 0)
                    except Exception:
                        follower_delta = 0

                ret10_prev = _pct(r["retention_10m"])
                avg_viewers_prev = float(r["avg_viewers"] or 0.0)
                unique_chat_prev = int(r["unique_chatters"] or 0)
                first_prev = int(r["first_time_chatters"] or 0)
                returning_prev = int(r["returning_chatters"] or 0)

                unique_est_prev = _estimate_unique_viewers(
                    avg_viewers_prev,
                    unique_chat_prev,
                    int(r["peak_viewers"] or 0),
                )

                prev_sum_avg_viewers += avg_viewers_prev
                prev_sum_ret10 += ret10_prev
                prev_sum_unique += unique_chat_prev
                prev_sum_first += first_prev
                prev_sum_returning += returning_prev
                prev_sum_peak += float(r["peak_viewers"] or 0.0)
                prev_sum_unique_est += float(unique_est_prev)
                prev_sum_watch_time_h += avg_viewers_prev * duration_hours
                prev_sum_follower_delta += float(follower_delta or 0.0)
                prev_total_duration_h += duration_hours

            avg_viewers_prev = prev_sum_avg_viewers / prev_sessions if prev_sessions else 0.0
            avg_ret10_prev = prev_sum_ret10 / prev_sessions if prev_sessions else 0.0
            unique_per_100_prev = (prev_sum_unique / max(prev_sum_avg_viewers, 1)) * 100 if prev_sum_avg_viewers else 0.0
            followers_per_hour_prev = (prev_sum_follower_delta / prev_total_duration_h) if prev_total_duration_h else 0.0
            avg_duration_min_prev = (prev_total_duration_h * 60 / prev_sessions) if prev_sessions else 0.0
            watch_time_per_viewer_min_prev = (prev_sum_watch_time_h * 60) / max(prev_sum_unique_est, 1) if prev_sum_unique_est else 0.0

            return {
                "sessions": prev_sessions,
                "avg_avg_viewers": avg_viewers_prev,
                "avg_ret10": avg_ret10_prev,
                "unique_per_100": unique_per_100_prev,
                "followers_per_hour": followers_per_hour_prev,
                "avg_duration_min": avg_duration_min_prev,
                "watch_time_per_viewer_min": watch_time_per_viewer_min_prev,
            }

        prev_window = _compute_prev_window(prev_cutoff_iso, cutoff_iso)

        avg_ret5 = sum_ret5 / total_sessions if total_sessions else 0.0
        avg_ret10 = sum_ret10 / total_sessions if total_sessions else 0.0
        avg_ret20 = sum_ret20 / total_sessions if total_sessions else 0.0
        avg_drop = sum_drop / total_sessions if total_sessions else 0.0
        avg_peak = sum_peak / total_sessions if total_sessions else 0.0
        avg_avg_viewers = sum_avg_viewers / total_sessions if total_sessions else 0.0

        followers_per_session = (sum_follower_delta / total_sessions) if total_sessions else 0.0
        followers_per_hour = (sum_follower_delta / total_duration_hours) if total_duration_hours else 0.0
        unique_per_100 = (sum_unique / max(sum_avg_viewers, 1)) * 100 if sum_avg_viewers else 0.0
        first_share = (sum_first / max(sum_unique, 1)) if sum_unique else 0.0
        returning_share = (sum_returning / max(sum_unique, 1)) if sum_unique else 0.0
        unique_est_avg = (sum_unique_viewers_est / total_sessions) if total_sessions else 0.0
        avg_to_unique_ratio = (avg_avg_viewers / max(unique_est_avg, 1)) if unique_est_avg else 0.0
        watch_time_hours = total_watch_time_hours
        avg_watch_time_per_viewer_min = (watch_time_hours * 60) / max(sum_unique_viewers_est, 1) if sum_unique_viewers_est else 0.0
        returning_rate_7d = (returning_7d / max(total_unique_chatters_7d, 1)) * 100 if total_unique_chatters_7d else 0.0
        returning_rate_30d = (returning_30d / max(total_unique_chatters_30d, 1)) * 100 if total_unique_chatters_30d else 0.0

        # chat peaks detection
        max_unique = max((s["uniqueChatters"] for s in sessions_data), default=0)
        avg_unique = (sum_unique / total_sessions) if total_sessions else 0
        chat_peaks = bool(max_unique and max_unique >= (avg_unique * 1.2 + 5))

        # chat health score (0-100)
        def _score(val: float) -> float:
            return max(0.0, min(100.0, val))

        unique_norm = _score(unique_per_100)
        first_norm = _score(first_share * 100)
        returning_norm = _score(returning_share * 100)
        peaks_norm = 100.0 if chat_peaks else 0.0
        trend_pct = 0.0
        if prev_unique_chatters > 0:
            trend_pct = ((total_unique_chatters_30d - prev_unique_chatters) / prev_unique_chatters) * 100
        trend_norm = _score(50 + trend_pct * 0.5)
        chat_health_score = round(
            0.4 * unique_norm + 0.2 * first_norm + 0.2 * returning_norm + 0.1 * peaks_norm + 0.1 * trend_norm,
            1,
        )
        messages_per_min = sum_messages / max(total_duration_hours * 60, 1) if total_duration_hours else 0.0
        messages_per_viewer = sum_messages / max(sum_unique_viewers_est, 1) if sum_unique_viewers_est else 0.0

        hourly_self_avg = {h: round(sum(vals) / len(vals), 1) for h, vals in hourly_self.items()}
        hourly_cat_avg = {h: round(sum(vals) / len(vals), 1) for h, vals in hourly_cat.items()}
        hourly_tracked_avg = {h: round(sum(vals) / len(vals), 1) for h, vals in hourly_tracked.items()}

        heatmap = {}
        for weekday, hours in weekday_hour.items():
            heatmap[weekday] = {h: round(sum(vals) / len(vals), 1) for h, vals in hours.items()}

        # best slots (weekday/hour) from heatmap
        slots_flat: List[tuple[int, int, float]] = []
        for w, hours in heatmap.items():
            for h, val in hours.items():
                slots_flat.append((w, h, val))
        top_slots = sorted(slots_flat, key=lambda t: t[2], reverse=True)[:3]

        category_list = []
        for name, stats in categories.items():
            sessions_count = stats["sessions"] or 1
            avg_v = stats["avg_sum"] / sessions_count
            peak_v = stats["peak_sum"] / sessions_count
            followers_h = (stats["followers_sum"] / max(stats["duration_h"], 0.01)) if stats["duration_h"] else 0.0
            category_list.append(
                {
                    "name": name,
                    "sessions": sessions_count,
                    "avgViewers": round(avg_v, 1),
                    "peakViewers": round(peak_v, 1),
                    "followersPerHour": round(followers_h, 2),
                    "chatPerSession": round((stats["unique_sum"] / sessions_count) if sessions_count else 0.0, 1),
                }
            )
        category_list = sorted(category_list, key=lambda c: c["avgViewers"], reverse=True)[:6]

        drops_sorted = sorted(
            [d for d in drops if d.get("dropPct") is not None],
            key=lambda d: d.get("dropPct", 0),
            reverse=True,
        )[:12]

        summary = {
            "totalSessions": total_sessions,
            "avgPeakViewers": round(avg_peak, 1),
            "followersDelta": sum_follower_delta,
            "retention10m": round(avg_ret10, 1),
            "uniqueChatPer100": round(unique_per_100, 1),
            "chatHealthScore": chat_health_score,
            "returning30d": returning_30d,
            "uniqueViewersEst": round(sum_unique_viewers_est, 0),
            "avgWatchTimeHours": round(watch_time_hours, 2),
        }

        retention_block = {
            "avg5m": round(avg_ret5, 1),
            "avg10m": round(avg_ret10, 1),
            "avg20m": round(avg_ret20, 1),
            "avgDropoff": round(avg_drop, 1),
            "trend": [
                {
                    "label": s["date"],
                    "r5": s["retention5m"],
                    "r10": s["retention10m"],
                    "r20": s["retention20m"],
                }
                for s in sessions_data[:12]
            ],
            "drops": drops_sorted,
        }

        discovery_block = {
            "avgPeak": round(avg_peak, 1),
            "followersDelta": sum_follower_delta,
            "followersPerSession": round(followers_per_session, 1),
            "followersPerHour": round(followers_per_hour, 2),
            "returning7d": returning_7d,
            "returning30d": returning_30d,
            "returningRate7d": round(returning_rate_7d, 1),
            "returningRate30d": round(returning_rate_30d, 1),
        }

        chat_block = {
            "uniquePer100": round(unique_per_100, 1),
            "firstShare": round(first_share * 100, 1),
            "returningShare": round(returning_share * 100, 1),
            "totalUnique30d": total_unique_chatters_30d,
            "chatHealthScore": chat_health_score,
            "chatPeaks": chat_peaks,
            "messagesPerMin": round(messages_per_min, 2),
            "messagesPerViewer": round(messages_per_viewer, 2),
        }

        compare_block = {}
        if prev_window:
            compare_block = {
                "prev": {
                    "avgViewers": round(prev_window.get("avg_avg_viewers", 0.0), 1),
                    "retention10m": round(prev_window.get("avg_ret10", 0.0), 1),
                    "chatPer100": round(prev_window.get("unique_per_100", 0.0), 1),
                    "followersPerHour": round(prev_window.get("followers_per_hour", 0.0), 2),
                    "avgDurationMin": round(prev_window.get("avg_duration_min", 0.0), 1),
                    "watchTimePerViewerMin": round(prev_window.get("watch_time_per_viewer_min", 0.0), 1),
                },
                "days": days,
            }

        audience_block = {
            "uniqueEstimateTotal": round(sum_unique_viewers_est, 0),
            "uniqueEstimateAvg": round(unique_est_avg, 1),
            "avgToUniqueRatio": round(avg_to_unique_ratio * 100, 1),
            "watchTimeHours": round(watch_time_hours, 2),
            "watchTimePerViewerMin": round(avg_watch_time_per_viewer_min, 1),
            "unique7d": total_unique_chatters_7d,
            "unique30d": total_unique_chatters_30d,
            "returningRate7d": round(returning_rate_7d, 1),
            "returningRate30d": round(returning_rate_30d, 1),
        }

        benchmark = {
            "topTracked": top_tracked,
            "topCategory": top_category,
            "categoryQuantiles": {
                "q25": round(q25, 1) if q25 is not None else None,
                "q50": round(q50, 1) if q50 is not None else None,
                "q75": round(q75, 1) if q75 is not None else None,
            },
        }

        analysis = {
            "hourlySelf": hourly_self_avg,
            "hourlyCategory": hourly_cat_avg,
            "hourlyTracked": hourly_tracked_avg,
            "heatmap": heatmap,
        }

        timing_block = {
            "topSlots": [{"weekday": w, "hour": h, "avgViewers": val} for w, h, val in top_slots]
        }
        categories_block = {"top": category_list}

        return {
            "timeframe": {"days": days, "from": cutoff_iso},
            "summary": summary,
            "retention": retention_block,
            "discovery": discovery_block,
            "chat": chat_block,
            "audience": audience_block,
            "benchmark": benchmark,
            "analysis": analysis,
            "timing": timing_block,
            "categories": categories_block,
            "compare": compare_block,
            "sessions": sessions_data,
        }

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
                        "    manual_partner_opt_out=0, "
                        "    is_on_discord=1 "
                        "WHERE twitch_login=?",
                        (login,),
                    )
                    base_msg = f"{login} dauerhaft verifiziert"
                else:
                    c.execute(
                        "UPDATE twitch_streamers "
                        "SET manual_verified_permanent=0, manual_verified_until=datetime('now','+30 days'), "
                        "    manual_verified_at=datetime('now'), manual_partner_opt_out=0, is_on_discord=1 "
                        "WHERE twitch_login=?",
                        (login,),
                    )
                    base_msg = f"{login} für 30 Tage verifiziert"

            notes: List[str] = []
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
                    "    manual_partner_opt_out=1 "
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
                        "    manual_partner_opt_out=0 "
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
        """Hot reload the entire Twitch cog."""
        try:
            await self.bot.reload_extension("cogs.twitch")
            log.info("Twitch cog hot reloaded via dashboard")
            return "Twitch-Modul erfolgreich neu geladen"
        except Exception as e:
            log.exception("Twitch cog hot reload failed")
            return f"Fehler beim Neuladen: {e}"

    async def _start_dashboard(self):
        if not getattr(self, "_dashboard_embedded", True):
            log.debug("Twitch dashboard embedded server disabled; skipping _start_dashboard")
            return
        try:
            app = Dashboard.build_app(
                noauth=self._dashboard_noauth,
                token=self._dashboard_token,
                partner_token=self._partner_dashboard_token,
                add_cb=self._dashboard_add,
                remove_cb=self._dashboard_remove,
                list_cb=self._dashboard_list,
                stats_cb=self._dashboard_stats,
                verify_cb=self._dashboard_verify,
                discord_flag_cb=self._dashboard_set_discord_flag,
                discord_profile_cb=self._dashboard_save_discord_profile,
                raid_history_cb=self._dashboard_raid_history if hasattr(self, "_dashboard_raid_history") else None,
                streamer_overview_cb=self._dashboard_streamer_overview,
                session_detail_cb=self._dashboard_session_detail,
                comparison_stats_cb=self._dashboard_comparison_stats,
                streamer_analytics_data_cb=self._dashboard_streamer_analytics_data,
                raid_bot=getattr(self, "_raid_bot", None),
                reload_cb=self._reload_twitch_cog,
                http_session=self.api.get_http_session() if self.api else None,
                redirect_uri=getattr(self, "_raid_redirect_uri", ""),
            )
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host=self._dashboard_host, port=self._dashboard_port)
            await site.start()
            self._web = runner
            self._web_app = app
            log.info("Twitch dashboard running on http://%s:%s/twitch", self._dashboard_host, self._dashboard_port)
        except Exception:
            log.exception("Konnte Dashboard nicht starten")

    async def _stop_dashboard(self):
        if self._web:
            await self._web.cleanup()
            self._web = None
            self._web_app = None
