# cogs/welcome_dm/step_steam_link.py
# — Steam-Link Schritt (Nudge + Optionen) —
# - „Jetzt verknüpfen“ öffnet eine Options-View (Discord-OAuth, manuell, Steam-OpenID, Schließen)
# - „Weiter“ wird clientseitig aktiv, sobald irgendein Steam-Link in der DB existiert
# - Serverseitiger Guard: Ohne vorhandenen DB-Eintrag lässt sich „Weiter“ NICHT ausführen
# - Views laufen ohne Timeout; Karten werden bei Abschluss entfernt
# - Keine Features des übrigen Systems entfernt; nutzt shared DB & (falls vorhanden) SteamLink-Cog

from __future__ import annotations

import re
import logging
import asyncio
from typing import Optional

import discord
from .base import StepView
from shared import db  # zentrale DB (gleich wie im SteamLink-OAuth-Cog)

log = logging.getLogger("WelcomeSteamStep")


# ---------------------------------------------------------------------------
# DB-Helfer (Schema absichern + Save)
# ---------------------------------------------------------------------------
def _ensure_schema() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS steam_links(
          user_id         INTEGER NOT NULL,
          steam_id        TEXT    NOT NULL,
          name            TEXT,
          verified        INTEGER DEFAULT 0,
          primary_account INTEGER DEFAULT 0,
          created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
          updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (user_id, steam_id)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_user ON steam_links(user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_steam ON steam_links(steam_id)")


def _save_steam_link_row(user_id: int, steam_id: str, name: str = "", verified: int = 0) -> None:
    _ensure_schema()
    db.execute(
        """
        INSERT INTO steam_links(user_id, steam_id, name, verified)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id, steam_id) DO UPDATE SET
          name=excluded.name,
          verified=excluded.verified,
          updated_at=CURRENT_TIMESTAMP
        """,
        (int(user_id), str(steam_id), name or "", int(verified)),
    )


# ---------------------------------------------------------------------------
# Modal: manuelle Eingabe
# ---------------------------------------------------------------------------
class _ManualSteamModal(discord.ui.Modal, title="Steam manuell verknüpfen"):
    # Discord-Limit: label <= 45
    steam_input = discord.ui.TextInput(
        label="SteamID/Vanity/Profil-Link",
        placeholder="z. B. 76561198… · deinVanity · https://steamcommunity.com/profiles/7656…",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )

    def __init__(self, bot: discord.Client, user: discord.abc.User):
        super().__init__()
        self.bot = bot
        self.user = user

    async def _fallback_resolve(self, raw: str) -> Optional[str]:
        """Minimal-Resolver ohne Steam API: akzeptiert 17-stellige ID oder /profiles/<id>-Link."""
        s = (raw or "").strip()
        if not s:
            return None
        if re.fullmatch(r"\d{17}", s):
            return s
        try:
            from urllib.parse import urlparse
            u = urlparse(s)
        except Exception:
            u = None
        if u and u.netloc and "steamcommunity.com" in u.netloc:
            path = (u.path or "").rstrip("/")
            m = re.search(r"/profiles/(\d{17})$", path)
            if m:
                return m.group(1)
        return None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            steam_cog = interaction.client.get_cog("SteamLink")  # cogs/live_match/steam_link_oauth.py
            raw = str(self.steam_input.value).strip()

            steam_id: Optional[str] = None
            persona: Optional[str] = None

            # Wenn das SteamLink-Cog da ist → vollen Resolver nutzen
            if steam_cog:
                try:
                    steam_id = await steam_cog._resolve_steam_input(raw)  # type: ignore[attr-defined]
                except Exception:
                    steam_id = None
                if steam_id:
                    try:
                        persona = await steam_cog._fetch_persona(steam_id)  # type: ignore[attr-defined]
                    except Exception:
                        persona = None

            # Fallback ohne Cog/Key
            if not steam_id:
                steam_id = await self._fallback_resolve(raw)

            if not steam_id:
                await interaction.response.send_message(
                    "❌ Konnte keine gültige SteamID bestimmen.\n"
                    "Nutze die **17-stellige SteamID64** oder einen **/profiles/<id>**-Link.\n"
                    "Für **Vanity** bitte „Mit Discord verbinden“ oder „Mit Steam anmelden“ nutzen.",
                    ephemeral=True,
                )
                return

            _save_steam_link_row(self.user.id, steam_id, persona or "", verified=0)

            await interaction.response.send_message(
                f"✅ Hinzugefügt: `{steam_id}` (manuell). Prüfe **/links**, setze **/setprimary**.",
                ephemeral=True,
            )
            try:
                await self.user.send(f"✅ Verknüpft (manuell): **{steam_id}**")
            except Exception:
                pass

        except Exception:
            log.exception("Manual Steam link failed")
            try:
                await interaction.response.send_message("❌ Unerwarteter Fehler beim manuellen Verknüpfen.", ephemeral=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Prompt-View (Optionen)
# ---------------------------------------------------------------------------
EMBED_TITLE = "Empfehlung für besseres Erlebnis"
EMBED_DESC = (
    "• **Wozu ist das gut?** Wir können deinen Voice-Status (z. B. **Lobby/In-Game**, **Anzahl im Match**) "
    "präziser als Kanalbeschreibung anzeigen und Events sauberer balancen.\n\n"
    "**Ablauf & Optionen:**\n"
    "• **Mit Discord verbinden (grün):** Schnellster Weg. Wir fragen über Discord **identify + connections** ab. "
    "Ist Steam bei deinen Discord-Verknüpfungen hinterlegt → speichern wir automatisch deine **SteamID64**. "
    "Falls nicht, leiten wir dich direkt zu **Steam OpenID** weiter.\n"
    "• **SteamID manuell eingeben (blau):** Du trägst **ID/Vanity/Profil-Link** ein. "
    "Vanity-Auflösung klappt nur, wenn das Steam-Modul aktiv ist; sonst bitte **/profiles/<id>** nutzen.\n"
    "• **Mit Steam anmelden (grau):** Offizielles **Steam OpenID**. Wir erhalten **nur** deine **SteamID64** (keine Passwörter) "
    "und schicken dir eine **DM-Bestätigung**.\n"
    "• **Schließen:** Bricht hier ab. Später kannst du `/link`, `/link_steam` oder `/addsteam` verwenden.\n\n"
    "**Wichtig:** Steam → Profil → **Datenschutzeinstellungen** → **Spieldetails = Öffentlich** "
    "(und **Gesamtspielzeit** nicht auf „immer privat“)."
)
EMBED_FOOTER = "Kurzbefehle: /link · /link_steam · /addsteam"


class _SteamLinkPromptView(discord.ui.View):
    """
    Optionen: (grün) Discord verbinden, (blau) manuell, (grau) Steam anmelden, (grau) schließen
    „Weiter“ wird clientseitig aktiv, sobald irgendein Link vorhanden ist.
    Serverseitiger Guard blockt das Weiterklicken ohne DB-Eintrag zuverlässig.
    """
    def __init__(self, bot: discord.Client, user: discord.abc.User, parent_step: SteamLinkNudgeView,
                 timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.user = user
        self.parent_step = parent_step

        # Reihe 1 – Hauptoptionen
        self.btn_discord = discord.ui.Button(
            label="Mit Discord verbinden", style=discord.ButtonStyle.success,
            emoji="🔗", custom_id="steam_oauth_discord"
        )
        self.btn_manual = discord.ui.Button(
            label="SteamID manuell eingeben", style=discord.ButtonStyle.primary,
            emoji="🔢", custom_id="steam_manual_open"
        )
        self.btn_steam = discord.ui.Button(
            label="Mit Steam anmelden", style=discord.ButtonStyle.secondary,
            emoji="🎮", custom_id="steam_openid"
        )
        self.btn_close = discord.ui.Button(
            label="Schließen", style=discord.ButtonStyle.secondary,
            emoji="❌", custom_id="steam_close"
        )

        self.btn_discord.callback = self._click_discord  # type: ignore[assignment]
        self.btn_manual.callback = self._click_manual    # type: ignore[assignment]
        self.btn_steam.callback = self._click_steam      # type: ignore[assignment]
        self.btn_close.callback = self._click_close      # type: ignore[assignment]

        self.add_item(self.btn_discord)
        self.add_item(self.btn_manual)
        self.add_item(self.btn_steam)
        self.add_item(self.btn_close)

        # Reihe 2 – Weiter (initial deaktiviert)
        self.btn_next = discord.ui.Button(
            label="Weiter", style=discord.ButtonStyle.primary,
            emoji="➡️", custom_id="steam_next", row=1, disabled=True
        )
        self.btn_next.callback = self._click_next  # type: ignore[assignment]
        self.add_item(self.btn_next)

        # Poll-Task
        self._poll_task: Optional[asyncio.Task] = None
        self.message: Optional[discord.Message] = None

    # ---- Helfer ----
    def _steam_cog(self):
        return self.bot.get_cog("SteamLink")

    async def _discord_oauth_url(self) -> Optional[str]:
        cog = self._steam_cog()
        if not cog:
            return None
        try:
            return cog._build_discord_auth_url(self.user.id)  # type: ignore[attr-defined]
        except Exception:
            return None

    async def _steam_openid_url(self) -> Optional[str]:
        cog = self._steam_cog()
        if not cog:
            return None
        try:
            state = cog._mk_state(self.user.id)  # type: ignore[attr-defined]
            return cog._build_steam_login_url(state)  # type: ignore[attr-defined]
        except Exception:
            return None

    def _has_any_link(self) -> bool:
        _ensure_schema()
        row = db.query_one("SELECT 1 FROM steam_links WHERE user_id=? LIMIT 1", (int(self.user.id),))
        return bool(row)

    async def _poll_links(self):
        """Pollt alle 5s die DB und aktiviert „Weiter“, sobald ein Link existiert."""
        try:
            while True:
                await asyncio.sleep(5)
                if not self.message:
                    continue
                enabled = self._has_any_link()
                if self.btn_next.disabled and enabled:
                    self.btn_next.disabled = False
                    try:
                        await self.message.edit(view=self)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Steam link poll task crashed")

    def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        super().stop()

    # ---- Button-Callbacks: Reihe 1 ----
    async def _click_discord(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Diese Aktion ist nicht für dich bestimmt.", ephemeral=True)
            return
        url = await self._discord_oauth_url()
        await interaction.response.send_message(
            content=("🔗 **Discord-Verknüpfung öffnen:**\n" + (url or "_Momentan nicht verfügbar._")),
            ephemeral=True,
        )

    async def _click_manual(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Diese Aktion ist nicht für dich bestimmt.", ephemeral=True)
            return
        await interaction.response.send_modal(_ManualSteamModal(self.bot, self.user))

    async def _click_steam(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Diese Aktion ist nicht für dich bestimmt.", ephemeral=True)
            return
        url = await self._steam_openid_url()
        await interaction.response.send_message(
            content=("🎮 **Steam-Login öffnen:**\n" + (url or "_Momentan nicht verfügbar._")),
            ephemeral=True,
        )

    async def _click_close(self, interaction: discord.Interaction):
        # Step beenden + Karte entfernen
        self.parent_step.force_finish()
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
            await interaction.message.delete()
        except Exception:
            try:
                # Fallback: Buttons sperren
                for it in self.children:
                    if isinstance(it, discord.ui.Button):
                        it.disabled = True
                await interaction.message.edit(view=self)
            except Exception:
                pass

    # ---- Navigation: Weiter (mit Server-Guard) ----
    async def _click_next(self, interaction: discord.Interaction):
        """Weiter nur, wenn in der DB mind. ein Steam-Link für den User existiert."""
        _ensure_schema()
        row = db.query_one("SELECT 1 FROM steam_links WHERE user_id=? LIMIT 1", (int(self.user.id),))
        if not row:
            # Sicherheitsgurt: Button wieder deaktivieren + Hinweis
            try:
                self.btn_next.disabled = True
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "⏳ Noch kein verknüpfter Steam-Account gefunden. "
                        "Bitte eine der Optionen oben nutzen.",
                        ephemeral=True,
                    )
                await interaction.message.edit(view=self)
            except Exception:
                pass
            return

        # OK → Step sauber beenden + Karte entfernen
        self.parent_step.force_finish()
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
            await interaction.message.delete()
        except Exception:
            try:
                for it in self.children:
                    if isinstance(it, discord.ui.Button):
                        it.disabled = True
                await interaction.message.edit(view=self)
            except Exception:
                pass

    # ---- Senden ----
    async def send(self, channel: discord.abc.Messageable) -> discord.Message:
        embed = discord.Embed(title=EMBED_TITLE, description=EMBED_DESC, color=discord.Color.blurple())
        embed.set_footer(text=EMBED_FOOTER)
        msg = await channel.send(embed=embed, view=self)
        self.message = msg
        self._poll_task = asyncio.create_task(self._poll_links())
        return msg


# ---------------------------------------------------------------------------
# Step (Nudge-View im Welcome-Flow)
# ---------------------------------------------------------------------------
class SteamLinkNudgeView(StepView):
    """Frage 5: Steam-Link (Nudge; öffnet bei Klick die Optionen-View)."""

    @discord.ui.button(label="Jetzt verknüpfen (empfohlen)", style=discord.ButtonStyle.success,
                       custom_id="wdm:q5:linknow", emoji="🔗")
    async def link_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Erste Nudge-Nachricht entfernen
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                try:
                    await interaction.message.delete()
                except Exception:
                    pass
                if getattr(self, "bound_message", None):
                    try:
                        await self.bound_message.delete()
                    except Exception:
                        pass
            except Exception:
                pass

            # Optionen-Karte senden (timeout=None)
            channel = interaction.channel or await interaction.user.create_dm()
            view = _SteamLinkPromptView(interaction.client, interaction.user, parent_step=self, timeout=None)
            await view.send(channel)

        except Exception:
            log.exception("Open SteamLinkPromptView failed")
            try:
                await interaction.followup.send(
                    "⚠️ Konnte die Verknüpfungs-Optionen gerade nicht öffnen. "
                    "Nutze alternativ **/link** oder **/link_steam**.",
                    ephemeral=True,
                )
            except Exception:
                pass

    @discord.ui.button(label="Später", style=discord.ButtonStyle.secondary, custom_id="wdm:q5:skip", emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        # „Später“ = Step beenden (erste Karte)
        if not await self._enforce_min_wait(interaction):
            return
        await self._finish(interaction)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="wdm:q5:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Falls jemand die Nudge-„Weiter“-Taste nutzt: wie gehabt erst nach Mindestzeit
        if not await self._enforce_min_wait(interaction):
            return
        await self._finish(interaction)
