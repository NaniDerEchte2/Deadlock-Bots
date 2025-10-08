from __future__ import annotations

import os
import re
from textwrap import dedent
from typing import Optional, Tuple
from urllib.parse import urlparse, urlsplit, urlunparse, urlunsplit

import discord

from cogs.steam import QuickInviteButton

__all__ = [
    "SteamLinkStepView",
    "SteamLinkView",          # Alias (Backcompat)
    "SteamLinkNudgeView",     # Alias (für rules_channel)
    "build_steam_intro_embed",
    "steam_link_dm_description",
    "steam_link_detailed_description",
]

# --- harte Abhängigkeit auf das OAuth/Link-Modul (keine Fallbacks) ---
try:
    from cogs.live_match import steam_link_oauth as _oauth
except Exception as e:
    raise ImportError(
        "Erforderliches Modul fehlt: cogs.live_match.steam_link_oauth. Abbruch."
    ) from e

if not hasattr(_oauth, "start_urls_for") or not callable(getattr(_oauth, "start_urls_for")):
    raise ImportError(
        "Ungültige Schnittstelle: cogs.live_match.steam_link_oauth exportiert keine start_urls_for(uid)."
    )

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
    except Exception:
        pass
    return browser_url, None

# --- Eingabe-Validierung ---
# Erlaubt:
#  - reine Ziffern-IDs mit 16–20 Stellen (nicht fix 17)
#  - Vanity (2–32) [A-Za-z0-9_-]
#  - vollständige Profil-Links: /profiles/<id> oder /id/<vanity>
STEAM_KEY_RE = re.compile(
    r"^(?:https?://steamcommunity\.com/(?:profiles|id)/)?([0-9]{16,20}|[A-Za-z0-9_\-]{2,32})/?$",
    re.I,
)


_STEAM_LINK_DM_DESC = dedent(
    """
    **Empfohlen:** Exakter **Voice-Status**, saubere **Event-Orga & Balancing**.


    🤝 **Freundschaft mit dem Bot:** Wenn du dich via Discord oder Steam verknüpfst, senden wir dir automatisch eine Freundschaftsanfrage. Alternativen findest du über den Button **Freundschafts-Optionen** (z. B. Bot-ID 820142646 oder der Schnell-Link).


    **Wichtig:** Steam → Profil → **Spieldetails = Öffentlich** (Gesamtspielzeit nicht „immer privat“).
    """
).strip()


_STEAM_LINK_DETAILED_DESC = dedent(
    """
    • Wozu ist das gut? Wir können deinen **Spiel-Status**
      (z. B. *Lobby/In-Game*, **Anzahl im Match**) als Status für den Sprach Kanel nehmen.
      Dadurch können wir präziser anzeigen wie der Status ist und Events sauberer balancen.


    **Ablauf & Optionen:**
    • **Via Discord verknüpfen** – Schnellster Weg.
    • **SteamID manuell eingeben**: Du trägst **ID64 / Vanity / Profil-Link** selbst ein.
    • **Steam Profil suchen**: Offizieller Steam OpenID-Flow (kein Passwort, wir sehen nur die **SteamID64**).


    • Sobald du dich via Discord oder Steam authentifizierst, schickt dir unser Bot automatisch eine Anfrage.
      Alternativ kannst du diesen manuell adden:
      ⚡ Über den Button **„Schnelle Anfrage senden“** erhältst du einen persönlichen Link.
      🔢 Freundescode: **820142646** oder schick dem Bot eine Freundschaftsanfrage über die ID


    **Wichtig:** In Steam → Profil → **Datenschutzeinstellungen** → **Spieldetails = Öffentlich** sonst funktioniert das nicht.
    """
).strip()


def steam_link_dm_description() -> str:
    return _STEAM_LINK_DM_DESC


def steam_link_detailed_description() -> str:
    return _STEAM_LINK_DETAILED_DESC


