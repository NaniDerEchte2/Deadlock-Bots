from __future__ import annotations

import logging
import os
from datetime import datetime
from textwrap import dedent
from typing import Any, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import discord

from .base import MIN_NEXT_SECONDS

__all__ = [
    "SteamLinkStepView",
    "SteamLinkView",          # Alias (Backcompat)
    "SteamLinkNudgeView",     # Alias (für rules_channel)
    "build_steam_intro_embed",
    "steam_link_dm_description",
    "steam_link_detailed_description",
]

# --- optionale Steam-Link-Integration (kann fehlen) ---
_LOGGER = logging.getLogger(__name__)
try:
    from cogs.steam import steam_link_oauth as _oauth  # type: ignore
except Exception:
    _oauth = None  # type: ignore[assignment]
    _LOGGER.info("Steam link OAuth module unavailable – link buttons will be disabled.")

if _oauth is not None and not hasattr(_oauth, "start_urls_for"):
    _LOGGER.warning(
        "cogs.steam.steam_link_oauth is missing 'start_urls_for'; disabling Steam link buttons.",
    )
    _oauth = None  # type: ignore[assignment]

_LINKS_ENABLED: bool = _oauth is not None

# --- ENV: Discord OAuth Deep-Link (in-App Dialog) ---
_DEEPLINK_EN = str(os.getenv("DISCORD_OAUTH_DEEPLINK", "0")).strip().lower() not in ("", "0", "false", "no")

