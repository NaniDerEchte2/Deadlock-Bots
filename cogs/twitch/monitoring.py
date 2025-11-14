"""Background polling and monitoring helpers for Twitch streams."""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import discord
from discord.ext import tasks

from . import storage
from .constants import (
    INVITES_REFRESH_INTERVAL_HOURS,
    POLL_INTERVAL_SECONDS,
    TWITCH_BRAND_COLOR_HEX,
    TWITCH_BUTTON_LABEL,
    TWITCH_DISCORD_REF_CODE,
    TWITCH_TARGET_GAME_NAME,
)
from .logger import log


class TwitchMonitoringMixin:
    """Polling loops and helpers used by the Twitch cog."""

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
                    "SELECT twitch_login, twitch_user_id, require_discord_link, "
                    "       manual_verified_permanent, manual_verified_until "
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
                    }
                )
                login_lower = login.lower()
                if login_lower and is_verified:
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

        self._tick_count += 1
        if self._tick_count % self._log_every_n == 0:
            try:
                await self._log_stats(streams_by_login, category_streams)
            except Exception:
                log.exception("Fehler beim Stats-Logging")

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
            tuple[str, str, int, str, Optional[str], Optional[str], int, Optional[str], Optional[str]]
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

            login_lower = login.lower()
            stream = streams_by_login.get(login_lower)
            previous_state = live_state.get(login_lower, {})
            was_live = bool(previous_state.get("is_live", 0))
            is_live = bool(stream)
            previous_game = (previous_state.get("last_game") or "").strip()
            previous_game_lower = previous_game.lower()
            was_deadlock = previous_game_lower == target_game_lower

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

            should_post = (
                notify_ch is not None
                and is_deadlock
                and (not was_live or not was_deadlock or not message_id_previous)
                and is_verified
            )

            if should_post:
                referral_url = self._build_referral_url(login)
                url = referral_url
                display_name = stream.get("user_name") or login
                message_prefix: List[str] = []
                if self._alert_mention:
                    message_prefix.append(self._alert_mention)
                stream_title = (stream.get("title") or "").strip()
                live_announcement = f"🔴 **{display_name}** ist live: {url}"
                if stream_title:
                    live_announcement = f"{live_announcement} – {stream_title}"
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

            ended_deadlock = (
                notify_ch is not None
                and message_id_previous
                and (not is_live or not is_deadlock)
            )

            if ended_deadlock:
                referral_url = self._build_referral_url(login)
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
                        ended_content = f"**{display_name}** (Beendet): {referral_url}"
                        try:
                            await fetched_message.edit(content=ended_content, embed=None, view=None)
                        except Exception:
                            log.exception(
                                "Konnte Deadlock-Ende-Posting nicht aktualisieren: %s", login
                            )
                        else:
                            message_id_to_store = None
                            tracking_token_to_store = None
                            self._drop_live_view(tracking_token_previous)

            user_id = str(entry.get("twitch_user_id") or "").strip()
            db_user_id = user_id or previous_state.get("twitch_user_id") or login_lower
            db_user_id = str(db_user_id)
            db_message_id = str(message_id_to_store) if message_id_to_store else None
            db_streamer_login = login_lower

            pending_state_rows.append(
                (
                    db_user_id,
                    db_streamer_login,
                    int(is_live),
                    now_iso,
                    (stream.get("title") if stream else None),
                    (stream.get("game_name") if stream else None),
                    int(stream.get("viewer_count") or 0) if stream else 0,
                    db_message_id,
                    tracking_token_to_store,
                )
            )

            if need_link and self._alert_channel_id and (now_utc.minute % 10 == 0) and is_live:
                # Platzhalter für deinen Profil-/Panel-Check
                pass

        await self._persist_live_state_rows(pending_state_rows)

    async def _persist_live_state_rows(
        self,
        rows: List[
            tuple[str, str, int, str, Optional[str], Optional[str], int, Optional[str], Optional[str]]
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
                        "(twitch_user_id, streamer_login, is_live, last_seen_at, last_title, last_game, last_viewer_count, last_discord_message_id, last_tracking_token) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        url = self._build_referral_url(login)
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
            url=url,
            colour=discord.Color(TWITCH_BRAND_COLOR_HEX),
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
        embed.set_author(name=f"🔴 {display_name}", url=url)

        return embed

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
        try:
            self.bot.remove_view(view)
        except Exception:
            log.debug("Konnte View nicht deregistrieren: %s", tracking_token, exc_info=True)
        view.stop()

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
        self._parent = parent

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self._parent.handle_click(interaction)


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