def build_steam_intro_embed() -> discord.Embed:
    """Intro/Erklärung für den Schritt – mit Hinweis auf 'SteamID manuell'."""
    em = discord.Embed(
        title="Empfehlung für besseres Erlebnis",
        description=steam_link_detailed_description(),
        colour=discord.Colour.blurple(),
    )
    em.set_footer(text="Kurzbefehle: /link, /link_steam, /addsteam")
    return em


class _ManualSteamModal(discord.ui.Modal, title="SteamID manuell eintragen"):
    """Modal zur manuellen Eingabe & Validierung der Steam-ID/Vanity/Links."""
    def __init__(self, on_submit_cb=None):
        super().__init__(timeout=300)
        self.on_submit_cb = on_submit_cb
        self.input: discord.ui.TextInput = discord.ui.TextInput(
            label="SteamID64 / Vanity / Profil-Link",
            placeholder="z. B. 76561198000000000 oder https://steamcommunity.com/profiles/7656…",
            required=True,
            max_length=200,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = (self.input.value or "").strip()
        sanitized = raw
        try:
            parsed = urlsplit(raw)
        except ValueError:
            parsed = None

        if parsed and parsed.scheme and parsed.netloc:
            path = (parsed.path or "").split(";", 1)[0]
            sanitized = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        else:
            sanitized = raw.split("?", 1)[0].split("#", 1)[0]
        sanitized = sanitized.strip()
        m = STEAM_KEY_RE.match(sanitized)
        if not m:
            await interaction.response.send_message(
                "❌ Das sieht nicht nach einer gültigen SteamID/Profil-URL aus.\n"
                "Akzeptiert: **ID64 (16–20 Ziffern)**, Vanity *(2–32 alphanum/`_`/`-`)*, "
                "`/profiles/<id>` oder `/id/<vanity>`.",
                ephemeral=True,
            )
            return

        steam_key = m.group(1)

        if callable(self.on_submit_cb):
            try:
                await self.on_submit_cb(interaction, steam_key)
            except Exception:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "⚠️ Eingabe erhalten, aber Speichern schlug fehl. Bitte später erneut versuchen.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        "⚠️ Eingabe erhalten, aber Speichern schlug fehl. Bitte später erneut versuchen.",
                        ephemeral=True,
                    )
                return

        content = (
            f"✅ **Gespeichert:** `{steam_key}`\n"
            f"_Wir prüfen die Verbindung in Kürze. Stelle sicher, dass **Spieldetails = Öffentlich** sind._"
        )
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)


class _LinkSheet(discord.ui.View):
    """Ephemere Mini-View mit den tatsächlichen Link-Buttons (mit ?uid=...)."""
    def __init__(self, *, discord_url: str, steam_url: str):
        super().__init__(timeout=120)
        self.add_item(discord.ui.Button(
            label="Via Discord verknüpfen",
            style=discord.ButtonStyle.link,
            url=discord_url,
            emoji="🔗",
        ))
        self.add_item(discord.ui.Button(
            label="Steam Profil suchen",
            style=discord.ButtonStyle.link,
            url=steam_url,
            emoji="🎮",
        ))


