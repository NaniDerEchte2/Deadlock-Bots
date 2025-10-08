# cogs/welcome_dm/dm_main.py
from __future__ import annotations

import asyncio
import discord
from discord.ext import commands

from .base import (
    build_step_embed,
    logger,
    STATUS_NEED_BETA,
    STATUS_NEW_PLAYER,
    STATUS_PLAYING,
    STATUS_RETURNING,
)
from .step_intro import IntroView
    # Intro info/weiter Button (nicht persistent registrieren)
from .step_status import PlayerStatusView
from .step_steam_link import SteamLinkStepView, steam_link_dm_description
from .step_rules import RulesView
from .step_streamer import StreamerIntroView  # Optionaler Schritt

class WelcomeDM(commands.Cog):
    """Welcome-DM: Intro → Status → Steam → (optional Streamer) → Regeln.
       WICHTIG: keine persistente Registrierung der Step-Views (enthalten Link-Buttons)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session_locks: dict[int, asyncio.Lock] = {}

    # ---------------- Intern ----------------

    def _get_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._session_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[user_id] = lock
        return lock

    async def cog_load(self):
        # KEINE persistente Registrierung der Step-Views; nur Logging.
        logger.info("WelcomeDM geladen (ohne persistente Step-Views).")

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Welcome DM System bereit")

    async def _cleanup_old_bot_dms(self, member: discord.Member, limit: int = 50):
        try:
            dm = member.dm_channel or await member.create_dm()
            async for msg in dm.history(limit=limit):
                if msg.author.id == self.bot.user.id:
                    try:
                        await msg.delete()
                    except discord.HTTPException as e:
                        logger.debug(f"DM-Cleanup: Bot-Nachricht {msg.id} nicht gelöscht: {e}")
                    except Exception:
                        logger.exception("DM-Cleanup: Unerwarteter Fehler beim Löschen")
        except Exception as e:
            logger.debug(f"DM-Cleanup für {member.id} übersprungen: {e}")

    async def _send_step_embed_dm(
        self,
        member: discord.Member,
        *,
        title: str,
        desc: str,
        step: int | None,
        total: int,
        view: discord.ui.View,
        color: int = 0x5865F2,
    ) -> bool:
        """Sendet einen Step als DM, wartet auf Abschluss und räumt auf."""
        emb = build_step_embed(title, desc, step, total, color=color)
        msg = await member.send(embed=emb, view=view)
        if hasattr(view, "bound_message"):
            view.bound_message = msg
        try:
            await view.wait()
        finally:
            try:
                await msg.delete()
            except discord.HTTPException as e:
                logger.debug(f"_send_step_embed_dm: Message {msg.id} nicht gelöscht: {e}")
            except Exception:
                logger.exception("_send_step_embed_dm: Unerwarteter Fehler beim Löschen")
        return bool(getattr(view, "proceed", False))

    async def _send_step_embed_channel(
        self,
        channel: discord.abc.Messageable,
        *,
        title: str,
        desc: str,
        step: int | None,
        total: int,
        view: discord.ui.View,
        color: int = 0x5865F2,
    ) -> bool:
        """Sendet einen Step in einen (Thread-)Kanal, wartet auf Abschluss und räumt auf."""
        emb = build_step_embed(title, desc, step, total, color=color)
        msg = await channel.send(embed=emb, view=view)
        if hasattr(view, "bound_message"):
            view.bound_message = msg
        try:
            await view.wait()
        finally:
            try:
                await msg.delete()
            except Exception:
                pass
        return bool(getattr(view, "proceed", False))

    # ---------------- Öffentliche Flows ----------------

    async def send_welcome_messages(self, member: discord.Member) -> bool:
        """Kompletter DM-Flow (Intro zählt nicht als Step; danach 1/3-3/3)."""
        lock = self._get_lock(member.id)
        async with lock:
            try:
                await self._cleanup_old_bot_dms(member, limit=50)

                # Intro (ohne Step-Zählung)
                intro_desc = (
                    "👋 **Willkommen in der Deutschen Deadlock Community!**\n\n"
                    "Ich helfe dir jetzt, dein Erlebnis hier **optimal** einzustellen. "
                    "Nimm dir kurz **2–3 Minuten** Zeit. 💙\n\n"
                    "**Ohne diese Schritte hast du keinen vollen Zugriff.**\n\n"
                    "Bitte lies die nächsten Schritte **in Ruhe**. "
                    "Ich halte es kurz und sorge dafür, dass du **genau die richtigen** "
                    "Channels & Features siehst."
                )
                if not await self._send_step_embed_dm(
                    member,
                    title="Willkommen 💙",
                    desc=intro_desc,
                    step=None,
                    total=3,  # gezählte Steps: Status, Steam, Regeln
                    view=IntroView(),
                    color=0x00AEEF,
                ):
                    return False

                # 1/3 Status
                status_view = PlayerStatusView()
                if not await self._send_step_embed_dm(
                    member,
                    title="Frage 1/3 · Dein Status",
                    desc="Sag mir kurz, wo du stehst – dann passe ich alles besser für dich an.",
                    step=1,
                    total=3,
                    view=status_view,
                    color=0x95A5A6,
                ):
                    return False
                status_choice = status_view.choice or STATUS_PLAYING

                # 2/3 Steam
                q2_desc = steam_link_dm_description()
                if not await self._send_step_embed_dm(
                    member,
                    title="Frage 2/3 · Verknüpfe deinen Steam Account",
                    desc=q2_desc,
                    step=2,
                    total=3,
                    view=SteamLinkStepView(),
                    color=0x5865F2,
                ):
                    return False

                # Streamer (optional)
                try:
                    embed = StreamerIntroView.build_embed(member)
                    view = StreamerIntroView()
                    msg = await member.send(embed=embed, view=view)
                    await view.wait()
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                except Exception:
                    logger.debug("StreamerIntro Schritt übersprungen (kein Modul/Fehler).", exc_info=True)

                # 3/3 Regeln
                q3_desc = (
                    "📜 **Regelwerk – kurz & klar**\n"
                    "✔ Respektvoller Umgang, keine Beleidigungen/Hassrede\n"
                    "✔ Keine NSFW/Explizites, keine Leaks fremder Daten\n"
                    "✔ Kein Spam/unnötige Pings, keine Fremdwerbung/Schadsoftware\n"
                    "👉 Universalregel: **Sei kein Arschloch.**"
                )
                if not await self._send_step_embed_dm(
                    member,
                    title="Frage 3/3 · Regeln bestätigen",
                    desc=q3_desc,
                    step=3,
                    total=3,
                    view=RulesView(),
                    color=0xE67E22,
                ):
                    return False

                # Abschluss
                closing_lines: list[str] = []
                if status_choice == STATUS_NEW_PLAYER:
                    closing_lines.append(
                        "✨ **Neu dabei?** Frag die Community – wir helfen gern. "
                        "Für eine kurze Einführung schreib **@earlysalty** oder poste in **#allgemein**."
                    )
                if status_choice == STATUS_NEED_BETA:
                    closing_lines.append(
                        "🎟️ **Beta-Invite benötigt?** Schau hier vorbei:\n"
                        "https://discord.com/channels/1289721245281292288/1410754840706945034\n"
                        "Poste dort deine **Steam-Freundschafts-ID** (Steam → Freunde → Freund hinzufügen). "
                        "Zum Akzeptieren: <https://store.steampowered.com/account/playtestinvites> "
                        "— das kann ein paar Stunden dauern."
                    )
                if status_choice == STATUS_RETURNING:
                    closing_lines.append("🔁 **Willkommen zurück!** Schau für Runden in LFG/Voice vorbei – viel Spaß!")
                if status_choice == STATUS_PLAYING:
                    closing_lines.append("✅ **Viel Spaß!** Check **Guides** & **Ankündigungen** – und ping uns, wenn du was brauchst.")

                if closing_lines:
                    try:
                        await member.send("\n\n".join(closing_lines))
                    except discord.Forbidden as e:
                        logger.warning(f"Abschluss-DM an {member} ({member.id}) nicht möglich: {e}")
                    except Exception:
                        logger.exception("Abschluss-DM: Unerwarteter Fehler beim Senden")

                logger.info(f"Welcome-DM abgeschlossen für {member} ({member.id})")
                return True

            except discord.Forbidden:
                logger.warning(f"DM an {member} ({member.id}) nicht möglich (DMs aus / blockiert)")
                return False
            except Exception as e:
                logger.error(f"Fehler beim Welcome-DM an {member} ({member.id}): {e}")
                return False

    async def run_flow_in_channel(self, channel: discord.abc.Messageable, member: discord.Member) -> bool:
        """Gleicher Flow im (privaten) Thread/Channel. Zählung 1/3–3/3; Intro ohne Zählung."""
        try:
            # Intro (ohne Zählung)
            intro_desc = (
                "👋 **Willkommen!** Ich helfe dir, dein Erlebnis hier optimal einzustellen. "
                "Die nächsten 2–3 Minuten genügen."
            )
            ok = await self._send_step_embed_channel(
                channel,
                title="Willkommen 💙",
                desc=intro_desc,
                step=None,
                total=3,
                view=IntroView(),
                color=0x00AEEF,
            )
            if not ok:
                return False

            # 1/3 Status
            status_view = PlayerStatusView()
            ok = await self._send_step_embed_channel(
                channel,
                title="Frage 1/3 · Dein Status",
                desc="Sag kurz, wo du stehst – dann passen wir alles besser an.",
                step=1,
                total=3,
                view=status_view,
                color=0x95A5A6,
            )
            if not ok:
                return False
            status_choice = status_view.choice or STATUS_PLAYING

            # 2/3 Steam
            q2_desc = steam_link_dm_description()
            ok = await self._send_step_embed_channel(
                channel,
                title="Frage 2/3 · Steam verknüpfen (skippbar)",
                desc=q2_desc,
                step=2,
                total=3,
                view=SteamLinkStepView(),
                color=0x5865F2,
            )
            if not ok:
                return False

            # Optional: Streamer
            try:
                embed = StreamerIntroView.build_embed(member)
                view = StreamerIntroView()
                msg = await channel.send(embed=embed, view=view)
                await view.wait()
                try:
                    await msg.delete()
                except Exception:
                    pass
            except Exception:
                logger.debug("StreamerIntro Schritt (Thread) übersprungen.", exc_info=True)

            # 3/3 Regeln
            q3_desc = (
                "📜 **Regelwerk**\n"
                "✔ Respektvoller Umgang, keine Beleidigungen/Hassrede\n"
                "✔ Keine NSFW/Explizites, keine Leaks fremder Daten\n"
                "✔ Kein Spam/unnötige Pings, keine Fremdwerbung/Schadsoftware\n"
                "👉 Universalregel: **Sei kein Arschloch.**"
            )
            ok = await self._send_step_embed_channel(
                channel,
                title="Frage 3/3 · Regeln bestätigen",
                desc=q3_desc,
                step=3,
                total=3,
                view=RulesView(),
                color=0xE67E22,
            )
            if not ok:
                return False

            # Abschluss-Text
            closing_lines: list[str] = []
            if status_choice == STATUS_NEW_PLAYER:
                closing_lines.append(
                    "✨ **Neu dabei?** Stell Fragen – wir helfen gern. "
                    "Kleine Einführung? Ping **@earlysalty** oder schreibe in **#allgemein**."
                )
            if status_choice == STATUS_NEED_BETA:
                closing_lines.append(
                    "🎟️ **Beta-Invite benötigt?** → "
                    "https://discord.com/channels/1289721245281292288/1410754840706945034\n"
                    "Poste deine **Steam-Freundschafts-ID** (Steam → Freunde → Freund hinzufügen). "
                    "Einlösen: <https://store.steampowered.com/account/playtestinvites>."
                )
            if status_choice == STATUS_RETURNING:
                closing_lines.append("🔁 **Willkommen zurück!** Schau für Runden in LFG/Voice vorbei – viel Spaß!")
            if status_choice == STATUS_PLAYING:
                closing_lines.append("✅ **Viel Spaß!** Check **Guides** & **Ankündigungen** – und ping uns, wenn du was brauchst.")

            if closing_lines:
                try:
                    await channel.send("\n\n".join(closing_lines))
                except Exception:
                    pass

            return True

        except Exception as e:
            logger.error(f"run_flow_in_channel Fehler: {e}", exc_info=True)
            return False

    # ---------------- Events & Commands ----------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await asyncio.sleep(2)
        await self.send_welcome_messages(member)

    @commands.command(name="testwelcome")
    @commands.has_permissions(administrator=True)
    async def test_welcome(self, ctx: commands.Context, user: discord.Member = None):
        if not user:
            await ctx.send("❌ Bitte gib einen User an: `!testwelcome @user`")
            return
        await ctx.send(f"📤 Sende Welcome-DM an {user.mention} …")
        ok = await self.send_welcome_messages(user)
        await ctx.send("✅ Erfolgreich gesendet!" if ok else "⚠️ Senden fehlgeschlagen.")


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeDM(bot))
