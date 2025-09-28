# cogs/welcome_dm/dm_main.py
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
from .step_status import PlayerStatusView
from .step_steam_link import SteamLinkNudgeView, _SteamLinkPromptView
from .step_rules import RulesView
from .step_streamer import StreamerView


class WelcomeDM(commands.Cog):
    """Welcome-DM (bereinigt): Intro → Status → Steam → (optional Streamer) → Regeln."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._session_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[user_id] = lock
        return lock

    async def cog_load(self):
        # Persistente Views registrieren (für Reboots)
        self.bot.add_view(IntroView())
        self.bot.add_view(PlayerStatusView())
        self.bot.add_view(SteamLinkNudgeView())
        self.bot.add_view(RulesView())
        self.bot.add_view(StreamerView())

        # Wichtig: auch die stateless Prompt-View der Steam-Optionen registrieren,
        # damit Buttons in alten Nachrichten nach Neustarts funktionieren.
        self.bot.add_view(_SteamLinkPromptView(self.bot))

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Welcome DM System geladen (persistente Views aktiv)")

    async def _cleanup_old_bot_dms(self, member: discord.Member, limit: int = 50):
        """Alte Bot-DMs aufräumen (optisch sauber)."""
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

    async def _send_step_embed(
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
        emb = build_step_embed(title, desc, step, total, color=color)
        msg = await member.send(embed=emb, view=view)
        if hasattr(view, "bound_message"):
            view.bound_message = msg  # falls ein Step das benötigt
        try:
            await view.wait()
        finally:
            try:
                await msg.delete()
            except discord.HTTPException as e:
                logger.debug(f"_send_step_embed: Message {msg.id} nicht gelöscht: {e}")
            except Exception:
                logger.exception("_send_step_embed: Unerwarteter Fehler beim Löschen")
        return getattr(view, "proceed", False)

    async def send_welcome_messages(self, member: discord.Member) -> bool:
        """Sende die Welcome-DM-Sequenz an den Nutzer."""
        lock = self._get_lock(member.id)
        async with lock:
            try:
                await self._cleanup_old_bot_dms(member, limit=50)

                # (0) Intro (ohne Zählung) — enthält jetzt den kompletten Begrüßungstext
                intro_desc = (
                    "👋 **Willkommen in der Deutschen Deadlock Community!**\n\n"
                    "Ich helfe dir jetzt, dein Erlebnis hier **optimal** einzustellen. "
                    "Nimm dir kurz **2–3 Minuten** Zeit. 💙\n\n"
                    "**:bangbang: Ohne diese Schritte hast du keinen vollen Zugriff. :bangbang:**\n\n"
                    "Bitte lies die nächsten Schritte **in Ruhe**. "
                    "Ich halte es kurz und sorge dafür, dass du **genau die richtigen** "
                    "Channels & Features siehst. 💙"
                )
                if not await self._send_step_embed(
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
                if not await self._send_step_embed(
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

                # 2/3 Steam verknüpfen
                q2_desc = (
                    "**Empfohlen für das beste Erlebnis:**\n"
                    "• Exakter **Voice-Status** (Lobby/Match, freie Slots)\n"
                    "• Saubere **Event-Orga & Balancing**\n\n"
                    "**Wichtig:** In Steam → Profil → **Privatsphäre** → "
                    "**Spieldetails = Öffentlich** (und **Gesamtspielzeit** nicht auf „immer privat“)."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 2/3 · Steam verknüpfen (skippbar)",
                    desc=q2_desc,
                    step=2,
                    total=3,
                    view=SteamLinkNudgeView(),
                    color=0x5865F2,
                ):
                    return False

                # Optional: Streamer-Partner (ohne Zählung)
                streamer_desc = (
                    "**Bist du Deadlock-Streamer?**\n"
                    "Erfülle kurz die Voraussetzungen und werde automatisch in **#live-on-twitch** "
                    "gepostet – **nur wenn du Deadlock streamst**.\n\n"
                    "**Voraussetzungen:**\n"
                    "1) **Permanenter Invite-Link** zu diesem Server (persönlich).\n"
                    "2) **Twitch-Bio** enthält den Text/Link: *Deutscher Deadlock Community Server*.\n"
                    "3) Verweise Interessierte **aktiv auf den Server**.\n"
                    "4) Eigener Server ist **okay** – keine Konkurrenz.\n"
                    "5) Deadlock-Content darfst du promoten – bitte **mit Server-Link**.\n\n"
                    "Wenn alles erledigt ist, klicke unten **„Ich habe alles gemacht – zum Streamer freischalten“**."
                )
                if not await self._send_step_embed(
                    member,
                    title="Streamer-Partner (optional)",
                    desc=streamer_desc,
                    step=None,
                    total=3,
                    view=StreamerView(),
                    color=0xF1C40F,
                ):
                    return False

                # 3/3 Regeln bestätigen
                q3_desc = (
                    "📜 **Regelwerk – kurz & klar**\n"
                    "✔ Respektvoller Umgang, keine Beleidigungen/Hassrede\n"
                    "✔ Keine NSFW/Explizites, keine Leaks fremder Daten\n"
                    "✔ Kein Spam/unnötige Pings, keine Fremdwerbung/Schadsoftware\n"
                    "👉 Universalregel: **Sei kein Arschloch.**"
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 3/3 · Regeln bestätigen",
                    desc=q3_desc,
                    step=3,
                    total=3,
                    view=RulesView(),
                    color=0xE67E22,
                ):
                    return False

                # Abschluss (abhängig vom Status)
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
                    closing_lines.append(
                        "🔁 **Willkommen zurück!** Schau für Runden in LFG/Voice vorbei – viel Spaß!"
                    )
                if status_choice == STATUS_PLAYING:
                    closing_lines.append(
                        "✅ **Viel Spaß!** Check **Guides** & **Ankündigungen** – und ping uns, wenn du was brauchst."
                    )

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

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # Kein Auto-Thread mehr; nur DM-Onboarding starten.
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


# WICHTIG: setup unten und ohne Selbst-Import
async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeDM(bot))
