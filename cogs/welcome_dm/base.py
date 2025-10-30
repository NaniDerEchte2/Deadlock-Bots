# cogs/welcome_dm/base.py
import discord
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ---------- IDs (prüfen/anpassen) ----------
MAIN_GUILD_ID                   = 1289721245281292288  # Haupt-Guild (für Member/Rollen in DMs)
ONBOARD_COMPLETE_ROLE_ID        = 1304216250649415771  # Rolle nach Regelbestätigung
THANK_YOU_DELETE_AFTER_SECONDS  = 300  # 5 Minuten
# -------------------------------------------

# Mindest-Lesezeit für alle "Weiter"- und "Ne danke"-Aktionen
MIN_NEXT_SECONDS = 2

# Status-Optionen (Frage 1)
STATUS_NEED_BETA   = "need_beta"
STATUS_PLAYING     = "already_playing"
STATUS_RETURNING   = "returning"
STATUS_NEW_PLAYER  = "new_player"

# Beta-Invite Infos
BETA_INVITE_CHANNEL_URL = "https://discord.com/channels/1289721245281292288/1428745737323155679"
BETA_INVITE_SUPPORT_CONTACT = "@earlysalty"

def build_step_embed(title: str, desc: str, step: Optional[int], total: int, color: int = 0x5865F2) -> discord.Embed:
    emb = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.now())
    footer = "Einführung • Deutsche Deadlock Community" if step is None else f"Frage {step} von {total} • Deutsche Deadlock Community"
    emb.set_footer(text=footer)
    return emb

def _is_dm_channel(ch: Optional[discord.abc.Messageable]) -> bool:
    return isinstance(ch, (discord.DMChannel, discord.GroupChannel))

def _is_thread(ch: Optional[discord.abc.Messageable]) -> bool:
    return isinstance(ch, discord.Thread)

class StepView(discord.ui.View):
    """Basis-View mit Persistenz + Mindestwartezeit. Funktioniert in DM und Threads."""
    def __init__(self):
        super().__init__(timeout=None)
        self.proceed: bool = False
        self.created_at: datetime = datetime.now()
        self.bound_message: Optional[discord.Message] = None

    @staticmethod
    def _get_guild_and_member(inter: discord.Interaction) -> tuple[Optional[discord.Guild], Optional[discord.Member]]:
        # Primär über MAIN_GUILD_ID (robust, falls die Interaction z. B. in einem Thread stattfindet)
        guild = inter.client.get_guild(MAIN_GUILD_ID)  # type: ignore
        if guild is None:
            # Fallback: benutze die Guild aus der Interaction, wenn vorhanden
            guild = getattr(inter, "guild", None)
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
                logger.debug(
                    "Min-wait Hinweis konnte nicht gesendet werden (user=%s).",
                    getattr(getattr(interaction, "user", None), "id", "?"),
                    exc_info=True,
                )
            return False
        return True

    def force_finish(self):
        self.proceed = True
        self.stop()

    async def _finish(self, interaction: discord.Interaction):
        """Buttons deaktivieren; in DMs löschen wir die Nachricht, in Threads/Guild-Chats bleibt sie bestehen."""
        # 1) Buttons disablen
        for child in self.children:
            child.disabled = True
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except Exception:
            logger.debug("Konnte View beim Abschluss nicht aktualisieren.", exc_info=True)

        # 2) Nur in DMs löschen (in Threads soll die Historie sichtbar bleiben)
        ch = interaction.channel
        if _is_dm_channel(ch):
            try:
                await interaction.message.delete()
            except Exception:
                logger.debug("Konnte Abschluss-Nachricht nicht löschen.", exc_info=True)

        self.force_finish()