def _prefer_discord_deeplink(browser_url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Liefert (primary_url, browser_fallback). Wenn Deep-Link aktiv und erkennbar,
    wird 'discord://-/oauth2/authorize?...' als primary geliefert, sonst (url, None).
    """
    if not browser_url:
        return None, None
    try:
        u = urlparse(browser_url)
        hostname = (u.hostname or "").lower()
        path = u.path or ""
        # akzeptiere /oauth2/authorize sowohl mit/ohne /api
        if (
            u.scheme in {"http", "https"}
            and hostname
            and (hostname == "discord.com" or hostname.endswith(".discord.com"))
            and (path == "/oauth2/authorize" or path.startswith("/oauth2/authorize/"))
        ):
            if _DEEPLINK_EN:
                deeplink = urlunparse(("discord", "-/oauth2/authorize", "", "", u.query, ""))
                return deeplink, browser_url
    except Exception as exc:
        _LOGGER.debug("Konnte Deep-Link URL nicht parsen (%s): %s", browser_url, exc)
    return browser_url, None

_STEAM_LINK_DM_DESC = dedent(
    """
    **Verknüpfe deinen Steam Account**
    """
).strip()


_STEAM_LINK_DETAILED_DESC = dedent(
    """
    • Wozu ist das gut? Über den Steam-Bot kannst du Freundschaftsanfragen austauschen
      und Einladungen schneller koordinieren.


    **Ablauf & Optionen:**
    • **Via Discord bei Steam anmelden** – Offizieller Login über unser Portal (kein Passwort, wir lesen nur die **SteamID64**).
    • **Direkt bei Steam anmelden** – Öffnet Steam, damit du deinen Account bestätigst (wir speichern nur die **SteamID64**).


    • Sobald du dich authentifizierst, kann dir unser Bot automatisch eine Freundschaftsanfrage schicken.


    **Hinweis:** Automatische Status-Anzeigen über Steam sind aktuell deaktiviert – die Verknüpfung ist freiwillig.
    """
).strip()


def steam_link_dm_description() -> str:
    return _STEAM_LINK_DM_DESC


def steam_link_detailed_description() -> str:
    return _STEAM_LINK_DETAILED_DESC


def build_steam_intro_embed() -> discord.Embed:
    """Intro/Erklärung für den Schritt mit allen verfügbaren Optionen."""
    em = discord.Embed(
        title="Empfehlung für besseres Erlebnis",
        description=steam_link_detailed_description(),
        colour=discord.Colour.blurple(),
    )
    em.set_footer(text="Kurzbefehle: /steam link, /steam link_steam")
    return em


class _LinkSheet(discord.ui.View):
    """Ephemere Mini-View mit den aktuellen Login-Optionen."""

    def __init__(self, *, discord_url: str, steam_url: str):
        super().__init__(timeout=120)
        self.add_item(
            discord.ui.Button(
                label="Via Discord bei Steam anmelden",
                style=discord.ButtonStyle.link,
                url=discord_url,
                emoji="🔗",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Direkt bei Steam anmelden",
                style=discord.ButtonStyle.link,
                url=steam_url,
                emoji="🎮",
            )
        )


class SteamLinkStepView(discord.ui.View):
    """
    View für den Steam-Verknüpfungsschritt in der Welcome-DM.
    WICHTIG: Diese View enthält KEINE Link-Buttons.
             Die tatsächlichen URLs werden erst beim Klick als ephemere Link-View gesendet.
    Dadurch keine ablaufenden/alten OAuth-Links in persistenter View.
    """
    def __init__(
        self,
        *,
        on_next=None,                 # async def (interaction) -> None
        timeout: float | None = None, # persistent-fähig
        show_next: bool = True,
        allowed_user_id: Optional[int] = None,
        created_at: Optional[datetime] = None,
    ):
        super().__init__(timeout=timeout)
        self.on_next = on_next
        self.show_next = show_next
        self.proceed: bool = False
        self.allowed_user_id: Optional[int] = allowed_user_id
        self.created_at: datetime = created_at or datetime.now()
        self._persistence_info: Optional[dict[str, Any]] = None

        if not _LINKS_ENABLED:
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.custom_id == "steam:discord":
                    child.disabled = True
                    child.label = "Verknüpfung deaktiviert"

    # --- Buttons (nur custom_id, keine URLs – dadurch persistent-fähig) ---

    @discord.ui.button(
        label="Via Discord bei Steam anmelden",
        style=discord.ButtonStyle.success,
        custom_id="steam:discord",
        row=0,
        emoji="🔗",
    )
    async def _start_discord(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._present_link_sheet(interaction)

    async def _present_link_sheet(self, interaction: discord.Interaction) -> None:
        if not _LINKS_ENABLED or _oauth is None:
            message = (
                "ℹ️ Die automatische Steam-Verknüpfung ist derzeit deaktiviert. "
                "Bitte sende dem Bot direkt eine Anfrage."
            )
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return
        uid = interaction.user.id
        try:
            urls = _oauth.start_urls_for(uid)
        except Exception:
            urls = {}
        if not urls.get("discord_start"):
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Start-Link nicht konfiguriert. Bitte später erneut versuchen.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "❌ Start-Link nicht konfiguriert. Bitte später erneut versuchen.",
                    ephemeral=True,
                )
            return

        # Deep-Link bevorzugen (falls aktiviert) + Browser-Fallback anfügen
        primary, browser_fallback = _prefer_discord_deeplink(urls["discord_start"])
        discord_link = primary or urls["discord_start"]

        sheet = _LinkSheet(
            discord_url=discord_link,
            steam_url=urls["steam_openid_start"],
        )

        msg = "🔐 Wähle den Link:"
        if browser_fallback and discord_link.startswith("discord://"):
            msg += f" _(Falls sich nichts öffnet: [Browser-Variante]({browser_fallback}))_"

        if interaction.response.is_done():
            await interaction.followup.send(msg, view=sheet, ephemeral=True)
        else:
            await interaction.response.send_message(msg, view=sheet, ephemeral=True)

    @discord.ui.button(
        label="Direkt bei Steam anmelden",
        style=discord.ButtonStyle.primary,
        custom_id="steam:openid",
        row=0,
        emoji="🎮",
    )
    async def _start_openid(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._present_link_sheet(interaction)

    @discord.ui.button(
        label="Weiter",
        style=discord.ButtonStyle.primary,
        custom_id="steam:next",
        row=1,
        emoji="⏭️",
    )
    async def _next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        self._notify_persistence_finished()
        self.proceed = True
        self.stop()
        if callable(self.on_next):
            try:
                await self.on_next(interaction)
                return
            except Exception as exc:
                _LOGGER.debug("SteamLinkStepView on_next handler failed: %s", exc, exc_info=True)

    def bind_persistence(self, manager: Any, message_id: int) -> None:
        self._persistence_info = {"manager": manager, "message_id": message_id}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.allowed_user_id is not None and interaction.user.id != self.allowed_user_id:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Nur der eingeladene Nutzer kann diesen Schritt abschließen.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        "Nur der eingeladene Nutzer kann diesen Schritt abschließen.",
                        ephemeral=True,
                    )
            except Exception:
                _LOGGER.debug(
                    "SteamLinkStepView interaction rejected (user=%s)",
                    getattr(interaction.user, "id", "?"),
                    exc_info=True,
                )
            return False
        return True

    def _notify_persistence_finished(self) -> None:
        info = self._persistence_info
        if not info:
            return
        self._persistence_info = None
        manager = info.get("manager")
        message_id = info.get("message_id")
        if manager is None or message_id is None:
            return
        try:
            manager._unpersist_view(int(message_id))  # type: ignore[attr-defined]
        except Exception:
            _LOGGER.debug(
                "SteamLinkStepView Persistenz-Abmeldung fehlgeschlagen (message_id=%s)",
                message_id,
                exc_info=True,
            )

    async def _enforce_min_wait(self, interaction: discord.Interaction) -> bool:
        elapsed = (datetime.now() - self.created_at).total_seconds()
        remain = int(MIN_NEXT_SECONDS - elapsed)
        if remain <= 0:
            return True
        message = "⏳ Kurzer Moment… bitte noch kurz lesen. Du schaffst das. 💙"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await interaction.followup.send(message, ephemeral=True)
        except Exception:
            _LOGGER.debug(
                "SteamLinkStepView Min-Wait Hinweis konnte nicht gesendet werden (user=%s)",
                getattr(interaction.user, "id", "?"),
                exc_info=True,
            )
        return False


# --- Aliase für ältere Imports ---
SteamLinkView = SteamLinkStepView
SteamLinkNudgeView = SteamLinkStepView
