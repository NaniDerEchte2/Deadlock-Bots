# cogs/welcome_dm/step_streamer.py
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Callable

import discord

from .base import StepView, logger

# Feste Rollen-ID für Streamer-Partner (vom Nutzer vorgegeben)
STREAMER_ROLE_ID = 1313624729466441769

_log = logging.getLogger(__name__)


def _find_callable(cog: object, names: list[str]) -> Optional[Callable]:
    for n in names:
        fn = getattr(cog, n, None)
        if callable(fn):
            return fn
    return None


def _get_twitch_cog(client: discord.Client) -> Optional[object]:
    # Versuche verschiedene bekannte Namen
    for name in ("TwitchDeadlock", "TwitchDeadlockCog", "TwitchBot", "TwitchLiveBot", "Twitch"):
        cog = client.get_cog(name)  # type: ignore[attr-defined]
        if cog:
            return cog
    return None


async def _register_in_twitch(client: discord.Client, user_id: int) -> bool:
    cog = _get_twitch_cog(client)
    if not cog:
        _log.info("StreamerStep: Kein Twitch-Cog gefunden – Registrierung wird übersprungen.")
        return False

    # Häufige Varianten annehmen – wir probieren mehrere Signaturen
    candidates = [
        ("register_streamer", (user_id,)),
        ("add_streamer", (user_id,)),
        ("ensure_streamer", (user_id,)),
        ("register_member", (user_id,)),
        ("whitelist_streamer", (user_id,)),
    ]

    for name, args in candidates:
        fn = getattr(cog, name, None)
        if not callable(fn):
            continue
        try:
            if asyncio.iscoroutinefunction(fn):  # type: ignore[arg-type]
                await fn(*args)
            else:
                fn(*args)
            _log.info("StreamerStep: Twitch-Registrierung via %s erfolgreich für user_id=%s", name, user_id)
            return True
        except Exception as e:
            _log.warning("StreamerStep: Twitch-Registrierung via %s fehlgeschlagen: %r", name, e)
    return False


class StreamerView(StepView):
    """Optionaler Schritt: Streamer-Partner werden.

    Nutzt dieselbe View-Klasse für DM **und** Thread (Regelwerk->Weiter).
    Buttons haben feste custom_id (persistent).
    """

    # --- Button: Ich habe alles gemacht (Rolle + Twitch-Register)
    @discord.ui.button(
        label="Ich habe alles gemacht – zum Streamer freischalten",
        style=discord.ButtonStyle.success,
        custom_id="wdm:streamer:done",
    )
    async def btn_done(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return

        guild, member = self._get_guild_and_member(interaction)
        if not guild or not member:
            try:
                await interaction.response.send_message(
                    "⚠️ Konnte dich in der Haupt-Guild nicht finden. Bitte melde dich beim Team.",
                    ephemeral=True,
                )
            except Exception:
                pass
            await self._finish(interaction)
            return

        # 1) Rolle vergeben
        role = guild.get_role(STREAMER_ROLE_ID)
        role_ok = False
        if role:
            try:
                await member.add_roles(role, reason="Welcome Onboarding: Streamer bestätigt")
                role_ok = True
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning("StreamerStep: Rolle konnte nicht gesetzt werden (%s): %r", STREAMER_ROLE_ID, e)
        else:
            logger.warning("StreamerStep: STREAMER_ROLE_ID=%s wurde in der Guild nicht gefunden.", STREAMER_ROLE_ID)

        # 2) Twitch-Bot: Registrierung versuchen (best-effort)
        twitch_ok = await _register_in_twitch(interaction.client, member.id)

        # 3) Rückmeldung
        msg_bits = []
        msg_bits.append("✅ **Streamer-Setup erledigt.**")
        if role_ok:
            msg_bits.append("• Rolle vergeben.")
        else:
            msg_bits.append("• Rolle **konnte nicht** vergeben werden – bitte Team pingen.")

        if twitch_ok:
            msg_bits.append("• Twitch-Bot: Registrierung aktiv. Wir posten dich in **#live-on-twitch**, wenn du Deadlock streamst.")
        else:
            msg_bits.append("• Twitch-Bot: Registrierung aktuell **nicht bestätigt**. Das Team checkt das.")

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("\n".join(msg_bits), ephemeral=True)
            else:
                await interaction.followup.send("\n".join(msg_bits), ephemeral=True)
        except Exception:
            pass

        await self._finish(interaction)

    # --- Button: Später erledigen / Ich bin kein Streamer
    @discord.ui.button(
        label="Später erledigen / Bin kein Streamer",
        style=discord.ButtonStyle.secondary,
        custom_id="wdm:streamer:skip",
    )
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Alles klar – wir machen später weiter. 💙", ephemeral=True)
            else:
                await interaction.followup.send("Alles klar – wir machen später weiter. 💙", ephemeral=True)
        except Exception:
            pass
        await self._finish(interaction)