class _FriendOptionsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(
            QuickInviteButton(
                style=discord.ButtonStyle.success,
                label="Schnelle Anfrage senden",
                emoji="⚡",
                row=0,
                source="welcome_dm_friend_options",
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
        on_manual_save=None,          # async def (interaction, steam_key) -> None
        timeout: float | None = None, # persistent-fähig
        show_next: bool = True,
    ):
        super().__init__(timeout=timeout)
        self.on_next = on_next
        self.on_manual_save = on_manual_save
        self.show_next = show_next
        self.proceed: bool = False

    # --- Buttons (nur custom_id, keine URLs – dadurch persistent-fähig) ---

    @discord.ui.button(
        label="SteamID manuell",
        style=discord.ButtonStyle.secondary,
        custom_id="steam:manual",
        row=0,
        emoji="📝",
    )
    async def _open_manual(self, interaction: discord.Interaction, _button: discord.ui.Button):
        modal = _ManualSteamModal(on_submit_cb=self.on_manual_save)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Via Discord verknüpfen",
        style=discord.ButtonStyle.success,
        custom_id="steam:discord",
        row=0,
        emoji="🔗",
    )
    async def _start_discord(self, interaction: discord.Interaction, _button: discord.ui.Button):
        uid = interaction.user.id
        try:
            urls = _oauth.start_urls_for(uid)
        except Exception:
            urls = {}
        if not urls.get("discord_start") or not urls.get("steam_openid_start"):
            if interaction.response.is_done():
                await interaction.followup.send("❌ Start-Links nicht konfiguriert. Bitte später erneut versuchen.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Start-Links nicht konfiguriert. Bitte später erneut versuchen.", ephemeral=True)
            return

        # Deep-Link bevorzugen (falls aktiviert) + Browser-Fallback anfügen
        primary, browser_fallback = _prefer_discord_deeplink(urls["discord_start"])
        discord_link = primary or urls["discord_start"]

        sheet = _LinkSheet(discord_url=discord_link, steam_url=urls["steam_openid_start"])

        msg = "🔐 Wähle den Link:"
        if browser_fallback and discord_link.startswith("discord://"):
            msg += f" _(Falls sich nichts öffnet: [Browser-Variante]({browser_fallback}))_"

        if interaction.response.is_done():
            await interaction.followup.send(msg, view=sheet, ephemeral=True)
        else:
            await interaction.response.send_message(msg, view=sheet, ephemeral=True)

    @discord.ui.button(
        label="Steam Profil suchen",
        style=discord.ButtonStyle.primary,
        custom_id="steam:openid",
        row=0,
        emoji="🎮",
    )
    async def _start_openid(self, interaction: discord.Interaction, _button: discord.ui.Button):
        # identisch: wir zeigen dieselbe ephemere Link-Sheet (mit beiden Links)
        await self._start_discord(interaction, _button)

    @discord.ui.button(
        label="Freundschafts-Optionen",
        style=discord.ButtonStyle.secondary,
        custom_id="steam:friendopts",
        row=1,
        emoji="🤝",
    )
    async def _show_friend_options(self, interaction: discord.Interaction, _button: discord.ui.Button):
        view = _FriendOptionsView()
        content = (
            "🤝 **So verbindest du dich mit unserem Steam-Bot:**\n"
            "• Sobald du dich über Discord oder Steam verknüpfst, senden wir dir automatisch eine Freundschaftsanfrage.\n\n"
            "• Alternativ kannst du den Bot selbst hinzufügen:\n"
            "  ⚡ Nutze **„Schnelle Anfrage senden“** für einen persönlichen Link (einmalig, 30 Tage gültig).\n"
            "  🔢 Freundescode: **820142646** (oder teile ihn uns mit, dann adden wir dich).\n\n"
            "Teile Schnell-Links nur mit Leuten, denen du vertraust."
        )
        if interaction.response.is_done():
            await interaction.followup.send(content, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(content, view=view, ephemeral=True)

    @discord.ui.button(
        label="Weiter",
        style=discord.ButtonStyle.primary,
        custom_id="steam:next",
        row=1,
        emoji="⏭️",
    )
    async def _next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.proceed = True
        self.stop()
        if callable(self.on_next):
            try:
                await self.on_next(interaction)
                return
            except Exception:
                pass
        if interaction.response.is_done():
            await interaction.followup.send("Alles klar – weiter geht’s! ✅", ephemeral=True)
        else:
            await interaction.response.send_message("Alles klar – weiter geht’s! ✅", ephemeral=True)


# --- Aliase für ältere Imports ---
SteamLinkView = SteamLinkStepView
SteamLinkNudgeView = SteamLinkStepView
