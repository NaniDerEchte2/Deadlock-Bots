# cogs/welcome_dm/dm_main.py
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from . import base as base_module
from .step_intro import IntroView
    # Intro info/weiter Button (nicht persistent registrieren)
from .step_master_overview import MasterBotIntroView, ServerTourView
from .step_status import PlayerStatusView
from .step_steam_link import SteamLinkStepView, steam_link_dm_description
from .step_rules import RulesView
from .step_streamer import StreamerIntroView  # Optionaler Schritt


def _fallback_build_step_embed(title, desc, step, total, color=0x5865F2):
    footer = "Einführung • Deutsche Deadlock Community" if step is None else f"Frage {step} von {total} • Deutsche Deadlock Community"
    emb = discord.Embed(title=title, description=desc, color=color)
    emb.set_footer(text=footer)
    return emb


build_step_embed = getattr(base_module, "build_step_embed", _fallback_build_step_embed)
logger = getattr(base_module, "logger", logging.getLogger(__name__))

STATUS_NEED_BETA = getattr(base_module, "STATUS_NEED_BETA", "need_beta")
STATUS_NEW_PLAYER = getattr(base_module, "STATUS_NEW_PLAYER", "new_player")
STATUS_PLAYING = getattr(base_module, "STATUS_PLAYING", "already_playing")
STATUS_RETURNING = getattr(base_module, "STATUS_RETURNING", "returning")

DEFAULT_BETA_INVITE_CHANNEL_URL = "https://discord.com/channels/1289721245281292288/1428745737323155679"
DEFAULT_BETA_INVITE_SUPPORT_CONTACT = "@earlysalty"

BETA_INVITE_CHANNEL_URL = getattr(
    base_module,
    "BETA_INVITE_CHANNEL_URL",
    DEFAULT_BETA_INVITE_CHANNEL_URL,
)
BETA_INVITE_SUPPORT_CONTACT = getattr(
    base_module,
    "BETA_INVITE_SUPPORT_CONTACT",
    DEFAULT_BETA_INVITE_SUPPORT_CONTACT,
)

REQUIRED_WELCOME_ROLE_ID = 1304216250649415771
WELCOME_DM_TEST_ROLE_IDS: tuple[int, ...] = tuple(
    getattr(base_module, "WELCOME_DM_TEST_ROLE_IDS", ())
)


async def _can_run_test_welcome(ctx: commands.Context) -> bool:
    if ctx.guild is None or not isinstance(ctx.author, discord.Member):
        return False

    if await ctx.bot.is_owner(ctx.author):  # type: ignore[arg-type]
        return True

    perms = ctx.author.guild_permissions
    if perms.administrator or perms.manage_guild or perms.manage_roles:
        return True

    if not WELCOME_DM_TEST_ROLE_IDS:
        return False

    return any(role.id in WELCOME_DM_TEST_ROLE_IDS for role in ctx.author.roles)


