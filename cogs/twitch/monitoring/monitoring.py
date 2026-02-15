"""Background polling and monitoring helpers for Twitch streams."""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone, timedelta
import os
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp
import discord
from discord.ext import tasks

from .. import storage
from ..constants import (
    INVITES_REFRESH_INTERVAL_HOURS,
    POLL_INTERVAL_SECONDS,
    TWITCH_BRAND_COLOR_HEX,
    TWITCH_BUTTON_LABEL,
    TWITCH_DISCORD_REF_CODE,
    TWITCH_VOD_BUTTON_LABEL,
    TWITCH_TARGET_GAME_NAME,
)
from ..constants import log


class TwitchMonitoringMixin:
    """Polling loops and helpers used by the Twitch cog."""

    @staticmethod
    def _reauth_chat_reminder_text() -> str:
        return (
            "Kurze Erinnerung: Für den Raid-/Stats-Bot fehlt noch die neue Twitch-Autorisierung. "
            "Du hast dazu bereits eine Discord-DM mit dem Re-Auth-Link erhalten. Danke dir!"
        )

    async def _resolve_live_stream_id_for_login(self, login_lower: str) -> Optional[str]:
        if not login_lower or not getattr(self, "api", None):
            return None
        try:
            streams = await self.api.get_streams_by_logins([login_lower])
            if not streams:
                return None
            stream_id = str((streams[0] or {}).get("id") or "").strip()
            return stream_id or None
        except Exception:
            log.debug(
                "ReAuth reminder: Konnte aktuelle stream_id nicht laden für %s",
                login_lower,
                exc_info=True,
            )
            return None

    async def _maybe_send_reauth_chat_reminder(
        self,
        *,
        chat_bot,
        broadcaster_id: str,
        login_lower: str,
    ) -> bool:
        """Sendet beim Streamstart einmalig eine freundliche Re-Auth-Erinnerung in den Twitch-Chat."""
        if not chat_bot or not broadcaster_id or not login_lower:
            return False

        broadcaster_key = str(broadcaster_id).strip()
        login_key = str(login_lower).strip().lower()
        if not broadcaster_key or not login_key:
            return False

        # Primärer Dedupe über stream_id (pro Streamstart genau eine Nachricht).
        stream_id = await self._resolve_live_stream_id_for_login(login_key)
        stream_guard = getattr(self, "_reauth_reminder_last_stream_id", None)
        if not isinstance(stream_guard, dict):
            stream_guard = {}
            setattr(self, "_reauth_reminder_last_stream_id", stream_guard)
        if stream_id:
            if stream_guard.get(broadcaster_key) == stream_id:
                return False
        else:
            # Fallback-Dedupe, falls stream_id temporär nicht geladen werden kann.
            fallback_guard = getattr(self, "_reauth_reminder_last_sent_ts", None)
            if not isinstance(fallback_guard, dict):
                fallback_guard = {}
                setattr(self, "_reauth_reminder_last_sent_ts", fallback_guard)
            now_ts = time.time()
            last_ts = float(fallback_guard.get(broadcaster_key) or 0.0)
            if now_ts - last_ts < 300.0:
                return False
            fallback_guard[broadcaster_key] = now_ts

        send_chat = getattr(chat_bot, "_send_chat_message", None)
        if not callable(send_chat):
            return False

        make_channel = getattr(chat_bot, "_make_promo_channel", None)
        if callable(make_channel):
            channel = make_channel(login_key, broadcaster_key)
        else:
            class _Channel:
                __slots__ = ("name", "id")

                def __init__(self, name: str, cid: str):
                    self.name = name
                    self.id = cid

            channel = _Channel(login_key, broadcaster_key)

        ok = await send_chat(
            channel,
            self._reauth_chat_reminder_text(),
            source="migration_reminder",
        )
        if ok:
            if stream_id:
                stream_guard[broadcaster_key] = stream_id
            log.info(
                "ReAuth reminder: Chat-Hinweis bei Streamstart gesendet für %s (%s)",
                login_key,
                broadcaster_key,
            )
        return bool(ok)

    def _get_target_game_lower(self) -> str:
        target = getattr(self, "_target_game_lower", None)
        if isinstance(target, str) and target:
            return target
        resolved = (TWITCH_TARGET_GAME_NAME or "").strip().lower()
        # Cache for subsequent lookups to avoid repeated normalization
        setattr(self, "_target_game_lower", resolved)
        return resolved

    def _stream_is_in_target_category(self, stream: Optional[dict]) -> bool:
        if not stream:
            return False
        target_game_lower = self._get_target_game_lower()
        if not target_game_lower:
            return False
        game_name = (stream.get("game_name") or "").strip().lower()
        return game_name == target_game_lower

    @staticmethod
    def _normalize_stream_meta(stream: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
        game_name = (stream.get("game_name") or "").strip() or None
        stream_title = (stream.get("title") or "").strip() or None

        tags_raw = stream.get("tags")
        tags_serialized: Optional[str] = None
        if isinstance(tags_raw, list):
            clean_tags = [str(tag).strip() for tag in tags_raw if str(tag).strip()]
            if clean_tags:
                tags_serialized = json.dumps(clean_tags, ensure_ascii=True, separators=(",", ":"))
        elif isinstance(tags_raw, str):
            tag_value = tags_raw.strip()
            if tag_value:
                tags_serialized = tag_value

        return game_name, stream_title, tags_serialized

    def _language_filter_values(self) -> List[Optional[str]]:
        filters: Optional[List[str]] = getattr(self, "_language_filters", None)
        if not filters:
            return [None]
        seen: List[str] = []
        for entry in filters:
            normalized = (entry or "").strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.append(normalized)
        return [*seen] or [None]

    def _eventsub_capacity_sample_interval_seconds(self) -> int:
        raw = (os.getenv("TWITCH_EVENTSUB_CAPACITY_SAMPLE_SECONDS") or "").strip()
        default_value = 300
        if not raw:
            return default_value
        try:
            value = int(raw)
        except ValueError:
            return default_value
        return max(30, min(3600, value))

    def _eventsub_capacity_retention_days(self) -> int:
        raw = (os.getenv("TWITCH_EVENTSUB_CAPACITY_RETENTION_DAYS") or "").strip()
        default_value = 45
        if not raw:
            return default_value
        try:
            value = int(raw)
        except ValueError:
            return default_value
        return max(7, min(365, value))

    @staticmethod
    def _eventsub_target_user_id(condition: Optional[Dict[str, Any]], *, fallback: str = "") -> str:
        condition_map = condition if isinstance(condition, dict) else {}
        for key in (
            "broadcaster_user_id",
            "to_broadcaster_user_id",
            "from_broadcaster_user_id",
            "user_id",
        ):
            value = str(condition_map.get(key) or "").strip()
            if value:
                return value
        return str(fallback or "").strip()

    def _resolve_twitch_logins_by_user_id(self, user_ids: List[str]) -> Dict[str, str]:
        unique_ids: List[str] = []
        seen: set[str] = set()
        for raw in user_ids:
            value = str(raw or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            unique_ids.append(value)

        if not unique_ids:
            return {}

        placeholders = ",".join("?" for _ in unique_ids)
        query = (
            "SELECT twitch_user_id, twitch_login "
            "FROM twitch_streamers "
            f"WHERE twitch_user_id IN ({placeholders}) "
            "AND twitch_login IS NOT NULL"
        )
        try:
            with storage.get_conn() as c:
                rows = c.execute(query, tuple(unique_ids)).fetchall()
            out: Dict[str, str] = {}
            for row in rows:
                uid = str(row["twitch_user_id"] if hasattr(row, "keys") else row[0]).strip()
                login = str(row["twitch_login"] if hasattr(row, "keys") else row[1]).strip().lower()
                if uid and login:
                    out[uid] = login
            return out
        except Exception:
            log.debug("EventSub: konnte twitch_login Mapping nicht laden", exc_info=True)
            return {}

    def _collect_eventsub_capacity_snapshot(self, *, reason: str) -> Dict[str, Any]:
        # Webhook-basiert: Subscription-Liste aus lokalem Tracking
        tracked_subs: List[Dict[str, Any]] = list(
            getattr(self, "_eventsub_webhook_active_subs", []) or []
        )
        active_subscriptions: List[Dict[str, Any]] = []
        sub_type_counts: Dict[str, int] = {}

        for sub in tracked_subs:
            sub_type = str(sub.get("sub_type") or "").strip().lower() or "unknown"
            broadcaster_user_id = str(sub.get("broadcaster_user_id") or "").strip()
            active_subscriptions.append(
                {
                    "listener_idx": 1,  # Webhook hat keinen Listener-Pool
                    "sub_type": sub_type,
                    "broadcaster_user_id": broadcaster_user_id,
                    "target_user_id": broadcaster_user_id,
                    "condition": {"broadcaster_user_id": broadcaster_user_id},
                }
            )
            sub_type_counts[sub_type] = int(sub_type_counts.get(sub_type, 0)) + 1

        used_slots = len(active_subscriptions)
        # Webhook skaliert auf 10.000 Subscriptions – repräsentiere das in der Snapshot-Struktur
        total_slots = 10000
        headroom_slots = max(0, total_slots - used_slots)
        utilization_pct = (float(used_slots) / float(total_slots) * 100.0) if total_slots > 0 else 0.0

        # listener_rows für Kompatibilität mit bestehenden Dashboard-Feldern
        listener_rows: List[Dict[str, Any]] = [{
            "idx": 1,
            "ready": 1,
            "failed": 0,
            "subscriptions": used_slots,
            "free_slots": headroom_slots,
        }]
        listener_count = 1
        ready_count = 1
        failed_count = 0
        listeners_at_limit = 0

        login_map = self._resolve_twitch_logins_by_user_id(
            [str(row.get("target_user_id") or "") for row in active_subscriptions]
        )

        for row in active_subscriptions:
            target_user_id = str(row.get("target_user_id") or "").strip()
            target_login = login_map.get(target_user_id)
            if not target_login:
                condition = row.get("condition") if isinstance(row.get("condition"), dict) else {}
                target_login = (
                    str(condition.get("broadcaster_user_login") or "").strip().lower()
                    or str(condition.get("to_broadcaster_user_login") or "").strip().lower()
                    or None
                )
            row["target_login"] = target_login

        channel_map: Dict[str, Dict[str, Any]] = {}
        for row in active_subscriptions:
            target_user_id = str(row.get("target_user_id") or "").strip()
            if not target_user_id:
                continue
            sub_type = str(row.get("sub_type") or "").strip().lower() or "unknown"
            target_login = str(row.get("target_login") or "").strip().lower() or None
            channel_entry = channel_map.setdefault(
                target_user_id,
                {
                    "twitch_user_id": target_user_id,
                    "twitch_login": target_login,
                    "subscription_count": 0,
                    "sub_types": set(),
                },
            )
            channel_entry["subscription_count"] = int(channel_entry.get("subscription_count") or 0) + 1
            if target_login and not channel_entry.get("twitch_login"):
                channel_entry["twitch_login"] = target_login
            channel_entry["sub_types"].add(sub_type)

        subscription_channels = sorted(
            [
                {
                    "twitch_user_id": str(entry.get("twitch_user_id") or ""),
                    "twitch_login": (str(entry.get("twitch_login") or "").strip().lower() or None),
                    "subscription_count": int(entry.get("subscription_count") or 0),
                    "sub_types": sorted(str(sub_type) for sub_type in entry.get("sub_types", set())),
                }
                for entry in channel_map.values()
            ],
            key=lambda entry: (
                -int(entry.get("subscription_count") or 0),
                str(entry.get("twitch_login") or ""),
                str(entry.get("twitch_user_id") or ""),
            ),
        )

        subscription_types = [
            {"sub_type": sub_type, "count": int(count)}
            for sub_type, count in sorted(
                sub_type_counts.items(),
                key=lambda item: (-int(item[1] or 0), str(item[0])),
            )
        ]

        return {
            "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": (reason or "unknown").strip()[:64],
            "listener_count": listener_count,
            "ready_listeners": ready_count,
            "failed_listeners": failed_count,
            "used_slots": used_slots,
            "total_slots": total_slots,
            "headroom_slots": headroom_slots,
            "listeners_at_limit": listeners_at_limit,
            "utilization_pct": round(utilization_pct, 2),
            "listeners": listener_rows,
            "subscription_count": len(active_subscriptions),
            "subscriptions": active_subscriptions,
            "subscription_types": subscription_types,
            "subscription_channels": subscription_channels,
        }

    async def _record_eventsub_capacity_snapshot(self, reason: str, *, force: bool = False) -> None:
        now_monotonic = time.monotonic()
        interval = self._eventsub_capacity_sample_interval_seconds()
        last_snapshot = float(getattr(self, "_eventsub_capacity_last_snapshot", 0.0) or 0.0)
        if not force and last_snapshot and (now_monotonic - last_snapshot) < interval:
            return

        snapshot = self._collect_eventsub_capacity_snapshot(reason=reason)
        listeners_json = json.dumps(
            snapshot.get("listeners", []),
            ensure_ascii=True,
            separators=(",", ":"),
        )

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_eventsub_capacity_snapshot (
                        ts_utc, trigger_reason, listener_count, ready_listeners, failed_listeners,
                        used_slots, total_slots, headroom_slots, listeners_at_limit, utilization_pct, listeners_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.get("ts_utc"),
                        snapshot.get("reason"),
                        int(snapshot.get("listener_count") or 0),
                        int(snapshot.get("ready_listeners") or 0),
                        int(snapshot.get("failed_listeners") or 0),
                        int(snapshot.get("used_slots") or 0),
                        int(snapshot.get("total_slots") or 0),
                        int(snapshot.get("headroom_slots") or 0),
                        int(snapshot.get("listeners_at_limit") or 0),
                        float(snapshot.get("utilization_pct") or 0.0),
                        listeners_json,
                    ),
                )

                last_cleanup = float(getattr(self, "_eventsub_capacity_last_cleanup", 0.0) or 0.0)
                if (now_monotonic - last_cleanup) >= 3600:
                    retention_days = self._eventsub_capacity_retention_days()
                    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
                    c.execute(
                        "DELETE FROM twitch_eventsub_capacity_snapshot WHERE ts_utc < ?",
                        (cutoff.isoformat(timespec="seconds"),),
                    )
                    setattr(self, "_eventsub_capacity_last_cleanup", now_monotonic)

            setattr(self, "_eventsub_capacity_last_snapshot", now_monotonic)
            utilization_pct = float(snapshot.get("utilization_pct") or 0.0)
            if utilization_pct >= 90.0:
                last_warn = float(getattr(self, "_eventsub_capacity_last_warn", 0.0) or 0.0)
                if (now_monotonic - last_warn) >= 600:
                    log.warning(
                        "EventSub Capacity hoch: %.1f%% (%d/%d Slots, %d Listener, Trigger=%s)",
                        utilization_pct,
                        int(snapshot.get("used_slots") or 0),
                        int(snapshot.get("total_slots") or 0),
                        int(snapshot.get("listener_count") or 0),
                        str(snapshot.get("reason") or "unknown"),
                    )
                    setattr(self, "_eventsub_capacity_last_warn", now_monotonic)
        except Exception:
            log.debug("EventSub: konnte Capacity-Snapshot nicht speichern", exc_info=True)

    async def _get_eventsub_capacity_overview(self, *, hours: int = 24) -> Dict[str, Any]:
        hours = max(1, min(168, int(hours or 24)))
        lookback = f"-{hours} hours"

        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    """
                    SELECT ts_utc, trigger_reason, listener_count, ready_listeners, failed_listeners,
                           used_slots, total_slots, headroom_slots, listeners_at_limit, utilization_pct
                      FROM twitch_eventsub_capacity_snapshot
                     WHERE ts_utc >= datetime('now', ?)
                     ORDER BY ts_utc ASC
                    """,
                    (lookback,),
                ).fetchall()
                hourly_rows = c.execute(
                    """
                    SELECT CAST(strftime('%H', ts_utc) AS INTEGER) AS hour,
                           COUNT(*) AS samples,
                           AVG(utilization_pct) AS avg_utilization_pct,
                           MAX(utilization_pct) AS max_utilization_pct,
                           AVG(used_slots) AS avg_used_slots,
                           MAX(used_slots) AS max_used_slots,
                           AVG(listener_count) AS avg_listener_count,
                           MAX(listener_count) AS max_listener_count
                      FROM twitch_eventsub_capacity_snapshot
                     WHERE ts_utc >= datetime('now', ?)
                     GROUP BY hour
                     ORDER BY hour ASC
                    """,
                    (lookback,),
                ).fetchall()
                reason_rows = c.execute(
                    """
                    SELECT trigger_reason,
                           COUNT(*) AS samples,
                           MAX(utilization_pct) AS peak_utilization_pct
                      FROM twitch_eventsub_capacity_snapshot
                     WHERE ts_utc >= datetime('now', ?)
                     GROUP BY trigger_reason
                     ORDER BY samples DESC, trigger_reason ASC
                    """,
                    (lookback,),
                ).fetchall()
        except Exception:
            log.debug("EventSub: konnte Capacity-Overview nicht laden", exc_info=True)
            rows = []
            hourly_rows = []
            reason_rows = []

        utilization_values: List[float] = []
        used_slot_values: List[float] = []
        listener_count_values: List[float] = []
        ready_count_values: List[float] = []
        failed_count_values: List[float] = []
        for row in rows:
            if hasattr(row, "keys"):
                utilization_values.append(float(row["utilization_pct"] or 0.0))
                used_slot_values.append(float(row["used_slots"] or 0.0))
                listener_count_values.append(float(row["listener_count"] or 0.0))
                ready_count_values.append(float(row["ready_listeners"] or 0.0))
                failed_count_values.append(float(row["failed_listeners"] or 0.0))
            else:
                utilization_values.append(float(row[9] or 0.0))
                used_slot_values.append(float(row[5] or 0.0))
                listener_count_values.append(float(row[2] or 0.0))
                ready_count_values.append(float(row[3] or 0.0))
                failed_count_values.append(float(row[4] or 0.0))

        def _avg(values: List[float]) -> float:
            return (sum(values) / len(values)) if values else 0.0

        def _max(values: List[float]) -> float:
            return max(values) if values else 0.0

        def _p95(values: List[float]) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            idx = int(round((len(ordered) - 1) * 0.95))
            idx = max(0, min(len(ordered) - 1, idx))
            return ordered[idx]

        current_snapshot = self._collect_eventsub_capacity_snapshot(reason="current")

        hourly: List[Dict[str, Any]] = []
        for row in hourly_rows:
            if hasattr(row, "keys"):
                hourly.append(
                    {
                        "hour": int(row["hour"] or 0),
                        "samples": int(row["samples"] or 0),
                        "avg_utilization_pct": float(row["avg_utilization_pct"] or 0.0),
                        "max_utilization_pct": float(row["max_utilization_pct"] or 0.0),
                        "avg_used_slots": float(row["avg_used_slots"] or 0.0),
                        "max_used_slots": int(row["max_used_slots"] or 0),
                        "avg_listener_count": float(row["avg_listener_count"] or 0.0),
                        "max_listener_count": int(row["max_listener_count"] or 0),
                    }
                )
            else:
                hourly.append(
                    {
                        "hour": int(row[0] or 0),
                        "samples": int(row[1] or 0),
                        "avg_utilization_pct": float(row[2] or 0.0),
                        "max_utilization_pct": float(row[3] or 0.0),
                        "avg_used_slots": float(row[4] or 0.0),
                        "max_used_slots": int(row[5] or 0),
                        "avg_listener_count": float(row[6] or 0.0),
                        "max_listener_count": int(row[7] or 0),
                    }
                )

        reasons: List[Dict[str, Any]] = []
        for row in reason_rows:
            if hasattr(row, "keys"):
                reasons.append(
                    {
                        "reason": str(row["trigger_reason"] or ""),
                        "samples": int(row["samples"] or 0),
                        "peak_utilization_pct": float(row["peak_utilization_pct"] or 0.0),
                    }
                )
            else:
                reasons.append(
                    {
                        "reason": str(row[0] or ""),
                        "samples": int(row[1] or 0),
                        "peak_utilization_pct": float(row[2] or 0.0),
                    }
                )

        last_snapshot_at = None
        if rows:
            last_row = rows[-1]
            last_snapshot_at = (
                str(last_row["ts_utc"]) if hasattr(last_row, "keys") else str(last_row[0])
            )

        return {
            "window_hours": hours,
            "samples": len(rows),
            "last_snapshot_at": last_snapshot_at,
            "avg_utilization_pct": round(_avg(utilization_values), 2),
            "p95_utilization_pct": round(_p95(utilization_values), 2),
            "max_utilization_pct": round(_max(utilization_values), 2),
            "avg_used_slots": round(_avg(used_slot_values), 2),
            "max_used_slots": int(round(_max(used_slot_values))),
            "avg_listener_count": round(_avg(listener_count_values), 2),
            "max_listener_count": int(round(_max(listener_count_values))),
            "avg_ready_listeners": round(_avg(ready_count_values), 2),
            "max_failed_listeners": int(round(_max(failed_count_values))),
            "hourly": hourly,
            "reasons": reasons,
            "active_subscriptions": current_snapshot.get("subscriptions", []),
            "active_subscription_types": current_snapshot.get("subscription_types", []),
            "active_subscription_channels": current_snapshot.get("subscription_channels", []),
            "current": {
                "ts_utc": current_snapshot.get("ts_utc"),
                "listener_count": int(current_snapshot.get("listener_count") or 0),
                "ready_listeners": int(current_snapshot.get("ready_listeners") or 0),
                "failed_listeners": int(current_snapshot.get("failed_listeners") or 0),
                "used_slots": int(current_snapshot.get("used_slots") or 0),
                "total_slots": int(current_snapshot.get("total_slots") or 0),
                "headroom_slots": int(current_snapshot.get("headroom_slots") or 0),
                "listeners_at_limit": int(current_snapshot.get("listeners_at_limit") or 0),
                "utilization_pct": float(current_snapshot.get("utilization_pct") or 0.0),
                "subscription_count": int(current_snapshot.get("subscription_count") or 0),
            },
        }

    def _get_raid_enabled_streamers_for_eventsub(self) -> List[Dict[str, str]]:
        """Broadcaster-Liste für EventSub stream.offline (nur raid_bot_enabled=1)."""
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    """
                    SELECT twitch_user_id, twitch_login
                      FROM twitch_streamers
                     WHERE raid_bot_enabled = 1
                       AND twitch_user_id IS NOT NULL
                       AND twitch_login IS NOT NULL
                       AND archived_at IS NULL
                    """
                ).fetchall()
            return [
                {
                    "twitch_user_id": str(r["twitch_user_id"] if hasattr(r, "keys") else r[0]),
                    "twitch_login": str(r["twitch_login"] if hasattr(r, "keys") else r[1]).lower(),
                }
                for r in rows
            ]
        except Exception:
            log.debug("EventSub: konnte raid_enabled Streamer nicht laden", exc_info=True)
            return []

    def _get_chat_scope_streamers_for_eventsub(self) -> List[Dict[str, str]]:
        """Broadcaster mit OAuth + Chat-Scopes (aktuell nicht für EventSub genutzt, nur für Info)."""
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    """
                    SELECT s.twitch_user_id, s.twitch_login, a.scopes
                      FROM twitch_streamers s
                      JOIN twitch_raid_auth a ON s.twitch_user_id = a.twitch_user_id
                     WHERE (s.manual_verified_permanent = 1
                            OR s.manual_verified_until IS NOT NULL
                            OR s.manual_verified_at IS NOT NULL)
                       AND s.manual_partner_opt_out = 0
                       AND s.archived_at IS NULL
                       AND s.twitch_user_id IS NOT NULL
                       AND s.twitch_login IS NOT NULL
                    """
                ).fetchall()
            out: List[Dict[str, str]] = []
            seen: set[str] = set()
            for row in rows:
                user_id = str(row["twitch_user_id"] if hasattr(row, "keys") else row[0]).strip()
                login = str(row["twitch_login"] if hasattr(row, "keys") else row[1]).strip().lower()
                scopes_raw = row["scopes"] if hasattr(row, "keys") else row[2]
                scopes = [s.strip().lower() for s in (scopes_raw or "").split() if s.strip()]
                has_chat_scope = any(
                    s in {"user:read:chat", "user:write:chat", "chat:read", "chat:edit"} for s in scopes
                )
                if not has_chat_scope or not user_id or not login:
                    continue
                key = f"{user_id}:{login}"
                if key in seen:
                    continue
                seen.add(key)
                out.append({"twitch_user_id": user_id, "twitch_login": login})
            return out
        except Exception:
            log.debug("EventSub online: konnte Streamer-Liste nicht laden", exc_info=True)
            return []

    def _get_tracked_logins_for_eventsub(self) -> List[str]:
        """Alle bekannten Streamer-Logins (für Online-Status der Partner bei EventSub)."""
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT twitch_login FROM twitch_streamers WHERE twitch_login IS NOT NULL AND archived_at IS NULL"
                ).fetchall()
            return [str(r["twitch_login"] if hasattr(r, "keys") else r[0]).lower() for r in rows]
        except Exception:
            log.debug("EventSub: konnte tracked Logins nicht laden", exc_info=True)
            return []


    async def _fetch_streams_by_logins_quick(self, logins: List[str]) -> Dict[str, dict]:
        """Hol Live-Streams fœr angegebene Logins (reduziert auf einmal pro EventSub-Offline)."""
        if not getattr(self, "api", None):
            return {}
        streams_by_login: Dict[str, dict] = {}
        logins = [lg for lg in logins if lg]
        if not logins:
            return {}
        for language in self._language_filter_values():
            try:
                streams = await self.api.get_streams_by_logins(logins, language=language)
            except Exception:
                label = language or "any"
                log.debug("EventSub: Streams fetch failed (language=%s)", label, exc_info=True)
                continue
            for stream in streams:
                login = (stream.get("user_login") or "").lower()
                if login:
                    streams_by_login[login] = stream
        return streams_by_login

    def _load_live_state_row(self, login_lower: str) -> Dict:
        """Lädt letzten Live-State aus DB, damit EventSub-Offlines sofort Daten haben."""
        if not login_lower:
            return {}
        try:
            with storage.get_conn() as c:
                row = c.execute(
                    """
                    SELECT is_live, last_seen_at, last_title, last_game, last_viewer_count,
                           last_stream_id, last_started_at, had_deadlock_in_session
                      FROM twitch_live_state
                     WHERE streamer_login = ?
                    """,
                    (login_lower,),
                ).fetchone()
            return dict(row) if row else {}
        except Exception:
            log.debug("EventSub: konnte live_state für %s nicht laden", login_lower, exc_info=True)
            return {}

    async def _on_eventsub_stream_offline(self, broadcaster_id: str, broadcaster_login: Optional[str]) -> None:
        """Direkter Auto-Raid-Trigger bei stream.offline EventSub."""
        if not broadcaster_id:
            return
        login_lower = (broadcaster_login or "").lower()
        # Fallback-Dedupe-Guard zurücksetzen, damit beim nächsten Streamstart erneut erinnert werden kann.
        try:
            fallback_guard = getattr(self, "_reauth_reminder_last_sent_ts", None)
            if isinstance(fallback_guard, dict):
                fallback_guard.pop(str(broadcaster_id), None)
        except Exception:
            log.debug("ReAuth reminder: Konnte Fallback-Guard nicht zurücksetzen", exc_info=True)
        # Doppel-Trigger (Polling + EventSub) vermeiden
        throttle = getattr(self, "_eventsub_offline_throttle", None)
        if throttle is None:
            throttle = {}
            setattr(self, "_eventsub_offline_throttle", throttle)
        now = time.time()
        last_ts = throttle.get(broadcaster_id)
        if last_ts and now - last_ts < 90:
            return
        throttle[broadcaster_id] = now

        previous_state = self._load_live_state_row(login_lower)

        # Frische Online-Streams sammeln, damit Auto-Raid Partner erkennen kann
        tracked_logins = self._get_tracked_logins_for_eventsub()
        streams_by_login = await self._fetch_streams_by_logins_quick(tracked_logins)

        try:
            await self._handle_auto_raid_on_offline(
                login=login_lower or broadcaster_login or "",
                twitch_user_id=broadcaster_id,
                previous_state=previous_state,
                streams_by_login=streams_by_login,
            )
        except Exception:
            log.exception("EventSub: Auto-Raid offline handling failed for %s", broadcaster_login or broadcaster_id)

    def _get_eventsub_webhook_url(self) -> Optional[str]:
        """Gibt die vollständige Webhook-Callback-URL zurück, falls konfiguriert."""
        base = getattr(self, "_webhook_base_url", None)
        if not base:
            return None
        return f"{base}/twitch/eventsub/callback"

    async def _cleanup_old_eventsub_subscriptions(self, webhook_url: str) -> None:
        """Löscht veraltete Webhook-Subscriptions vom letzten Start."""
        if not getattr(self, "api", None):
            return
        try:
            existing = await self.api.list_eventsub_subscriptions(status="enabled")
            deleted = 0
            for sub in existing:
                if sub.get("transport", {}).get("callback") == webhook_url:
                    sub_id = sub.get("id")
                    if sub_id:
                        await self.api.delete_eventsub_subscription(sub_id)
                        deleted += 1
            if deleted:
                log.info("EventSub Webhook: %d alte Subscriptions gelöscht", deleted)
        except Exception:
            log.exception("EventSub Webhook: Cleanup alter Subscriptions fehlgeschlagen")

    async def _start_eventsub_listener(self):
        """Startet Webhook-basierte EventSub Subscriptions."""
        if getattr(self, "_eventsub_started", False):
            log.debug("EventSub Listener bereits gestartet, überspringe.")
            return
        setattr(self, "_eventsub_started", True)

        webhook_url = self._get_eventsub_webhook_url()
        webhook_secret = getattr(self, "_webhook_secret", None)
        webhook_handler = getattr(self, "_eventsub_webhook_handler", None)

        if not webhook_url or not webhook_secret or not webhook_handler:
            log.warning(
                "EventSub Webhook: TWITCH_WEBHOOK_SECRET nicht konfiguriert – "
                "EventSub Subscriptions werden nicht erstellt. "
                "Bitte TWITCH_WEBHOOK_SECRET setzen."
            )
            setattr(self, "_eventsub_started", False)
            return

        if not getattr(self, "api", None):
            log.warning("EventSub Webhook: Keine API vorhanden, Listener wird nicht gestartet.")
            setattr(self, "_eventsub_started", False)
            return

        try:
            await self.bot.wait_until_ready()
        except Exception:
            log.exception("EventSub Webhook: wait_until_ready fehlgeschlagen")
            return

        # Callbacks registrieren
        async def _offline_cb(bid: str, login: str, _event: dict):
            try:
                await self._on_eventsub_stream_offline(bid, login)
            except Exception:
                log.exception("EventSub Webhook: Offline-Callback fehlgeschlagen für %s", login)

        async def _raid_cb(to_bid: str, to_login: str, event: dict):
            try:
                raid_bot = getattr(self, "_raid_bot", None)
                if not raid_bot:
                    log.debug("EventSub Webhook: Raid-Bot nicht verfügbar für channel.raid von %s", to_login)
                    return
                from_login = (event.get("from_broadcaster_user_login") or "").strip().lower()
                from_broadcaster_id = str(event.get("from_broadcaster_user_id") or "").strip()
                viewer_count = int(event.get("viewers") or 0)
                if not from_login:
                    log.warning("EventSub Webhook: channel.raid event ohne from_broadcaster_user_login")
                    return
                log.info(
                    "EventSub Webhook: channel.raid: %s -> %s (%d viewers)",
                    from_login, to_login, viewer_count,
                )
                await raid_bot.on_raid_arrival(
                    to_broadcaster_id=to_bid,
                    to_broadcaster_login=to_login,
                    from_broadcaster_login=from_login,
                    from_broadcaster_id=from_broadcaster_id,
                    viewer_count=viewer_count,
                )
            except Exception:
                log.exception("EventSub Webhook: Raid-Callback fehlgeschlagen für %s", to_login or to_bid)

        async def _bits_cb(bid: str, login: str, event: dict):
            try:
                await self._store_bits_event(bid, event)
            except Exception:
                log.exception("EventSub Webhook: Bits-Callback fehlgeschlagen für %s", login)

        async def _hype_begin_cb(bid: str, login: str, event: dict):
            try:
                await self._store_hype_train_event(bid, event, ended=False)
            except Exception:
                log.exception("EventSub Webhook: Hype-Train-Begin-Callback fehlgeschlagen für %s", login)

        async def _hype_end_cb(bid: str, login: str, event: dict):
            try:
                await self._store_hype_train_event(bid, event, ended=True)
            except Exception:
                log.exception("EventSub Webhook: Hype-Train-End-Callback fehlgeschlagen für %s", login)

        async def _hype_progress_cb(bid: str, login: str, event: dict):
            try:
                await self._store_hype_train_event(bid, event, ended=False, progress=True)
            except Exception:
                log.exception("EventSub Webhook: Hype-Train-Progress-Callback fehlgeschlagen für %s", login)

        async def _sub_end_cb(bid: str, login: str, event: dict):
            try:
                await self._store_subscription_event(bid, event, "end")
            except Exception:
                log.exception("EventSub Webhook: subscription.end-Callback fehlgeschlagen für %s", login)

        async def _ban_cb(bid: str, login: str, event: dict):
            try:
                await self._store_ban_event(bid, event, unbanned=False)
            except Exception:
                log.exception("EventSub Webhook: channel.ban-Callback fehlgeschlagen für %s", login)

        async def _unban_cb(bid: str, login: str, event: dict):
            try:
                await self._store_ban_event(bid, event, unbanned=True)
            except Exception:
                log.exception("EventSub Webhook: channel.unban-Callback fehlgeschlagen für %s", login)

        async def _bits_use_cb(bid: str, login: str, event: dict):
            try:
                await self._store_bits_event(bid, event)
            except Exception:
                log.exception("EventSub Webhook: channel.bits.use-Callback fehlgeschlagen für %s", login)

        async def _shoutout_create_cb(bid: str, login: str, event: dict):
            try:
                await self._store_shoutout_event(bid, event, direction="sent")
            except Exception:
                log.exception("EventSub Webhook: shoutout.create-Callback fehlgeschlagen für %s", login)

        async def _shoutout_receive_cb(bid: str, login: str, event: dict):
            try:
                await self._store_shoutout_event(bid, event, direction="received")
            except Exception:
                log.exception("EventSub Webhook: shoutout.receive-Callback fehlgeschlagen für %s", login)

        async def _online_cb(bid: str, login: str, event: dict):
            try:
                await self._handle_stream_online(bid, login, event)
            except Exception:
                log.exception("EventSub Webhook: stream.online-Callback fehlgeschlagen für %s", login)

        async def _channel_update_cb(bid: str, login: str, event: dict):
            try:
                await self._handle_channel_update(bid, event)
            except Exception:
                log.exception("EventSub Webhook: channel.update-Callback fehlgeschlagen für %s", login)

        async def _subscribe_cb(bid: str, login: str, event: dict):
            try:
                await self._store_subscription_event(bid, event, "subscribe")
            except Exception:
                log.exception("EventSub Webhook: channel.subscribe-Callback fehlgeschlagen für %s", login)

        async def _gift_cb(bid: str, login: str, event: dict):
            try:
                await self._store_subscription_event(bid, event, "gift")
            except Exception:
                log.exception("EventSub Webhook: channel.subscription.gift-Callback fehlgeschlagen für %s", login)

        async def _resub_cb(bid: str, login: str, event: dict):
            try:
                await self._store_subscription_event(bid, event, "resub")
            except Exception:
                log.exception("EventSub Webhook: channel.subscription.message-Callback fehlgeschlagen für %s", login)

        async def _ad_break_cb(bid: str, login: str, event: dict):
            try:
                await self._store_ad_break_event(bid, event)
            except Exception:
                log.exception("EventSub Webhook: channel.ad_break.begin-Callback fehlgeschlagen für %s", login)

        async def _follow_cb(bid: str, login: str, event: dict):
            user_login = (event.get("user_login") or event.get("user_name") or "").strip().lower()
            user_id = str(event.get("user_id") or "").strip()
            followed_at = (event.get("followed_at") or datetime.now(timezone.utc).isoformat())
            log.debug("EventSub: channel.follow – %s followed %s", user_login, login)
            try:
                with storage.get_conn() as c:
                    c.execute(
                        """
                        INSERT INTO twitch_follow_events
                            (streamer_login, twitch_user_id, follower_login, follower_id, followed_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (login, bid, user_login, user_id or None, followed_at),
                    )
            except Exception:
                log.exception("EventSub: _follow_cb – DB-Insert fehlgeschlagen für %s", login)

        async def _points_auto_cb(bid: str, login: str, event: dict):
            try:
                await self._store_channel_points_event(bid, event)
            except Exception:
                log.exception("EventSub: channel.channel_points_automatic_reward_redemption.add fehlgeschlagen für %s", login)

        async def _points_custom_cb(bid: str, login: str, event: dict):
            try:
                await self._store_channel_points_event(bid, event)
            except Exception:
                log.exception("EventSub: channel.channel_points_custom_reward_redemption.add fehlgeschlagen für %s", login)

        webhook_handler.set_callback("stream.online", _online_cb)
        webhook_handler.set_callback("stream.offline", _offline_cb)
        webhook_handler.set_callback("channel.follow", _follow_cb)
        webhook_handler.set_callback("channel.raid", _raid_cb)
        webhook_handler.set_callback("channel.update", _channel_update_cb)
        webhook_handler.set_callback("channel.subscribe", _subscribe_cb)
        webhook_handler.set_callback("channel.subscription.gift", _gift_cb)
        webhook_handler.set_callback("channel.subscription.message", _resub_cb)
        webhook_handler.set_callback("channel.ad_break.begin", _ad_break_cb)
        webhook_handler.set_callback("channel.cheer", _bits_cb)
        webhook_handler.set_callback("channel.hype_train.begin", _hype_begin_cb)
        webhook_handler.set_callback("channel.hype_train.end", _hype_end_cb)
        webhook_handler.set_callback("channel.hype_train.progress", _hype_progress_cb)
        webhook_handler.set_callback("channel.subscription.end", _sub_end_cb)
        webhook_handler.set_callback("channel.ban", _ban_cb)
        webhook_handler.set_callback("channel.unban", _unban_cb)
        webhook_handler.set_callback("channel.bits.use", _bits_use_cb)
        webhook_handler.set_callback("channel.shoutout.create", _shoutout_create_cb)
        webhook_handler.set_callback("channel.shoutout.receive", _shoutout_receive_cb)
        webhook_handler.set_callback("channel.channel_points_automatic_reward_redemption.add", _points_auto_cb)
        webhook_handler.set_callback("channel.channel_points_custom_reward_redemption.add", _points_custom_cb)

        # Go-Live Handler für Polling
        async def _handle_stream_went_live(bid: str, login: str):
            """
            Wird vom Polling aufgerufen wenn ein Stream live geht.
            Subscribed stream.offline via Webhook und joined Chat-Bot.
            """
            try:
                # 1. Chat-Bot joinen (falls Partner mit Chat-Scope)
                chat_bot = getattr(self, "_twitch_chat_bot", None)
                if chat_bot:
                    login_norm = login or ""
                    if not login_norm:
                        try:
                            with storage.get_conn() as c:
                                row = c.execute(
                                    "SELECT twitch_login FROM twitch_streamers WHERE twitch_user_id = ?",
                                    (bid,)
                                ).fetchone()
                            if row:
                                login_norm = str(row[0]).lower()
                        except Exception:
                            log.debug(
                                "Polling: login lookup for user id %s failed",
                                bid,
                                exc_info=True,
                            )
                    if login_norm:
                        monitored = getattr(chat_bot, "_monitored_streamers", set())
                        if login_norm not in monitored:
                            success = await chat_bot.join(login_norm, channel_id=bid)
                            if success:
                                log.info("Polling: Chat-Bot joined %s (%s) nach Go-Live", login_norm, bid)

                # 2. stream.offline Webhook Subscription (nur bei vollständiger Auth)
                # Webhook-Subscriptions erfordern App-Access-Token → oauth_token=None
                fully_authed = (
                    await self._is_fully_authed(str(bid))
                    if hasattr(self, "_is_fully_authed")
                    else True
                )
                if fully_authed:
                    if self._eventsub_has_sub("stream.offline", str(bid)):
                        log.debug(
                            "Polling: stream.offline bereits subscribed für %s, überspringe",
                            login or bid,
                        )
                    else:
                        result = await self.api.subscribe_eventsub_webhook(
                            sub_type="stream.offline",
                            condition={"broadcaster_user_id": str(bid)},
                            webhook_url=webhook_url,
                            secret=webhook_secret,
                            oauth_token=None,
                        )
                        if result:
                            log.info(
                                "Polling: stream.offline Webhook Subscription erstellt für %s",
                                login or bid,
                            )
                            self._eventsub_track_sub("stream.offline", str(bid))
                            await self._record_eventsub_capacity_snapshot("stream_offline_subscribed", force=True)
                else:
                    log.info(
                        "Polling: stream.offline übersprungen für %s (needs_reauth=1)",
                        login or bid,
                    )
                    if chat_bot and login_norm:
                        try:
                            await self._maybe_send_reauth_chat_reminder(
                                chat_bot=chat_bot,
                                broadcaster_id=str(bid),
                                login_lower=login_norm,
                            )
                        except Exception:
                            log.debug(
                                "ReAuth reminder: Chat-Hinweis fehlgeschlagen für %s",
                                login_norm,
                                exc_info=True,
                            )

                # 3. Broadcaster-Token Subscriptions (Bits, Hype, Subs, Ads)
                broadcaster_token = await self._resolve_eventsub_broadcaster_token(str(bid))
                if broadcaster_token:
                    # Scopes des Tokens aus DB laden – nur Subs subscriben, für die der Scope vorhanden ist
                    try:
                        with storage.get_conn() as _sc:
                            _scope_row = _sc.execute(
                                "SELECT scopes FROM twitch_raid_auth WHERE twitch_user_id = ?",
                                (str(bid),),
                            ).fetchone()
                        _token_scopes: set[str] = set(
                            (_scope_row[0] if _scope_row and _scope_row[0] else "").split()
                        )
                    except Exception:
                        log.debug("EventSub: Konnte Scopes für %s nicht laden", login or bid, exc_info=True)
                        _token_scopes = set()

                    broadcaster_subs = [
                        # (sub_type, version, required_scope)
                        ("channel.cheer",                  "1", "bits:read"),
                        ("channel.bits.use",               "1", "bits:read"),
                        ("channel.hype_train.begin",       "1", "channel:read:hype_train"),
                        ("channel.hype_train.progress",    "1", "channel:read:hype_train"),
                        ("channel.hype_train.end",         "1", "channel:read:hype_train"),
                        ("channel.subscribe",              "1", "channel:read:subscriptions"),
                        ("channel.subscription.gift",      "1", "channel:read:subscriptions"),
                        ("channel.subscription.message",   "1", "channel:read:subscriptions"),
                        ("channel.subscription.end",       "1", "channel:read:subscriptions"),
                        ("channel.ad_break.begin",         "1", "channel:read:ads"),
                        ("channel.ban",                    "1", "moderator:manage:banned_users"),
                        ("channel.unban",                  "1", "moderator:manage:banned_users"),
                        ("channel.shoutout.create",        "1", "moderator:manage:shoutouts"),
                        ("channel.shoutout.receive",       "1", "moderator:manage:shoutouts"),
                        ("channel.channel_points_automatic_reward_redemption.add", "2", "channel:read:redemptions"),
                        ("channel.channel_points_custom_reward_redemption.add",    "1", "channel:read:redemptions"),
                    ]
                    for sub_type, version, required_scope in broadcaster_subs:
                        # Scope-Check: überspringen wenn Token den Scope nicht hat
                        if _token_scopes and required_scope not in _token_scopes:
                            log.debug(
                                "EventSub Webhook: %s übersprungen für %s (Scope '%s' fehlt im Token)",
                                sub_type, login or bid, required_scope,
                            )
                            continue
                        if self._eventsub_has_sub(sub_type, str(bid)):
                            log.debug("EventSub Webhook: %s bereits subscribed für %s, überspringe", sub_type, login or bid)
                            continue
                        try:
                            await self.api.subscribe_eventsub_webhook(
                                sub_type=sub_type,
                                condition={"broadcaster_user_id": str(bid)},
                                webhook_url=webhook_url,
                                secret=webhook_secret,
                                version=version,
                            )
                            self._eventsub_track_sub(sub_type, str(bid))
                            log.debug(
                                "EventSub Webhook: %s Subscription erstellt für %s",
                                sub_type, login or bid,
                            )
                        except aiohttp.ClientResponseError as exc:
                            if int(getattr(exc, "status", 0) or 0) == 401:
                                if hasattr(self, "_clear_legacy_snapshot_for_user"):
                                    try:
                                        cleared = bool(
                                            self._clear_legacy_snapshot_for_user(
                                                str(bid),
                                                reason="eventsub_401_invalid_oauth",
                                            )
                                        )
                                        if cleared:
                                            log.warning(
                                                "EventSub Webhook: legacy_* Snapshot für %s nach 401 entfernt (needs_reauth bleibt 1)",
                                                login or bid,
                                            )
                                    except Exception:
                                        log.debug(
                                            "EventSub Webhook: Konnte legacy_* Snapshot nach 401 nicht entfernen für %s",
                                            login or bid,
                                            exc_info=True,
                                        )
                                log.warning(
                                    "EventSub Webhook: %s fehlgeschlagen für %s (HTTP 401 Invalid OAuth token). "
                                    "Weitere Broadcaster-Subscriptions werden übersprungen.",
                                    sub_type,
                                    login or bid,
                                )
                                break
                            log.debug(
                                "EventSub Webhook: %s fehlgeschlagen für %s (HTTP %s, evtl. Scope fehlt)",
                                sub_type,
                                login or bid,
                                int(getattr(exc, "status", 0) or 0),
                                exc_info=True,
                            )
                        except Exception:
                            log.debug(
                                "EventSub Webhook: %s fehlgeschlagen für %s (evtl. Scope fehlt)",
                                sub_type, login or bid, exc_info=True,
                            )

                    # channel.follow v2 – braucht moderator_user_id in der Condition
                    if self._eventsub_has_sub("channel.follow", str(bid)):
                        log.debug("EventSub Webhook: channel.follow bereits subscribed für %s, überspringe", login or bid)
                    else:
                        try:
                            await self.api.subscribe_eventsub_webhook(
                                sub_type="channel.follow",
                                condition={
                                    "broadcaster_user_id": str(bid),
                                    "moderator_user_id": str(bid),
                                },
                                webhook_url=webhook_url,
                                secret=webhook_secret,
                                version="2",
                            )
                            self._eventsub_track_sub("channel.follow", str(bid))
                            log.debug("EventSub Webhook: channel.follow v2 subscribed für %s", login or bid)
                        except Exception:
                            log.debug(
                                "EventSub Webhook: channel.follow v2 fehlgeschlagen für %s (evtl. Scope fehlt)",
                                login or bid, exc_info=True,
                            )

            except Exception:
                log.exception("Polling: Go-Live Handler fehlgeschlagen für %s", login or bid)

        setattr(self, "_handle_stream_went_live", _handle_stream_went_live)

        # 1. Alte Subscriptions bereinigen
        await self._cleanup_old_eventsub_subscriptions(webhook_url)

        # Lokale Subscription-Tracking-Liste leeren
        setattr(self, "_eventsub_webhook_active_subs", [])

        # 2. Broadcaster sammeln
        raid_enabled_streamers = self._get_raid_enabled_streamers_for_eventsub()
        if not raid_enabled_streamers:
            log.info("EventSub Webhook: Keine Streamer für EventSub monitoring gefunden.")
            try:
                await self._record_eventsub_capacity_snapshot("startup_no_streamers", force=True)
            except Exception:
                log.debug("EventSub: Snapshot für startup_no_streamers fehlgeschlagen", exc_info=True)
            return

        # 3. Live-Status abrufen
        raid_logins = [s["twitch_login"] for s in raid_enabled_streamers]
        currently_live_streams: Dict[str, dict] = {}
        try:
            live_streams = await self.api.get_streams_by_logins(raid_logins)
            for stream in live_streams:
                login_lower = (stream.get("user_login") or "").lower()
                if login_lower:
                    currently_live_streams[login_lower] = stream
            log.info(
                "EventSub Webhook: %d von %d raid-enabled Streamern sind aktuell live",
                len(currently_live_streams),
                len(raid_enabled_streamers),
            )
        except Exception:
            log.exception(
                "EventSub Webhook: Konnte Live-Status nicht abrufen, "
                "subscribe keine stream.offline beim Start"
            )

        # 4. stream.online + stream.offline + channel.update für alle/live Streamer
        offline_added = 0
        online_added = 0
        update_added = 0
        for entry in raid_enabled_streamers:
            bid = entry.get("twitch_user_id")
            login = entry.get("twitch_login", "").lower()
            if not bid:
                continue

            # stream.online für ALLE (so erkennen wir Go-Live sofort statt per 15s-Polling)
            # Webhook-Subscriptions erfordern einen App-Access-Token (client_credentials),
            # daher oauth_token=None → TwitchAPI nutzt automatisch den App-Token.
            try:
                result = await self.api.subscribe_eventsub_webhook(
                    sub_type="stream.online",
                    condition={"broadcaster_user_id": str(bid)},
                    webhook_url=webhook_url,
                    secret=webhook_secret,
                    oauth_token=None,
                )
                if result:
                    self._eventsub_track_sub("stream.online", str(bid))
                    online_added += 1
            except Exception:
                log.debug("EventSub Webhook: stream.online fehlgeschlagen für %s", login, exc_info=True)

            # channel.update für ALLE (Titel/Game-Änderungen mitbekommen)
            try:
                result = await self.api.subscribe_eventsub_webhook(
                    sub_type="channel.update",
                    condition={"broadcaster_user_id": str(bid)},
                    webhook_url=webhook_url,
                    secret=webhook_secret,
                    oauth_token=None,
                    version="2",
                )
                if result:
                    self._eventsub_track_sub("channel.update", str(bid))
                    update_added += 1
            except Exception:
                log.debug("EventSub Webhook: channel.update fehlgeschlagen für %s", login, exc_info=True)

            # stream.offline für ALLE Streamer (nicht nur live) – so wird
            # auch ein Offline-Ereignis erkannt wenn der Bot während eines
            # laufenden Streams neu gestartet wurde oder der Streamer offline
            # ist, aber danach wieder live geht und wir keinen Neustart haben.
            try:
                result = await self.api.subscribe_eventsub_webhook(
                    sub_type="stream.offline",
                    condition={"broadcaster_user_id": str(bid)},
                    webhook_url=webhook_url,
                    secret=webhook_secret,
                    oauth_token=None,
                )
                if result:
                    self._eventsub_track_sub("stream.offline", str(bid))
                    offline_added += 1
                    log.debug("EventSub Webhook: stream.offline subscribed für %s", login)
            except Exception:
                log.exception("EventSub Webhook: stream.offline fehlgeschlagen für %s", login)

        log.info(
            "EventSub Webhook: stream.online=%d, channel.update=%d, stream.offline=%d subscribiert",
            online_added, update_added, offline_added,
        )
        try:
            await self._record_eventsub_capacity_snapshot("startup_distribution", force=True)
        except Exception:
            log.debug("EventSub: Startup-Capacity-Snapshot fehlgeschlagen", exc_info=True)

    async def _resolve_eventsub_bot_token(self) -> Optional[str]:
        """Gibt den aktuellen Bot-Token zurück (ohne 'oauth:' Präfix)."""
        bot_token_mgr = getattr(self, "_bot_token_manager", None)
        if not bot_token_mgr:
            return None
        try:
            token, _ = await bot_token_mgr.get_valid_token()
            if not token:
                return None
            token = token.strip()
            if token.lower().startswith("oauth:"):
                token = token[6:]
            return token
        except Exception:
            log.debug("EventSub Webhook: konnte Bot-Token nicht laden", exc_info=True)
            return None

    async def _resolve_eventsub_broadcaster_token(self, broadcaster_user_id: str) -> Optional[str]:
        """Gibt den Broadcaster-Token für eine bestimmte User-ID zurück (falls vorhanden).
        Bei needs_reauth=1 wird der legacy_access_token genutzt (Übergangsmodus)."""
        if hasattr(self, "_resolve_broadcaster_token_with_legacy"):
            return await self._resolve_broadcaster_token_with_legacy(broadcaster_user_id)
        # Fallback falls Mixin nicht eingebunden
        try:
            with storage.get_conn() as c:
                row = c.execute(
                    "SELECT access_token FROM twitch_raid_auth WHERE twitch_user_id = ?",
                    (broadcaster_user_id,),
                ).fetchone()
            if not row:
                return None
            token = str(row[0] if not hasattr(row, "keys") else row["access_token"]).strip()
            if token.lower().startswith("oauth:"):
                token = token[6:]
            return token or None
        except Exception:
            log.debug(
                "EventSub Webhook: konnte Broadcaster-Token nicht laden",
                exc_info=True,
            )
            return None

    def _eventsub_has_sub(self, sub_type: str, broadcaster_user_id: str) -> bool:
        """Prüft ob eine Webhook-Subscription bereits in dieser Session registriert wurde."""
        tracked: set = getattr(self, "_eventsub_webhook_tracked", None)
        if tracked is None:
            return False
        return (sub_type, str(broadcaster_user_id)) in tracked

    def _eventsub_track_sub(self, sub_type: str, broadcaster_user_id: str) -> None:
        """Merkt sich eine aktive Webhook-Subscription für spätere Capacity-Snapshots."""
        tracked: set = getattr(self, "_eventsub_webhook_tracked", None)
        if tracked is None:
            tracked = set()
            setattr(self, "_eventsub_webhook_tracked", tracked)
        tracked.add((sub_type, str(broadcaster_user_id)))
        # Kompatibilität: active_subs-Liste für Capacity-Snapshots weiter befüllen
        active_subs: List[Dict] = getattr(self, "_eventsub_webhook_active_subs", None)
        if active_subs is None:
            active_subs = []
            setattr(self, "_eventsub_webhook_active_subs", active_subs)
        if not any(
            s.get("sub_type") == sub_type and s.get("broadcaster_user_id") == str(broadcaster_user_id)
            for s in active_subs
        ):
            active_subs.append({
                "sub_type": sub_type,
                "broadcaster_user_id": str(broadcaster_user_id),
            })


    async def subscribe_raid_target_dynamic(self, broadcaster_id: str, broadcaster_login: str) -> bool:
        """
        Erstellt dynamisch eine channel.raid Webhook-Subscription für einen Broadcaster.

        Wird aufgerufen wenn ein Raid gestartet wird, um zu erkennen wenn der Raid ankommt.

        Returns:
            True wenn die Subscription erfolgreich erstellt wurde, False sonst.
        """
        webhook_url = self._get_eventsub_webhook_url()
        webhook_secret = getattr(self, "_webhook_secret", None)

        if not webhook_url or not webhook_secret:
            log.error(
                "EventSub Webhook: Keine Webhook-URL/Secret konfiguriert für channel.raid subscription"
            )
            return False

        if not getattr(self, "api", None):
            log.error("EventSub Webhook: Keine API verfügbar für channel.raid subscription")
            await self._record_eventsub_capacity_snapshot("raid_no_api", force=True)
            return False

        try:
            result = await self.api.subscribe_eventsub_webhook(
                sub_type="channel.raid",
                condition={"to_broadcaster_user_id": str(broadcaster_id)},
                webhook_url=webhook_url,
                secret=webhook_secret,
                oauth_token=None,  # Webhook-Subscriptions benötigen App-Access-Token
            )
            if result:
                self._eventsub_track_sub("channel.raid", str(broadcaster_id))
                log.info(
                    "EventSub Webhook: channel.raid Subscription erstellt für %s (ID: %s)",
                    broadcaster_login,
                    broadcaster_id,
                )
                await self._record_eventsub_capacity_snapshot("raid_subscribed", force=True)
                return True
            log.error(
                "EventSub Webhook: channel.raid Subscription fehlgeschlagen für %s",
                broadcaster_login,
            )
            await self._record_eventsub_capacity_snapshot("raid_subscribe_failed", force=True)
            return False
        except Exception:
            log.exception(
                "EventSub Webhook: channel.raid Subscription fehlgeschlagen für %s",
                broadcaster_login,
            )
            await self._record_eventsub_capacity_snapshot("raid_subscribe_error", force=True)
            return False

    async def _start_eventsub_offline_listener(self):
        """Kompatibilitäts-Stub (wird nun über _start_eventsub_listener erledigt)."""
        await self._start_eventsub_listener()

    async def _start_eventsub_online_listener(self):
        """Kompatibilitäts-Stub (wird nun über _start_eventsub_listener erledigt)."""
        pass

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_streams(self):
        if self.api is None:
            return
        try:
            await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Polling-Tick fehlgeschlagen")

    @poll_streams.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=INVITES_REFRESH_INTERVAL_HOURS)
    async def invites_refresh(self):
        try:
            await self._refresh_all_invites()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Invite-Refresh fehlgeschlagen")

    @invites_refresh.before_loop
    async def _before_invites(self):
        await self.bot.wait_until_ready()

    async def _ensure_category_id(self):
        if self.api is None:
            return
        try:
            self._category_id = await self.api.get_category_id(TWITCH_TARGET_GAME_NAME)
            if self._category_id:
                log.debug("Deadlock category_id = %s", self._category_id)
        except Exception:
            log.exception("Konnte Twitch-Kategorie-ID nicht ermitteln")

    async def _tick(self):
        """Ein Tick: tracked Streamer + Kategorie-Streams prüfen, Postings/DB aktualisieren, Stats loggen."""
        if self.api is None:
            return

        if not self._category_id:
            await self._ensure_category_id()

        partner_logins: set[str] = set()
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT twitch_login, twitch_user_id, require_discord_link, "
                    "       manual_verified_permanent, manual_verified_until, archived_at "
                    "FROM twitch_streamers"
                ).fetchall()
            tracked: List[Dict[str, object]] = []
            now_utc = datetime.now(timezone.utc)
            for row in rows:
                row_dict = dict(row)
                login = str(row_dict.get("twitch_login") or "").strip()
                if not login:
                    continue
                user_id = str(row_dict.get("twitch_user_id") or "").strip()
                require_link = bool(row_dict.get("require_discord_link"))
                archived_at_raw = row_dict.get("archived_at")
                archived_dt: Optional[datetime] = None
                if archived_at_raw:
                    try:
                        archived_dt = datetime.fromisoformat(str(archived_at_raw))
                    except Exception:
                        archived_dt = None
                is_archived = archived_dt is not None
                is_verified = False
                try:
                    is_verified = self._is_partner_verified(row_dict, now_utc)
                except Exception:
                    log.debug("Konnte Verifizierungsstatus für %s nicht bestimmen", login, exc_info=True)

                tracked.append(
                    {
                        "login": login,
                        "twitch_user_id": user_id,
                        "require_link": require_link,
                        "is_verified": is_verified,
                        "archived_at": archived_at_raw,
                        "is_archived": is_archived,
                    }
                )
                login_lower = login.lower()
                if login_lower and is_verified and not is_archived:
                    partner_logins.add(login_lower)
        except Exception:
            log.exception("Konnte tracked Streamer nicht aus DB lesen")
            tracked = []
            partner_logins = set()

        logins = [str(entry.get("login") or "") for entry in tracked if entry.get("login")]
        language_filters = self._language_filter_values()
        streams_by_login: Dict[str, dict] = {}

        if logins:
            for language in language_filters:
                try:
                    streams = await self.api.get_streams_by_logins(logins, language=language)
                except Exception:
                    label = language or "any"
                    log.exception("Konnte Streams für tracked Logins nicht abrufen (language=%s)", label)
                    continue
                for stream in streams:
                    login = (stream.get("user_login") or "").lower()
                    if login:
                        streams_by_login[login] = stream

        for login, stream in list(streams_by_login.items()):
            if login in partner_logins:
                stream["is_partner"] = True

        category_streams: List[dict] = []
        if self._category_id:
            collected: Dict[str, dict] = {}
            for language in language_filters:
                remaining = self._category_sample_limit - len(collected)
                if remaining <= 0:
                    break
                try:
                    streams = await self.api.get_streams_by_category(
                        self._category_id,
                        language=language,
                        limit=max(1, remaining),
                    )
                except Exception:
                    label = language or "any"
                    log.exception("Konnte Kategorie-Streams nicht abrufen (language=%s)", label)
                    continue
                for stream in streams:
                    login = (stream.get("user_login") or "").lower()
                    if login and login not in collected:
                        collected[login] = stream
            category_streams = list(collected.values())

        for stream in category_streams:
            login = (stream.get("user_login") or "").lower()
            if login in partner_logins:
                stream["is_partner"] = True

        try:
            await self._process_postings(tracked, streams_by_login)
        except Exception:
            log.exception("Fehler in _process_postings")

        try:
            await self._record_eventsub_capacity_snapshot("poll_tick")
        except Exception:
            log.debug("EventSub: Snapshot im Poll-Tick fehlgeschlagen", exc_info=True)

        self._tick_count += 1
        if self._tick_count % self._log_every_n == 0:
            try:
                await self._log_stats(streams_by_login, category_streams)
            except Exception:
                log.exception("Fehler beim Stats-Logging")

        # Partner-Rekrutierung (intern rate-limitiert auf 30 min)
        try:
            await self._run_partner_recruit(category_streams)
        except Exception:
            log.exception("Fehler bei Partner-Rekrutierung")

    async def _process_postings(
        self,
        tracked: List[Dict[str, object]],
        streams_by_login: Dict[str, dict],
    ):
        notify_ch: Optional[discord.TextChannel] = None
        if self._notify_channel_id:
            notify_ch = self.bot.get_channel(self._notify_channel_id) or None  # type: ignore[assignment]

        now_utc = datetime.now(tz=timezone.utc)
        now_iso = now_utc.isoformat(timespec="seconds")
        pending_state_rows: List[
            tuple[
                str,
                str,
                int,
                str,
                Optional[str],
                Optional[str],
                int,
                Optional[str],
                Optional[str],
                Optional[str],
                Optional[str],
                int,
                Optional[int],
                Optional[str],
            ]
        ] = []

        with storage.get_conn() as c:
            live_state_rows = c.execute("SELECT * FROM twitch_live_state").fetchall()

        live_state: Dict[str, dict] = {}
        for row in live_state_rows:
            row_dict = dict(row)
            key = str(row_dict.get("streamer_login") or "").lower()
            if key:
                live_state[key] = row_dict

        target_game_lower = self._get_target_game_lower()

        for entry in tracked:
            login = str(entry.get("login") or "").strip()
            if not login:
                continue

            referral_url = self._build_referral_url(login)
            login_lower = login.lower()
            stream = streams_by_login.get(login_lower)
            previous_state = live_state.get(login_lower, {})
            is_archived = bool(entry.get("is_archived"))
            was_live = bool(previous_state.get("is_live", 0))
            is_live = bool(stream)
            twitch_user_id = str(entry.get("twitch_user_id") or "").strip() or None

            # Go-Live Detection: Subscribe stream.offline für raid-enabled Streamer
            if not was_live and is_live and twitch_user_id:
                # Stream ist gerade live gegangen!
                handler = getattr(self, "_handle_stream_went_live", None)
                if handler:
                    # Checke ob der Streamer raid_bot_enabled hat
                    try:
                        with storage.get_conn() as c:
                            raid_enabled_row = c.execute(
                                "SELECT raid_bot_enabled FROM twitch_streamers WHERE twitch_user_id = ?",
                                (twitch_user_id,)
                            ).fetchone()
                        if raid_enabled_row and bool(raid_enabled_row[0]):
                            # Asynchron aufrufen (fire-and-forget, blockiert nicht den Tick)
                            asyncio.create_task(
                                handler(twitch_user_id, login_lower),
                                name=f"golive.{login_lower}"
                            )
                    except Exception:
                        log.debug("Go-Live: Konnte raid_enabled Status nicht checken für %s", login_lower, exc_info=True)

            # Auto-Entarchivierung sobald jemand wieder streamt
            if is_live and is_archived:
                try:
                    await self._dashboard_archive(login, "unarchive")
                    is_archived = False
                    entry["is_archived"] = False
                except Exception:
                    log.debug("Auto-Unarchive fehlgeschlagen für %s", login, exc_info=True)
            previous_game = (previous_state.get("last_game") or "").strip()
            previous_game_lower = previous_game.lower()
            was_deadlock = previous_game_lower == target_game_lower
            stream_started_at_value = self._extract_stream_start(stream, previous_state)
            previous_stream_id = (previous_state.get("last_stream_id") or "").strip()
            current_stream_id_raw = stream.get("id") if stream else ""
            current_stream_id = str(current_stream_id_raw or "").strip()
            stream_id_value = current_stream_id or previous_stream_id or None
            had_deadlock_prev = bool(int(previous_state.get("had_deadlock_in_session", 0) or 0))
            active_session_id: Optional[int] = None
            previous_last_deadlock_seen = (previous_state.get("last_deadlock_seen_at") or "").strip() or None

            if is_live and stream:
                try:
                    active_session_id = await self._ensure_stream_session(
                        login=login_lower,
                        stream=stream,
                        previous_state=previous_state,
                        twitch_user_id=twitch_user_id,
                    )
                except Exception:
                    log.exception("Konnte Streamsitzung nicht starten: %s", login)
            elif was_live and not is_live:
                try:
                    await self._finalize_stream_session(login=login_lower, reason="offline")
                except Exception:
                    log.exception("Konnte Streamsitzung nicht abschliessen: %s", login)
            elif not is_live and previous_state.get("active_session_id"):
                try:
                    await self._finalize_stream_session(login=login_lower, reason="stale")
                except Exception:
                    log.debug("Konnte alte Session nicht bereinigen: %s", login, exc_info=True)

            if not was_live:
                had_deadlock_prev = False
            elif is_live and previous_stream_id and current_stream_id and previous_stream_id != current_stream_id:
                had_deadlock_prev = False

            message_id_previous = str(previous_state.get("last_discord_message_id") or "").strip() or None
            message_id_to_store = message_id_previous
            tracking_token_previous = (
                str(previous_state.get("last_tracking_token") or "").strip() or None
            )
            tracking_token_to_store = tracking_token_previous

            need_link = bool(entry.get("require_link"))
            is_verified = bool(entry.get("is_verified"))

            game_name = (stream.get("game_name") or "").strip() if stream else ""
            game_name_lower = game_name.lower()
            is_deadlock = is_live and bool(target_game_lower) and game_name_lower == target_game_lower
            had_deadlock_in_session = had_deadlock_prev or is_deadlock
            had_deadlock_to_store = had_deadlock_in_session if is_live else False
            last_title_value = (stream.get("title") if stream else previous_state.get("last_title")) or None
            last_game_value = (game_name or previous_state.get("last_game") or "").strip() or None
            last_viewer_count_value = (
                int(stream.get("viewer_count") or 0)
                if stream
                else int(previous_state.get("last_viewer_count") or 0)
            )
            last_deadlock_seen_at_value: Optional[str] = None
            if is_deadlock:
                last_deadlock_seen_at_value = now_iso
            elif had_deadlock_to_store and previous_last_deadlock_seen:
                last_deadlock_seen_at_value = previous_last_deadlock_seen

            should_post = (
                notify_ch is not None
                and is_deadlock
                and (not was_live or not was_deadlock or not message_id_previous)
                and is_verified
                and not is_archived
            )

            if should_post:
                referral_url = self._build_referral_url(login)
                display_name = stream.get("user_name") or login
                message_prefix: List[str] = []
                if self._alert_mention:
                    message_prefix.append(self._alert_mention)
                stream_title = (stream.get("title") or "").strip()
                live_announcement = f"**{display_name}** ist live! Schau ueber den Button unten rein."
                if stream_title:
                    live_announcement = f"{live_announcement} - {stream_title}"
                message_prefix.append(live_announcement)
                content = " ".join(part for part in message_prefix if part).strip()

                embed = self._build_live_embed(login, stream)
                new_tracking_token = self._generate_tracking_token()
                view = self._build_live_view(
                    login,
                    referral_url,
                    new_tracking_token,
                )

                try:
                    message = await notify_ch.send(content=content or None, embed=embed, view=view)
                except Exception:
                    log.exception("Konnte Go-Live-Posting nicht senden: %s", login)
                else:
                    message_id_to_store = str(message.id)
                    tracking_token_to_store = new_tracking_token
                    if view is not None:
                        view.bind_to_message(channel_id=getattr(notify_ch, "id", None), message_id=message.id)
                        self._register_live_view(
                            tracking_token=new_tracking_token,
                            view=view,
                            message_id=message.id,
                        )
                    # Store notification text if we have an active session
                    if active_session_id:
                        try:
                            with storage.get_conn() as c:
                                c.execute(
                                    "UPDATE twitch_stream_sessions SET notification_text = ? WHERE id = ?",
                                    (content or "", active_session_id),
                                )
                        except Exception:
                            log.debug("Could not save notification text for %s", login, exc_info=True)

            ended_deadlock_posting = (
                notify_ch is not None
                and message_id_previous
                and (not is_live or not is_deadlock)
            )
            should_auto_raid = (
                notify_ch is not None
                and was_live
                and not is_live
                and had_deadlock_in_session
                and not is_archived
            )

            # Auto-Raid beim Offline-Gehen (Throttle gemeinsam mit EventSub-Pfad)
            if should_auto_raid:
                raid_uid = str(twitch_user_id or previous_state.get("twitch_user_id") or "")
                throttle = getattr(self, "_eventsub_offline_throttle", None)
                if throttle is None:
                    throttle = {}
                    setattr(self, "_eventsub_offline_throttle", throttle)
                last_ts = throttle.get(raid_uid)
                if not (last_ts and time.time() - last_ts < 90):
                    throttle[raid_uid] = time.time()
                    await self._handle_auto_raid_on_offline(
                        login=login,
                        twitch_user_id=raid_uid,
                        previous_state=previous_state,
                        streams_by_login=streams_by_login,
                    )

            if ended_deadlock_posting:
                display_name = (
                    (stream.get("user_name") if stream else previous_state.get("streamer_login"))
                    or login
                )
                try:
                    message_id_int = int(message_id_previous)
                except (TypeError, ValueError):
                    message_id_int = None

                if message_id_int is None:
                    log.warning("Ungültige Message-ID für Deadlock-Ende bei %s: %r", login, message_id_previous)
                else:
                    try:
                        fetched_message = await notify_ch.fetch_message(message_id_int)
                    except discord.NotFound:
                        log.warning(
                            "Deadlock-Ende-Posting nicht mehr vorhanden für %s (ID %s)",
                            login,
                            message_id_previous,
                        )
                        message_id_to_store = None
                        tracking_token_to_store = None
                        self._drop_live_view(tracking_token_previous)
                    except Exception:
                        log.exception("Konnte Deadlock-Ende-Posting nicht laden: %s", login)
                    else:
                        preview_image_url = await self._get_latest_vod_preview_url(
                            login=login,
                            twitch_user_id=twitch_user_id or previous_state.get("twitch_user_id"),
                        )

                        ended_content = f"**{display_name}** ist OFFLINE - VOD per Button."
                        offline_embed = self._build_offline_embed(
                            login=login,
                            display_name=display_name,
                            last_title=last_title_value,
                            last_game=last_game_value,
                            preview_image_url=preview_image_url,
                        )
                        offline_view = self._build_offline_link_view(referral_url, label=TWITCH_VOD_BUTTON_LABEL)
                        try:
                            await fetched_message.edit(content=ended_content, embed=offline_embed, view=offline_view)
                        except Exception:
                            log.exception(
                                "Konnte Deadlock-Ende-Posting nicht aktualisieren: %s", login
                            )
                        else:
                            message_id_to_store = None
                            tracking_token_to_store = None
                            self._drop_live_view(tracking_token_previous)

            db_user_id = twitch_user_id or previous_state.get("twitch_user_id") or login_lower
            db_user_id = str(db_user_id)
            db_message_id = str(message_id_to_store) if message_id_to_store else None
            db_streamer_login = login_lower

            pending_state_rows.append(
                (
                    db_user_id,
                    db_streamer_login,
                    int(is_live),
                    now_iso,
                    last_title_value,
                    last_game_value,
                    last_viewer_count_value,
                    db_message_id,
                    tracking_token_to_store,
                    stream_id_value,
                    stream_started_at_value,
                    int(had_deadlock_to_store),
                    active_session_id,
                    last_deadlock_seen_at_value,
                )
            )

            if need_link and self._alert_channel_id and (now_utc.minute % 10 == 0) and is_live:
                # Platzhalter für deinen Profil-/Panel-Check
                pass

        await self._persist_live_state_rows(pending_state_rows)
        await self._auto_archive_inactive_streamers()

    async def _persist_live_state_rows(
        self,
        rows: List[
            tuple[
                str,
                str,
                int,
                str,
                Optional[str],
                Optional[str],
                int,
                Optional[str],
                Optional[str],
                Optional[str],
                Optional[str],
                int,
                Optional[int],
            ]
        ],
    ) -> None:
        if not rows:
            return

        retry_delay = 0.5
        for attempt in range(3):
            try:
                with storage.get_conn() as c:
                    c.executemany(
                        "INSERT OR REPLACE INTO twitch_live_state "
                        "("
                        "twitch_user_id, streamer_login, is_live, last_seen_at, last_title, last_game, "
                        "last_viewer_count, last_discord_message_id, last_tracking_token, last_stream_id, "
                        "last_started_at, had_deadlock_in_session, active_session_id, last_deadlock_seen_at"
                        ") "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                return
            except sqlite3.OperationalError as exc:
                locked = "locked" in str(exc).lower()
                if not locked or attempt == 2:
                    log.exception(
                        "Konnte Live-State-Updates nicht speichern (%s Eintraege)",
                        len(rows),
                    )
                    return
                await asyncio.sleep(retry_delay)
                retry_delay *= 2

    async def _auto_archive_inactive_streamers(self, *, days: int = 10) -> None:
        """
        Archiviert Partner automatisch, wenn sie länger als `days` Tage nicht gestreamt haben.
        Läuft maximal alle 15 Minuten, um DB-Load gering zu halten.
        """
        now = datetime.now(timezone.utc)
        last_run = getattr(self, "_last_archive_check", 0.0)
        if time.time() - last_run < 900:
            return
        setattr(self, "_last_archive_check", time.time())

        cutoff = now - timedelta(days=days)

        try:
            target_game = (os.getenv("TWITCH_TARGET_GAME_NAME") or TWITCH_TARGET_GAME_NAME or "").strip()
            with storage.get_conn() as c:
                rows = c.execute(
                    """
                    SELECT s.twitch_login,
                           s.archived_at,
                           MAX(
                               CASE
                                 WHEN LOWER(COALESCE(sess.game_name,'')) = LOWER(?)
                                 THEN COALESCE(sess.ended_at, sess.started_at)
                               END
                           ) AS last_deadlock_stream_at
                      FROM twitch_streamers s
                      LEFT JOIN twitch_stream_sessions sess
                        ON LOWER(sess.streamer_login) = LOWER(s.twitch_login)
                     WHERE s.manual_partner_opt_out = 0
                       AND (s.manual_verified_permanent = 1
                            OR s.manual_verified_until IS NOT NULL
                            OR s.manual_verified_at IS NOT NULL)
                    GROUP BY s.twitch_login, s.archived_at
                    """,
                    (target_game,),
                ).fetchall()
        except Exception:
            log.debug("Auto-Archivierung: konnte Streamer-Liste nicht laden", exc_info=True)
            return

        for row in rows:
            try:
                login = (row["twitch_login"] if hasattr(row, "keys") else row[0] or "").strip()
            except Exception:
                continue
            if not login:
                continue

            archived_at = row["archived_at"] if hasattr(row, "keys") else row[1]
            if archived_at:
                continue

            last_stream_raw = row["last_deadlock_stream_at"] if hasattr(row, "keys") else row[2]
            if not last_stream_raw:
                # Keine Historie -> keine automatische Archivierung
                continue

            try:
                last_dt = datetime.fromisoformat(str(last_stream_raw).replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
            except Exception:
                log.debug("Auto-Archivierung: Datum unlesbar für %s (%r)", login, last_stream_raw, exc_info=True)
                continue

            if last_dt < cutoff:
                try:
                    await self._dashboard_archive(login, "archive")
                    log.info(
                        "Auto-archiviert %s (letzter Stream %s, cutoff %s)",
                        login,
                        last_dt.date().isoformat(),
                        cutoff.date().isoformat(),
                    )
                except Exception:
                    log.debug("Auto-Archivierung fehlgeschlagen für %s", login, exc_info=True)

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _extract_stream_start(self, stream: Optional[dict], previous_state: dict) -> Optional[str]:
        candidate = None
        if stream:
            candidate = stream.get("started_at") or stream.get("start_time")
        if not candidate:
            candidate = previous_state.get("last_started_at")
        dt = self._parse_dt(candidate)
        if dt:
            return dt.isoformat(timespec="seconds")
        return None

    def _get_active_sessions_cache(self) -> Dict[str, int]:
        cache = getattr(self, "_active_sessions", None)
        if cache is None:
            cache = {}
            setattr(self, "_active_sessions", cache)
        return cache

    def _rehydrate_active_sessions(self) -> None:
        cache = self._get_active_sessions_cache()
        cache.clear()
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT id, streamer_login FROM twitch_stream_sessions WHERE ended_at IS NULL"
                ).fetchall()
        except Exception:
            log.debug("Konnte offene Twitch-Sessions nicht laden", exc_info=True)
            return
        for row in rows:
            try:
                session_id = int(row["id"] if hasattr(row, "keys") else row[0])
                login = str(row["streamer_login"] if hasattr(row, "keys") else row[1]).lower()
            except Exception:
                continue
            if login:
                cache[login] = session_id

    def _lookup_open_session_id(self, login: str) -> Optional[int]:
        try:
            with storage.get_conn() as c:
                row = c.execute(
                    "SELECT id FROM twitch_stream_sessions WHERE streamer_login = ? AND ended_at IS NULL "
                    "ORDER BY started_at DESC LIMIT 1",
                    (login.lower(),),
                ).fetchone()
        except Exception:
            log.debug("Lookup offene Session fehlgeschlagen fuer %s", login, exc_info=True)
            return None
        if not row:
            return None
        session_id = int(row["id"] if hasattr(row, "keys") else row[0])
        cache = self._get_active_sessions_cache()
        cache[login.lower()] = session_id
        return session_id

    def _get_active_session_id(self, login: str) -> Optional[int]:
        cache = self._get_active_sessions_cache()
        cached = cache.get(login.lower())
        if cached:
            return cached
        return self._lookup_open_session_id(login)

    async def _ensure_stream_session(
        self,
        *,
        login: str,
        stream: dict,
        previous_state: dict,
        twitch_user_id: Optional[str],
    ) -> Optional[int]:
        login_lower = login.lower()
        stream_id = str(stream.get("id") or "").strip() or None

        session_id = self._get_active_session_id(login_lower)
        if session_id:
            try:
                with storage.get_conn() as c:
                    row = c.execute(
                        "SELECT stream_id FROM twitch_stream_sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                current_stream_id = str(row["stream_id"] if hasattr(row, "keys") else row[0] or "").strip() if row else ""
            except Exception:
                current_stream_id = ""
            if current_stream_id and stream_id and current_stream_id != stream_id:
                await self._finalize_stream_session(login=login_lower, reason="restarted")
                session_id = None

        if session_id:
            return session_id

        followers_start = await self._fetch_followers_total_safe(
            twitch_user_id=twitch_user_id,
            login=login_lower,
            stream=stream,
        )
        started_at_iso = self._extract_stream_start(stream, previous_state)
        stream_title = str(stream.get("title") or "").strip()
        language = str(stream.get("language") or "").strip()
        is_mature = bool(stream.get("is_mature"))
        tags_list = stream.get("tags") or []
        tags_str = ",".join(tags_list) if isinstance(tags_list, list) else ""

        return self._start_stream_session(
            login=login_lower,
            stream=stream,
            started_at_iso=started_at_iso,
            twitch_user_id=twitch_user_id,
            followers_start=followers_start,
            title=stream_title,
            language=language,
            is_mature=is_mature,
            tags=tags_str,
        )

    def _start_stream_session(
        self,
        *,
        login: str,
        stream: dict,
        started_at_iso: Optional[str],
        twitch_user_id: Optional[str],
        followers_start: Optional[int],
        title: str = "",
        language: str = "",
        is_mature: bool = False,
        tags: str = "",
    ) -> Optional[int]:
        start_ts = started_at_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")
        viewer_count = int(stream.get("viewer_count") or 0)
        stream_id = str(stream.get("id") or "").strip() or None
        game_name = (stream.get("game_name") or "").strip() or None
        had_deadlock_initial = 1 if self._stream_is_in_target_category(stream) else 0
        session_id: Optional[int] = None
        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_stream_sessions (
                        streamer_login, stream_id, started_at, start_viewers, peak_viewers,
                        end_viewers, avg_viewers, samples, followers_start, stream_title,
                        language, is_mature, tags, game_name, had_deadlock_in_session
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        login,
                        stream_id,
                        start_ts,
                        viewer_count,
                        viewer_count,
                        viewer_count,
                        float(viewer_count),
                        0,
                        followers_start,
                        title,
                        language,
                        1 if is_mature else 0,
                        tags,
                        game_name,
                        had_deadlock_initial,
                    ),
                )
                session_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
                c.execute(
                    "UPDATE twitch_live_state SET active_session_id = ? WHERE streamer_login = ?",
                    (session_id, login),
                )
        except Exception:
            log.debug("Konnte neue Twitch-Session nicht speichern: %s", login, exc_info=True)
            return None
        if session_id is not None:
            self._get_active_sessions_cache()[login] = session_id
        return session_id

    def _record_session_sample(self, *, login: str, stream: dict) -> None:
        session_id = self._get_active_session_id(login)
        if session_id is None:
            return
        now_dt = datetime.now(timezone.utc)
        viewer_count = int(stream.get("viewer_count") or 0)
        try:
            with storage.get_conn() as c:
                session_row = c.execute(
                    "SELECT started_at, samples, avg_viewers, start_viewers, peak_viewers "
                    "FROM twitch_stream_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session_row:
                    return
                start_dt = self._parse_dt(session_row["started_at"] if hasattr(session_row, "keys") else session_row[0]) or now_dt
                minutes_from_start = int(max(0, (now_dt - start_dt).total_seconds() // 60))
                c.execute(
                    """
                    INSERT OR REPLACE INTO twitch_session_viewers
                        (session_id, ts_utc, minutes_from_start, viewer_count)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        now_dt.isoformat(timespec="seconds"),
                        minutes_from_start,
                        viewer_count,
                    ),
                )
                samples = int(session_row["samples"] if hasattr(session_row, "keys") else session_row[1] or 0)
                avg_prev = float(session_row["avg_viewers"] if hasattr(session_row, "keys") else session_row[2] or 0.0)
                new_samples = samples + 1
                new_avg = ((avg_prev * samples) + viewer_count) / max(1, new_samples)
                start_viewers = int(session_row["start_viewers"] if hasattr(session_row, "keys") else session_row[3] or 0) or viewer_count
                peak_viewers = int(session_row["peak_viewers"] if hasattr(session_row, "keys") else session_row[4] or 0)
                peak_viewers = max(peak_viewers, viewer_count)
                c.execute(
                    """
                    UPDATE twitch_stream_sessions
                       SET samples = ?, avg_viewers = ?, peak_viewers = ?, end_viewers = ?, start_viewers = ?
                     WHERE id = ?
                    """,
                    (
                        new_samples,
                        new_avg,
                        peak_viewers,
                        viewer_count,
                        start_viewers,
                        session_id,
                    ),
                )
        except Exception:
            log.debug("Konnte Session-Sample nicht speichern fuer %s", login, exc_info=True)

    async def _finalize_stream_session(self, *, login: str, reason: str = "done") -> None:
        login_lower = login.lower()
        cache = self._get_active_sessions_cache()
        session_id = cache.pop(login_lower, None) or self._lookup_open_session_id(login_lower)
        if session_id is None:
            return

        now_dt = datetime.now(timezone.utc)
        try:
            with storage.get_conn() as c:
                session_row = c.execute(
                    "SELECT * FROM twitch_stream_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
        except Exception:
            log.debug("Konnte Session nicht laden fuer Abschluss: %s", login, exc_info=True)
            return
        if not session_row:
            return

        def _row_val(row, key, idx, default=None):
            if hasattr(row, "keys"):
                try:
                    return row[key]
                except Exception:
                    return default
            try:
                return row[idx]
            except Exception:
                return default

        started_at_raw = _row_val(session_row, "started_at", 3, None)
        start_dt = self._parse_dt(started_at_raw) or now_dt
        duration_seconds = int(max(0, (now_dt - start_dt).total_seconds()))

        try:
            with storage.get_conn() as c:
                viewer_rows = c.execute(
                    "SELECT minutes_from_start, viewer_count FROM twitch_session_viewers WHERE session_id = ? ORDER BY ts_utc",
                    (session_id,),
                ).fetchall()
        except Exception:
            viewer_rows = []

        def _retention_at(minutes: int, start_viewers: int) -> Optional[float]:
            if not viewer_rows:
                return None
            # Find peak viewer count BEFORE the target minute as baseline
            peak_before = start_viewers
            for row in viewer_rows:
                mins = int(_row_val(row, "minutes_from_start", 0, 0) or 0)
                val = int(_row_val(row, "viewer_count", 1, 0) or 0)
                if mins < minutes:
                    peak_before = max(peak_before, val)
            if peak_before <= 0:
                return None
            # Find closest viewer count AT or AFTER target minute
            best: Optional[tuple[int, int]] = None
            for row in viewer_rows:
                mins = int(_row_val(row, "minutes_from_start", 0, 0) or 0)
                val = int(_row_val(row, "viewer_count", 1, 0) or 0)
                if mins < minutes:
                    continue
                if best is None or mins < best[0]:
                    best = (mins, val)
            # Fallback to last data point if stream ended before target
            if best is None:
                last = viewer_rows[-1]
                best = (
                    int(_row_val(last, "minutes_from_start", 0, 0) or 0),
                    int(_row_val(last, "viewer_count", 1, 0) or 0),
                )
            if best is None:
                return None
            return max(0.0, min(1.0, best[1] / peak_before))

        start_viewers = int(_row_val(session_row, "start_viewers", 6, 0) or 0)
        end_viewers = int(_row_val(session_row, "end_viewers", 8, 0) or 0)
        peak_viewers = int(_row_val(session_row, "peak_viewers", 7, 0) or 0)
        avg_viewers = float(_row_val(session_row, "avg_viewers", 9, 0.0) or 0.0)
        samples = int(_row_val(session_row, "samples", 10, 0) or 0)

        if viewer_rows:
            end_viewers = int(_row_val(viewer_rows[-1], "viewer_count", 1, end_viewers) or end_viewers)
            peak_viewers = max(peak_viewers, *(int(_row_val(vr, "viewer_count", 1, 0) or 0) for vr in viewer_rows))
            samples = max(samples, len(viewer_rows))
            try:
                avg_viewers = sum(int(_row_val(vr, "viewer_count", 1, 0) or 0) for vr in viewer_rows) / max(
                    1, len(viewer_rows)
                )
            except Exception as exc:
                log.debug("Konnte Durchschnitts-Viewerzahl nicht berechnen", exc_info=exc)

        retention_5 = _retention_at(5, start_viewers)
        retention_10 = _retention_at(10, start_viewers)
        retention_20 = _retention_at(20, start_viewers)

        dropoff_pct: Optional[float] = None
        dropoff_label = ""
        prev_val = start_viewers or (viewer_rows[0]["viewer_count"] if viewer_rows else 0)
        for row in viewer_rows:
            current_val = int(_row_val(row, "viewer_count", 1, 0) or 0)
            mins = int(_row_val(row, "minutes_from_start", 0, 0) or 0)
            if prev_val > 0 and current_val < prev_val:
                delta = prev_val - current_val
                pct = delta / prev_val
                if dropoff_pct is None or pct > dropoff_pct:
                    dropoff_pct = pct
                    dropoff_label = f"t={mins}m ({prev_val}->{current_val})"
            prev_val = current_val

        try:
            with storage.get_conn() as c:
                chatter_row = c.execute(
                    """
                    SELECT COUNT(*) AS uniq,
                           SUM(is_first_time_global) AS firsts
                      FROM twitch_session_chatters
                     WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
        except Exception:
            chatter_row = None
        unique_chatters = int(_row_val(chatter_row, "uniq", 0, 0) or 0) if chatter_row else 0
        first_time_chatters = int(_row_val(chatter_row, "firsts", 1, 0) or 0) if chatter_row else 0
        returning_chatters = max(0, unique_chatters - first_time_chatters)

        followers_start = _row_val(session_row, "followers_start", 19, None)

        twitch_user_id: Optional[str] = None
        had_deadlock_state = False
        try:
            with storage.get_conn() as c:
                state_row = c.execute(
                    "SELECT twitch_user_id, last_game, had_deadlock_in_session FROM twitch_live_state WHERE streamer_login = ?",
                    (login_lower,),
                ).fetchone()
            if state_row is not None:
                twitch_user_id = _row_val(state_row, "twitch_user_id", 0, None)
                last_game_value = _row_val(state_row, "last_game", 1, None)
                had_deadlock_state = bool(int(_row_val(state_row, "had_deadlock_in_session", 2, 0) or 0))
            else:
                last_game_value = None
        except Exception:
            last_game_value = None
            twitch_user_id = None
            had_deadlock_state = False

        followers_end = await self._fetch_followers_total_safe(
            twitch_user_id=twitch_user_id,
            login=login_lower,
            stream=None,
        )
        follower_delta = None
        if followers_start is not None and followers_end is not None:
            if int(followers_end) == 0 and int(followers_start) > 0:
                # API returned 0 without user token — treat as missing data
                followers_end = None
                follower_delta = None
            else:
                follower_delta = int(followers_end) - int(followers_start)

        target_game_lower = self._get_target_game_lower()
        last_game_lower = (last_game_value or "").strip().lower() if last_game_value else ""
        had_deadlock_session = had_deadlock_state or (
            bool(target_game_lower) and last_game_lower == target_game_lower
        )

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    UPDATE twitch_stream_sessions
                       SET ended_at = ?,
                           duration_seconds = ?,
                           end_viewers = ?,
                           peak_viewers = ?,
                           avg_viewers = ?,
                           samples = ?,
                           retention_5m = ?,
                           retention_10m = ?,
                           retention_20m = ?,
                           dropoff_pct = ?,
                           dropoff_label = ?,
                           unique_chatters = ?,
                           first_time_chatters = ?,
                           returning_chatters = ?,
                           followers_end = ?,
                           follower_delta = ?,
                           notes = ?,
                           had_deadlock_in_session = ?,
                           game_name = COALESCE(game_name, ?)
                     WHERE id = ?
                    """,
                    (
                        now_dt.isoformat(timespec="seconds"),
                        duration_seconds,
                        end_viewers,
                        peak_viewers,
                        avg_viewers,
                        samples,
                        retention_5,
                        retention_10,
                        retention_20,
                        dropoff_pct,
                        dropoff_label,
                        unique_chatters,
                        first_time_chatters,
                        returning_chatters,
                        followers_end,
                        follower_delta,
                        reason,
                        1 if had_deadlock_session else 0,
                        last_game_value,
                        session_id,
                    ),
                )
                c.execute(
                    "UPDATE twitch_live_state SET active_session_id = NULL WHERE streamer_login = ?",
                    (login_lower,),
                )
        except Exception:
            log.debug("Konnte Session-Abschluss nicht speichern: %s", login_lower, exc_info=True)
        finally:
            cache.pop(login_lower, None)

    async def _fetch_followers_total_safe(
        self,
        *,
        twitch_user_id: Optional[str],
        login: str,
        stream: Optional[dict],
    ) -> Optional[int]:
        if self.api is None:
            return None
        user_id = twitch_user_id
        if not user_id and stream:
            user_id = stream.get("user_id")

        user_token: Optional[str] = None
        try:
            if hasattr(self, "_raid_bot") and self._raid_bot and self.api is not None:
                session = self.api.get_http_session()
                result = await self._raid_bot.auth_manager.get_valid_token_for_login(login, session)
                if result:
                    auth_user_id, token = result
                    user_id = user_id or auth_user_id
                    user_token = token
        except Exception:
            log.debug("Konnte OAuth-Daten fuer Follower-Check nicht laden: %s", login, exc_info=True)

        if not user_id:
            return None
        try:
            return await self.api.get_followers_total(str(user_id), user_token=user_token)
        except Exception:
            log.debug("Follower-Abfrage fehlgeschlagen fuer %s", login, exc_info=True)
            return None

    async def _log_stats(self, streams_by_login: Dict[str, dict], category_streams: List[dict]):
        now_utc = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        try:
            with storage.get_conn() as c:
                for stream in streams_by_login.values():
                    if not self._stream_is_in_target_category(stream):
                        continue
                    login = (stream.get("user_login") or "").lower()
                    viewers = int(stream.get("viewer_count") or 0)
                    is_partner = 1 if stream.get("is_partner") else 0
                    game_name, stream_title, tags = self._normalize_stream_meta(stream)
                    c.execute(
                        """
                        INSERT INTO twitch_stats_tracked (
                            ts_utc, streamer, viewer_count, is_partner, game_name, stream_title, tags
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (now_utc, login, viewers, is_partner, game_name, stream_title, tags),
                    )
        except Exception:
            log.exception("Konnte tracked-Stats nicht loggen")

        try:
            for stream in streams_by_login.values():
                if not self._stream_is_in_target_category(stream):
                    continue
                login = (stream.get("user_login") or "").lower()
                self._record_session_sample(login=login, stream=stream)
        except Exception:
            log.debug("Konnte Session-Metrik nicht loggen", exc_info=True)

        try:
            with storage.get_conn() as c:
                for stream in category_streams:
                    login = (stream.get("user_login") or "").lower()
                    viewers = int(stream.get("viewer_count") or 0)
                    is_partner = 1 if stream.get("is_partner") else 0
                    game_name, stream_title, tags = self._normalize_stream_meta(stream)
                    c.execute(
                        """
                        INSERT INTO twitch_stats_category (
                            ts_utc, streamer, viewer_count, is_partner, game_name, stream_title, tags
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (now_utc, login, viewers, is_partner, game_name, stream_title, tags),
                    )
        except Exception:
            log.exception("Konnte category-Stats nicht loggen")

    async def _get_latest_vod_preview_url(self, *, login: str, twitch_user_id: Optional[str]) -> Optional[str]:
        """Hole das juengste VOD-Thumbnail; faellt bei Fehler still auf None."""
        if self.api is None:
            return None
        try:
            return await self.api.get_latest_vod_thumbnail(user_id=twitch_user_id, login=login)
        except Exception:
            log.exception("Konnte VOD-Thumbnail nicht laden: %s", login)
            return None

    def _build_live_embed(self, login: str, stream: dict) -> discord.Embed:
        """Erzeuge ein Discord-Embed für das Go-Live-Posting mit Stream-Vorschau."""

        display_name = stream.get("user_name") or login
        game = stream.get("game_name") or TWITCH_TARGET_GAME_NAME
        title = stream.get("title") or "Live!"
        viewer_count = int(stream.get("viewer_count") or 0)

        timestamp = datetime.now(tz=timezone.utc)
        started_at_raw = stream.get("started_at")
        if isinstance(started_at_raw, str) and started_at_raw:
            try:
                timestamp = datetime.fromisoformat(started_at_raw.replace("Z", "+00:00"))
            except ValueError as exc:
                log.debug("Ungültiger started_at-Wert '%s': %s", started_at_raw, exc)

        embed = discord.Embed(
            title=f"{display_name} ist LIVE in {game}!",
            description=title,
            colour=discord.Color(TWITCH_BRAND_COLOR_HEX),
            timestamp=timestamp,
        )

        embed.add_field(name="Viewer", value=str(viewer_count), inline=True)
        embed.add_field(name="Kategorie", value=game, inline=True)

        thumbnail_url = (stream.get("thumbnail_url") or "").strip()
        if thumbnail_url:
            thumbnail_url = thumbnail_url.replace("{width}", "1280").replace("{height}", "720")
            cache_bust = int(datetime.now(tz=timezone.utc).timestamp())
            embed.set_image(url=f"{thumbnail_url}?rand={cache_bust}")

        embed.set_footer(text="Auf Twitch ansehen fuer mehr Deadlock-Action!")
        embed.set_author(name=f"LIVE: {display_name}")

        return embed

    def _build_offline_embed(
        self,
        *,
        login: str,
        display_name: str,
        last_title: Optional[str],
        last_game: Optional[str],
        preview_image_url: Optional[str],
    ) -> discord.Embed:
        """Offline-Overlay: gleicher Stil wie live, aber klar als VOD markiert."""

        game = last_game or TWITCH_TARGET_GAME_NAME or "Twitch"
        description = last_title or "Letzten Stream als VOD ansehen."

        embed = discord.Embed(
            title=f"{display_name} ist OFFLINE",
            description=description,
            colour=discord.Color(TWITCH_BRAND_COLOR_HEX),
            timestamp=datetime.now(tz=timezone.utc),
        )

        embed.add_field(name="Status", value="OFFLINE", inline=True)
        embed.add_field(name="Kategorie", value=game, inline=True)
        embed.add_field(name="Hinweis", value="VOD ueber den Button abrufen.", inline=False)

        if preview_image_url:
            embed.set_image(url=preview_image_url)

        embed.set_footer(text="Letzten Stream auf Twitch ansehen.")
        embed.set_author(name=f"OFFLINE: {display_name}")

        return embed

    def _build_offline_link_view(self, referral_url: str, *, label: Optional[str] = None) -> discord.ui.View:
        """Offline-Ansicht: einfacher Link-Button ohne Tracking."""
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label=label or TWITCH_BUTTON_LABEL,
                style=discord.ButtonStyle.link,
                url=referral_url,
            )
        )
        return view

    async def cog_load(self) -> None:
        await super().cog_load()
        spawner = getattr(self, "_spawn_bg_task", None)
        if callable(spawner):
            spawner(self._register_persistent_live_views(), "twitch.register_live_views")
        else:
            asyncio.create_task(self._register_persistent_live_views(), name="twitch.register_live_views")

    def _build_live_view(
        self,
        streamer_login: str,
        referral_url: str,
        tracking_token: str,
    ) -> Optional["_TwitchLiveAnnouncementView"]:
        """Create a persistent view that tracks button clicks before redirecting."""
        if not tracking_token:
            return None
        return _TwitchLiveAnnouncementView(
            cog=self,
            streamer_login=streamer_login,
            referral_url=referral_url,
            tracking_token=tracking_token,
        )

    @staticmethod
    def _generate_tracking_token() -> str:
        return secrets.token_hex(8)

    def _build_referral_url(self, login: str) -> str:
        """Append the configured referral parameter to the Twitch URL."""
        normalized_login = (login or "").strip()
        base_url = f"https://www.twitch.tv/{normalized_login}" if normalized_login else "https://www.twitch.tv/"
        ref_code = (TWITCH_DISCORD_REF_CODE or "").strip()
        if not ref_code:
            return base_url
        parsed = urlparse(base_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["ref"] = ref_code
        encoded = urlencode(query)
        return urlunparse(parsed._replace(query=encoded))

    async def _register_persistent_live_views(self) -> None:
        """Re-register live announcement views after a restart."""
        if not self._notify_channel_id:
            return
        try:
            await self.bot.wait_until_ready()
        except Exception:
            log.exception("wait_until_ready für Twitch-Views fehlgeschlagen")
            return

        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT streamer_login, last_discord_message_id, last_tracking_token "
                    "FROM twitch_live_state "
                    "WHERE last_discord_message_id IS NOT NULL AND last_tracking_token IS NOT NULL"
                ).fetchall()
        except Exception:
            log.exception("Konnte persistente Twitch-Views nicht registrieren")
            return

        for row in rows:
            login = (row["streamer_login"] or "").strip()
            token = (row["last_tracking_token"] or "").strip()
            message_id_raw = row["last_discord_message_id"]
            if not login or not token or not message_id_raw:
                continue
            try:
                message_id = int(message_id_raw)
            except (TypeError, ValueError):
                continue
            referral_url = self._build_referral_url(login)
            view = self._build_live_view(login, referral_url, token)
            if view is None:
                continue
            view.bind_to_message(channel_id=self._notify_channel_id, message_id=message_id)
            self._register_live_view(tracking_token=token, view=view, message_id=message_id)

    def _get_live_view_registry(self) -> Dict[str, "_TwitchLiveAnnouncementView"]:
        registry = getattr(self, "_live_view_registry", None)
        if registry is None:
            registry = {}
            setattr(self, "_live_view_registry", registry)
        return registry

    def _register_live_view(
        self,
        *,
        tracking_token: str,
        view: "_TwitchLiveAnnouncementView",
        message_id: int,
    ) -> None:
        if not tracking_token:
            return
        registry = self._get_live_view_registry()
        registry[tracking_token] = view
        try:
            self.bot.add_view(view, message_id=message_id)
        except Exception:
            log.exception("Konnte View für Twitch-Posting %s nicht registrieren", message_id)

    def _drop_live_view(self, tracking_token: Optional[str]) -> None:
        if not tracking_token:
            return
        registry = self._get_live_view_registry()
        view = registry.pop(tracking_token, None)
        if view is None:
            return
        
        # discord.py hat kein natives remove_view am Bot-Objekt. 
        # view.stop() reicht aus, um die Interaktionen zu beenden.
        view.stop()
        log.debug("Live-View gestoppt und aus Registry entfernt: %s", tracking_token)

    def _log_link_click(
        self,
        *,
        interaction: discord.Interaction,
        view: "_TwitchLiveAnnouncementView",
    ) -> None:
        clicked_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        user = interaction.user
        user_id = str(getattr(user, "id", "") or "") or None
        username = str(user) if user else None
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        channel_source = interaction.channel_id or view.channel_id
        channel_id = str(channel_source) if channel_source else None
        if interaction.message and interaction.message.id:
            message_id = str(interaction.message.id)
        elif view.message_id:
            message_id = str(view.message_id)
        else:
            message_id = None
        ref_code = (TWITCH_DISCORD_REF_CODE or "").strip() or None

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_link_clicks (
                        clicked_at,
                        streamer_login,
                        tracking_token,
                        discord_user_id,
                        discord_username,
                        guild_id,
                        channel_id,
                        message_id,
                        ref_code,
                        source_hint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clicked_at,
                        view.streamer_login.lower(),
                        view.tracking_token,
                        user_id,
                        username,
                        guild_id,
                        channel_id,
                        message_id,
                        ref_code,
                        "live_button",
                    ),
                )
        except Exception:
            log.exception("Konnte Twitch-Link-Klick nicht speichern")

    async def _handle_tracked_button_click(
        self,
        interaction: discord.Interaction,
        view: "_TwitchLiveAnnouncementView",
    ) -> None:
        try:
            self._log_link_click(interaction=interaction, view=view)
        except Exception:
            log.exception("Konnte Klick nicht loggen")

        content = f"Hier ist dein Twitch-Link für **{view.streamer_login}**."
        response_view = _TwitchReferralLinkView(view.referral_url)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, view=response_view, ephemeral=True)
            else:
                await interaction.response.send_message(content, view=response_view, ephemeral=True)
        except Exception:
            log.exception("Antwort mit Referral-Link fehlgeschlagen")


