from __future__ import annotations

import os
import asyncio
import logging
import re
import sqlite3
from typing import Optional, Dict, Union, Tuple, Iterable
from urllib.parse import urlparse, urlunparse

import discord
from discord.ext import commands

from service import db

from cogs.steam import (
    SCHNELL_LINK_CUSTOM_ID,
    SchnellLinkButton,
    respond_with_schnelllink,
)
from cogs.steam.friend_requests import queue_friend_request
from cogs.steam.logging_utils import safe_log_extra
from cogs.welcome_dm.step_steam_link import steam_link_detailed_description


log = logging.getLogger("SteamVoiceNudge")

# ---------- Einstellungen ----------
MIN_VOICE_MINUTES = 30          # Mindest-Verweildauer im Voice (einmalig)
POLL_INTERVAL = 15              # Sekunden – Voice-Alive-Check
DEFAULT_TEST_TARGET_ID = int(os.getenv("NUDGE_TEST_DEFAULT_ID", "0"))
LOG_CHANNEL_ID = 1374364800817303632  # Meldungen in diesen Kanal posten
NUDGE_VIEW_VERSION = 1          # Version der persistierten DM-View

# Rollen mit Opt-Out (werden NICHT kontaktiert)
# Standard enthält die gewünschte English-only Rolle: 1309741866098491479
_EXEMPT_DEFAULT = "1309741866098491479"
EXEMPT_ROLE_IDS = {
    int(x) for x in os.getenv("NUDGE_EXEMPT_ROLE_IDS", _EXEMPT_DEFAULT).split(",")
    if x.strip().isdigit()
}

# Deep-Link Toggle für Discord OAuth
_DEEPLINK_EN = str(os.getenv("DISCORD_OAUTH_DEEPLINK", "0")).strip().lower() not in ("", "0", "false", "no")

