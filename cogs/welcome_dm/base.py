# cogs/welcome_dm/base.py
import discord
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, Union

logger = logging.getLogger(__name__)

# ---------- IDs (prüfen/anpassen) ----------
MAIN_GUILD_ID                   = 1289721245281292288  # Haupt-Guild (für Member/Rollen in DMs)
FUNNY_CUSTOM_ROLE_ID            = 1407085699374649364
GRIND_CUSTOM_ROLE_ID            = 1407086020331311144
PATCHNOTES_ROLE_ID              = 1330994309524357140
UBK_ROLE_ID                     = 1397687886580547745  # UBK (= Unbekannt) Pflicht-Fallback für Rang
PHANTOM_NOTIFICATION_CHANNEL_ID = 1374364800817303632
ONBOARD_COMPLETE_ROLE_ID        = 1304216250649415771  # Rolle nach Regelbestätigung
THANK_YOU_DELETE_AFTER_SECONDS  = 300  # 5 Minuten
# -------------------------------------------

# Mindest-Lesezeit für alle "Weiter"- und "Ne danke"-Aktionen
MIN_NEXT_SECONDS = 5

# Status-Optionen (Frage 1)
STATUS_NEED_BETA   = "need_beta"
STATUS_PLAYING     = "already_playing"
STATUS_RETURNING   = "returning"
STATUS_NEW_PLAYER  = "new_player"

# ========= Emoji-Konfiguration =========
RANK_EMOJI_OVERRIDES: Dict[str, Union[str, int]] = {
    # "phantom": "dl_phantom",
    # "ascendant": 123456789012345678,
    # "ubk": "ubk_emoji_name_or_id",  # optional
}
UNKNOWN_FALLBACK_EMOJI = "❓"
# ======================================

def _find_custom_emoji(guild: discord.Guild, key: Union[str, int]) -> Optional[Union[discord.Emoji, discord.PartialEmoji]]:
    try:
        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            emoji_id = int(key)
            for e in guild.emojis:
                if e.id == emoji_id:
                    return e
            return discord.PartialEmoji(name=None, id=emoji_id, animated=False)
        else:
            name = str(key).lower()
            for e in guild.emojis:
                if e.name.lower() == name:
                    return e
            for e in guild.emojis:
                if name in e.name.lower():
                    return e
    except Exception:
        return None
    return None

def get_rank_emoji(guild: Optional[discord.Guild], rank_key: str) -> Optional[Union[discord.Emoji, discord.PartialEmoji, str]]:
    if guild is None:
        return UNKNOWN_FALLBACK_EMOJI if rank_key == "ubk" else None
    if rank_key in RANK_EMOJI_OVERRIDES:
        e = _find_custom_emoji(guild, RANK_EMOJI_OVERRIDES[rank_key])
        if e:
            return e
    e2 = _find_custom_emoji(guild, rank_key)
    if e2:
        return e2
    if rank_key == "ubk":
        return UNKNOWN_FALLBACK_EMOJI
    return None

async def remove_all_rank_roles(member: discord.Member, guild: discord.Guild):
    ranks = {
        "initiate", "seeker", "alchemist", "arcanist", "ritualist",
        "emissary", "archon", "oracle", "phantom", "ascendant", "eternus"
    }
    to_remove = [r for r in member.roles if r.name.lower() in ranks]
    if to_remove:
        await member.remove_roles(*to_remove, reason="Welcome DM Rangauswahl")

def build_step_embed(title: str, desc: str, step: Optional[int], total: int, color: int = 0x5865F2) -> discord.Embed:
    emb = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.now())
    footer = "Einführung • Deadlock DACH" if step is None else f"Frage {step} von {total} • Deadlock DACH"
    emb.set_footer(text=footer)
    return emb

def _safe_role_name(guild: Optional[discord.Guild], role_id: int, fallback: str) -> str:
    if guild:
        r = guild.get_role(role_id)
        if r:
            return r.name
    return fallback

class StepView(discord.ui.View):
    """Basis-View mit Persistenz + Mindestwartezeit."""
    def __init__(self):
        super().__init__(timeout=None)
        self.proceed: bool = False
        self.created_at: datetime = datetime.now()
        self.bound_message: Optional[discord.Message] = None

    @staticmethod
    def _get_guild_and_member(inter: discord.Interaction) -> tuple[Optional[discord.Guild], Optional[discord.Member]]:
        guild = inter.client.get_guild(MAIN_GUILD_ID)  # type: ignore
        if guild is None:
            return None, None
        m = guild.get_member(inter.user.id)
        return guild, m

    async def _enforce_min_wait(self, interaction: discord.Interaction, *, custom_txt: Optional[str] = None) -> bool:
        elapsed = (datetime.now() - self.created_at).total_seconds()
        remain = int(MIN_NEXT_SECONDS - elapsed)
        if remain > 0:
            txt = custom_txt or "⏳ Kurzer Moment… bitte noch kurz lesen. Du schaffst das. 💙"
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(txt, ephemeral=True)
                else:
                    await interaction.followup.send(txt, ephemeral=True)
            except Exception:
                pass
            return False
        return True

    def force_finish(self):
        self.proceed = True
        self.stop()

    async def _finish(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except Exception:
            pass
        try:
            await interaction.message.delete()
        except Exception:
            pass
        self.force_finish()
