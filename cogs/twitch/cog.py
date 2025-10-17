# ============================================
# cogs/twitch/cog.py — Vollständige Version
# ============================================
# - Keine direkte Registrierung des !twl-Commands hier.
#   Die Registrierung erfolgt zentral in cogs/twitch/__init__.py als Proxy.
# - Diese Datei stellt:
#   * TwitchStreamCog (Monitoring, Posting, Dashboard)
#   * Admin-Hybrid-Gruppe /twitch [...]
#   * Methode twitch_leaderboard(ctx, *, filters="") für den Proxy
# - Sauberes Logging, keine "leeren except"-Blöcke, kein doppeltes Registrieren.

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

import discord
from aiohttp import web
from discord.ext import commands, tasks

from .twitch_api import TwitchAPI
from . import storage
from .dashboard import Dashboard

log = logging.getLogger("TwitchStreams")

# ============================
# 🛠️ CONFIG — EDIT HERE
# ============================
# ⚠️ Secrets (Client-ID/Secret) KOMMEN NICHT HIER REIN, sondern aus ENV (siehe unten)!
TWITCH_DASHBOARD_NOAUTH = True                     # ohne Token (nur lokal empfohlen)
TWITCH_DASHBOARD_HOST = "127.0.0.1"
TWITCH_DASHBOARD_PORT = 8765

TWITCH_LANGUAGE = "de"
TWITCH_TARGET_GAME_NAME = "Deadlock"
TWITCH_REQUIRED_DISCORD_MARKER = ""                # optionaler Marker im Profiltext (zusätzl. zur Discord-URL)

# Benachrichtigungskanäle
TWITCH_NOTIFY_CHANNEL_ID = 1304169815505637458     # Live-Postings (optional global)
TWITCH_ALERT_CHANNEL_ID  = 1374364800817303632     # Warnungen (30d Re-Check)
TWITCH_ALERT_MENTION     = ""                      # z. B. "<@123>" oder "<@&456>"

# Öffentlicher Statistik-Kanal (nur dort reagiert !twl)
TWITCH_STATS_CHANNEL_ID  = 1428062025145385111

# Stats/Sampling: alle N Ticks (Tick=60s) in DB loggen
TWITCH_LOG_EVERY_N_TICKS = 5

# Zusätzliche Streams aus der Deadlock-Kategorie für Statistiken loggen (Maximalanzahl je Tick)
TWITCH_CATEGORY_SAMPLE_LIMIT = 400

# Invite-Refresh alle X Stunden
INVITES_REFRESH_INTERVAL_HOURS = 12

# Poll-Intervall (Sekunden)
POLL_INTERVAL_SECONDS = 60

# ============================
# 🔒 SECRETS — aus ENV
# ============================


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
        "samples": "Samples",
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
            parts.append(f"Samples ≥ {self.min_samples}")
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

    def clamp(self) -> None:
        self.limit = max(1, min(20, int(self.limit or 5)))
        if self.sort_key not in {"avg", "samples", "peak", "name"}:
            self.sort_key = "avg"
        if self.sort_order not in {"asc", "desc"}:
            self.sort_order = "desc"
        if self.partner_filter not in {"any", "only", "exclude"}:
            self.partner_filter = "any"
        if self.min_samples is not None:
            self.min_samples = max(0, int(self.min_samples))
            if self.min_samples == 0:
                self.min_samples = None
        if self.min_avg is not None:
            self.min_avg = max(0.0, float(self.min_avg))
            if self.min_avg == 0:
                self.min_avg = None

    def cycle_sort_key(self) -> None:
        order = ("avg", "samples", "peak", "name")
        try:
            idx = order.index(self.sort_key)
        except ValueError:
            idx = 0
        self.sort_key = order[(idx + 1) % len(order)]

    def toggle_sort_order(self) -> None:
        self.sort_order = "asc" if self.sort_order == "desc" else "desc"

    def cycle_partner_filter(self) -> None:
        order = ("any", "only", "exclude")
        try:
            idx = order.index(self.partner_filter)
        except ValueError:
            idx = 0
        self.partner_filter = order[(idx + 1) % len(order)]

    @staticmethod
    def _cycle_value(levels: Sequence[Optional[float]], current: Optional[float]):
        if current in levels:
            idx = levels.index(current)
            return levels[(idx + 1) % len(levels)]
        if current is None:
            return levels[1] if len(levels) > 1 else levels[0]
        numeric_levels = [v for v in levels if v is not None]
        numeric_levels.sort()
        for val in numeric_levels:
            if current < val:
                return val
        return levels[0]

    def cycle_min_samples(self) -> None:
        next_val = self._cycle_value(self._SAMPLES_STEPS, self.min_samples)
        self.min_samples = None if next_val is None else int(next_val)

    def cycle_min_avg(self) -> None:
        next_val = self._cycle_value(self._AVG_STEPS, self.min_avg)
        self.min_avg = None if next_val is None else float(next_val)

    def cycle_limit(self) -> None:
        next_val = self._cycle_value(self._LIMIT_STEPS, self.limit)
        self.limit = int(next_val or 5)

    def reset(self) -> None:
        self.min_samples = None
        self.min_avg = None
        self.partner_filter = "any"
        self.limit = 5
        self.sort_key = "avg"
        self.sort_order = "desc"


