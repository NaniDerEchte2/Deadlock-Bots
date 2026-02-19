"""Leaderboard dataclasses, views and commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import discord
from discord.ext import commands

from .. import storage
from ..constants import TWITCH_STATS_CHANNEL_IDS
from ..constants import log


@dataclass
class LeaderboardOptions:
    min_samples: Optional[int] = None
    min_avg: Optional[float] = None
    partner_filter: str = "any"
    limit: int = 5
    sort_key: str = "avg"
    sort_order: str = "desc"

    _SORT_LABELS = {
        "avg": "Ø Viewer",
        "samples": "Stichproben",
        "peak": "Peak",
        "name": "Name",
    }
    _PARTNER_LABELS = {
        "any": "Alle",
        "only": "Nur Partner",
        "exclude": "Ohne Partner",
    }
    _SAMPLES_STEPS: Tuple[Optional[int], ...] = (None, 5, 10, 15, 20, 30, 40, 50)
    _AVG_STEPS: Tuple[Optional[float], ...] = (
        None,
        5.0,
        10.0,
        15.0,
        20.0,
        25.0,
        30.0,
        40.0,
        50.0,
        75.0,
        100.0,
    )
    _LIMIT_STEPS: Tuple[int, ...] = (5, 10, 15, 20)

    def clone(self) -> "LeaderboardOptions":
        return LeaderboardOptions(
            min_samples=self.min_samples,
            min_avg=self.min_avg,
            partner_filter=self.partner_filter,
            limit=self.limit,
            sort_key=self.sort_key,
            sort_order=self.sort_order,
        )

    @property
    def sort_label(self) -> str:
        return self._SORT_LABELS.get(self.sort_key, "Ø Viewer")

    @property
    def order_label(self) -> str:
        return "aufsteigend" if self.sort_order == "asc" else "absteigend"

    @property
    def partner_label(self) -> str:
        return self._PARTNER_LABELS.get(self.partner_filter, "Alle")

    @property
    def samples_label(self) -> str:
        if self.min_samples is None:
            return "keine"
        return f"≥ {self.min_samples}"

    @property
    def avg_label(self) -> str:
        if self.min_avg is None:
            return "keine"
        return f"≥ {self.min_avg:.0f}"

    @property
    def limit_label(self) -> str:
        return f"Top {self.limit}"

    def filter_summary(self) -> List[str]:
        parts: List[str] = []
        if self.min_samples is not None:
            parts.append(f"Stichproben ≥ {self.min_samples}")
        if self.min_avg is not None:
            parts.append(f"Ø Viewer ≥ {self.min_avg:.1f}")
        if self.partner_filter == "only":
            parts.append("nur Partner")
        elif self.partner_filter == "exclude":
            parts.append("ohne Partner")
        if not parts:
            parts.append("keine Filter")
        return parts

    def sort_summary(self) -> str:
        return f"Sortierung: {self.sort_label} {self.order_label}"

    def cycle_sort_key(self) -> None:
        order = ("avg", "samples", "peak", "name")
        idx = order.index(self.sort_key) if self.sort_key in order else 0
        self.sort_key = order[(idx + 1) % len(order)]

    def toggle_sort_order(self) -> None:
        self.sort_order = "asc" if self.sort_order == "desc" else "desc"

    def cycle_partner_filter(self) -> None:
        order = ("any", "only", "exclude")
        idx = order.index(self.partner_filter) if self.partner_filter in order else 0
        self.partner_filter = order[(idx + 1) % len(order)]

    def cycle_min_samples(self) -> None:
        idx = self._SAMPLES_STEPS.index(self.min_samples) if self.min_samples in self._SAMPLES_STEPS else 0
        self.min_samples = self._SAMPLES_STEPS[(idx + 1) % len(self._SAMPLES_STEPS)]

    def cycle_min_avg(self) -> None:
        idx = self._AVG_STEPS.index(self.min_avg) if self.min_avg in self._AVG_STEPS else 0
        self.min_avg = self._AVG_STEPS[(idx + 1) % len(self._AVG_STEPS)]

    def cycle_limit(self) -> None:
        idx = self._LIMIT_STEPS.index(self.limit) if self.limit in self._LIMIT_STEPS else 0
        self.limit = self._LIMIT_STEPS[(idx + 1) % len(self._LIMIT_STEPS)]

    def reset(self) -> None:
        self.min_samples = None
        self.min_avg = None
        self.partner_filter = "any"
        self.limit = 5
        self.sort_key = "avg"
        self.sort_order = "desc"

    def clamp(self) -> None:
        if self.limit < 1:
            self.limit = 1
        elif self.limit > 20:
            self.limit = 20


class TwitchLeaderboardView(discord.ui.View):
    """Interaktive Ansicht für das Twitch-Leaderboard."""

    def __init__(
        self,
        cog,
        ctx: commands.Context,
        tracked_items: Sequence[dict],
        category_items: Sequence[dict],
        options: LeaderboardOptions,
    ):
        super().__init__(timeout=None)
        self._cog = cog
        self._ctx = ctx
        self._author_id = ctx.author.id
        self._tracked_items = list(tracked_items)
        self._category_items = list(category_items)
        self._options = options.clone()
        self._message: Optional[discord.Message] = None
        self._refresh_labels()

    async def send_initial(self) -> discord.Message:
        embed = self._cog._build_leaderboard_embed(
            self._tracked_items,
            self._category_items,
            self._options,
        )
        message = await self._ctx.reply(embed=embed, view=self, mention_author=False)
        self._message = message
        return message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            try:
                await interaction.response.send_message(
                    "Nur der ursprüngliche Aufrufer kann diese Steuerung verwenden.",
                    ephemeral=True,
                )
            except Exception as exc:
                log.debug(
                    "Konnte Hinweis für fremde Leaderboard-Interaktion nicht senden (user_id=%s): %s",
                    interaction.user.id,
                    exc,
                )
            return False
        return True

    async def update(
        self,
        interaction: discord.Interaction,
        *,
        refresh_stats: bool = False,
    ) -> None:
        if refresh_stats:
            try:
                stats = await self._cog._compute_stats()
            except Exception:
                log.exception("Konnte Leaderboard für Refresh nicht laden")
                await interaction.response.send_message(
                    "Konnte Daten nicht neu laden.",
                    ephemeral=True,
                )
                return
            self._tracked_items = stats.get("tracked", {}).get("top", []) or []
            self._category_items = stats.get("category", {}).get("top", []) or []

        embed = self._cog._build_leaderboard_embed(
            self._tracked_items,
            self._category_items,
            self._options,
        )
        self._refresh_labels()

        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
            if interaction.message is not None:
                self._message = interaction.message
        except Exception:
            log.exception("Konnte Leaderboard-Interaktion nicht aktualisieren")

    def _refresh_labels(self) -> None:
        arrow = "⬆️" if self._options.sort_order == "asc" else "⬇️"
        self.sort_button.label = f"Sortierung: {self._options.sort_label}"
        self.order_button.label = f"Reihenfolge: {arrow}"
        self.partner_button.label = f"Partner: {self._options.partner_label}"
        self.samples_button.label = f"Stichproben: {self._options.samples_label}"
        self.avg_button.label = f"Ø Viewer: {self._options.avg_label}"
        self.limit_button.label = f"Limit: {self._options.limit_label}"

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self._disable_all()
        if self._message is not None:
            try:
                await self._message.edit(view=self)
            except Exception as exc:
                log.debug("Konnte Leaderboard-Timeout-Nachricht nicht aktualisieren: %s", exc)

    @discord.ui.button(
        label="Sortierung",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="twl:sort",
    )
    async def sort_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.cycle_sort_key()
        await self.update(interaction)

    @discord.ui.button(
        label="Reihenfolge",
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id="twl:order",
    )
    async def order_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.toggle_sort_order()
        await self.update(interaction)

    @discord.ui.button(
        label="Partner",
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id="twl:partner",
    )
    async def partner_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.cycle_partner_filter()
        await self.update(interaction)

    @discord.ui.button(
        label="Stichproben",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="twl:samples",
    )
    async def samples_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.cycle_min_samples()
        await self.update(interaction)

    @discord.ui.button(
        label="Ø Viewer",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="twl:avg",
    )
    async def avg_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.cycle_min_avg()
        await self.update(interaction)

    @discord.ui.button(
        label="Limit",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="twl:limit",
    )
    async def limit_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.cycle_limit()
        await self.update(interaction)

    @discord.ui.button(
        label="Neu laden",
        style=discord.ButtonStyle.success,
        row=2,
        custom_id="twl:refresh",
    )
    async def refresh_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.update(interaction, refresh_stats=True)

    @discord.ui.button(
        label="Zurücksetzen",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="twl:reset",
    )
    async def reset_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.reset()
        await self.update(interaction)

    @discord.ui.button(
        label="Schließen",
        style=discord.ButtonStyle.danger,
        row=2,
        custom_id="twl:close",
    )
    async def close_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._disable_all()
        self.stop()
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(view=self)
            else:
                await interaction.response.edit_message(view=self)
            if interaction.message is not None:
                self._message = interaction.message
        except Exception:
            log.exception("Konnte Leaderboard schließen nicht anwenden")


class TwitchLeaderboardMixin:
    """User-facing logic used by the !twl proxy command."""

    async def twitch_leaderboard(
        self,
        ctx: Optional[commands.Context] = None,
        *maybe_filters: Any,
        filters: str = "",
    ):
        extra_parts: List[str] = []

        if ctx is not None and not isinstance(ctx, commands.Context):
            extra_parts.append(str(ctx))
            ctx = None

        remaining = list(maybe_filters)
        if ctx is None and remaining and isinstance(remaining[0], commands.Context):
            ctx = remaining.pop(0)

        for part in remaining:
            if isinstance(part, str):
                extra_parts.append(part)
            elif part is not None:
                extra_parts.append(str(part))

        filter_text = " ".join(extra_parts).strip()
        if filters.strip():
            filter_text = f"{filter_text} {filters.strip()}".strip()

        if ctx is None:
            log.error("twitch_leaderboard invoked ohne discord Context; aborting call")
            return

        allowed_ids = {int(x) for x in TWITCH_STATS_CHANNEL_IDS}

        if ctx.channel.id not in allowed_ids:
            mentions = []
            for channel_id in allowed_ids:
                channel = ctx.bot.get_channel(channel_id)
                mentions.append(channel.mention if channel else f"<#{channel_id}>")
            erlaubt = ", ".join(mentions)
            await ctx.reply(f"Dieser Befehl kann nur in {erlaubt} verwendet werden.")
            return

        if filter_text.lower() in {"help", "?", "hilfe"}:
            help_text = (
                "Verwendung: !twl [samples=Zahl] [avg=Zahl] [partner=only|exclude|any] [limit=Zahl] [sort=avg|samples|peak|name] [order=asc|desc]\n"
                "Beispiel: !twl samples=15 avg=25 partner=only sort=avg order=desc"
            )
            await ctx.reply(help_text)
            return

        min_samples: Optional[int] = None
        min_avg: Optional[float] = None
        partner_filter = "any"
        limit = 5
        sort_key = "avg"
        sort_order = "desc"

        for token in filter_text.split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            key = key.lower().strip()
            value = value.strip()
            if key in {"samples", "min_samples"}:
                try:
                    parsed = max(0, int(value))
                except ValueError:
                    continue
                min_samples = parsed
            elif key in {"avg", "min_avg", "avg_viewers"}:
                try:
                    parsed_avg = max(0.0, float(value))
                except ValueError:
                    continue
                min_avg = parsed_avg
            elif key == "partner":
                lowered = value.lower()
                if lowered in {"only", "exclude", "any", "all"}:
                    partner_filter = "any" if lowered in {"any", "all"} else lowered
            elif key == "limit":
                try:
                    limit_val = max(1, min(20, int(value)))
                except ValueError:
                    continue
                limit = limit_val
            elif key == "sort":
                lowered = value.lower()
                if lowered in {"avg", "samples", "peak", "name"}:
                    sort_key = lowered
            elif key in {"order", "direction"}:
                lowered = value.lower()
                if lowered in {"asc", "desc"}:
                    sort_order = lowered

        try:
            stats = await self._compute_stats()
        except Exception as exc:
            log.exception("!twl stats fetch failed: %s", exc)
            await ctx.reply("Konnte Statistiken nicht laden.")
            return

        tracked_items = stats.get("tracked", {}).get("top", [])
        category_items = stats.get("category", {}).get("top", [])

        options = LeaderboardOptions(
            min_samples=min_samples,
            min_avg=min_avg,
            partner_filter=partner_filter,
            limit=limit,
            sort_key=sort_key,
            sort_order=sort_order,
        )
        options.clamp()

        view = TwitchLeaderboardView(self, ctx, tracked_items, category_items, options)
        await view.send_initial()

    async def _compute_stats(
        self,
        *,
        hour_from: Optional[int] = None,
        hour_to: Optional[int] = None,
        streamer: Optional[str] = None,
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {"tracked": {}, "category": {}}

        tracked_logins: set[str] = set()
        verified_logins: set[str] = set()
        discord_lookup: Dict[str, Dict[str, Any]] = {}

        def _parse_db_datetime(value: Any) -> Optional[datetime]:
            if not value:
                return None
            if isinstance(value, datetime):
                dt = value
            else:
                try:
                    dt = datetime.fromisoformat(str(value))
                except ValueError:
                    return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        now_utc = datetime.now(timezone.utc)
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    """
                    SELECT twitch_login,
                           is_on_discord,
                           discord_user_id,
                           discord_display_name,
                           manual_verified_permanent,
                           manual_verified_until
                      FROM twitch_streamers
                    """
                ).fetchall()
            for row in rows:
                if isinstance(row, tuple):
                    columns = (
                        "twitch_login",
                        "is_on_discord",
                        "discord_user_id",
                        "discord_display_name",
                        "manual_verified_permanent",
                        "manual_verified_until",
                    )
                    row_data = {
                        key: row[idx] if idx < len(row) else None for idx, key in enumerate(columns)
                    }
                elif hasattr(row, "keys"):
                    row_data = {key: row[key] for key in row.keys()}
                else:
                    row_data = dict(row)

                login = str(row_data.get("twitch_login") or "").strip().lower()
                if not login:
                    continue

                tracked_logins.add(login)

                is_on_discord_raw = row_data.get("is_on_discord", 0)
                discord_id_raw = row_data.get("discord_user_id")
                discord_name_raw = row_data.get("discord_display_name")

                is_verified = False
                try:
                    if row_data.get("manual_verified_permanent"):
                        is_verified = True
                    else:
                        until_dt = _parse_db_datetime(row_data.get("manual_verified_until"))
                        if until_dt and until_dt >= now_utc:
                            is_verified = True
                except Exception:
                    log.debug("Konnte Verifizierungsstatus für %s nicht lesen", login, exc_info=True)

                if is_verified:
                    verified_logins.add(login)

                discord_lookup[login] = {
                    "is_on_discord": int(is_on_discord_raw or 0),
                    "discord_user_id": discord_id_raw,
                    "discord_display_name": discord_name_raw,
                }
        except Exception:
            log.exception("Konnte gespeicherte Twitch-Logins nicht laden")
            tracked_logins = set()
            verified_logins = set()
            discord_lookup = {}

        def _normalize_hour(value: Optional[int]) -> Optional[int]:
            if value is None:
                return None
            if value < 0:
                return 0
            if value > 23:
                return 23
            return value

        start_hour = _normalize_hour(hour_from)
        end_hour = _normalize_hour(hour_to)
        if start_hour is None and end_hour is None:
            hour_filter_mode = "none"
            hour_start = 0
            hour_end = 0
        else:
            if start_hour is None:
                start_hour = end_hour
            if end_hour is None:
                end_hour = start_hour
            assert start_hour is not None
            assert end_hour is not None
            if start_hour <= end_hour:
                hour_filter_mode = "between"
            else:
                hour_filter_mode = "wrap"
            hour_start = start_hour
            hour_end = end_hour

        hour_filter_params: Tuple[Any, ...] = (
            hour_filter_mode,
            hour_filter_mode,
            hour_start,
            hour_end,
            hour_filter_mode,
            hour_start,
            hour_end,
        )

        def _aggregate(sql: str, params: Sequence[object]) -> List[dict]:
            try:
                with storage.get_conn() as c:
                    rows = c.execute(sql, tuple(params)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                log.exception("Fehler bei Stats-Aggregation")
                return []

        top_sql = """
        WITH source_rows AS (
            SELECT 'tracked' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_tracked
            UNION ALL
            SELECT 'category' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_category
        )
        SELECT streamer,
               AVG(viewer_count) AS avg_viewers,
               MAX(viewer_count) AS max_viewers,
               COUNT(*)          AS samples,
               MAX(is_partner)   AS is_partner
          FROM source_rows
         WHERE source_key = ?
           AND ts_utc >= datetime('now', '-30 days')
           AND (
                (source_key = 'tracked' AND LOWER(streamer) IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
                OR
                (source_key = 'category' AND LOWER(streamer) NOT IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
           )
           AND (
                ? = 'none'
                OR (? = 'between' AND CAST(strftime('%H', ts_utc) AS INTEGER) BETWEEN ? AND ?)
                OR (? = 'wrap' AND (
                        CAST(strftime('%H', ts_utc) AS INTEGER) >= ?
                        OR CAST(strftime('%H', ts_utc) AS INTEGER) <= ?
                    ))
           )
         GROUP BY streamer
         ORDER BY avg_viewers DESC
        """
        hourly_sql = """
        WITH source_rows AS (
            SELECT 'tracked' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_tracked
            UNION ALL
            SELECT 'category' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_category
        )
        SELECT CAST(strftime('%H', ts_utc) AS INTEGER) AS hour,
               AVG(viewer_count) AS avg_viewers,
               MAX(viewer_count) AS max_viewers,
               COUNT(*)          AS samples
          FROM source_rows
         WHERE source_key = ?
           AND ts_utc >= datetime('now', '-30 days')
           AND (
                (source_key = 'tracked' AND LOWER(streamer) IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
                OR
                (source_key = 'category' AND LOWER(streamer) NOT IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
           )
           AND (
                ? = 'none'
                OR (? = 'between' AND CAST(strftime('%H', ts_utc) AS INTEGER) BETWEEN ? AND ?)
                OR (? = 'wrap' AND (
                        CAST(strftime('%H', ts_utc) AS INTEGER) >= ?
                        OR CAST(strftime('%H', ts_utc) AS INTEGER) <= ?
                    ))
           )
         GROUP BY hour
         ORDER BY hour
        """
        weekday_sql = """
        WITH source_rows AS (
            SELECT 'tracked' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_tracked
            UNION ALL
            SELECT 'category' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_category
        )
        SELECT CAST(strftime('%w', ts_utc) AS INTEGER) AS weekday,
               AVG(viewer_count) AS avg_viewers,
               MAX(viewer_count) AS max_viewers,
               COUNT(*)          AS samples
          FROM source_rows
         WHERE source_key = ?
           AND ts_utc >= datetime('now', '-30 days')
           AND (
                (source_key = 'tracked' AND LOWER(streamer) IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
                OR
                (source_key = 'category' AND LOWER(streamer) NOT IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
           )
           AND (
                ? = 'none'
                OR (? = 'between' AND CAST(strftime('%H', ts_utc) AS INTEGER) BETWEEN ? AND ?)
                OR (? = 'wrap' AND (
                        CAST(strftime('%H', ts_utc) AS INTEGER) >= ?
                        OR CAST(strftime('%H', ts_utc) AS INTEGER) <= ?
                    ))
           )
         GROUP BY weekday
         ORDER BY weekday
        """
        user_top_sql = """
        WITH source_rows AS (
            SELECT 'tracked' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_tracked
            UNION ALL
            SELECT 'category' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_category
        )
        SELECT streamer,
               AVG(viewer_count) AS avg_viewers,
               MAX(viewer_count) AS max_viewers,
               COUNT(*)          AS samples,
               MAX(is_partner)   AS is_partner
          FROM source_rows
         WHERE source_key = ?
           AND ts_utc >= datetime('now', '-30 days')
           AND (
                (source_key = 'tracked' AND LOWER(streamer) IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
                OR
                (source_key = 'category' AND LOWER(streamer) NOT IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
           )
           AND streamer = ?
           AND (
                ? = 'none'
                OR (? = 'between' AND CAST(strftime('%H', ts_utc) AS INTEGER) BETWEEN ? AND ?)
                OR (? = 'wrap' AND (
                        CAST(strftime('%H', ts_utc) AS INTEGER) >= ?
                        OR CAST(strftime('%H', ts_utc) AS INTEGER) <= ?
                    ))
           )
         GROUP BY streamer
        """
        user_hourly_sql = """
        WITH source_rows AS (
            SELECT 'tracked' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_tracked
            UNION ALL
            SELECT 'category' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_category
        )
        SELECT CAST(strftime('%H', ts_utc) AS INTEGER) AS hour,
               AVG(viewer_count) AS avg_viewers,
               MAX(viewer_count) AS max_viewers,
               COUNT(*)          AS samples
          FROM source_rows
         WHERE source_key = ?
           AND ts_utc >= datetime('now', '-30 days')
           AND (
                (source_key = 'tracked' AND LOWER(streamer) IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
                OR
                (source_key = 'category' AND LOWER(streamer) NOT IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
           )
           AND streamer = ?
           AND (
                ? = 'none'
                OR (? = 'between' AND CAST(strftime('%H', ts_utc) AS INTEGER) BETWEEN ? AND ?)
                OR (? = 'wrap' AND (
                        CAST(strftime('%H', ts_utc) AS INTEGER) >= ?
                        OR CAST(strftime('%H', ts_utc) AS INTEGER) <= ?
                    ))
           )
         GROUP BY hour
         ORDER BY hour
        """
        user_weekday_sql = """
        WITH source_rows AS (
            SELECT 'tracked' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_tracked
            UNION ALL
            SELECT 'category' AS source_key, streamer, viewer_count, is_partner, ts_utc
            FROM twitch_stats_category
        )
        SELECT CAST(strftime('%w', ts_utc) AS INTEGER) AS weekday,
               AVG(viewer_count) AS avg_viewers,
               MAX(viewer_count) AS max_viewers,
               COUNT(*)          AS samples
          FROM source_rows
         WHERE source_key = ?
           AND ts_utc >= datetime('now', '-30 days')
           AND (
                (source_key = 'tracked' AND LOWER(streamer) IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
                OR
                (source_key = 'category' AND LOWER(streamer) NOT IN (
                    SELECT LOWER(twitch_login)
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                ))
           )
           AND streamer = ?
           AND (
                ? = 'none'
                OR (? = 'between' AND CAST(strftime('%H', ts_utc) AS INTEGER) BETWEEN ? AND ?)
                OR (? = 'wrap' AND (
                        CAST(strftime('%H', ts_utc) AS INTEGER) >= ?
                        OR CAST(strftime('%H', ts_utc) AS INTEGER) <= ?
                    ))
           )
         GROUP BY weekday
         ORDER BY weekday
        """

        def _apply_partner_flag(items: List[dict]) -> List[dict]:
            if not tracked_logins:
                return items
            for item in items:
                login = str(item.get("streamer") or "").strip().lower()
                if not login:
                    continue
                if login in tracked_logins:
                    item["is_partner"] = 1 if login in verified_logins else 0
            return items

        def _apply_discord_info(
            items: List[dict], *, assume_members: Optional[set[str]] = None
        ) -> List[dict]:
            assumed_members = assume_members or set()
            for item in items:
                login = str(item.get("streamer") or "").strip().lower()
                info = discord_lookup.get(login, {})
                discord_user_id = info.get("discord_user_id") if info else None
                discord_display_name = info.get("discord_display_name") if info else None
                has_profile = bool(
                    (discord_user_id and str(discord_user_id).strip())
                    or (discord_display_name and str(discord_display_name).strip())
                )
                default_member = login in assumed_members
                is_member = default_member or bool(info.get("is_on_discord")) or has_profile
                item["is_on_discord"] = 1 if is_member else 0
                item["discord_user_id"] = discord_user_id
                item["discord_display_name"] = discord_display_name
                item["has_discord_profile"] = 1 if has_profile else 0
            return items

        out["tracked"]["top"] = _apply_discord_info(
            _apply_partner_flag(_aggregate(top_sql, ("tracked", *hour_filter_params))),
            assume_members=verified_logins,
        )
        out["category"]["top"] = _apply_discord_info(
            _apply_partner_flag(_aggregate(top_sql, ("category", *hour_filter_params))),
            assume_members=verified_logins,
        )

        out["tracked"]["hourly"] = _aggregate(hourly_sql, ("tracked", *hour_filter_params))
        out["category"]["hourly"] = _aggregate(hourly_sql, ("category", *hour_filter_params))
        out["tracked"]["weekday"] = _aggregate(weekday_sql, ("tracked", *hour_filter_params))
        out["category"]["weekday"] = _aggregate(weekday_sql, ("category", *hour_filter_params))

        if streamer is not None:
            normalized_login = self._normalize_login(streamer)
            if not normalized_login:
                normalized_login = (streamer or "").strip().lower()
            normalized_login = normalized_login.strip()

            user_entry: Dict[str, Any] = {
                "login": normalized_login,
                "display_login": normalized_login,
                "summary": {},
                "hourly": [],
                "weekday": [],
                "source": None,
                "had_results": False,
                "is_tracked": normalized_login in tracked_logins if normalized_login else False,
                "discord_user_id": None,
                "discord_display_name": None,
                "is_on_discord": 0,
            }

            if normalized_login:
                discord_info = discord_lookup.get(normalized_login, {})
                discord_user_id = discord_info.get("discord_user_id")
                discord_display_name = discord_info.get("discord_display_name")
                has_profile = bool(
                    (discord_user_id and str(discord_user_id).strip())
                    or (discord_display_name and str(discord_display_name).strip())
                )
                default_member = normalized_login in verified_logins
                is_member = default_member or bool(discord_info.get("is_on_discord")) or has_profile
                user_entry["discord_user_id"] = discord_user_id
                user_entry["discord_display_name"] = discord_display_name
                user_entry["is_on_discord"] = 1 if is_member else 0

                try:
                    with storage.get_conn() as c:
                        # Subs
                        sub_row = c.execute(
                            "SELECT total, points, snapshot_at FROM twitch_subscriptions_snapshot WHERE twitch_login = ? ORDER BY snapshot_at DESC LIMIT 1",
                            (normalized_login,)
                        ).fetchone()
                        if sub_row:
                            user_entry["subs"] = {
                                "total": sub_row[0],
                                "points": sub_row[1],
                                "updated_at": sub_row[2]
                            }
                        
                        # Shared Audience
                        shared_rows = c.execute(
                            """
                            SELECT other.streamer_login, COUNT(DISTINCT t1.chatter_login) as overlap
                            FROM twitch_chatter_rollup t1
                            JOIN twitch_chatter_rollup other ON t1.chatter_login = other.chatter_login
                            WHERE t1.streamer_login = ? 
                              AND other.streamer_login != ?
                              AND t1.last_seen_at >= datetime('now', '-30 days')
                            GROUP BY other.streamer_login
                            ORDER BY overlap DESC
                            LIMIT 10
                            """,
                            (normalized_login, normalized_login)
                        ).fetchall()
                        user_entry["shared_audience"] = [
                            {"streamer": r[0], "overlap": r[1]} for r in shared_rows
                        ]
                except Exception:
                    log.exception("Failed to fetch extended user stats (subs/shared)")

                sources = ("tracked", "category")
                user_payload: Optional[Dict[str, Any]] = None
                for source_key in sources:
                    params = [source_key, normalized_login, *hour_filter_params]
                    summary_rows = _aggregate(user_top_sql, params)
                    if not summary_rows:
                        continue
                    summary_row = dict(summary_rows[0])
                    samples = int(summary_row.get("samples") or 0)
                    if samples <= 0:
                        continue
                    user_payload = {
                        "summary": summary_row,
                        "hourly": _aggregate(user_hourly_sql, params),
                        "weekday": _aggregate(user_weekday_sql, params),
                        "source": source_key,
                    }
                    break

                if user_payload:
                    summary = user_payload["summary"]
                    user_entry["summary"] = summary
                    user_entry["hourly"] = user_payload["hourly"]
                    user_entry["weekday"] = user_payload["weekday"]
                    user_entry["source"] = user_payload["source"]
                    user_entry["display_login"] = str(summary.get("streamer") or normalized_login)
                    user_entry["had_results"] = True
            out["streamer"] = user_entry

        try:
            with storage.get_conn() as c:
                session_rows = c.execute(
                    """
                    SELECT id, streamer_login, started_at, duration_seconds, start_viewers, peak_viewers,
                           end_viewers, avg_viewers, samples, retention_5m, retention_10m, retention_20m,
                           dropoff_pct, dropoff_label, unique_chatters, first_time_chatters,
                           returning_chatters, follower_delta, followers_start, followers_end,
                           stream_title, notification_text
                      FROM twitch_stream_sessions
                     WHERE started_at >= datetime('now', '-30 days')
                       AND ended_at IS NOT NULL
                     ORDER BY started_at DESC
                     LIMIT 400
                    """
                ).fetchall()

                chat_peak_rows = c.execute(
                    """
                    SELECT cm.session_id,
                           s.streamer_login,
                           s.started_at,
                           strftime('%Y-%m-%dT%H:%M:00Z', cm.message_ts) AS minute_bucket,
                           COUNT(*) AS messages
                      FROM twitch_chat_messages cm
                      JOIN twitch_stream_sessions s ON s.id = cm.session_id
                     WHERE cm.message_ts >= datetime('now', '-30 days')
                     GROUP BY cm.session_id, minute_bucket
                     ORDER BY messages DESC
                     LIMIT 5
                    """
                ).fetchall()

                active_7_row = c.execute(
                    "SELECT COUNT(*) FROM twitch_chatter_rollup WHERE last_seen_at >= datetime('now','-7 days')"
                ).fetchone()
                returning_7_row = c.execute(
                    """
                    SELECT COUNT(*)
                      FROM twitch_chatter_rollup
                     WHERE first_seen_at < datetime('now','-7 days')
                       AND last_seen_at >= datetime('now','-7 days')
                    """
                ).fetchone()
                active_30_row = c.execute(
                    "SELECT COUNT(*) FROM twitch_chatter_rollup WHERE last_seen_at >= datetime('now','-30 days')"
                ).fetchone()
                returning_30_row = c.execute(
                    """
                    SELECT COUNT(*)
                      FROM twitch_chatter_rollup
                     WHERE first_seen_at < datetime('now','-30 days')
                       AND last_seen_at >= datetime('now','-30 days')
                    """
                ).fetchone()
        except Exception:
            log.debug("Konnte erweiterte Twitch-Metriken nicht berechnen", exc_info=True)
        else:
            sessions_count = len(session_rows)

            def _num(row, key, default=0):
                try:
                    return row[key] if row[key] is not None else default
                except Exception:
                    return default

            ret5_vals = [float(_num(row, "retention_5m")) for row in session_rows if row["retention_5m"] is not None]
            ret10_vals = [float(_num(row, "retention_10m")) for row in session_rows if row["retention_10m"] is not None]
            ret20_vals = [float(_num(row, "retention_20m")) for row in session_rows if row["retention_20m"] is not None]
            drop_vals = [float(_num(row, "dropoff_pct")) for row in session_rows if row["dropoff_pct"] is not None]

            def _avg(values: List[float]) -> Optional[float]:
                return sum(values) / len(values) if values else None

            dropoff_examples = []
            for row in session_rows:
                pct = row["dropoff_pct"]
                if pct is None:
                    continue
                dropoff_examples.append(
                    {
                        "streamer": _num(row, "streamer_login", ""),
                        "started_at": _num(row, "started_at", ""),
                        "dropoff_pct": float(pct),
                        "label": row["dropoff_label"] or "",
                        "start_viewers": _num(row, "start_viewers", 0),
                        "peak_viewers": _num(row, "peak_viewers", 0),
                    }
                )
            dropoff_examples.sort(key=lambda r: r.get("dropoff_pct", 0.0), reverse=True)
            dropoff_examples = dropoff_examples[:5]

            retention_payload = {
                "sessions": sessions_count,
                "ret5": _avg(ret5_vals),
                "ret10": _avg(ret10_vals),
                "ret20": _avg(ret20_vals),
                "avg_drop": _avg(drop_vals),
                "examples": dropoff_examples,
            }

            peak_vals = [int(_num(row, "peak_viewers", 0)) for row in session_rows]
            follower_deltas = [int(row["follower_delta"]) for row in session_rows if row["follower_delta"] is not None]
            follower_per_hour: List[float] = []
            for row in session_rows:
                delta = row["follower_delta"]
                duration = _num(row, "duration_seconds", 0)
                if delta is None or not duration:
                    continue
                hours = max(0.1, duration / 3600.0)
                follower_per_hour.append(float(delta) / hours)

            unique_chat_sessions = [row for row in session_rows if _num(row, "unique_chatters", 0) > 0]
            total_unique_chatters = sum(_num(row, "unique_chatters", 0) for row in unique_chat_sessions)
            total_first_chatters = sum(_num(row, "first_time_chatters", 0) for row in unique_chat_sessions)
            total_returning_chatters = sum(_num(row, "returning_chatters", 0) for row in unique_chat_sessions)

            unique_per_100: Optional[float] = None
            if unique_chat_sessions:
                ratios: List[float] = []
                for row in unique_chat_sessions:
                    viewers_base = _num(row, "avg_viewers", 0.0) or _num(row, "start_viewers", 1)
                    ratios.append((_num(row, "unique_chatters", 0) / max(1, viewers_base)) * 100.0)
                unique_per_100 = _avg(ratios)

            chat_peaks = []
            for row in chat_peak_rows:
                chat_peaks.append(
                    {
                        "session_id": _num(row, "session_id", 0),
                        "streamer": _num(row, "streamer_login", ""),
                        "minute": row["minute_bucket"] if hasattr(row, "__getitem__") else "",
                        "messages": _num(row, "messages", 0),
                        "started_at": _num(row, "started_at", ""),
                    }
                )

            chat_payload = {
                "unique_per_100": unique_per_100,
                "first_share": (total_first_chatters / total_unique_chatters) if total_unique_chatters else None,
                "returning_share": (total_returning_chatters / total_unique_chatters) if total_unique_chatters else None,
                "peaks": chat_peaks,
                "total_unique": total_unique_chatters,
            }

            discovery_payload = {
                "sessions": sessions_count,
                "unique_viewers_estimate": _avg(peak_vals) if peak_vals else None,
                "followers_total_delta": sum(follower_deltas) if follower_deltas else 0,
                "followers_per_session": _avg(follower_deltas),
                "followers_per_hour": _avg(follower_per_hour),
                "returning_7d": {
                    "total": int(active_7_row[0]) if active_7_row else 0,
                    "returning": int(returning_7_row[0]) if returning_7_row else 0,
                },
                "returning_30d": {
                    "total": int(active_30_row[0]) if active_30_row else 0,
                    "returning": int(returning_30_row[0]) if returning_30_row else 0,
                },
            }

            # Content Performance (Titles & Notifications)
            content_performance = []
            for row in session_rows:
                title = _num(row, "stream_title", "")
                notify = _num(row, "notification_text", "")
                if not title and not notify:
                    continue
                
                followers_start = _num(row, "followers_start", 0)
                peak = _num(row, "peak_viewers", 0)
                avg = _num(row, "avg_viewers", 0.0)
                
                engagement_ratio = None
                if followers_start > 0:
                    engagement_ratio = (peak / followers_start) * 100.0

                content_performance.append({
                    "streamer": _num(row, "streamer_login", ""),
                    "started_at": _num(row, "started_at", ""),
                    "title": title,
                    "notification": notify,
                    "peak_viewers": peak,
                    "avg_viewers": avg,
                    "followers_start": followers_start,
                    "engagement_ratio": engagement_ratio
                })
            
            # Sort by peak viewers to show best performing content first
            content_performance.sort(key=lambda x: x["peak_viewers"], reverse=True)
            content_performance = content_performance[:20]  # Top 20

            out["retention"] = retention_payload
            out["chat"] = chat_payload
            out["discovery"] = discovery_payload
            out["content_performance"] = content_performance

        return out

    @staticmethod
    def _filter_stats_items(
        items: Sequence[dict],
        *,
        min_samples: Optional[int],
        min_avg_viewers: Optional[float],
        partner_filter: str,
    ) -> List[dict]:
        def _ok(data: dict) -> bool:
            samples = int(data.get("samples") or 0)
            avgv = float(data.get("avg_viewers") or 0.0)
            is_partner = bool(data.get("is_partner"))
            if (min_samples is not None) and (samples < min_samples):
                return False
            if (min_avg_viewers is not None) and (avgv < min_avg_viewers):
                return False
            if partner_filter == "only" and not is_partner:
                return False
            if partner_filter == "exclude" and is_partner:
                return False
            return True

        return [row for row in items if _ok(row)]

    @staticmethod
    def _sort_stats_items(
        items: Sequence[dict],
        *,
        sort_key: str,
        descending: bool,
        limit: int,
    ) -> List[dict]:
        def _key_func(item: dict):
            if sort_key == "samples":
                return int(item.get("samples") or 0)
            if sort_key == "peak":
                return int(item.get("max_viewers") or 0)
            if sort_key == "name":
                return str(item.get("streamer") or "").lower()
            return float(item.get("avg_viewers") or 0.0)

        limited = sorted(items, key=_key_func, reverse=descending)
        return list(limited[: max(1, limit)])

    def _build_leaderboard_embed(
        self,
        tracked_items: Sequence[dict],
        category_items: Sequence[dict],
        options: LeaderboardOptions,
    ) -> discord.Embed:
        tracked_filtered = self._filter_stats_items(
            tracked_items,
            min_samples=options.min_samples,
            min_avg_viewers=options.min_avg,
            partner_filter=options.partner_filter,
        )
        category_filtered = self._filter_stats_items(
            category_items,
            min_samples=options.min_samples,
            min_avg_viewers=options.min_avg,
            partner_filter=options.partner_filter,
        )

        tracked_sorted = self._sort_stats_items(
            tracked_filtered,
            sort_key=options.sort_key,
            descending=options.sort_order != "asc",
            limit=options.limit,
        )
        category_sorted = self._sort_stats_items(
            category_filtered,
            sort_key=options.sort_key,
            descending=options.sort_order != "asc",
            limit=options.limit,
        )

        def _format_lines(items: Sequence[dict]) -> str:
            if not items:
                return "Keine Daten für die aktuellen Filter."
            lines: List[str] = []
            for idx, item in enumerate(items, start=1):
                streamer = item.get("streamer") or "?"
                avg_viewers = float(item.get("avg_viewers") or 0.0)
                samples = int(item.get("samples") or 0)
                peak = int(item.get("max_viewers") or 0)
                partner_flag = " (Partner)" if item.get("is_partner") else ""
                lines.append(
                    f"{idx}. {streamer} — Ø {avg_viewers:.1f} Viewer (Stichproben: {samples}, Peak: {peak}){partner_flag}"
                )
            text = "\n".join(lines)
            if len(text) > 1024:
                text = text[:1021] + "…"
            return text

        filter_summary = ", ".join(options.filter_summary())
        description_lines = [
            f"Filter: {filter_summary}",
            options.sort_summary(),
            f"Anzeige: {options.limit_label}",
        ]
        embed = discord.Embed(
            title="Twitch Leaderboard",
            description="\n".join(description_lines),
            color=discord.Color.purple(),
        )
        embed.add_field(name="Top Tracked", value=_format_lines(tracked_sorted), inline=False)
        embed.add_field(name="Top Kategorie", value=_format_lines(category_sorted), inline=False)
        embed.set_footer(text="Nutze !twl help für weitere Optionen.")
        return embed
