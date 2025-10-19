"""Background polling and monitoring helpers for Twitch streams."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import tasks

from . import storage
from .constants import INVITES_REFRESH_INTERVAL_HOURS, POLL_INTERVAL_SECONDS, TWITCH_TARGET_GAME_NAME
from .logger import log


class TwitchMonitoringMixin:
    """Polling loops and helpers used by the Twitch cog."""

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
                    "SELECT twitch_login, twitch_user_id, require_discord_link "
                    "FROM twitch_streamers"
                ).fetchall()
            tracked: List[Tuple[str, str, bool]] = []
            for row in rows:
                login = str(row["twitch_login"])
                tracked.append((login, str(row["twitch_user_id"]), bool(row["require_discord_link"])))
                partner_logins.add(login.lower())
        except Exception:
            log.exception("Konnte tracked Streamer nicht aus DB lesen")
            tracked = []
            partner_logins = set()

        logins = [login for login, _, _ in tracked]
        streams_by_login: Dict[str, dict] = {}

        try:
            if logins:
                streams = await self.api.get_streams_by_logins(logins, language=self._language_filter)
                for stream in streams:
                    login = (stream.get("user_login") or "").lower()
                    if login:
                        streams_by_login[login] = stream
        except Exception:
            log.exception("Konnte Streams für tracked Logins nicht abrufen")

        for login, stream in list(streams_by_login.items()):
            if login in partner_logins:
                stream["is_partner"] = True

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

        for stream in category_streams:
            login = (stream.get("user_login") or "").lower()
            if login in partner_logins:
                stream["is_partner"] = True

        try:
            await self._process_postings(tracked, streams_by_login)
        except Exception:
            log.exception("Fehler in _process_postings")

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
        notify_ch: Optional[discord.TextChannel] = None
        if self._notify_channel_id:
            notify_ch = self.bot.get_channel(self._notify_channel_id) or None  # type: ignore[assignment]

        now_utc = datetime.now(tz=timezone.utc)

        with storage.get_conn() as c:
            live_state = {
                str(row["streamer_login"]): dict(row)
                for row in c.execute("SELECT * FROM twitch_live_state").fetchall()
            }

        for login, _user_id, need_link in tracked:
            stream = streams_by_login.get(login.lower())
            was_live = bool(live_state.get(login, {}).get("is_live", 0))
            is_live = bool(stream)

            if is_live and not was_live and notify_ch is not None:
                url = f"https://twitch.tv/{login}"
                display_name = stream.get("user_name") or login
                message_prefix = []
                if self._alert_mention:
                    message_prefix.append(self._alert_mention)
                message_prefix.append(f"**{display_name}** ist live: {url}")
                content = " ".join(part for part in message_prefix if part).strip()

                embed = self._build_live_embed(login, stream)
                view = self._build_live_view(url)

                try:
                    await notify_ch.send(content=content or None, embed=embed, view=view)
                except Exception:
                    log.exception("Konnte Go-Live-Posting nicht senden: %s", login)

            with storage.get_conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO twitch_live_state "
                    "(streamer_login, is_live, last_seen_at, last_title, last_game, last_viewer_count) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        login,
                        int(is_live),
                        now_utc.isoformat(timespec="seconds"),
                        (stream.get("title") if stream else None),
                        (stream.get("game_name") if stream else None),
                        int(stream.get("viewer_count") or 0) if stream else 0,
                    ),
                )

            if need_link and self._alert_channel_id and (now_utc.minute % 10 == 0) and is_live:
                # Platzhalter für deinen Profil-/Panel-Check
                pass

    async def _log_stats(self, streams_by_login: Dict[str, dict], category_streams: List[dict]):
        now_utc = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        try:
            with storage.get_conn() as c:
                for stream in streams_by_login.values():
                    login = (stream.get("user_login") or "").lower()
                    viewers = int(stream.get("viewer_count") or 0)
                    is_partner = 1 if stream.get("is_partner") else 0
                    c.execute(
                        "INSERT INTO twitch_stats_tracked (ts_utc, streamer, viewer_count, is_partner) VALUES (?, ?, ?, ?)",
                        (now_utc, login, viewers, is_partner),
                    )
        except Exception:
            log.exception("Konnte tracked-Stats nicht loggen")

        try:
            with storage.get_conn() as c:
                for stream in category_streams:
                    login = (stream.get("user_login") or "").lower()
                    viewers = int(stream.get("viewer_count") or 0)
                    is_partner = 1 if stream.get("is_partner") else 0
                    c.execute(
                        "INSERT INTO twitch_stats_category (ts_utc, streamer, viewer_count, is_partner) VALUES (?, ?, ?, ?)",
                        (now_utc, login, viewers, is_partner),
                    )
        except Exception:
            log.exception("Konnte category-Stats nicht loggen")

    def _build_live_embed(self, login: str, stream: dict) -> discord.Embed:
        """Erzeuge ein Discord-Embed für das Go-Live-Posting mit Stream-Vorschau."""

        display_name = stream.get("user_name") or login
        url = f"https://twitch.tv/{login}"
        game = stream.get("game_name") or TWITCH_TARGET_GAME_NAME
        title = stream.get("title") or "Live!"
        viewer_count = int(stream.get("viewer_count") or 0)

        timestamp = datetime.now(tz=timezone.utc)
        started_at_raw = stream.get("started_at")
        if isinstance(started_at_raw, str) and started_at_raw:
            try:
                timestamp = datetime.fromisoformat(started_at_raw.replace("Z", "+00:00"))
            except ValueError:
                pass

        embed = discord.Embed(
            title=f"{display_name} ist LIVE in {game}!",
            description=title,
            url=url,
            colour=discord.Color(0x9146FF),
            timestamp=timestamp,
        )

        embed.add_field(name="Viewer", value=str(viewer_count), inline=True)
        embed.add_field(name="Kategorie", value=game, inline=True)
        embed.add_field(name="Link", value=url, inline=False)

        thumbnail_url = (stream.get("thumbnail_url") or "").strip()
        if thumbnail_url:
            thumbnail_url = thumbnail_url.replace("{width}", "1280").replace("{height}", "720")
            cache_bust = int(datetime.now(tz=timezone.utc).timestamp())
            embed.set_image(url=f"{thumbnail_url}?rand={cache_bust}")

        embed.set_footer(text="Auf Twitch ansehen für mehr Deadlock-Action!")
        embed.set_author(name=display_name, url=url)

        return embed

    @staticmethod
    def _build_live_view(url: str) -> discord.ui.View:
        """Stellt eine View mit Button zum Öffnen des Streams bereit."""

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Auf Twitch ansehen", url=url))
        return view