class TwitchLeaderboardView(discord.ui.View):
    """Interaktive Ansicht für das Twitch-Leaderboard."""

    def __init__(
        self,
        cog: "TwitchStreamCog",
        ctx: commands.Context,
        tracked_items: Sequence[dict],
        category_items: Sequence[dict],
        options: LeaderboardOptions,
    ):
        super().__init__(timeout=180)
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
            except Exception:
                pass
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
        self.samples_button.label = f"Samples: {self._options.samples_label}"
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
            except Exception:
                pass

    @discord.ui.button(label="Sortierung", style=discord.ButtonStyle.primary, row=0)
    async def sort_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.cycle_sort_key()
        await self.update(interaction)

    @discord.ui.button(label="Reihenfolge", style=discord.ButtonStyle.secondary, row=0)
    async def order_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.toggle_sort_order()
        await self.update(interaction)

    @discord.ui.button(label="Partner", style=discord.ButtonStyle.secondary, row=0)
    async def partner_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.cycle_partner_filter()
        await self.update(interaction)

    @discord.ui.button(label="Samples", style=discord.ButtonStyle.secondary, row=1)
    async def samples_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.cycle_min_samples()
        await self.update(interaction)

    @discord.ui.button(label="Ø Viewer", style=discord.ButtonStyle.secondary, row=1)
    async def avg_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.cycle_min_avg()
        await self.update(interaction)

    @discord.ui.button(label="Limit", style=discord.ButtonStyle.secondary, row=1)
    async def limit_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.cycle_limit()
        await self.update(interaction)

    @discord.ui.button(label="Neu laden", style=discord.ButtonStyle.success, row=2)
    async def refresh_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.update(interaction, refresh_stats=True)

    @discord.ui.button(label="Zurücksetzen", style=discord.ButtonStyle.secondary, row=2)
    async def reset_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._options.reset()
        await self.update(interaction)

    @discord.ui.button(label="Schließen", style=discord.ButtonStyle.danger, row=2)
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
class TwitchStreamCog(commands.Cog):
    """Monitor Twitch-Streamer (Deadlock), poste Go-Live, sammle Stats, Dashboard."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # 🔒 Secrets nur aus ENV (nicht hardcoden!)
        self.client_id = os.getenv("TWITCH_CLIENT_ID") or ""
        self.client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            log.error("TWITCH_CLIENT_ID/SECRET not configured; cog disabled")
            self.api: Optional[TwitchAPI] = None
            # Keine Tasks ohne API starten
            self._web: Optional[web.AppRunner] = None
            self._web_app: Optional[web.Application] = None
            self._category_id: Optional[str] = None
            self._language_filter = (TWITCH_LANGUAGE or "").strip() or None
            self._tick_count = 0
            self._log_every_n = max(1, int(TWITCH_LOG_EVERY_N_TICKS or 5))
            self._category_sample_limit = max(50, int(TWITCH_CATEGORY_SAMPLE_LIMIT or 400))
            self._notify_channel_id = int(TWITCH_NOTIFY_CHANNEL_ID or 0)
            self._alert_channel_id = int(TWITCH_ALERT_CHANNEL_ID or 0)
            self._alert_mention = TWITCH_ALERT_MENTION or ""
            self._invite_codes: Dict[int, Set[str]] = {}
            self._twl_command: Optional[commands.Command] = None
            return

        self.api = TwitchAPI(self.client_id, self.client_secret)

        # Laufzeit-Zustand / Config
        self._category_id: Optional[str] = None
        self._language_filter = (TWITCH_LANGUAGE or "").strip() or None

        # Dashboard/Auth (aus Config-Header)
        self._dashboard_token = os.getenv("TWITCH_DASHBOARD_TOKEN") or None
        self._dashboard_noauth = bool(TWITCH_DASHBOARD_NOAUTH)
        self._dashboard_host = TWITCH_DASHBOARD_HOST or ("127.0.0.1" if self._dashboard_noauth else "0.0.0.0")
        self._dashboard_port = int(TWITCH_DASHBOARD_PORT)
        self._partner_dashboard_token = os.getenv("TWITCH_PARTNER_TOKEN") or None
        self._required_marker_default = TWITCH_REQUIRED_DISCORD_MARKER or None

        # Channels/Alerts
        self._notify_channel_id = int(TWITCH_NOTIFY_CHANNEL_ID or 0)
        self._alert_channel_id = int(TWITCH_ALERT_CHANNEL_ID or 0)
        self._alert_mention = TWITCH_ALERT_MENTION or ""

        # Stats logging cadence
        self._tick_count = 0
        self._log_every_n = max(1, int(TWITCH_LOG_EVERY_N_TICKS or 5))
        self._category_sample_limit = max(50, int(TWITCH_CATEGORY_SAMPLE_LIMIT or 400))

        # Dashboard
        self._web: Optional[web.AppRunner] = None
        self._web_app: Optional[web.Application] = None

        # Invite-Cache: {guild_id: {code, .}}
        self._invite_codes: Dict[int, Set[str]] = {}

        # Prefix-Command-Referenz (wird vom setup() gesetzt)
        self._twl_command: Optional[commands.Command] = None

        # Background tasks
        self.poll_streams.start()
        self.invites_refresh.start()
        self.bot.loop.create_task(self._ensure_category_id())
        self.bot.loop.create_task(self._start_dashboard())
        self.bot.loop.create_task(self._refresh_all_invites())

    # -------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------
    def cog_unload(self):
        """Sauberer Shutdown ohne leere except-Blöcke (CWE-390/703-freundlich)."""
        loops = (self.poll_streams, self.invites_refresh)

        async def _graceful_shutdown():
            # 1) laufende Tasks abbrechen
            for lp in loops:
                try:
                    if lp.is_running():
                        lp.cancel()
                except Exception:
                    log.exception("Konnte Loop nicht canceln: %r", lp)
            await asyncio.sleep(0)

            # 2) Dashboard herunterfahren
            if self._web:
                try:
                    await self._stop_dashboard()
                except Exception:
                    log.exception("Dashboard shutdown fehlgeschlagen")

            # 3) HTTP-Session schließen
            if self.api is not None:
                try:
                    await self.api.aclose()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.exception("TwitchAPI-Session konnte nicht geschlossen werden")

        try:
            self.bot.loop.create_task(_graceful_shutdown())
        except Exception:
            log.exception("Fehler beim Start des Shutdown-Tasks")

        # Den dynamisch registrierten Prefix-Command sauber deregistrieren
        try:
            if self._twl_command is not None:
                existing = self.bot.get_command(self._twl_command.name)
                if existing is self._twl_command:
                    self.bot.remove_command(self._twl_command.name)
        except Exception:
            log.exception("Konnte !twl-Command nicht deregistrieren")
        finally:
            self._twl_command = None

    def set_prefix_command(self, command: commands.Command) -> None:
        """Speichert die Referenz auf den dynamisch registrierten Prefix-Command."""
        self._twl_command = command

    # -------------------------------------------------------
    # Admin-Hybrid-Gruppe /twitch [...]
    # -------------------------------------------------------
    @commands.hybrid_group(name="twitch", with_app_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Subcommands: add, remove, list, channel, forcecheck, invites")

    @twitch_group.command(name="channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        try:
            self._set_channel(ctx.guild.id, channel.id)
            await ctx.reply(f"Live-Posts gehen jetzt in {channel.mention}")
        except Exception:
            log.exception("Konnte Twitch-Channel speichern")
            await ctx.reply("Konnte Kanal nicht speichern.")

    @twitch_group.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_add(self, ctx: commands.Context, login: str, require_discord_link: Optional[bool] = False):
        try:
            msg = await self._cmd_add(login, bool(require_discord_link))
        except Exception:
            log.exception("twitch add fehlgeschlagen")
            await ctx.reply("Fehler beim Hinzufügen.")
            return
        await ctx.reply(msg)

    @twitch_group.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_remove(self, ctx: commands.Context, login: str):
        try:
            msg = await self._cmd_remove(login)
        except Exception:
            log.exception("twitch remove fehlgeschlagen")
            await ctx.reply("Fehler beim Entfernen.")
            return
        await ctx.reply(msg)

    @twitch_group.command(name="list")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_list(self, ctx: commands.Context):
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT twitch_login, manual_verified_permanent, manual_verified_until FROM twitch_streamers ORDER BY twitch_login"
                ).fetchall()
        except Exception:
            log.exception("Konnte Streamer-Liste aus DB lesen")
            await ctx.reply("Fehler beim Lesen der Streamer-Liste.")
            return

        if not rows:
            await ctx.reply("Keine Streamer gespeichert.")
            return

        def _fmt(r: dict) -> str:
            until = r.get("manual_verified_until")
            perm = bool(r.get("manual_verified_permanent"))
            tail = " (permanent verifiziert)" if perm else (f" (verifiziert bis {until})" if until else "")
            return f"- {r.get('twitch_login','?')}{tail}"

        try:
            lines = [_fmt(dict(r)) for r in rows]
            await ctx.reply("\n".join(lines)[:1900])
        except Exception:
            log.exception("Fehler beim Formatieren der Streamer-Liste")
            await ctx.reply("Fehler beim Anzeigen der Liste.")

    @twitch_group.command(name="forcecheck")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_forcecheck(self, ctx: commands.Context):
        await ctx.reply("Prüfe jetzt…")
        try:
            await self._tick()
        except Exception:
            log.exception("Forcecheck fehlgeschlagen")
            await ctx.send("Fehler beim Forcecheck.")

    @twitch_group.command(name="invites")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_invites(self, ctx: commands.Context):
        try:
            await self._refresh_guild_invites(ctx.guild)
            codes = sorted(self._invite_codes.get(ctx.guild.id, set()))
            if not codes:
                await ctx.reply("Keine aktiven Einladungen gefunden.")
            else:
                urls = [f"https://discord.gg/{c}" for c in codes]
                await ctx.reply("Aktive Einladungen:\n" + "\n".join(urls)[:1900])
        except Exception:
            log.exception("Konnte Einladungen nicht abrufen")
            await ctx.reply("Fehler beim Abrufen der Einladungen.")

    # -------------------------------------------------------
    # User-facing Logik (wird vom Proxy !twl aufgerufen)
    # -------------------------------------------------------
    async def twitch_leaderboard(
        self,
        ctx: Optional[commands.Context] = None,
        *maybe_filters: Any,
        filters: str = "",
    ):
        """Zeigt Twitch-Statistiken im Partner-Kanal an.

        Nutzung: !twl [samples=Zahl] [avg=Zahl] [partner=only|exclude|any] [limit=Zahl] [sort=avg|samples|peak|name] [order=asc|desc]
        """

        # Flexible Signatur robust entfalten
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

        # Kanal-Gate
        if ctx.channel.id != TWITCH_STATS_CHANNEL_ID:
            channel_hint = f"<#{TWITCH_STATS_CHANNEL_ID}>"
            await ctx.reply(f"Dieser Befehl kann nur in {channel_hint} verwendet werden.")
            return

        # Help
        if filter_text.lower() in {"help", "?", "hilfe"}:
            help_text = (
                "Verwendung: !twl [samples=Zahl] [avg=Zahl] [partner=only|exclude|any] [limit=Zahl] [sort=avg|samples|peak|name] [order=asc|desc]\n"
                "Beispiel: !twl samples=15 avg=25 partner=only sort=avg order=desc"
            )
            await ctx.reply(help_text)
            return

        # Filter parsen
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

        # Stats holen
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

    # -------------------------------------------------------
    # Background: Polling / Invites / Dashboard
    # -------------------------------------------------------
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
                log.info("Deadlock category_id = %s", self._category_id)
        except Exception:
            log.exception("Konnte Twitch-Kategorie-ID nicht ermitteln")

    # -------------------------------------------------------
    # Core Tick: Daten holen, posten, loggen
    # -------------------------------------------------------
    async def _tick(self):
        """Ein Tick: tracked Streamer + Kategorie-Streams prüfen, Postings/DB aktualisieren, Stats loggen."""
        if self.api is None:
            return

        # Ggf. Kategorie-ID nachziehen
        if not self._category_id:
            await self._ensure_category_id()

        # 1) Tracked Streamer aus DB lesen
        now_utc = datetime.now(tz=timezone.utc)
        partner_logins: Set[str] = set()
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT twitch_login, twitch_user_id, require_discord_link, "
                    "       manual_verified_permanent, manual_verified_until "
                    "FROM twitch_streamers"
                ).fetchall()
            tracked: List[Tuple[str, str, bool]] = []
            for row in rows:
                login = str(row["twitch_login"])
                tracked.append(
                    (login, str(row["twitch_user_id"]), bool(row["require_discord_link"]))
                )
                if self._is_partner_verified(dict(row), now_utc):
                    partner_logins.add(login.lower())
        except Exception:
            log.exception("Konnte tracked Streamer nicht aus DB lesen")
            tracked = []
            partner_logins = set()

        logins = [login for login, _, _ in tracked]
        streams_by_login: Dict[str, dict] = {}

        # 2) Live-Daten für tracked Streamer holen
        try:
            if logins:
                streams = await self.api.get_streams_by_logins(logins, language=self._language_filter)
                # Normalisieren auf Dict nach login
                for s in streams:
                    login = (s.get("user_login") or "").lower()
                    if login:
                        streams_by_login[login] = s
        except Exception:
            log.exception("Konnte Streams für tracked Logins nicht abrufen")

        # Partner-Flag für live tracked Streams anwenden
        for login, stream in list(streams_by_login.items()):
            if login in partner_logins:
                stream["is_partner"] = True

        # 3) Kategorie-Streams (optional für Statistiken)
        category_streams: List[dict] = []
        if self._category_id:
            try:
                category_streams = await self.api.get_streams_by_category(
                    self._category_id,
                    language=self._language_filter,
                    limit=self._category_sample_limit,
                )
            except Exception:
                log.exception("Konnte Kategorie-Streams nicht abrufen")

        # Partner-Flag für Kategorie-Streams anwenden (wenn sie tracked Partner sind)
        for stream in category_streams:
            login = (stream.get("user_login") or "").lower()
            if login in partner_logins:
                stream["is_partner"] = True

        # 4) Postings/Warnungen verarbeiten (z. B. Live-Ankündigungen, Link-Checks)
        try:
            await self._process_postings(tracked, streams_by_login)
        except Exception:
            log.exception("Fehler in _process_postings")

        # 5) Stats regelmäßig loggen
        self._tick_count += 1
        if self._tick_count % self._log_every_n == 0:
            try:
                await self._log_stats(streams_by_login, category_streams)
            except Exception:
                log.exception("Fehler beim Stats-Logging")

    async def _process_postings(
        self,
        tracked: List[Tuple[str, str, bool]],
        streams_by_login: Dict[str, dict],
    ):
        """Go-Live Postings + Link-Checks."""
        # Channel ermitteln
        notify_ch: Optional[discord.TextChannel] = None
        if self._notify_channel_id:
            notify_ch = self.bot.get_channel(self._notify_channel_id) or None  # type: ignore[assignment]

        now_utc = datetime.now(tz=timezone.utc)

        # DB-Live-States holen
        with storage.get_conn() as c:
            live_state = {
                str(r["streamer_login"]): dict(r)
                for r in c.execute("SELECT * FROM twitch_live_state").fetchall()
            }

        for login, user_id, need_link in tracked:
            s = streams_by_login.get(login.lower())
            was_live = bool(live_state.get(login, {}).get("is_live", 0))
            is_live = bool(s)

            # 4.1 Übergang: offline -> live → Posten
            if is_live and not was_live and notify_ch is not None:
                title = s.get("title") or "Live!"
                url = f"https://twitch.tv/{login}"
                game = s.get("game_name") or TWITCH_TARGET_GAME_NAME
                viewer_count = s.get("viewer_count") or 0

                try:
                    await notify_ch.send(
                        f"🔴 **{login}** ist jetzt live in **{game}** — *{title}*  (👀 {viewer_count})\n{url}"
                    )
                except Exception:
                    log.exception("Konnte Go-Live-Posting nicht senden: %s", login)

            # 4.2 State persistieren
            with storage.get_conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO twitch_live_state "
                    "(streamer_login, is_live, last_seen_at, last_title, last_game, last_viewer_count) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        login,
                        int(is_live),
                        now_utc.isoformat(timespec="seconds"),
                        (s.get("title") if s else None),
                        (s.get("game_name") if s else None),
                        int(s.get("viewer_count") or 0) if s else 0,
                    ),
                )

            # 4.3 Optional: Link-Check/Marker-Check rollierend
            if need_link and self._alert_channel_id and (now_utc.minute % 10 == 0) and is_live:
                # Platzhalter für deinen Profil-/Panel-Check
                pass

    async def _log_stats(self, streams_by_login: Dict[str, dict], category_streams: List[dict]):
        """Stats in DB loggen (tracked + category)."""
        now_utc = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        # Tracked
        try:
            with storage.get_conn() as c:
                for s in streams_by_login.values():
                    login = (s.get("user_login") or "").lower()
                    viewers = int(s.get("viewer_count") or 0)
                    is_partner = 1 if s.get("is_partner") else 0
                    c.execute(
                        "INSERT INTO twitch_stats_tracked (ts_utc, streamer, viewer_count, is_partner) VALUES (?, ?, ?, ?)",
                        (now_utc, login, viewers, is_partner),
                    )
        except Exception:
            log.exception("Konnte tracked-Stats nicht loggen")

        # Kategorie
        try:
            with storage.get_conn() as c:
                for s in category_streams:
                    login = (s.get("user_login") or "").lower()
                    viewers = int(s.get("viewer_count") or 0)
                    is_partner = 1 if s.get("is_partner") else 0
                    c.execute(
                        "INSERT INTO twitch_stats_category (ts_utc, streamer, viewer_count, is_partner) VALUES (?, ?, ?, ?)",
                        (now_utc, login, viewers, is_partner),
                    )
        except Exception:
            log.exception("Konnte category-Stats nicht loggen")

    # -------------------------------------------------------
    # Leaderboard-Berechnung
    # -------------------------------------------------------
    async def _compute_stats(self) -> Dict[str, Any]:
        """Aggregiert Top-Listen für tracked & category (avg/peak/samples)."""
        out: Dict[str, Any] = {"tracked": {}, "category": {}}

        def _aggregate(sql: str) -> List[dict]:
            try:
                with storage.get_conn() as c:
                    rows = c.execute(sql).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                log.exception("Fehler bei Stats-Aggregation")
                return []

        # AVG/PEAK/SAMPLES pro Streamer seit z. B. 30 Tagen
        tracked_sql = """
        SELECT streamer,
               AVG(viewer_count) AS avg_viewers,
               MAX(viewer_count) AS max_viewers,
               COUNT(*)          AS samples,
               MAX(is_partner)   AS is_partner
        FROM twitch_stats_tracked
        WHERE ts_utc >= datetime('now', '-30 days')
        GROUP BY streamer
        ORDER BY avg_viewers DESC
        LIMIT 100
        """
        category_sql = """
        SELECT streamer,
               AVG(viewer_count) AS avg_viewers,
               MAX(viewer_count) AS max_viewers,
               COUNT(*)          AS samples,
               MAX(is_partner)   AS is_partner
        FROM twitch_stats_category
        WHERE ts_utc >= datetime('now', '-30 days')
        GROUP BY streamer
        ORDER BY avg_viewers DESC
        LIMIT 100
        """

        out["tracked"]["top"] = _aggregate(tracked_sql)
        out["category"]["top"] = _aggregate(category_sql)
        return out

    @staticmethod
    def _filter_stats_items(
        items: Sequence[dict],
        *,
        min_samples: Optional[int],
        min_avg_viewers: Optional[float],
        partner_filter: str,
    ) -> List[dict]:
        def _ok(d: dict) -> bool:
            samples = int(d.get("samples") or 0)
            avgv = float(d.get("avg_viewers") or 0.0)
            is_partner = bool(d.get("is_partner"))
            if (min_samples is not None) and (samples < min_samples):
                return False
            if (min_avg_viewers is not None) and (avgv < min_avg_viewers):
                return False
            if partner_filter == "only" and not is_partner:
                return False
            if partner_filter == "exclude" and is_partner:
                return False
            return True

        return [d for d in items if _ok(d)]

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
                    f"{idx}. {streamer} — Ø {avg_viewers:.1f} Viewer (Samples: {samples}, Peak: {peak}){partner_flag}"
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

    # -------------------------------------------------------
    # Dashboard-Callbacks (für volle UI)
    # -------------------------------------------------------
    async def _dashboard_add(self, login: str, require_link: bool) -> str:
        return await self._cmd_add(login, require_link)

    async def _dashboard_remove(self, login: str) -> None:
        await self._cmd_remove(login)

    async def _dashboard_list(self):
        with storage.get_conn() as c:
            rows = c.execute("""
                SELECT twitch_login,
                       manual_verified_permanent,
                       manual_verified_until,
                       manual_verified_at
                  FROM twitch_streamers
                 ORDER BY twitch_login
            """).fetchall()
        return [dict(r) for r in rows]

    async def _dashboard_stats(self) -> dict:
        stats = await self._compute_stats()
        tracked_top = stats.get("tracked", {}).get("top", []) or []
        category_top = stats.get("category", {}).get("top", []) or []

        def _agg(items):
            samples = sum(int(d.get("samples") or 0) for d in items)
            uniq = len(items)
            avg_over_streamers = (sum(float(d.get("avg_viewers") or 0.0) for d in items) / float(uniq)) if uniq else 0.0
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

    async def _dashboard_export(self) -> dict:
        return await self._dashboard_stats()

    async def _dashboard_export_csv(self) -> str:
        stats = await self._compute_stats()
        items = stats.get("tracked", {}).get("top", []) or []
        lines = ["streamer,samples,avg_viewers,max_viewers,is_partner"]
        for d in items:
            streamer = str(d.get("streamer") or "")
            samples = int(d.get("samples") or 0)
            avgv = float(d.get("avg_viewers") or 0.0)
            peak = int(d.get("max_viewers") or 0)
            isp = 1 if d.get("is_partner") else 0
            lines.append(f"{streamer},{samples},{avgv:.3f},{peak},{isp}")
        return "\n".join(lines)

    async def _dashboard_verify(self, login: str, mode: str) -> str:
        login = self._normalize_login(login)
        if not login:
            return "Ungültiger Login"

        with storage.get_conn() as c:
            if mode == "permanent":
                c.execute(
                    "UPDATE twitch_streamers "
                    "SET manual_verified_permanent=1, manual_verified_until=NULL, manual_verified_at=datetime('now') "
                    "WHERE twitch_login=?", (login,)
                )
                return f"{login} dauerhaft verifiziert"
            elif mode == "temp":
                c.execute(
                    "UPDATE twitch_streamers "
                    "SET manual_verified_permanent=0, manual_verified_until=datetime('now','+30 days'), "
                    "    manual_verified_at=datetime('now') "
                    "WHERE twitch_login=?", (login,)
                )
                return f"{login} für 30 Tage verifiziert"
            elif mode == "clear":
                c.execute(
                    "UPDATE twitch_streamers "
                    "SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL "
                    "WHERE twitch_login=?", (login,)
                )
                return f"Verifizierung für {login} zurückgesetzt"
            else:
                return "Unbekannter Modus"

    # -------------------------------------------------------
    # Dashboard
    # -------------------------------------------------------
    async def _start_dashboard(self):
        """Startet das Dashboard (aiohttp) — Non-blocking."""
        try:
            app = Dashboard.build_app(
                noauth=self._dashboard_noauth,
                token=self._dashboard_token,
                partner_token=self._partner_dashboard_token,
                add_cb=self._dashboard_add,
                remove_cb=self._dashboard_remove,
                list_cb=self._dashboard_list,
                stats_cb=self._dashboard_stats,
                export_cb=self._dashboard_export,
                export_csv_cb=self._dashboard_export_csv,
                verify_cb=self._dashboard_verify,
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
        """Dashboard stoppen."""
        if self._web:
            await self._web.cleanup()
            self._web = None
            self._web_app = None

    # -------------------------------------------------------
    # DB-Helpers / Guild-Setup / Invites
    # -------------------------------------------------------
    def _set_channel(self, guild_id: int, channel_id: int) -> None:
        with storage.get_conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO twitch_guild_settings (guild_id, notify_channel_id) VALUES (?, ?)",
                (int(guild_id), int(channel_id)),
            )
        if self._notify_channel_id == 0:
            self._notify_channel_id = int(channel_id)

    async def _refresh_all_invites(self):
        """Alle Guild-Einladungen sammeln (für Link-Checks/Partner-Validierung sinnvoll)."""
        try:
            await self.bot.wait_until_ready()
        except Exception:
            log.exception("wait_until_ready fehlgeschlagen")
            return

        for g in list(self.bot.guilds):
            try:
                await self._refresh_guild_invites(g)
            except Exception:
                log.exception("Einladungen für Guild %s fehlgeschlagen", g.id)

    async def _refresh_guild_invites(self, guild: discord.Guild):
        codes: Set[str] = set()
        try:
            invites = await guild.invites()
            for inv in invites:
                if inv.code:
                    codes.add(inv.code)
        except discord.Forbidden:
            log.warning("Fehlende Berechtigung, um Invites von Guild %s zu lesen", guild.id)
        except discord.HTTPException:
            log.exception("HTTP-Fehler beim Abruf der Invites für Guild %s", guild.id)

        self._invite_codes[guild.id] = codes

    # -------------------------------------------------------
    # Admin-Commands: Add/Remove Helpers
    # -------------------------------------------------------
    @staticmethod
    def _parse_db_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @classmethod
    def _is_partner_verified(cls, row: Dict[str, Any], now_utc: datetime) -> bool:
        try:
            if bool(row.get("manual_verified_permanent")):
                return True
        except Exception:
            pass

        until_raw = row.get("manual_verified_until")
        until_dt = cls._parse_db_datetime(str(until_raw)) if until_raw else None
        if until_dt and until_dt >= now_utc:
            return True
        return False

    async def _cmd_add(self, login: str, require_link: bool) -> str:
        assert self.api is not None
        normalized = self._normalize_login(login)
        if not normalized:
            return "Ungültiger Twitch-Login"

        users = await self.api.get_users([normalized])
        u = users.get(normalized)
        if not u:
            return "Unbekannter Twitch-Login"

        try:
            with storage.get_conn() as c:
                c.execute(
                    "INSERT OR IGNORE INTO twitch_streamers "
                    "(twitch_login, twitch_user_id, require_discord_link, next_link_check_at) "
                    "VALUES (?, ?, ?, datetime('now','+30 days'))",
                    (u["login"].lower(), u["id"], int(require_link)),
                )
                c.execute(
                    "UPDATE twitch_streamers "
                    "SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL "
                    "WHERE twitch_login=?",
                    (normalized,),
                )
        except Exception:
            log.exception("DB-Fehler beim Hinzufügen von %s", normalized)
            return "Datenbankfehler beim Hinzufügen."

        return f"{u['display_name']} hinzugefügt"

    async def _cmd_remove(self, login: str) -> str:
        normalized = self._normalize_login(login)
        if not normalized:
            return "Ungültiger Twitch-Login"

        deleted = 0
        try:
            with storage.get_conn() as c:
                cur = c.execute("DELETE FROM twitch_streamers WHERE twitch_login=?", (normalized,))
                deleted = cur.rowcount or 0
                c.execute("DELETE FROM twitch_live_state WHERE streamer_login=?", (normalized,))
        except Exception:
            log.exception("DB-Fehler beim Entfernen von %s", normalized)
            return "Datenbankfehler beim Entfernen."

        if deleted:
            return f"{normalized} entfernt"
        return f"{normalized} war nicht gespeichert"

    # -------------------------------------------------------
    # Utils
    # -------------------------------------------------------
    @staticmethod
    def _normalize_login(raw: str) -> str:
        login = (raw or "").strip()
        if not login:
            return ""
        login = login.split("?")[0].split("#")[0].strip()
        lowered = login.lower()
        if "twitch.tv" in lowered:
            if "//" not in login:
                login = f"https://{login}"
            try:
                parsed = urlparse(login)
                path = (parsed.path or "").strip("/")
                if path:
                    login = path.split("/")[0]
                else:
                    login = ""
            except Exception:
                login = ""
        login = login.strip().lstrip("@")
        login = re.sub(r"[^a-z0-9_]", "", login.lower())
        return login
