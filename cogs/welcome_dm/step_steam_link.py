# cogs/welcome_dm/step_steam_link.py
from __future__ import annotations

import re
import discord

__all__ = [
    "SteamLinkStepView",
    "SteamLinkView",          # Alias (Backcompat)
    "SteamLinkNudgeView",     # Alias (für rules_channel)
    "build_steam_intro_embed",
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

# --- Eingabe-Validierung ---
# Erlaubt:
#  - reine Ziffern-IDs mit 16–20 Stellen (nicht fix 17)
#  - Vanity (2–32) [A-Za-z0-9_-]
#  - vollständige Profil-Links: /profiles/<id> oder /id/<vanity>
STEAM_KEY_RE = re.compile(
    r"^(?:https?://steamcommunity\.com/(?:profiles|id)/)?([0-9]{16,20}|[A-Za-z0-9_\-]{2,32})/?$",
    re.I,
)


def build_steam_intro_embed() -> discord.Embed:
    """Intro/Erklärung für den Schritt – mit Hinweis auf 'SteamID manuell'."""
    em = discord.Embed(
        title="Empfehlung für besseres Erlebnis",
        description=(
            "• Wozu ist das gut? Wir können deinen **Voice-Status** (z. B. *Lobby/In-Game*, **Anzahl im Match**) "
            "präziser anzeigen und Events sauberer balancen.\n\n"
            "**Ablauf & Optionen:**\n"
            "• **Via Discord verknüpfen**: Schnellster, sicherer Weg (wir fragen *identify + connections* ab).\n"
            "• **SteamID manuell eingeben**: Du trägst **ID64 / Vanity / Profil-Link** selbst ein.\n"
            "• **Steam Profil suchen**: Offizieller Steam OpenID-Flow (kein Passwort, wir sehen nur die **SteamID64**).\n\n"
            "**Wichtig:** In Steam → Profil → **Datenschutzeinstellungen** → **Spieldetails = Öffentlich** "
            "(und **Gesamtspielzeit** nicht auf „immer privat“)."
        ),
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
        sanitized = raw.split("?", 1)[0].split("#", 1)[0].strip()
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
        # gleiche alten Labels
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


class SteamLinkStepView(discord.ui.View):
    """
    View für den Steam-Verknüpfungsschritt in der Welcome-DM.
    WICHTIG: Diese View enthält KEINE Link-Buttons.
             Die tatsächlichen URLs werden erst beim Klick als ephemere Link-View gesendet.
    Damit kann diese View auch persistent registriert werden.
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
        sheet = _LinkSheet(discord_url=urls["discord_start"], steam_url=urls["steam_openid_start"])
        if interaction.response.is_done():
            await interaction.followup.send("🔐 Wähle den Link:", view=sheet, ephemeral=True)
        else:
            await interaction.response.send_message("🔐 Wähle den Link:", view=sheet, ephemeral=True)

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
        label="Weiter",
        style=discord.ButtonStyle.primary,
        custom_id="steam:next",
        row=1,
        emoji="⏭️",
    )
    async def _next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        # markiere und beende die View sofort
        self.proceed = True
        self.stop()
        # optionaler Callback des Aufrufers (kann selbst antworten)
        if callable(self.on_next):
            try:
                await self.on_next(interaction)
                return
            except Exception:
                # falls der Callback nichts sendet/fehlschlägt, sorgen wir für eine Antwort
                pass
        # sichere Bestätigung, damit kein "Interaction failed" erscheint
        if interaction.response.is_done():
            await interaction.followup.send("Alles klar – weiter geht’s! ✅", ephemeral=True)
        else:
            await interaction.response.send_message("Alles klar – weiter geht’s! ✅", ephemeral=True)


# --- Aliase für ältere Imports ---
SteamLinkView = SteamLinkStepView
SteamLinkNudgeView = SteamLinkStepView