class WelcomeDM(commands.Cog):
    """Welcome-Onboarding: Intro → Master-Bot → Server-Überblick → Status → Steam → (optional Streamer) → Regeln.
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
            except Exception as exc:
                logger.debug("_send_step_embed_channel: Nachricht konnte nicht gelöscht werden: %s", exc)
        return bool(getattr(view, "proceed", False))

    @staticmethod
    def _beta_invite_message() -> str:
        return (
            "🎟️ **Beta-Invite benötigt?**\n"
            f"Schau in <{BETA_INVITE_CHANNEL_URL}> vorbei – dort bekommst du einen Beta-Invite mit `/betainvite`.\n"
            f"Sollten Probleme auftreten, ping bitte {BETA_INVITE_SUPPORT_CONTACT}."
        )

    # ---------------- Öffentliche Flows ----------------

    async def send_welcome_messages(self, member: discord.Member) -> bool:
        """Kompletter DM-Flow (Intro zählt nicht als Step; danach 1/5-5/5)."""
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
                total_steps = 5
                if not await self._send_step_embed_dm(
                    member,
                    title="Willkommen 💙",
                    desc=intro_desc,
                    step=None,
                    total=total_steps,
                    view=IntroView(),
                    color=0x00AEEF,
                ):
                    return False

                master_intro_desc = (
                    "🤖 **Ich bin der Master Bot.**\n"
                    "Ich kümmere mich um diesen Server und halte dir den Rücken frei."
                    " Schön, dass du hier bist!\n\n"
                    "Wenn du Fragen hast, probiere gern `/serverfaq` aus oder schreib dem "
                    "Moderatorenteam – wir schauen immer nach dir."
                )
                if not await self._send_step_embed_dm(
                    member,
                    title="Schritt 1/5 · Lerne den Master Bot kennen",
                    desc=master_intro_desc,
                    step=1,
                    total=total_steps,
                    view=MasterBotIntroView(),
                    color=0x5865F2,
                ):
                    return False

                tour_desc = (
                    "🧭 **Server-Rundgang**\n"
                    "• **#ankündigungen** – Hier landen alle wichtigen News, damit du nichts verpasst.\n"
                    "• **#live-auf-twitch** – Zeigt dir sofort, wer gerade aus unserer Community streamt.\n"
                    "• **#clip-submission** – Teil deine Highlights und lass alle mitfiebern.\n"
                    "• **#coaching** – Hol dir persönliches Coaching, um noch stärker zu werden.\n"
                    "• **Die 3 Lanes** – So findest du deine perfekte Lobby:\n"
                    "   • **Entspannte Lanes** – Gemütliche Sessions ohne Voraussetzungen.\n"
                    "   • **Grind Lanes** – Fokussierte Matches mit Mindest-Rang und Tools zum Verwalten deiner Lobby.\n"
                    "   • **Ranked Lanes** – Strikte +/-1-Rang-Lobbys für kompetitives Play.\n"
                    "   Mit den Buttons im Panel kannst du deine Lane verwalten, einer Lobby beitreten oder eine neue starten.\n"
                    "• **#rang-auswahl** – Wähle deinen aktuellen Rang aus, damit dich alle direkt einordnen können.\n\n"
                    "Mach es dir gemütlich und hab ganz viel Spaß beim Entdecken! 💙"
                )
                if not await self._send_step_embed_dm(
                    member,
                    title="Schritt 2/5 · Dein Überblick",
                    desc=tour_desc,
                    step=2,
                    total=total_steps,
                    view=ServerTourView(),
                    color=0x3498DB,
                ):
                    return False

                # 3/5 Status
                status_view = PlayerStatusView()
                if not await self._send_step_embed_dm(
                    member,
                    title="Schritt 3/5 · Dein Status",
                    desc="Sag mir kurz, wo du stehst – dann passe ich alles besser für dich an.",
                    step=3,
                    total=total_steps,
                    view=status_view,
                    color=0x95A5A6,
                ):
                    return False
                status_choice = status_view.choice or STATUS_PLAYING

                if status_choice == STATUS_NEED_BETA:
                    try:
                        await member.send(self._beta_invite_message())
                    except discord.Forbidden as e:
                        logger.warning(f"Beta-Invite DM an {member} ({member.id}) nicht möglich: {e}")
                    except Exception:
                        logger.exception("Beta-Invite DM konnte nicht gesendet werden")
                    return True

                # 4/5 Steam
                q2_desc = steam_link_dm_description()
                if not await self._send_step_embed_dm(
                    member,
                    title="Schritt 4/5 · Verknüpfe deinen Steam Account",
                    desc=q2_desc,
                    step=4,
                    total=total_steps,
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
                    except Exception as exc:
                        logger.debug("StreamerIntro DM-Message nicht gelöscht: %s", exc)
                except Exception:
                    logger.debug("StreamerIntro Schritt übersprungen (kein Modul/Fehler).", exc_info=True)

                # 5/5 Regeln
                q3_desc = (
                    "📜 **Regelwerk – kurz & klar**\n"
                    "✔ Respektvoller Umgang, keine Beleidigungen/Hassrede\n"
                    "✔ Keine NSFW/Explizites, keine Leaks fremder Daten\n"
                    "✔ Kein Spam/unnötige Pings, keine Fremdwerbung/Schadsoftware\n"
                    "👉 Universalregel: **Sei kein Arschloch.**"
                )
                if not await self._send_step_embed_dm(
                    member,
                    title="Schritt 5/5 · Regeln bestätigen",
                    desc=q3_desc,
                    step=5,
                    total=total_steps,
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
                    closing_lines.append(self._beta_invite_message())
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
        """Gleicher Flow im (privaten) Thread/Channel. Zählung 1/5–5/5; Intro ohne Zählung."""
        try:
            # Intro (ohne Zählung)
            intro_desc = (
                "👋 **Willkommen!** Ich helfe dir, dein Erlebnis hier optimal einzustellen. "
                "Die nächsten 2–3 Minuten genügen."
            )
            total_steps = 5
            ok = await self._send_step_embed_channel(
                channel,
                title="Willkommen 💙",
                desc=intro_desc,
                step=None,
                total=total_steps,
                view=IntroView(),
                color=0x00AEEF,
            )
            if not ok:
                return False

            master_intro_desc = (
                "🤖 **Ich bin der Master Bot.**\n"
                "Ich halte hier alles am Laufen und freue mich, dich zu begleiten."
                " Schön, dass du da bist!\n\n"
                "Wenn etwas unklar ist, probiere `/serverfaq` oder schreib dem Moderatorenteam –"
                " wir kümmern uns gern."
            )
            ok = await self._send_step_embed_channel(
                channel,
                title="Schritt 1/5 · Master Bot",
                desc=master_intro_desc,
                step=1,
                total=total_steps,
                view=MasterBotIntroView(),
                color=0x5865F2,
            )
            if not ok:
                return False

            tour_desc = (
                "🧭 **Server-Rundgang**\n"
                "• **#ankündigungen** – Hier landen alle wichtigen News, damit du nichts verpasst.\n"
                "• **#live-auf-twitch** – Zeigt dir sofort, wer gerade aus unserer Community streamt.\n"
                "• **#clip-submission** – Teil deine Highlights und lass alle mitfiebern.\n"
                "• **#coaching** – Hol dir persönliches Coaching, um noch stärker zu werden.\n"
                "• **Die 3 Lanes** – So findest du deine perfekte Lobby:\n"
                "   • **Entspannte Lanes** – Gemütliche Sessions ohne Voraussetzungen.\n"
                "   • **Grind Lanes** – Fokussierte Matches mit Mindest-Rang und Tools zum Verwalten deiner Lobby.\n"
                "   • **Ranked Lanes** – Strikte +/-1-Rang-Lobbys für kompetitives Play.\n"
                "   Mit den Buttons im Panel kannst du deine Lane verwalten, einer Lobby beitreten oder eine neue starten.\n"
                "• **#rang-auswahl** – Wähle deinen aktuellen Rang aus, damit dich alle direkt einordnen können.\n\n"
                "Mach es dir gemütlich und hab ganz viel Spaß beim Entdecken! 💙"
            )
            ok = await self._send_step_embed_channel(
                channel,
                title="Schritt 2/5 · Dein Überblick",
                desc=tour_desc,
                step=2,
                total=total_steps,
                view=ServerTourView(),
                color=0x3498DB,
            )
            if not ok:
                return False

            # 3/5 Status
            status_view = PlayerStatusView()
            ok = await self._send_step_embed_channel(
                channel,
                title="Schritt 3/5 · Dein Status",
                desc="Sag kurz, wo du stehst – dann passen wir alles besser an.",
                step=3,
                total=total_steps,
                view=status_view,
                color=0x95A5A6,
            )
            if not ok:
                return False
            status_choice = status_view.choice or STATUS_PLAYING

            if status_choice == STATUS_NEED_BETA:
                try:
                    await channel.send(self._beta_invite_message())
                except Exception as exc:
                    logger.debug("Beta-Invite Hinweis im Channel konnte nicht gesendet werden: %s", exc)
                return True

            # 4/5 Steam
            q2_desc = steam_link_dm_description()
            ok = await self._send_step_embed_channel(
                channel,
                title="Schritt 4/5 · Steam verknüpfen (skippbar)",
                desc=q2_desc,
                step=4,
                total=total_steps,
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
                except Exception as exc:
                    logger.debug("StreamerIntro Channel-Message nicht gelöscht: %s", exc)
            except Exception:
                logger.debug("StreamerIntro Schritt (Thread) übersprungen.", exc_info=True)

            # 5/5 Regeln
            q3_desc = (
                "📜 **Regelwerk**\n"
                "✔ Respektvoller Umgang, keine Beleidigungen/Hassrede\n"
                "✔ Keine NSFW/Explizites, keine Leaks fremder Daten\n"
                "✔ Kein Spam/unnötige Pings, keine Fremdwerbung/Schadsoftware\n"
                "👉 Universalregel: **Sei kein Arschloch.**"
            )
            ok = await self._send_step_embed_channel(
                channel,
                title="Schritt 5/5 · Regeln bestätigen",
                desc=q3_desc,
                step=5,
                total=total_steps,
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
                closing_lines.append(self._beta_invite_message())
            if status_choice == STATUS_RETURNING:
                closing_lines.append("🔁 **Willkommen zurück!** Schau für Runden in LFG/Voice vorbei – viel Spaß!")
            if status_choice == STATUS_PLAYING:
                closing_lines.append("✅ **Viel Spaß!** Check **Guides** & **Ankündigungen** – und ping uns, wenn du was brauchst.")

            if closing_lines:
                try:
                    await channel.send("\n\n".join(closing_lines))
                except Exception as exc:
                    logger.debug("Abschlussnachricht im Channel konnte nicht gesendet werden: %s", exc)

            return True

        except Exception as e:
            logger.error(f"run_flow_in_channel Fehler: {e}", exc_info=True)
            return False

    # ---------------- Events & Commands ----------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        logger.info(
            "WelcomeDM: Automatische Willkommens-DMs sind deaktiviert. Onboarding läuft über den Regelkanal. (%s)",
            member.id,
        )

    @commands.command(name="tw")
    @commands.guild_only()
    @commands.check(_can_run_test_welcome)
    async def test_welcome(self, ctx: commands.Context, user: discord.Member = None):
        target = user
        if target is None:
            default_user_id = 662995601738170389
            guild = ctx.guild
            target = guild.get_member(default_user_id) if guild else None
            if target is None and guild is not None:
                try:
                    target = await guild.fetch_member(default_user_id)
                except discord.HTTPException:
                    target = None

            if target is None:
                await ctx.send(
                    "❌ Konnte den Standard-User nicht finden. Bitte gib `!tw @user` an."
                )
                return

        await ctx.send(f"📤 Sende Welcome-DM an {target.mention} …")
        ok = await self.send_welcome_messages(target)
        await ctx.send("✅ Erfolgreich gesendet!" if ok else "⚠️ Senden fehlgeschlagen.")

    @test_welcome.error
    async def test_welcome_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            await ctx.send(
                "❌ Dir fehlen die benötigten Rechte für diesen Befehl. "
                "Er erfordert Administrator, Serververwaltung, Rollenverwaltung oder eine freigeschaltete Rolle."
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeDM(bot))
