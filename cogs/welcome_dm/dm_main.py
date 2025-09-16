# cogs/welcome_dm/main.py
import discord
from discord.ext import commands
import asyncio

from .base import (
    build_step_embed, _safe_role_name, logger,
    STATUS_NEED_BETA, STATUS_NEW_PLAYER, STATUS_PLAYING, STATUS_RETURNING,
    FUNNY_CUSTOM_ROLE_ID, GRIND_CUSTOM_ROLE_ID
)
from .step_intro import IntroView
from .step_status import PlayerStatusView
from .step_customs import CustomGamesView
from .step_patchnotes import PatchnotesView
from .step_rank import RankView
from .step_steam_link import SteamLinkNudgeView
from .step_rules import RulesView


class WelcomeDM(commands.Cog):
    """Modularer Welcome-DM (jede Frage in eigenem Modul)"""

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
        self.bot.add_view(CustomGamesView())
        self.bot.add_view(PatchnotesView())
        self.bot.add_view(RankView(guild_for_emojis=None))
        self.bot.add_view(SteamLinkNudgeView())
        self.bot.add_view(RulesView())

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Welcome DM System geladen (persistente Views aktiv, modular)")

    async def _cleanup_old_bot_dms(self, member: discord.Member, limit: int = 50):
        try:
            dm = member.dm_channel or await member.create_dm()
            async for msg in dm.history(limit=limit):
                if msg.author.id == self.bot.user.id:
                    try:
                        await msg.delete()
                    except discord.HTTPException as e:
                        logger.debug(f"DM-Cleanup: Konnte Bot-Nachricht {msg.id} nicht löschen: {e}")
                    except Exception:
                        logger.exception("DM-Cleanup: Unerwarteter Fehler beim Löschen einer Bot-Nachricht")
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
        color: int = 0x5865F2
    ) -> bool:
        emb = build_step_embed(title, desc, step, total, color=color)
        msg = await member.send(embed=emb, view=view)
        if hasattr(view, "bound_message"):
            view.bound_message = msg  # für Rank-Confirm-Flow
        try:
            await view.wait()
        finally:
            try:
                await msg.delete()
            except discord.HTTPException as e:
                logger.debug(f"_send_step_embed: Message {msg.id} konnte nicht gelöscht werden: {e}")
            except Exception:
                logger.exception("_send_step_embed: Unerwarteter Fehler beim Löschen der Message")
        return getattr(view, "proceed", False)

    async def send_welcome_messages(self, member: discord.Member):
        lock = self._get_lock(member.id)
        async with lock:
            greet_msg: discord.Message | None = None
            try:
                await self._cleanup_old_bot_dms(member, limit=50)

                guild = self.bot.get_guild(1289721245281292288)
                funny_name = _safe_role_name(guild, FUNNY_CUSTOM_ROLE_ID, "Funny Custom")
                grind_name = _safe_role_name(guild, GRIND_CUSTOM_ROLE_ID, "Grind Custom")

                # (0) Begrüßung
                greet_msg = await member.send(
                    "👋 **Herzlich willkommen in der Deutschen Deadlock Community!**\n\n"
                    "Ich helfe dir jetzt, dein Spielerlebnis hier **bestmöglich** einzustellen. "
                    "Dazu brauche ich **kurz** deine Aufmerksamkeit. 💙\n\n"
                    "**:bangbang: __Ohne diese Schritte hast du keinen Zugriff auf den Server.__:bangbang: **"
                )

                # (0.5) Intro
                intro_desc = (
                    "Hey, schön dass du da bist! 🫶\n\n"
                    "Bitte nimm dir **2–3 Minuten** Zeit, die nächsten Fragen **in Ruhe** zu lesen "
                    "und zu verstehen, was ich von dir brauche. Ich bin dafür da, "
                    "dein Spielerlebnis auf dem Server **maximal angenehm** zu machen – "
                    "mit möglichst wenig Chaos und maximal viel **Liebe**. 💙\n\n"
                    "_Kleiner Tipp:_ Wer liest, bekommt die besseren Rollen. 😉"
                )
                if not await self._send_step_embed(
                    member,
                    title="Willkommen 💙",
                    desc=intro_desc,
                    step=None, total=6,
                    view=IntroView(),
                    color=0x00AEEF
                ):
                    return False

                # 1/6 Status
                status_view = PlayerStatusView()
                if not await self._send_step_embed(
                    member,
                    title="Frage 1/6 · Spielst du schon Deadlock – oder wieder?",
                    desc="Sag mir kurz, wo du stehst – dann passe ich alles besser für dich an.",
                    step=1, total=6,
                    view=status_view,
                    color=0x95A5A6
                ):
                    return False
                status_choice = status_view.choice or STATUS_PLAYING

                # 2/6 Customs
                q2_desc = (
                    "🎮 **Custom Games**\n\n"
                    "**Was sind Custom Games?**\n"
                    "Customs sind selbsterstellte Lobbys, die nichts mit dem normalen Matchmaking zu tun haben. "
                    "Hier legen wir eigene Regeln fest → Fokus auf Spaß, Lernen oder gemeinsames Training.\n\n"
                    "Dafür gibt es 2 Rollen:\n"
                    f"• **{funny_name}** → Für Fun & kreative Custom-Runden 🤪\n"
                    f"• **{grind_name}** → Für Scrims & ernsthafte Trainings 💪\n\n"
                    "➡ Über die Buttons kannst du dir die Rolle(n) selbst geben, wenn du mitmachen willst."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 2/6 · Lust auf Custom Games?",
                    desc=q2_desc,
                    step=2, total=6,
                    view=CustomGamesView(),
                    color=0x2ECC71
                ):
                    return False

                # 3/6 Patchnotes
                q3_desc = (
                    "Möchtest du über neue **Patchnotes** informiert werden?\n"
                    "So verpasst du keine Balance-Änderungen oder neuen Content."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 3/6 · Patchnotes-Benachrichtigungen",
                    desc=q3_desc,
                    step=3, total=6,
                    view=PatchnotesView(),
                    color=0x3498DB
                ):
                    return False

                # 4/6 Rang
                q4_desc = (
                    "Bitte wähle hier deinen **AKTUELLEN RANG**\n"
                    "**Kein MAX RANG, NICHT PEAK, auch NICHT WEIHNACHTEN IN AFRIKA**\n"
                    "SONDERN DEIN JETZIGER RANG**😄\n"
                    "____________________________\n"
                    "**Du weißt deinen Rang nicht oder findest ihn nicht?**\n"
                    "• Starte **Deadlock**\n"
                    "• Drücke **Esc** → **Profil**\n"
                    "• Unter dem **letzten Match**, neben **Sortieren nach: Spielzeit**, findest du deinen **Rang**\n"
                    f"• Oder schau hier aufs Bild: [Hier Klicken](https://media.discordapp.net/attachments/1330665839078146059/1412581096436269096/image.png?ex=68c7f969&is=68c6a7e9&hm=8c6c3ce664f644f99b2d5114cb3a09d7874a6624a7a9a569ea2a8b5c2ea3f239&=&format=webp&quality=lossless&width=2162&height=1216)\n"
                    "**Deutsch/Englisch verwirrt?**\n"
                    "Vergleiche einfach **das Aussehen** der Abzeichen mit dem, was du im Dropdown siehst.\n\n"
                    "Wenn du **neu im Game** bist, wähle bitte **„Neu im Game“**."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 4/6 · Rang auswählen (Pflicht)",
                    desc=q4_desc,
                    step=4, total=6,
                    view=RankView(guild_for_emojis=guild),
                    color=0x9B59B6
                ):
                    return False

                # 5/6 Steam-Nudge
                q5_desc = (
                    "**Empfehlung für besseres Erlebnis:**\n"
                    "• **Wozu ist das gut?** Wir können dadurch einen **exakten Voice-Status** "
                    "(z. B. *Lobby/In-Game*, **Anzahl im Match**) als Kanalbeschreibung bereitstellen.\n"
                    "• Zudem ermöglicht es **sauberere Orga & Balancing** bei Events.\n\n"
                    "**Wichtig:** In Steam → Profil → **Datenschutzeinstellungen** → "
                    "**Spieldetails = Öffentlich** (und **Gesamtspielzeit** nicht auf „immer privat“)."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 5/6 · Steam verknüpfen (empfohlen, skippbar)",
                    desc=q5_desc,
                    step=5, total=6,
                    view=SteamLinkNudgeView(),
                    color=0x5865F2
                ):
                    return False

                # 6/6 Regeln
                q6_desc = (
                    "📜 **Regelwerk – Das Wichtigste in Kürze**\n\n"
                    "✔ Respektvoller Umgang – keine Beleidigungen oder persönlichen Angriffe\n"
                    "✔ Null Toleranz bei Rassismus, Sexismus oder Hassrede\n"
                    "✔ Keine NSFW / expliziten Inhalte\n"
                    "✔ Privatsphäre respektieren – keine fremden Daten leaken\n"
                    "✔ Kein Spam / unnötige Pings\n"
                    "✔ Keine Fremdwerbung oder Schadsoftware\n\n"
                    "👉 Universalregel: **Sei kein Arschloch.**"
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 6/6 · Regelwerk bestätigen",
                    desc=q6_desc,
                    step=6, total=6,
                    view=RulesView(),
                    color=0xE67E22
                ):
                    return False

                # Abschluss abhängig vom Status
                closing_lines = []
                if status_choice == STATUS_NEW_PLAYER:
                    closing_lines.append(
                        "✨ **Schön, dass du neu bist!** Für alle Fragen rund um Deadlock frag liebend gern die Community – "
                        "die wartet nur darauf zu helfen. Wenn du eine **Einführung** ins Game (kleines Tutorial) möchtest, "
                        "schreib einfach **@earlysalty**. Oder poste in **#allgemein**: "
                        "_„Hey, ich bin neu und möchte das Spiel Schritt für Schritt entdecken.“_ 💙"
                    )
                if status_choice == STATUS_NEED_BETA:
                    closing_lines.append(
                        "🎟️ **Beta-Invite benötigt?** Super, dass du spielen willst! Deine Einladung bekommst du hier:\n"
                        "https://discord.com/channels/1289721245281292288/1410754840706945034\n\n"
                        "Bitte poste dort eine kurze Nachricht, z. B.:\n"
                        "```\n"
                        "Hey :)\n"
                        "wäre jemand so lieb und könnte mich für den Deadlock-Playtest einladen?\n"
                        "Meine Steam-Freundschafts-ID: 444500904\n"
                        "```\n"
                        "👉 Deine **Steam-Freundschafts-ID** findest du in Steam unter **Freunde → Freund hinzufügen**.\n"
                        "Nachdem dich jemand eingeladen hat, prüfe zum **Akzeptieren** hier:\n"
                        "<https://store.steampowered.com/account/playtestinvites>\n"
                        "_Das kann ein paar Stunden dauern – nicht wundern._"
                    )
                if status_choice == STATUS_RETURNING:
                    closing_lines.append("🔁 **Willkommen zurück!** Fürs Reinkommen frag gern nach **Scrims/Grind-Runden** oder schau bei **Customs** rein.")
                if status_choice == STATUS_PLAYING:
                    closing_lines.append("✅ **Viel Spaß!** Nutz **Customs**, **Patchnotes** & **Guides** – und ping uns, wenn du was brauchst.")

                if closing_lines:
                    try:
                        await member.send("\n\n".join(closing_lines))
                    except discord.Forbidden as e:
                        logger.warning(f"Abschluss-Nachricht: DM an {member} ({member.id}) nicht möglich: {e}")
                    except Exception:
                        logger.exception("Abschluss-Nachricht: Unerwarteter Fehler beim Senden")

                try:
                    if greet_msg:
                        await greet_msg.delete()
                except discord.HTTPException as e:
                    logger.debug(f"Begrüßungsnachricht konnte nicht gelöscht werden: {e}")
                except Exception:
                    logger.exception("Unerwarteter Fehler beim Löschen der Begrüßungsnachricht")

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
