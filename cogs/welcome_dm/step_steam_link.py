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
        "Erforderliches Modul fehlt: cogs.live_match.steam_link_oauth (exportiert get_public_urls). Abbruch."
    ) from e

if not hasattr(_oauth, "get_public_urls") or not callable(_oauth.get_public_urls):
    raise ImportError(
        "Ungültige Schnittstelle: cogs.live_match.steam_link_oauth exportiert keine get_public_urls()."
    )

# Erwartete Keys der URL-Funktion
_REQUIRED_URL_KEYS = {"discord_start", "steam_openid_start"}

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
            "• **Wozu?** Damit können wir deinen **Voice-Status** (z. B. *Lobby/In-Game*, **Anzahl im Match**) "
            "genauer anzeigen und Events sauberer balancen.\n\n"
            "**Ablauf & Optionen:**\n"
            "• **Via Discord verknüpfen** – schnell & sicher (wir lesen *identify + connections*).\n"
            "• **SteamID manuell eingeben** – du trägst **ID64/Vanity/Profil-Link** selbst ein.\n"
            "• **Steam Profil suchen** – offizieller Steam-OpenID-Flow (kein Passwort; wir sehen nur die **SteamID64**).\n\n"
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
        # Query/Fragment abschneiden, dann prüfen
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

        # Callback (z. B. Persistenz im aufrufenden Cog)
        if callable(self.on_submit_cb):
            try:
                await self.on_submit_cb(interaction, steam_key)
            except Exception:
                await interaction.response.send_message(
                    "⚠️ Eingabe erhalten, aber Speichern schlug fehl. Bitte später erneut versuchen.",
                    ephemeral=True,
                )
                return

        # Default-Bestätigung
        content = (
            f"✅ **Gespeichert:** `{steam_key}`\n"
            f"_Wir prüfen die Verbindung in Kürze. Stelle sicher, dass **Spieldetails = Öffentlich** sind._"
        )
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)


class SteamLinkStepView(discord.ui.View):
    """
    View für den Steam-Verknüpfungsschritt in der Welcome-DM.
    - Buttons/Links kommen ausschließlich aus dem OAuth-Cog (`get_public_urls()`).
    - Keine eigenen ENV-Fallbacks hier.
    """
    def __init__(
        self,
        *,
        on_next=None,                 # async def (interaction) -> None
        on_manual_save=None,          # async def (interaction, steam_key) -> None
        timeout: float | None = 600.0,
        show_next: bool = True,
    ):
        super().__init__(timeout=timeout)
        self.on_next = on_next
        self.on_manual_save = on_manual_save
        self.show_next = show_next

        # URLs strikt aus dem OAuth/Link-Cog beziehen
        urls = _oauth.get_public_urls()
        missing = _REQUIRED_URL_KEYS - set(urls.keys())
        if missing:
            # explizit hart abbrechen – Konfiguration fehlerhaft
            raise ImportError(
                f"Ungültige get_public_urls()-Rückgabe, fehlende Keys: {', '.join(sorted(missing))}"
            )

        discord_start = urls["discord_start"]
        steam_openid_start = urls["steam_openid_start"]

        # Row 1 – große Aktionsbuttons
        self.add_item(discord.ui.Button(
            label="Jetzt verknüpfen (empfohlen)",
            style=discord.ButtonStyle.success,
            url=discord_start,
            emoji="🔗",
        ))
        self.add_item(discord.ui.Button(
            label="SteamID manuell",
            style=discord.ButtonStyle.secondary,
            custom_id="steam:manual",
            emoji="📝",
        ))
        self.add_item(discord.ui.Button(
            label="Steam Profil suchen",   # ehemals „Mit Steam anmelden“
            style=discord.ButtonStyle.primary,
            url=steam_openid_start,
            emoji="🎮",
        ))

        # Row 2 – Navigation
        if self.show_next:
            self.add_item(discord.ui.Button(
                label="Weiter",
                style=discord.ButtonStyle.primary,
                custom_id="steam:next",
                emoji="⏭️",
            ))

    # --- Hidden Handler-Buttons für custom_id-Actions ---
    @discord.ui.button(label="__hidden__", style=discord.ButtonStyle.secondary, custom_id="steam:manual", row=0)
    async def _open_manual(self, interaction: discord.Interaction, _button: discord.ui.Button):
        modal = _ManualSteamModal(on_submit_cb=self.on_manual_save)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="__hidden__", style=discord.ButtonStyle.primary, custom_id="steam:next", row=1)
    async def _next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if callable(self.on_next):
            await self.on_next(interaction)
            return
        await interaction.response.send_message("Alles klar – weiter geht’s! ✅", ephemeral=True)


# --- Aliase für ältere Imports ---
SteamLinkView = SteamLinkStepView
SteamLinkNudgeView = SteamLinkStepView