class _TwitchReferralLinkView(discord.ui.View):
    """Ephemeral view with a direct Twitch hyperlink."""

    def __init__(self, referral_url: str):
        super().__init__(timeout=60)
        self.add_item(
            discord.ui.Button(
                label=TWITCH_BUTTON_LABEL,
                style=discord.ButtonStyle.link,
                url=referral_url,
            )
        )


class _TrackedTwitchButton(discord.ui.Button):
    def __init__(self, parent: "_TwitchLiveAnnouncementView", *, custom_id: str):
        super().__init__(label=TWITCH_BUTTON_LABEL, style=discord.ButtonStyle.primary, custom_id=custom_id)
        self._view_ref = parent  # Renamed from _parent to avoid discord.py conflict

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self._view_ref.handle_click(interaction)


class _TwitchLiveAnnouncementView(discord.ui.View):
    """Persistent live announcement view that tracks clicks before redirecting."""

    def __init__(
        self,
        *,
        cog: TwitchMonitoringMixin,
        streamer_login: str,
        referral_url: str,
        tracking_token: str,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.streamer_login = streamer_login
        self.referral_url = referral_url
        self.tracking_token = tracking_token
        self.message_id: Optional[int] = None
        self.channel_id: Optional[int] = None

        custom_id = self._build_custom_id(streamer_login, tracking_token)
        self.add_item(_TrackedTwitchButton(self, custom_id=custom_id))

    @staticmethod
    def _build_custom_id(streamer_login: str, tracking_token: str) -> str:
        login_part = "".join(ch for ch in streamer_login.lower() if ch.isalnum())[:24] or "stream"
        token_part = (tracking_token or "")[:32] or secrets.token_hex(4)
        return f"twitch-live:{login_part}:{token_part}"

    def bind_to_message(self, *, channel_id: Optional[int], message_id: Optional[int]) -> None:
        self.channel_id = channel_id
        self.message_id = message_id

    async def handle_click(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_tracked_button_click(interaction, self)
