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
            await target.move_to(staging, reason="TempVoice: Kick")
            return True, f"KICK {target.display_name} -> {staging.name}"
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
        uid, err_msg = await self.core.parse_user_identifier(lane.guild, raw)
        if not uid:
            return False, f"Konnte den Nutzer nicht finden. {err_msg or 'Bitte @Erwähnung oder ID angeben.'}"

        # Persistenter Owner-Ban (DB) + Permission Overwrite
        try:
            await self.core.bans.add_ban(int(owner_id), int(uid))
        except Exception as e:
            logger.warning("Ban-Persistenz fehlgeschlagen (owner=%s, target=%s): %r", owner_id, uid, e)

        target_member = await self.core.resolve_member(lane.guild, uid)
        overwrite_applied = False
        if target_member:
            try:
                await lane.set_permissions(
                    target_member,
                    connect=False, reason="TempVoice: Owner-Ban"
                )
                overwrite_applied = True
            except discord.Forbidden:
                logger.warning("Owner-Ban set_permissions: fehlende Rechte (owner=%s, target=%s, lane=%s)", owner_id, uid, lane.id)
                return False, "Konnte Ban nicht setzen (fehlende Rechte)."
            except discord.HTTPException as e:
                logger.error("Owner-Ban set_permissions HTTPException (owner=%s, target=%s, lane=%s): %s", owner_id, uid, lane.id, e)
                return False, "Konnte Ban nicht setzen (HTTP-Fehler)."
            except Exception:
                logger.exception("Owner-Ban set_permissions: unerwarteter Fehler (owner=%s, target=%s, lane=%s)", owner_id, uid, lane.id)
                return False, "Konnte Ban nicht setzen."
        else:
            logger.debug("Owner-Ban: Member %s nicht in Guild %s - nur Persistenz", uid, lane.guild.id)

        if overwrite_applied:
            return True, "User gebannt (dauerhaft für diesen Owner)."
        return True, "User gebannt (dauerhaft für diesen Owner). Hinweis: User ist aktuell nicht auf dem Server; Sperre greift beim nächsten Join."

    async def unban(self, lane: discord.VoiceChannel, owner_id: int, raw: str) -> Tuple[bool, str]:
        uid, err_msg = await self.core.parse_user_identifier(lane.guild, raw)
        if not uid:
            return False, f"Konnte den Nutzer nicht finden. {err_msg or 'Bitte @Erwähnung oder ID angeben.'}"

        try:
            await self.core.bans.remove_ban(int(owner_id), int(uid))
        except Exception as e:
            logger.warning("Unban-Persistenz fehlgeschlagen (owner=%s, target=%s): %r", owner_id, uid, e)

        target_member = await self.core.resolve_member(lane.guild, uid)
        if target_member:
            try:
                await lane.set_permissions(
                    target_member,
                    overwrite=None, reason="TempVoice: Owner-Unban"
                )
                return True, "User entbannt."
            except discord.Forbidden:
                logger.warning("Owner-Unban set_permissions: fehlende Rechte (owner=%s, target=%s, lane=%s)", owner_id, uid, lane.id)
                return False, "Konnte Unban nicht setzen (fehlende Rechte)."
            except discord.HTTPException as e:
                logger.error("Owner-Unban set_permissions HTTPException (owner=%s, target=%s, lane=%s): %s", owner_id, uid, lane.id, e)
                return False, "Konnte Unban nicht setzen (HTTP-Fehler)."
            except Exception:
                logger.exception("Owner-Unban set_permissions: unerwarteter Fehler (owner=%s, target=%s, lane=%s)", owner_id, uid, lane.id)
                return False, "Konnte Unban nicht setzen."
        logger.debug("Owner-Unban: Member %s nicht in Guild %s - nur Datenbankeintrag entfernt", uid, lane.guild.id)
        return True, "User entbannt (es waren keine aktiven Channel-Rechte vorhanden)."

    async def toggle_lurker(self, lane: discord.VoiceChannel, member: discord.Member) -> Tuple[bool, str]:
        """
        Lurker-Status für den klickenden User umschalten.
        Keine Auswahl/Droplist – der Button-Drücker ist immer das Ziel.
        """
        if not member.voice or member.voice.channel != lane:
            return False, "Du musst in der Lane sein."

        role = lane.guild.get_role(1447747896253485127)  # Hardcoded Lurker Role ID
        if not role:
            return False, "Die konfigurierte Lurker-Rolle (ID 1447747896253485127) existiert nicht auf dem Server."

        existing_lurker_data = await self.core.lurkers.get_lurker(lane.id, member.id)

        if existing_lurker_data:  # User is currently a lurker, so remove
            try:
                if role in member.roles:
                    await member.remove_roles(role, reason="TempVoice: Remove Lurker")
            except discord.Forbidden:
                return False, "Fehlende Berechtigung für Rolle 'Lurker' zu entfernen."
            except Exception as e:
                logger.error("toggle_lurker remove_roles error: %r", e)
                return False, "Konnte Rolle nicht entfernen."

            # Restore original nickname
            original_nick = existing_lurker_data.get("original_nick")
            try:
                await member.edit(nick=original_nick, reason="TempVoice: Restore Nick")
            except discord.Forbidden:
                pass  # Not critical, continue
            except Exception as e:
                logger.warning("toggle_lurker nick restore failed: %r", e)

            # Decrease Limit
            if lane.user_limit > 0:
                new_limit = max(0, lane.user_limit - 1)
                await self.core.safe_edit_channel(lane, desired_limit=new_limit, reason="TempVoice: Lurker removed")

            try:
                await self.core.lurkers.remove_lurker(lane.id, member.id)
            except Exception as e:
                logger.error("toggle_lurker DB remove error: %r", e)

            return True, f"{member.display_name} ist kein Lurker mehr."

        # User is not a lurker, so add
        original_nick = member.nick

        try:
            await self.core.lurkers.add_lurker(lane.guild.id, lane.id, member.id, original_nick)
        except Exception as e:
            logger.error("toggle_lurker DB add error: %r", e)
            return False, "Datenbankfehler beim Hinzufügen des Lurker-Status."

        try:
            await member.add_roles(role, reason="TempVoice: Make Lurker")
        except discord.Forbidden:
            await self.core.lurkers.remove_lurker(lane.id, member.id)  # Rollback DB
            return False, "Fehlende Berechtigung für Rolle 'Lurker' zu vergeben."
        except Exception as e:
            await self.core.lurkers.remove_lurker(lane.id, member.id)  # Rollback DB
            logger.error("toggle_lurker add_roles error: %r", e)
            return False, "Konnte Rolle nicht vergeben."

        try:
            await member.edit(nick="Lurker", reason="TempVoice: Make Lurker")
        except discord.Forbidden:
            pass  # Not critical, continue
        except Exception as e:
            logger.warning("toggle_lurker nick change failed: %r", e)

        if lane.user_limit > 0:
            new_limit = min(99, lane.user_limit + 1)
            await self.core.safe_edit_channel(lane, desired_limit=new_limit, reason="TempVoice: Lurker added")

        return True, f"{member.display_name} ist jetzt Lurker."