def _prefer_discord_deeplink(browser_url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    (primary_url, browser_fallback).
    Aktiviert 'discord://-/oauth2/authorize?...' als primary, wenn möglich.
    """
    if not browser_url:
        return None, None
    try:
        u = urlparse(browser_url)
        hostname = (u.hostname or "").lower()
        path = u.path or ""
        if (
            _DEEPLINK_EN
            and u.scheme in {"http", "https"}
            and hostname
            and (hostname == "discord.com" or hostname.endswith(".discord.com"))
            and (path == "/oauth2/authorize" or path.startswith("/oauth2/authorize/"))
        ):
            deeplink = urlunparse(("discord", "-/oauth2/authorize", "", "", u.query, ""))
            return deeplink, browser_url
    except Exception as exc:
        log.debug("[nudge] Deeplink-Erkennung schlug fehl für %r: %s", browser_url, exc)
    return browser_url, None

# ---------- DB ----------
def _save_steam_link_row(user_id: int, steam_id: str, name: str = "", verified: int = 0) -> None:
    _ensure_schema()
    db.execute(
        """
        INSERT INTO steam_links(user_id, steam_id, name, verified)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id, steam_id) DO UPDATE SET
          name=excluded.name,
          verified=excluded.verified,
          updated_at=CURRENT_TIMESTAMP
        """,
        (int(user_id), str(steam_id), name or "", int(verified)),
    )
    try:
        queue_friend_request(steam_id)
    except Exception:
        log.exception(
            "[nudge] Konnte Steam-Freundschaftsanfrage nicht einreihen",
            extra=safe_log_extra({"steam_id": steam_id}),
        )

def _ensure_schema():
    db.execute("""
        CREATE TABLE IF NOT EXISTS steam_nudge_state(
          user_id     INTEGER PRIMARY KEY,
          notified_at DATETIME,
          first_seen  DATETIME DEFAULT CURRENT_TIMESTAMP,
          message_id  INTEGER,
          channel_id  INTEGER,
          view_version INTEGER DEFAULT 0
        )
    """)
    for sql in (
        "ALTER TABLE steam_nudge_state ADD COLUMN message_id INTEGER",
        "ALTER TABLE steam_nudge_state ADD COLUMN channel_id INTEGER",
        "ALTER TABLE steam_nudge_state ADD COLUMN view_version INTEGER DEFAULT 0",
    ):
        try:
            db.execute(sql)
        except sqlite3.OperationalError as exc:
            log.debug("[nudge] Konnte Schema-Änderung nicht anwenden (%s): %s", sql, exc)
    db.execute("""
        CREATE TABLE IF NOT EXISTS steam_links(
          user_id         INTEGER NOT NULL,
          steam_id        TEXT    NOT NULL,
          name            TEXT,
          verified        INTEGER DEFAULT 0,
          primary_account INTEGER DEFAULT 0,
          created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
          updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (user_id, steam_id)
        )
    """)

def _has_any_steam_link(user_id: int) -> bool:
    _ensure_schema()
    row = db.query_one("SELECT 1 FROM steam_links WHERE user_id=? LIMIT 1", (int(user_id),))
    return bool(row)

def _load_nudge_state(user_id: int) -> Optional[sqlite3.Row]:
    _ensure_schema()
    return db.query_one(
        "SELECT user_id, notified_at, first_seen, message_id, channel_id, view_version FROM steam_nudge_state WHERE user_id=?",
        (int(user_id),),
    )


def _iter_nudge_states() -> Iterable[sqlite3.Row]:
    _ensure_schema()
    return db.query_all(
        "SELECT user_id, notified_at, first_seen, message_id, channel_id, view_version FROM steam_nudge_state",
    )


def _mark_notified(
    user_id: int,
    *,
    message_id: Optional[int],
    channel_id: Optional[int],
    view_version: int = NUDGE_VIEW_VERSION,
) -> None:
    _ensure_schema()
    db.execute(
        """
        INSERT INTO steam_nudge_state(user_id, notified_at, message_id, channel_id, view_version)
        VALUES(?, CURRENT_TIMESTAMP, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
          notified_at=excluded.notified_at,
          message_id=excluded.message_id,
          channel_id=excluded.channel_id,
          view_version=excluded.view_version
        """,
        (int(user_id), message_id, channel_id, view_version),
    )


def _clear_nudge_state(user_id: int) -> None:
    _ensure_schema()
    db.execute(
        "UPDATE steam_nudge_state SET message_id=NULL, channel_id=NULL, view_version=0 WHERE user_id=?",
        (int(user_id),),
    )

def _member_has_exempt_role(member: discord.Member) -> bool:
    return any((r.id in EXEMPT_ROLE_IDS) for r in getattr(member, "roles", []) or [])

def _log_chan(bot: commands.Bot) -> Optional[discord.TextChannel]:
    ch = bot.get_channel(LOG_CHANNEL_ID)
    return ch if isinstance(ch, discord.TextChannel) else None

# ---------- Voice-Monitor ----------
async def _count_voice_minutes(member: discord.Member, minutes: int) -> bool:
    """
    Wartet bis zu `minutes` Minuten, während der Member (irgendeinem) Voice-Channel angehört.
    Gibt True zurück, wenn er die gesamte Zeit im Voice war (abzügl. kurzer Poll-Gaps),
    sonst False, wenn er vorher abhaut.
    """
    seen = 0
    try:
        while seen < minutes * 60:
            await asyncio.sleep(POLL_INTERVAL)
            vc = getattr(member, "voice", None)
            if not vc or not vc.channel:
                return False
            seen += POLL_INTERVAL
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("voice wait failed for member=%s", getattr(member, "id", "?"))
        return False

# ---------- OAuth/OpenID Hilfen ----------
def _find_steamlink_cog(bot: commands.Bot):
    # 1) explizit
    for name in ("SteamLink", "SteamLinkOAuth", "SteamLinkOpenID"):
        cog = bot.get_cog(name)
        if cog:
            return cog
    # 2) heuristisch
    for name, cog in bot.cogs.items():
        low = name.lower()
        if "steam" in low and ("oauth" in low or "link" in low or "openid" in low):
            return cog
    return None

async def _fetch_oauth_urls(bot: commands.Bot, user: Union[discord.User, discord.Member]) -> Tuple[Optional[str], Optional[str]]:
    """
    Holt gültige (server-registrierte) Start-URLs vom SteamLink-OAuth-Cog.
    Bevorzugt Lazy-Start (state wird erst beim Klick erzeugt).
    Gibt (discord_start_url, steam_start_url) zurück oder (None, None) als Fallback.
    """
    cog = _find_steamlink_cog(bot)
    if cog:
        try:
            if hasattr(cog, "discord_start_url_for"):
                d = cog.discord_start_url_for(int(user.id))
            else:
                d = cog.build_discord_link_for(int(user.id))  # falls vorhanden
        except Exception:
            log.exception("fetch discord oauth url failed")
            d = None
        try:
            if hasattr(cog, "steam_start_url_for"):
                s = cog.steam_start_url_for(int(user.id))
            else:
                state = cog._mk_state(int(user.id))  # type: ignore[attr-defined]
                s = cog._build_steam_login_url(state)  # type: ignore[attr-defined]
        except Exception:
            log.exception("fetch steam openid url failed")
            s = None
        return d or None, s or None
    return None, None

# ---------- View/Modal ----------
class _ManualModal(discord.ui.Modal, title="Steam manuell verknüpfen"):
    steam_input = discord.ui.TextInput(
        label="Profil-Link, Vanity oder SteamID64",
        placeholder="z. B. https://steamcommunity.com/id/DeinName oder 7656119…",
        required=True,
        max_length=120,
        custom_id="nudge_manual_input",
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.steam_input.value).strip()
        steam_id: Optional[str] = None
        persona: Optional[str] = None

        # Try resolve via SteamLink cog (handles vanity + links)
        try:
            steam_cog = interaction.client.get_cog("SteamLink")
        except Exception:
            steam_cog = None

        if steam_cog:
            try:
                steam_id = await steam_cog._resolve_steam_input(raw)  # type: ignore[attr-defined]
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("[nudge] resolve via SteamLink cog failed: %r", e)
                steam_id = None
            if steam_id:
                try:
                    persona = await steam_cog._fetch_persona(steam_id)  # type: ignore[attr-defined]
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("[nudge] persona fetch failed: %r", e)
                    persona = None

        # Fallback: accept 17-digit or /profiles/<id> links only (no vanity here)
        if not steam_id:
            s = raw
            if re.fullmatch(r"\d{17}", s):
                steam_id = s
            else:
                try:
                    u = urlparse(s)
                except Exception:
                    u = None
                if u and (u.scheme in ("http", "https")):
                    host = (u.hostname or "").lower().rstrip(".")
                    path = (u.path or "").rstrip("/")
                    if host == "steamcommunity.com" or host.endswith(".steamcommunity.com"):
                        m2 = re.fullmatch(r"/profiles/(\d{17})", path)
                        if m2:
                            steam_id = m2.group(1)

        if not steam_id:
            try:
                await interaction.response.send_message(
                    "❌ Konnte keine gültige SteamID bestimmen.\n"
                    "Nutze bitte die **17-stellige SteamID64** oder einen **steamcommunity.com/profiles/<id>**-Link.\n"
                    "Für **Vanity**-URLs verwende „Via Discord verknüpfen“ oder „Mit Steam anmelden“.",
                    ephemeral=True,
                )
            except Exception as exc:
                log.warning(
                    "[nudge] Konnte Fehlerhinweis an %s nicht senden: %s",
                    interaction.user.id,
                    exc,
                )
            return

        try:
            _save_steam_link_row(interaction.user.id, steam_id, persona or "", verified=0)
            await interaction.response.send_message(
                f"✅ Hinzugefügt: `{steam_id}` (manuell). Prüfe **/links**, setze **/setprimary**.",
                ephemeral=True,
            )
            try:
                await interaction.user.send(f"✅ Verknüpft (manuell): **{steam_id}**")
            except Exception as e:
                log.debug("[nudge] DM notify failed: %r", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[nudge] Manual Steam link failed: %r", e)
            try:
                await interaction.response.send_message("❌ Unerwarteter Fehler beim manuellen Verknüpfen.", ephemeral=True)
            except Exception as exc:
                log.warning(
                    "[nudge] Konnte Fehler-DM beim manuellen Verknüpfen nicht senden (User %s): %s",
                    interaction.user.id,
                    exc,
                )

class _ManualButton(discord.ui.Button):
    def __init__(self, row: int = 0):
        super().__init__(label="SteamID manuell eingeben", style=discord.ButtonStyle.primary,
                         emoji="🔢", custom_id="nudge_manual", row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_ManualModal())

class _CloseButton(discord.ui.Button):
    def __init__(self, row: int = 1):
        super().__init__(label="Schließen", style=discord.ButtonStyle.secondary,
                         emoji="❌", custom_id="nudge_close", row=row)

    async def callback(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("close-button defer failed: %r", e)
        try:
            await interaction.message.delete()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("close-button delete failed: %r", e)

class _OptionsView(discord.ui.View):
    """
    Nicht-persistente Instanz (enthält benutzerspezifische Link-URLs),
    aber die custom_id-Buttons registrieren wir zusätzlich in _PersistentRegistryView,
    damit Interaktionen auch nach Reboot klappen.
    """
    def __init__(self, *, discord_oauth_url: Optional[str], steam_openid_url: Optional[str]):
        super().__init__(timeout=None)

        if discord_oauth_url:
            self.add_item(discord.ui.Button(
                label="Via Discord verknüpfen", style=discord.ButtonStyle.link,
                url=discord_oauth_url, emoji="🔗", row=0
            ))
        else:
            self.add_item(discord.ui.Button(
                label="Via Discord verknüpfen (/link)", style=discord.ButtonStyle.secondary,
                disabled=True, emoji="🔗", row=0
            ))

        if steam_openid_url:
            self.add_item(discord.ui.Button(
                label="Mit Steam anmelden", style=discord.ButtonStyle.link,
                url=steam_openid_url, emoji="🎮", row=0
            ))
        else:
            self.add_item(discord.ui.Button(
                label="Mit Steam anmelden (/link_steam)", style=discord.ButtonStyle.secondary,
                disabled=True, emoji="🎮", row=0
            ))

        self.add_item(SchnellLinkButton(row=1, source="voice_nudge_view"))
        self.add_item(_ManualButton(row=1))
        self.add_item(_CloseButton(row=1))

# Persistente Registry-View (falls weitere Buttons mit custom_id notwendig)
class _PersistentRegistryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="SteamID manuell eingeben", style=discord.ButtonStyle.primary,
                       emoji="🔢", custom_id="nudge_manual")
    async def _open_manual(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_ManualModal())

    @discord.ui.button(
        label="Schnelle Anfrage senden",
        style=discord.ButtonStyle.success,
        emoji="⚡",
        custom_id=SCHNELL_LINK_CUSTOM_ID,
    )
    async def _quick_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        await respond_with_schnelllink(interaction, source="voice_nudge_persistent")

    @discord.ui.button(label="Schließen", style=discord.ButtonStyle.secondary,
                       emoji="❌", custom_id="nudge_close")
    async def _close(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("[nudge] Close-Button konnte nicht deferen: %s", exc)
        try:
            await interaction.message.delete()
        except Exception as exc:
            log.debug("[nudge] Konnte Interaktionsnachricht nicht löschen: %s", exc)

# ---------- Cog ----------
class SteamLinkVoiceNudge(commands.Cog):
    """
    Nudge-Nachricht in DMs, wenn Member lange genug im Voice war und noch kein Steam-Link hinterlegt ist.
    Mit Rollen-Opt-Out, Logging und robustem Cleanup.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._tasks: Dict[int, asyncio.Task] = {}
        self._restore_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        self.bot.add_view(_PersistentRegistryView())
        try:
            self._restore_task = asyncio.create_task(self._restore_persistent_messages())
        except Exception:
            log.exception("[nudge] Konnte Persistenz-Wiederherstellung nicht starten")

    async def cog_unload(self):
        for uid, t in list(self._tasks.items()):
            try:
                t.cancel()
            except Exception as exc:
                log.debug("[nudge] Konnte Task %s nicht canceln: %s", uid, exc)
        self._tasks.clear()
        if self._restore_task:
            try:
                self._restore_task.cancel()
            except Exception as exc:
                log.debug("[nudge] Konnte Restore-Task nicht canceln: %s", exc)
            self._restore_task = None

    async def _restore_persistent_messages(self) -> None:
        try:
            rows = list(_iter_nudge_states())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[nudge] Konnte Nudge-States nicht laden")
            return

        for row in rows:
            try:
                user_id = int(row["user_id"])
            except Exception:
                continue

            message_id = row.get("message_id") if isinstance(row, dict) else row["message_id"]
            if not message_id:
                continue

            user: Optional[Union[discord.Member, discord.User]] = self.bot.get_user(user_id)
            if not user:
                try:
                    user = await self.bot.fetch_user(user_id)
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    user = None
            if not user:
                continue

            message = await self._fetch_nudge_message(user, row)
            if not message:
                _clear_nudge_state(user_id)
                continue

            try:
                embed, view = await self._build_dm_payload(user)
                await message.edit(embed=embed, view=view)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug(
                    "[nudge] Persistente DM konnte nicht aktualisiert werden: %r",
                    exc,
                    extra=safe_log_extra({"user_id": user_id}),
                )
                continue

            _mark_notified(
                user_id,
                message_id=message.id,
                channel_id=message.channel.id,
                view_version=NUDGE_VIEW_VERSION,
            )

    async def _build_dm_payload(
        self, user: Union[discord.User, discord.Member]
    ) -> Tuple[discord.Embed, _OptionsView]:
        discord_url, steam_url = await _fetch_oauth_urls(self.bot, user)
        primary_discord, browser_fallback = _prefer_discord_deeplink(discord_url)

        desc = steam_link_detailed_description()
        if browser_fallback and (primary_discord or "").startswith("discord://"):
            desc += f"\n\n_Falls sich nichts öffnet:_ [Browser-Variante]({browser_fallback})"
        if not primary_discord or not steam_url:
            desc += "\n\n_Heads-up:_ Der Link-Dienst ist gerade nicht verfügbar. Nutze vorerst **/link** oder **/link_steam**."

        embed = discord.Embed(
            title="Kleiner Tipp für besseres Voice-Erlebnis 🎧",
            description=desc,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Kurzbefehle: /link · /link_steam · /addsteam · /unlink · /setprimary")

        view = _OptionsView(discord_oauth_url=primary_discord, steam_openid_url=steam_url)
        return embed, view

    async def _fetch_nudge_message(
        self,
        user: Union[discord.Member, discord.User],
        state: sqlite3.Row,
    ) -> Optional[discord.Message]:
        message_id = state["message_id"] if "message_id" in state.keys() else None  # type: ignore[index]
        if not message_id:
            return None

        channel_id = state["channel_id"] if "channel_id" in state.keys() else None  # type: ignore[index]
        dm_channel: Optional[discord.DMChannel] = None

        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(channel_id))
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    channel = None
            if isinstance(channel, discord.DMChannel):
                dm_channel = channel

        if dm_channel is None:
            try:
                dm_channel = user.dm_channel or await user.create_dm()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("[nudge] DM-Channel konnte nicht geöffnet werden: %r", exc)
                return None

        try:
            return await dm_channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden):
            return None
        except discord.HTTPException as exc:
            log.debug("[nudge] DM-Message fetch fehlgeschlagen: %r", exc)
            return None

    async def _has_active_nudge(self, member: discord.Member) -> bool:
        state = _load_nudge_state(member.id)
        if not state:
            return False

        message = await self._fetch_nudge_message(member, state)
        if not message:
            _clear_nudge_state(member.id)
            return False

        try:
            stored_version = int(state.get("view_version", 0)) if hasattr(state, "get") else int(state["view_version"] or 0)
        except Exception:
            stored_version = 0

        if stored_version < NUDGE_VIEW_VERSION:
            try:
                embed, view = await self._build_dm_payload(member)
                await message.edit(embed=embed, view=view)
                _mark_notified(
                    member.id,
                    message_id=message.id,
                    channel_id=message.channel.id,
                    view_version=NUDGE_VIEW_VERSION,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("[nudge] Aktualisierung der vorhandenen Nudge-DM fehlgeschlagen: %r", exc)

        return True

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        try:
            if (before.channel is None) and (after.channel is not None):
                if _member_has_exempt_role(member):
                    log.debug("[nudge] skip exempt member id=%s", member.id)
                    return
                if _has_any_steam_link(member.id):
                    log.debug("[nudge] user already linked id=%s", member.id)
                    return
                if await self._has_active_nudge(member):
                    log.debug("[nudge] user already notified id=%s", member.id)
                    return
                if member.id in self._tasks and not self._tasks[member.id].done():
                    try:
                        self._tasks[member.id].cancel()
                    except Exception as exc:
                        log.debug("[nudge] Bestehenden Task für %s konnte nicht abgebrochen werden: %s", member.id, exc)
                self._tasks[member.id] = asyncio.create_task(self._wait_and_notify(member))
        except Exception:
            log.exception("on_voice_state_update error")

    async def _send_dm_nudge(self, user: Union[discord.User, discord.Member], *, force: bool = False) -> bool:
        uid = int(user.id)
        if not force and _has_any_steam_link(uid):
            return False

        try:
            if not force:
                state = _load_nudge_state(uid)
            else:
                state = None

            if state:
                message = await self._fetch_nudge_message(user, state)
                if message:
                    try:
                        stored_version = int(state.get("view_version", 0)) if hasattr(state, "get") else int(state["view_version"] or 0)
                    except Exception:
                        stored_version = 0

                    if stored_version < NUDGE_VIEW_VERSION:
                        try:
                            embed, view = await self._build_dm_payload(user)
                            await message.edit(embed=embed, view=view)
                            _mark_notified(
                                uid,
                                message_id=message.id,
                                channel_id=message.channel.id,
                                view_version=NUDGE_VIEW_VERSION,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.debug("[nudge] Aktualisierung bestehender Nudge-DM fehlgeschlagen: %r", exc)
                    return False
                else:
                    _clear_nudge_state(uid)

            dm = user.dm_channel or await user.create_dm()
            embed, view = await self._build_dm_payload(user)
            message = await dm.send(embed=embed, view=view)

            _mark_notified(
                uid,
                message_id=message.id,
                channel_id=dm.id,
                view_version=NUDGE_VIEW_VERSION,
            )

            ch = _log_chan(self.bot)
            if ch:
                await ch.send(f"📨 Nudge-DM an **{user}** ({uid}) gesendet.")
            return True

        except asyncio.CancelledError:
            raise
        except discord.Forbidden:
            ch = _log_chan(self.bot)
            if ch:
                await ch.send(f"⚠️ Nudge-DM an **{user}** ({uid}) fehlgeschlagen: DMs deaktiviert.")
            return False
        except Exception as e:
            log.exception("[nudge] Fehler beim Senden der DM")
            ch = _log_chan(self.bot)
            if ch:
                await ch.send(f"❌ Nudge-DM an **{user}** ({uid}) fehlgeschlagen: `{e}`")
            return False

    async def _wait_and_notify(self, member: discord.Member):
        try:
            if _member_has_exempt_role(member):
                ch = _log_chan(self.bot)
                if ch:
                    await ch.send(f"ℹ️ Übersprungen (Exempt): **{member}** ({member.id})")
                return

            ok = await _count_voice_minutes(member, MIN_VOICE_MINUTES)
            if not ok:
                log.debug("[nudge] %s left voice early – abort", member.id)
                return

            if _has_any_steam_link(member.id):
                log.debug("[nudge] already linked after wait, skip")
                return

            await self._send_dm_nudge(member)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("wait_and_notify failed")

    @commands.hybrid_command(
        name="nudgesend",
        description="(Admin) Schickt die Steam-Nudge-DM an einen Nutzer.",
        aliases=("t30",),
    )
    @commands.has_permissions(administrator=True)
    async def nudgesend(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User]] = None,
    ):
        target = await self._resolve_test_target(ctx, target)
        if not target:
            await ctx.reply("Bitte Ziel angeben: `!nudgesend @user`", mention_author=False)
            return

        if isinstance(target, discord.Member) and _member_has_exempt_role(target):
            await ctx.reply("ℹ️ Test abgebrochen: Ziel hat eine ausgenommene Rolle.", mention_author=False)
            return

        ok = await self._send_dm_nudge(target, force=True)
        if ok:
            await ctx.reply(f"📨 Test-DM an {getattr(target, 'mention', target.id)} gesendet.", mention_author=False)
        else:
            await ctx.reply("⚠️ Test-DM konnte nicht gesendet werden (DMs aus? oder bereits benachrichtigt).", mention_author=False)

    async def _resolve_test_target(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User]],
    ) -> Optional[Union[discord.Member, discord.User]]:
        if target:
            return target

        if not DEFAULT_TEST_TARGET_ID:
            return None

        # Try resolve as guild member first.
        guild = getattr(ctx, "guild", None)
        if guild:
            member = guild.get_member(DEFAULT_TEST_TARGET_ID)
            if member:
                return member
            try:
                member = await guild.fetch_member(DEFAULT_TEST_TARGET_ID)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                member = None
            if member:
                return member

        # Fall back to any known user object in cache/API.
        user = self.bot.get_user(DEFAULT_TEST_TARGET_ID)
        if user:
            return user

        try:
            return await self.bot.fetch_user(DEFAULT_TEST_TARGET_ID)
        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
            return None


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamLinkVoiceNudge(bot))
