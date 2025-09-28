# cogs/live_match/steam_link_voice_nudge.py
from __future__ import annotations

import os
import asyncio
import logging
import inspect
from typing import Optional, Dict, Union, Tuple

import discord
from discord.ext import commands

from service import db

log = logging.getLogger("SteamVoiceNudge")

# ---------- Einstellungen ----------
MIN_VOICE_MINUTES = 30          # Mindest-Verweildauer im Voice (einmalig)
POLL_INTERVAL = 15              # Sekunden – Voice-Alive-Check
DEFAULT_TEST_TARGET_ID = int(os.getenv("NUDGE_TEST_DEFAULT_ID", "0"))
LOG_CHANNEL_ID = 1374364800817303632  # Meldungen in diesen Kanal posten

# Rollen mit Opt-Out (werden NICHT kontaktiert)
# Standard enthält die gewünschte English-only Rolle: 1309741866098491479
_EXEMPT_DEFAULT = "1309741866098491479"
EXEMPT_ROLE_IDS = {
    int(x) for x in os.getenv("NUDGE_EXEMPT_ROLE_IDS", _EXEMPT_DEFAULT).split(",")
    if x.strip().isdigit()
}

# ---------- DB ----------
def _ensure_schema():
    db.execute("""
        CREATE TABLE IF NOT EXISTS steam_nudge_state(
          user_id     INTEGER PRIMARY KEY,
          notified_at DATETIME,
          first_seen  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
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

def _already_notified(user_id: int) -> bool:
    _ensure_schema()
    row = db.query_one("SELECT notified_at FROM steam_nudge_state WHERE user_id=?", (int(user_id),))
    return bool(row and row["notified_at"])

def _mark_notified(user_id: int):
    _ensure_schema()
    db.execute("""
        INSERT INTO steam_nudge_state(user_id, notified_at)
        VALUES(?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET notified_at=CURRENT_TIMESTAMP
    """, (int(user_id),))

def _table_exists(name: str) -> bool:
    try:
        row = db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return bool(row)
    except Exception as e:
        log.debug("table_exists(%s) failed: %r", name, e)
        return False

def _had_prior_long_voice_session(user_id: int, threshold_sec: int) -> bool:
    _ensure_schema()
    candidates = []
    if _table_exists("voice_sessions"):
        candidates += [
            ("SELECT 1 FROM voice_sessions WHERE user_id=? AND duration_seconds>=? LIMIT 1", (user_id, threshold_sec)),
            ("SELECT 1 FROM voice_sessions WHERE user_id=? AND duration>=? LIMIT 1", (user_id, threshold_sec)),
            ("SELECT 1 FROM voice_sessions WHERE user_id=? AND total_seconds>=? LIMIT 1", (user_id, threshold_sec)),
        ]
    if _table_exists("vat_sessions"):
        candidates += [
            ("SELECT 1 FROM vat_sessions WHERE user_id=? AND duration_seconds>=? LIMIT 1", (user_id, threshold_sec)),
            ("SELECT 1 FROM vat_sessions WHERE user_id=? AND duration>=? LIMIT 1", (user_id, threshold_sec)),
        ]
    if _table_exists("voice_activity"):
        candidates += [
            ("SELECT 1 FROM voice_activity WHERE user_id=? AND duration_seconds>=? LIMIT 1", (user_id, threshold_sec)),
            ("SELECT 1 FROM voice_activity WHERE user_id=? AND duration>=? LIMIT 1", (user_id, threshold_sec)),
        ]
    for sql, params in candidates:
        try:
            row = db.query_one(sql, params)
            if row:
                return True
        except Exception as e:
            log.debug("query candidate failed (%s): %r", sql, e)
            continue
    return False


# ---------- Utilities ----------
def _member_has_exempt_role(member: discord.Member) -> bool:
    try:
        return any((r.id in EXEMPT_ROLE_IDS) for r in getattr(member, "roles", []) if isinstance(r, discord.Role))
    except Exception as e:
        log.debug("member_has_exempt_role failed for %s: %r", getattr(member, "id", None), e)
        return False

async def _cleanup_bot_dms(user: Union[discord.User, discord.Member], bot_user_id: int, limit: int = 50):
    try:
        dm = user.dm_channel or await user.create_dm()
        async for msg in dm.history(limit=limit):
            if msg.author and msg.author.id == bot_user_id:
                try:
                    await msg.delete()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("delete DM message failed for %s: %r", user.id, e)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.debug(f"[nudge] DM-Cleanup für {user.id} übersprungen: {e}")

def _log_chan(bot: commands.Bot) -> Optional[discord.TextChannel]:
    ch = bot.get_channel(LOG_CHANNEL_ID)
    return ch if isinstance(ch, discord.TextChannel) else None

async def _maybe_call(obj, names: Tuple[str, ...], *args, **kwargs):
    """Ruft die erste existierende Methode/Funktion aus `names` auf (sync/async)."""
    for n in names:
        if not hasattr(obj, n):
            continue
        fn = getattr(obj, n)
        if callable(fn):
            res = fn(*args, **kwargs)
            if inspect.isawaitable(res):
                res = await res
            return res
    return None

def _find_steam_oauth_cog(bot: commands.Bot):
    # 1) feste Namen
    for name in ("SteamLink", "SteamLinkOAuth", "SteamOAuth", "SteamLinkOpenID"):
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
    cog = _find_steam_oauth_cog(bot)
    if not cog:
        log.warning("[nudge] SteamLink OAuth-Cog nicht gefunden – Link-Buttons werden ausgeblendet.")
        return None, None

    uid = int(user.id)

    # 👉 Priorität: Lazy-Start-Methoden zuerst, dann rückwärtskompatible Builder.
    discord_methods = (
        "discord_start_url_for",         # bevorzugt
        "public_discord_oauth_url_for",  # evtl. alternative Namensgebung
        "discord_oauth_url_for",         # evtl. alternative Namensgebung
        "get_discord_link_url_for",      # evtl. alternative Namensgebung
        "get_discord_oauth_url_for",     # evtl. alternative Namensgebung
        "build_discord_link_for",        # Fallback: erzeugt state SOFORT (nicht ideal)
        "build_discord_oauth_url_for",   # Fallback-Variante
        "make_discord_oauth_url",        # Fallback-Variante
    )
    steam_methods = (
        "steam_start_url_for",           # bevorzugt
        "public_steam_openid_url_for",   # evtl. alternative Namensgebung
        "steam_openid_url_for",          # evtl. alternative Namensgebung
        "get_steam_openid_url_for",      # evtl. alternative Namensgebung
        "build_steam_openid_for",        # Fallback: erzeugt state SOFORT (nicht ideal)
        "build_steam_openid_url_for",    # Fallback-Variante
        "make_steam_openid_url",         # Fallback-Variante
    )

    discord_url = await _maybe_call(cog, discord_methods, uid)
    steam_url   = await _maybe_call(cog, steam_methods,   uid)

    # Fallback: versuche das Modul selbst (falls die Helper als Modulexporte existieren)
    if (not discord_url or not steam_url) and hasattr(cog, "__module__"):
        try:
            mod = __import__(cog.__module__, fromlist=["*"])
            if not discord_url:
                discord_url = await _maybe_call(mod, discord_methods, uid)
            if not steam_url:
                steam_url = await _maybe_call(mod, steam_methods,   uid)
        except Exception as e:
            log.debug("oauth url module fallback failed: %r", e)

    if not discord_url or not steam_url:
        log.warning("[nudge] OAuth-Start-URLs nicht verfügbar – Buttons werden deaktiviert, Hinweis auf /link gezeigt.")
        return None, None

    try:
        log.info(f"[nudge] OAuth-Start-URLs bereit (discord={str(discord_url)[:60]}…, steam={str(steam_url)[:60]}…)")
    except Exception as e:
        log.debug("logging oauth urls failed: %r", e)
    return str(discord_url), str(steam_url)


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
        txt = str(self.steam_input.value).strip()
        steamid64 = txt  # (echte Validierung übernimmt dein Backend/Cog)

        try:
            _ensure_schema()
            db.execute("""
                INSERT INTO steam_links(user_id, steam_id, name, verified, primary_account)
                VALUES(?,?,?,?,?)
                ON CONFLICT(user_id, steam_id) DO UPDATE SET updated_at=CURRENT_TIMESTAMP
            """, (int(interaction.user.id), steamid64, None, 0, 0))
            await interaction.response.send_message(
                "✅ Eingang gespeichert! Wir prüfen/verwenden das beim nächsten Check. "
                "Du kannst auch `/setprimary` nutzen, wenn mehrere Einträge vorhanden sind.",
                ephemeral=True
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Manual link insert failed")
            await interaction.response.send_message(f"⚠️ Konnte den Eintrag nicht speichern: {e}", ephemeral=True)

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

        # Reihe 1: zwei Link-Buttons (grau, öffnen extern) + manuell
        if discord_oauth_url:
            self.add_item(discord.ui.Button(
                label="Mit Discord verbinden", style=discord.ButtonStyle.link,
                url=discord_oauth_url, emoji="🔗", row=0
            ))
        else:
            self.add_item(discord.ui.Button(
                label="Mit Discord verbinden (/link)", style=discord.ButtonStyle.secondary,
                disabled=True, emoji="🔗", row=0
            ))

        self.add_item(_ManualButton(row=0))

        if steam_openid_url:
            self.add_item(discord.ui.Button(
                label="Mit Steam anmelden", style=discord.ButtonStyle.link,
                url=steam_openid_url, emoji="🛰️", row=0
            ))
        else:
            self.add_item(discord.ui.Button(
                label="Mit Steam anmelden", style=discord.ButtonStyle.secondary,
                disabled=True, emoji="🛰️", row=0
            ))

        # Reihe 2: Schließen
        self.add_item(_CloseButton(row=1))

class _PersistentRegistryView(discord.ui.View):
    """Registriert die custom_id Buttons global, damit Interaktionen nach Reboot funktionieren."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(_ManualButton())
        self.add_item(_CloseButton())


# ---------- Cog ----------
class SteamLinkVoiceNudge(commands.Cog):
    """Schickt nach *erstem* ≥30-Min-Voice-Join eine freundliche DM zum Steam-Linken (einmalig)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._watch_tasks: Dict[int, asyncio.Task] = {}

    async def cog_load(self):
        # persistente Buttons registrieren (nur custom_id-basierte)
        self.bot.add_view(_PersistentRegistryView())

    # --- Helper ---
    async def _still_in_voice(self, member: discord.Member) -> bool:
        try:
            m = member.guild.get_member(member.id) or await member.guild.fetch_member(member.id)
            return bool(m.voice and m.voice.channel)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("still_in_voice check failed for %s: %r", member.id, e)
            return False

    async def _send_dm_nudge(self, user: Union[discord.Member, discord.User], *, force: bool = False) -> bool:
        # Exempt-Rollen NIE kontaktieren – auch nicht bei force
        if isinstance(user, discord.Member) and _member_has_exempt_role(user):
            ch = _log_chan(self.bot)
            if ch:
                await ch.send(f"ℹ️ Nudge übersprungen (Exempt-Rolle) für **{user}** ({int(user.id)}).")
            return False

        uid = int(user.id)
        if not force:
            if _already_notified(uid) or _has_any_steam_link(uid) or _had_prior_long_voice_session(uid, MIN_VOICE_MINUTES * 60):
                return False

        # Sicherheit: Falls während Wartezeit Rolle hinzugekommen ist
        if isinstance(user, discord.Member) and _member_has_exempt_role(user):
            ch = _log_chan(self.bot)
            if ch:
                await ch.send(f"ℹ️ Nudge abgebrochen (Exempt-Rolle erkannt) für **{user}** ({uid}).")
            return False

        await _cleanup_bot_dms(user, self.bot.user.id if self.bot.user else 0, limit=50)

        try:
            dm = user.dm_channel or await user.create_dm()

            # URLs vom OAuth-Cog holen (Lazy-Start bevorzugt; state wird serverseitig erst beim Klick erzeugt)
            discord_url, steam_url = await _fetch_oauth_urls(self.bot, user)

            desc = (
                "Cool, dass du aktiv im Voice bist! 💙\n\n"
                "Damit wir **einheitlich** anzeigen können, wer **in der Lobby** ist und wer **im Match**, "
                "hilft uns die Verknüpfung zwischen Discord und Steam.\n\n"
                "• So können wir, **wenn du im Voice bist**, checken, ob du **gerade in Deadlock im Match** bist.\n"
                "• Ergebnis: präzisere **Kanal-Beschreibungen** (z. B. „3 im Match“) & bessere **Orga/Balancing** bei Events.\n\n"
                "**Wie kannst du dabei helfen?**\n"
                "1) Klicke **„Mit Discord verbinden“**, **„SteamID manuell eingeben“** oder **„Mit Steam anmelden“**.\n"
                "2) Folge den kurzen Schritten. Wir bekommen niemals dein Passwort – bei Steam erhalten wir nur die **SteamID64**.\n\n"
                "**Wichtig:** In Steam → Profil → **Datenschutzeinstellungen** → **Spieldetails = Öffentlich** "
                "(und **Gesamtspielzeit** nicht auf „immer privat“).\n\n"
                "Du kannst dich **jederzeit abmelden** (z. B. mit `/unlink`)."
            )
            if not discord_url or not steam_url:
                desc += "\n\n_Heads-up:_ Der Link-Dienst ist gerade nicht verfügbar. Nutze vorerst **/link** oder **/link_steam**."

            embed = discord.Embed(
                title="Kleiner Tipp für besseres Voice-Erlebnis 🎧",
                description=desc,
                color=discord.Color.blurple()
            )
            embed.set_footer(text="Kurzbefehle: /link · /link_steam · /addsteam · /unlink · /setprimary")

            view = _OptionsView(discord_oauth_url=discord_url, steam_openid_url=steam_url)
            await dm.send(embed=embed, view=view)

            if not force:
                _mark_notified(uid)

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
            # Wenn exempt, gar nicht erst warten/notify
            if _member_has_exempt_role(member):
                ch = _log_chan(self.bot)
                if ch:
                    await ch.send(f"ℹ️ Watch übersprungen (Exempt-Rolle) für **{member}** ({int(member.id)}).")
                return

            total = 0
            while total < MIN_VOICE_MINUTES * 60:
                await asyncio.sleep(POLL_INTERVAL)
                total += POLL_INTERVAL
                if not await self._still_in_voice(member):
                    return
                # Live während der Wartezeit exempt geworden?
                if _member_has_exempt_role(member):
                    ch = _log_chan(self.bot)
                    if ch:
                        await ch.send(f"ℹ️ Watch abgebrochen (Exempt-Rolle erkannt) für **{member}** ({int(member.id)}).")
                    return

            if _already_notified(member.id) or _has_any_steam_link(member.id):
                return
            if _had_prior_long_voice_session(member.id, MIN_VOICE_MINUTES * 60):
                return
            await self._send_dm_nudge(member, force=False)
        except asyncio.CancelledError:
            # normal bei Disconnect/Shutdown
            raise
        except Exception:
            log.exception("[nudge] Watch-Task crashed")

    # --- Events ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        joined = before.channel is None and after.channel is not None
        left   = before.channel is not None and after.channel is None

        if joined:
            # Exempt-Rollen: nicht verfolgen, nicht anschreiben
            if _member_has_exempt_role(member):
                ch = _log_chan(self.bot)
                if ch:
                    await ch.send(f"ℹ️ Join erkannt, aber Exempt-Rolle: **{member}** ({int(member.id)}).")
                return

            if _already_notified(member.id) or _has_any_steam_link(member.id):
                return
            if _had_prior_long_voice_session(member.id, MIN_VOICE_MINUTES * 60):
                return
            if member.id in self._watch_tasks and not self._watch_tasks[member.id].done():
                return
            self._watch_tasks[member.id] = asyncio.create_task(self._wait_and_notify(member))
        elif left:
            t = self._watch_tasks.pop(member.id, None)
            if t and not t.done():
                t.cancel()

    # --- TEST ---
    @commands.command(name="test30", aliases=["testnudge", "nudge30"])
    async def test30(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        target: Union[discord.Member, discord.User]
        if user is not None:
            target = user
        else:
            target = None
            if DEFAULT_TEST_TARGET_ID:
                if ctx.guild:
                    target = ctx.guild.get_member(DEFAULT_TEST_TARGET_ID)
                if target is None:
                    target = self.bot.get_user(DEFAULT_TEST_TARGET_ID)
            if target is None:
                target = ctx.author

        # Auch beim Test NICHT kontaktieren, wenn Exempt-Rolle vorhanden
        if isinstance(target, discord.Member) and _member_has_exempt_role(target):
            await ctx.reply("ℹ️ Test abgebrochen: Ziel hat eine ausgenommene Rolle.", mention_author=False)
            return

        ok = await self._send_dm_nudge(target, force=True)
        if ok:
            await ctx.reply(f"📨 Test-DM an {getattr(target, 'mention', target.id)} gesendet.", mention_author=False)
        else:
            await ctx.reply("⚠️ Test-DM konnte nicht gesendet werden (DMs aus? oder bereits benachrichtigt).", mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamLinkVoiceNudge(bot))
