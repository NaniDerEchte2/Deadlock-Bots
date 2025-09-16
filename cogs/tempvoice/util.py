from __future__ import annotations
from typing import Optional, Tuple
import logging
import discord
from .core import STAGING_CHANNEL_IDS

logger = logging.getLogger(__name__)

class TempVoiceUtil:
    def __init__(self, core):
        self.core = core  # TempVoiceCore

    # ---------- Helpers ----------
    async def _find_staging(self, guild: discord.Guild) -> Optional[discord.VoiceChannel]:
        for cid in STAGING_CHANNEL_IDS:
            ch = guild.get_channel(cid)
            if isinstance(ch, discord.VoiceChannel):
                return ch
        return None

    # ---------- Actions ----------
    async def kick(self, lane: discord.VoiceChannel, target_id: int) -> Tuple[bool, str]:
        target = lane.guild.get_member(int(target_id))
        if not target or not target.voice or target.voice.channel != lane:
            return False, "User ist nicht (mehr) in der Lane."
        staging = await self._find_staging(lane.guild)
        if not staging:
            return False, "Staging-Channel nicht gefunden."
        try:
            await target.move_to(staging, reason=f"TempVoice: Kick")
            return True, f"👢 {target.display_name} → {staging.name}"
        except discord.Forbidden:
            logger.warning("Kick fehlgeschlagen: fehlende Rechte (member=%s, lane=%s)", target_id, lane.id)
            return False, "Keine Berechtigung, um Nutzer zu verschieben."
        except discord.HTTPException as e:
            logger.error("Kick HTTPException (member=%s, lane=%s): %s", target_id, lane.id, e)
            return False, "Konnte nicht verschieben (HTTP-Fehler)."
        except Exception:
            logger.exception("Kick unerwarteter Fehler (member=%s, lane=%s)", target_id, lane.id)
            return False, "Konnte nicht verschieben."

    async def ban(self, lane: discord.VoiceChannel, owner_id: int, raw: str) -> Tuple[bool, str]:
        # @Mention, Name oder ID robust auflösen
        uid = await self.core.parse_user_identifier(lane.guild, raw)
        if not uid:
            return False, "Konnte den Nutzer nicht eindeutig erkennen. Bitte @Mention oder numerische ID angeben."

        # Persistenter Owner-Ban (DB) + Permission Overwrite
        try:
            await self.core.bans.add_ban(int(owner_id), int(uid))
        except Exception as e:
            logger.warning("Ban-Persistenz fehlgeschlagen (owner=%s, target=%s): %r", owner_id, uid, e)

        target_member = lane.guild.get_member(int(uid))
        try:
            await lane.set_permissions(
                target_member or discord.Object(id=int(uid)),
                connect=False, reason="TempVoice: Owner-Ban"
            )
        except discord.Forbidden:
            logger.warning("Owner-Ban set_permissions: fehlende Rechte (owner=%s, target=%s, lane=%s)", owner_id, uid, lane.id)
            return False, "Konnte Ban nicht setzen (fehlende Rechte)."
        except discord.HTTPException as e:
            logger.error("Owner-Ban set_permissions HTTPException (owner=%s, target=%s, lane=%s): %s", owner_id, uid, lane.id, e)
            return False, "Konnte Ban nicht setzen (HTTP-Fehler)."
        except Exception:
            logger.exception("Owner-Ban set_permissions: unerwarteter Fehler (owner=%s, target=%s, lane=%s)", owner_id, uid, lane.id)
            return False, "Konnte Ban nicht setzen."

        # Falls der User gerade in der Lane ist, in Staging schieben
        if target_member and target_member.voice and target_member.voice.channel == lane:
            staging = await self._find_staging(lane.guild)
            if staging:
                try:
                    await target_member.move_to(staging, reason="Owner-Ban aktiv")
                except discord.Forbidden:
                    logger.info("Ban-Move: fehlende Rechte beim Verschieben (target=%s, lane=%s)", uid, lane.id)
                except discord.HTTPException as e:
                    logger.info("Ban-Move HTTPException (target=%s, lane=%s): %s", uid, lane.id, e)
                except Exception as e:
                    logger.debug("Ban-Move unerwarteter Fehler (target=%s, lane=%s): %r", uid, lane.id, e)
        return True, "Nutzer gebannt (owner-persistent)."

    async def unban(self, lane: discord.VoiceChannel, owner_id: int, raw: str) -> Tuple[bool, str]:
        uid = await self.core.parse_user_identifier(lane.guild, raw)
        if not uid:
            return False, "Konnte den Nutzer nicht eindeutig erkennen. Bitte @Mention oder numerische ID angeben."

        try:
            await self.core.bans.remove_ban(int(owner_id), int(uid))
        except Exception as e:
            logger.warning("Unban-Persistenz fehlgeschlagen (owner=%s, target=%s): %r", owner_id, uid, e)

        target_member = lane.guild.get_member(int(uid))
        try:
            await lane.set_permissions(
                target_member or discord.Object(id=int(uid)),
                overwrite=None, reason="TempVoice: Owner-Unban"
            )
            return True, "Nutzer entbannt."
        except discord.Forbidden:
            logger.warning("Owner-Unban set_permissions: fehlende Rechte (owner=%s, target=%s, lane=%s)", owner_id, uid, lane.id)
            return False, "Konnte Unban nicht setzen (fehlende Rechte)."
        except discord.HTTPException as e:
            logger.error("Owner-Unban set_permissions HTTPException (owner=%s, target=%s, lane=%s): %s", owner_id, uid, lane.id, e)
            return False, "Konnte Unban nicht setzen (HTTP-Fehler)."
        except Exception:
            logger.exception("Owner-Unban set_permissions: unerwarteter Fehler (owner=%s, target=%s, lane=%s)", owner_id, uid, lane.id)
            return False, "Konnte Unban nicht setzen."
