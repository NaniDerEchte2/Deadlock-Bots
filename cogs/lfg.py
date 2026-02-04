"""
Smart LFG Agent - Deadlock LFG System
Analysiert Anfragen mit KI und routet Spieler basierend auf Skill, Modus und Verfügbarkeit.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional, Tuple, Any

import discord
from discord.ext import commands

log = logging.getLogger("SmartLFG")

# --- Konfiguration ---

# Test-Kanal für die neue Logik (User Request)
LFG_CHANNEL_ID = 1374364800817303632 
# LFG_CHANNEL_ID = 1376335502919335936  # Live Channel (aktuell deaktiviert für Tests)

GUILD_ID = 1289721245281292288

# AI Config (ChatGPT/OpenAI)
OPENAI_MODEL = "gpt-5.2"

# Spezielle Channel / Kategorien
NEW_PLAYER_LANE_ID = 1465839460485697556
STREET_BRAWL_LANE_ID = 1357422958544420944
CASUAL_CATEGORY_ID = 1289721245281292290
RANKED_CATEGORY_ID = 1412804540994162789  # "Grind" Category

# Rollen & Ranks
UNKNOWN_ROLE_ID = 1397687886580547745
UNKNOWN_RANK_NAME = "Unbekannt"

# Rank Definitionen
# 1-5: New Player Friendly
# 6-7: Mid Elo
# 8+: High Elo
DISCORD_RANK_ROLES = {
    1331457571118387210: ("Initiate", 1),
    1331457652877955072: ("Seeker", 2),
    1331457699992436829: ("Alchemist", 3),
    1331457724848017539: ("Arcanist", 4),
    1331457879345070110: ("Ritualist", 5),
    1331457898781474836: ("Emissary", 6),
    1331457949654319114: ("Archon", 7),
    1316966867033653338: ("Oracle", 8),
    1331458016356208680: ("Phantom", 9),
    1331458049637875785: ("Ascendant", 10),
    1331458087349129296: ("Eternus", 11),
    1397687886580547745: ("Unbekannt", 0),
}


class SmartLFGAgent(commands.Cog):
    """
    KI-gesteuerter LFG Bot, der Nutzer basierend auf Rang und Anfrage intelligent zuweist.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.lfg_cooldowns: Dict[int, float] = {}
        self.cooldown_seconds = 60  # Kurzer Cooldown gegen Spam

    async def cog_load(self) -> None:
        log.info("SmartLFGAgent geladen - Channel: %s", LFG_CHANNEL_ID)

    def _get_user_rank(self, member: discord.Member) -> Tuple[str, int]:
        """Ermittelt den höchsten Rang eines Users."""
        highest = ("Unbekannt", 0)
        for role in member.roles:
            if role.id in DISCORD_RANK_ROLES:
                r_name, r_val = DISCORD_RANK_ROLES[role.id]
                if r_val > highest[1]:
                    highest = (r_name, r_val)
        
        # Fallback: Wenn User 'Unbekannt' Rolle explizit hat oder gar keine Rank Rolle
        if highest[1] == 0:
            return (UNKNOWN_RANK_NAME, 0)
        return highest

    def _get_voice_state_context(self, guild: discord.Guild) -> str:
        """
        Scannt relevante Voice-Kanäle und baut einen Kontext-String für die KI.
        Zeigt: Name, ID, Anzahl User, Avg Rank (textuell).
        """
        lines = []

        # Helper um Channel-Info zu bauen
        def analyze_channel(channel: discord.VoiceChannel, label: str):
            members = [m for m in channel.members if not m.bot]
            count = len(members)
            
            ranks = []
            for m in members:
                _, r_val = self._get_user_rank(m)
                if r_val > 0:
                    ranks.append(r_val)
            
            avg_rank_str = "Leer"
            if ranks:
                avg_val = sum(ranks) / len(ranks)
                # Mapping back roughly to name
                # 1-5 Low, 6-7 Mid, 8+ High
                if avg_val < 5.5: avg_rank_str = f"Low (~{avg_val:.1f})"
                elif avg_val < 7.5: avg_rank_str = f"Mid (~{avg_val:.1f})"
                else: avg_rank_str = f"High (~{avg_val:.1f})"

            link = f"https://discord.com/channels/{guild.id}/{channel.id}"
            return f"- {label}: [{channel.name}]({link}) (User: {count}, Skill: {avg_rank_str}, ID: {channel.id})"

        # 1. New Player Lane
        np_chan = guild.get_channel(NEW_PLAYER_LANE_ID)
        if np_chan and isinstance(np_chan, discord.VoiceChannel):
            lines.append(analyze_channel(np_chan, "New Player Lane"))

        # 2. Street Brawl
        sb_chan = guild.get_channel(STREET_BRAWL_LANE_ID)
        if sb_chan and isinstance(sb_chan, discord.VoiceChannel):
            lines.append(analyze_channel(sb_chan, "Street Brawl Lane"))

        # 3. Casual Category
        cat_casual = guild.get_channel(CASUAL_CATEGORY_ID)
        if cat_casual and isinstance(cat_casual, discord.CategoryChannel):
            # Zeige nur Channels mit Usern ODER die ersten 2 leeren
            empty_shown = 0
            for vc in cat_casual.voice_channels:
                if vc.id in [NEW_PLAYER_LANE_ID, STREET_BRAWL_LANE_ID]: continue # Skip duplicates
                if len(vc.members) > 0:
                    lines.append(analyze_channel(vc, "Casual Lane"))
                elif empty_shown < 2:
                    lines.append(analyze_channel(vc, "Casual Lane (Leer)"))
                    empty_shown += 1

        # 4. Ranked Category
        cat_ranked = guild.get_channel(RANKED_CATEGORY_ID)
        if cat_ranked and isinstance(cat_ranked, discord.CategoryChannel):
            empty_shown = 0
            for vc in cat_ranked.voice_channels:
                if len(vc.members) > 0:
                    lines.append(analyze_channel(vc, "Ranked/Grind Lane"))
                elif empty_shown < 1: # Zeige nur 1 leeren Ranked Channel
                    lines.append(analyze_channel(vc, "Ranked/Grind Lane (Leer)"))
                    empty_shown += 1

        return "\n".join(lines)

    async def _handle_lfg_request(self, message: discord.Message):
        """
        Verarbeitet die Anfrage via OpenAI (ChatGPT).
        """
        # 1. User Info
        rank_name, rank_val = self._get_user_rank(message.author)
        is_new_player = rank_val <= 5  # Unbekannt (0) bis Ritualist (5)
        
        # 2. Voice Context holen
        voice_context = self._get_voice_state_context(message.guild)

        # 3. Prompt bauen
        system_prompt = (
            "Du bist der LFG-Buddy der deutschen Deadlock Community. "
            "Dein Ziel ist es, Spieler basierend auf ihrem Rang und Wunschmodus in den richtigen Voice-Channel zu lotsen.\n\n"
            
            "**DEINE PERSÖNLICHKEIT (Authentisch):**\n"
            "- Du bist freundlich, direkt und nutzt lockere Umgangssprache (Duzen).\n"
            "- Du nutzt Smileys wie `:)` `;)` oder auch mal `❤️`, aber spamst sie nicht.\n"
            "- Du schreibst eher kurze Sätze. Keine Romane.\n"
            "- **KERN-PHILOSOPHIE:** Wenn ein empfohlener Channel LEER ist, motivierst du IMMER dazu, ihn aufzumachen. "
            "Dein Mantra: 'Mach dir ne Lane auf, andere kommen dann schon dazu.'\n"
            "- Du antwortest DIREKT mit dem Link zum Channel.\n\n"

            "**BEISPIELE FÜR DEINEN STIL (Nutze diese Art zu sprechen aber nicht exakt immer alles so Picken!):**\n"
            "- \"Mach dir ne Lane auf, die meisten kommen so 17/18h.\"\n"
            "- \"Komm einfach in Lane 2 :)\"\n"
            "- \"Schau mal hier ist zwar gerade keiner da aber wenn du joinst kommen bestimmt paar dazu :)\"\n"
            "- \"Du kannst hier [Link] dazu stoßen.\"\n"
            "- \"Möglichkeiten gibt's hier genug musst dich nur blicken lassen ❤️\"\n"
            "- \"Hard core ge carryt werden da gibts nur eine die Juicer Lane [Link]\"\n"
            "- \"Ansonsten gibts ne humane Lane hier [Link]\"\n"
            "- \"joa mach ne lane auf ich komme dazu\"\n\n"
            
            "**ROUTING REGELN (STRIKTE PRIORITÄT):**\n"
            "1. **Street Brawl:** Wenn der User 'Street Brawl' erwähnt -> Street Brawl Lane.\n"
            "2. **Neue Spieler:** Wenn User Rank 'Unbekannt' bis 'Ritualist' (Rank 0-5) ist UND NICHT explizit nach Ranked fragt -> New Player Lane.\n"
            "   - Schicke neue Spieler NICHT in Lanes mit High Elo Spielern (Oracle+).\n"
            "   - Sag ihnen ruhig, dass das die Lane für Einsteiger ist.\n"
            "3. **Ranked/Grind:** Wenn User explizit 'Ranked' oder 'Grind' will:\n"
            "   - Suche Channel in Ranked Kategorie.\n"
            "   - Toleranz: +/- 2 Ränge (User Rang vs Avg Channel Rank).\n"
            "   - Beispiel: User Emissary (6) passt zu Arcanist(4) bis Oracle(8).\n"
            "4. **Casual/Default:** Wenn nichts anderes passt -> Casual Lanes.\n"
            "   - Hier ist der Rang fast egal, aber vermeide extreme Unterschiede wenn möglich.\n\n"
            
            "**OUTPUT FORMAT:**\n"
            "Antworte kurz (2-3 Sätze). Verlinke den Voice Channel im Format `[ChannelName](URL)`. "
            "Erkläre kurz warum du diesen Channel empfiehlst (z.B. 'passender Rang', 'perfekt für Einsteiger'). "
            "Keine 'Hallo' Floskeln am Anfang, steig direkt ein wie in den Beispielen.\n"
        )

        user_input = (
            f"User: {message.author.display_name}\n"
            f"Rang: {rank_name} (Wert: {rank_val})\n"
            f"Ist New Player: {'Ja' if is_new_player else 'Nein'}\n"
            f"Nachricht: \"{message.content}\"\n\n"
            f"VERFÜGBARE VOICE CHANNELS (Status):\n{voice_context}\n\n"
            "Empfiehl den besten Channel und antworte im Persona-Style."
        )

        # 4. AI Request
        ai = getattr(self.bot, "get_cog", lambda name: None)("AIConnector")
        if not ai:
            log.error("AIConnector nicht gefunden!")
            await message.reply("⚠️ AI Modul nicht geladen. Kann gerade nicht helfen.")
            return

        async with message.channel.typing():
            response_text, _ = await ai.generate_text(
                provider="openai",
                prompt=user_input,
                system_prompt=system_prompt,
                model=OPENAI_MODEL,
                max_output_tokens=250,
                temperature=0.7
            )

        if response_text:
            # Clean up potential markdown code blocks provided by AI
            clean_text = response_text.replace("```markdown", "").replace("```", "").strip()
            await message.reply(clean_text)
        else:
            await message.reply("🤔 Puh, gerade hakt's bei mir. Versuch's gleich nochmal.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        
        # Nur im konfigurierten Channel lauschen
        if message.channel.id != LFG_CHANNEL_ID:
            return

        # Cooldown Check
        now = time.time()
        if now - self.lfg_cooldowns.get(message.author.id, 0) < self.cooldown_seconds:
            # Silent ignore bei Spam oder kurze Reaction
            return
        
        self.lfg_cooldowns[message.author.id] = now
        
        # Wir gehen davon aus, dass alles in diesem Channel LFG ist (oder Smalltalk dazu)
        # Die AI soll auch auf Smalltalk reagieren ("Hat wer bock?" -> "Ja schau mal Lane 1")
        await self._handle_lfg_request(message)

    @commands.command(name="lfgtest")
    @commands.has_permissions(administrator=True)
    async def lfg_debug(self, ctx):
        """Zeigt den aktuellen Voice-Kontext für Debugging."""
        ctx_str = self._get_voice_state_context(ctx.guild)
        await ctx.send(f"**Voice Context Snapshot:**\n{ctx_str}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SmartLFGAgent(bot))
