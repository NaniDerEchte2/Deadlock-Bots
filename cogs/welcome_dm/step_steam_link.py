# cogs/welcome_dm/step_steam_link.py
# — Steam-Link Schritt (Nudge + Optionen) —
# Optionen-View: ALLES grau (link-Buttons zwangsgrau; Rest secondary).
# „Weiter“ & „Schließen“ liegen in der ZWEITEN Reihe (row=1).
# „Weiter“ wird erst aktiv, wenn in der DB ein Steam-Link existiert (Poll + Guard).
# Keine nackten URLs; Link-Buttons werden beim Senden erzeugt (keine custom_id/url-Kollision).

from __future__ import annotations

import re
import logging
import asyncio
from typing import Optional
from urllib.parse import urlparse  # <- fürs saubere Host-Checking

import discord
from .base import StepView
from service import db  # zentrale DB

log = logging.getLogger("WelcomeSteamStep")


# --------------------- DB ---------------------
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


# --------------------- Modal: manuelle Eingabe ---------------------
class _ManualSteamModal(discord.ui.Modal, title="Steam manuell verknüpfen"):
    steam_input = discord.ui.TextInput(
        label="SteamID/Vanity/Profil-Link",
        placeholder="z. B. 7656119… · deinVanity · https://steamcommunity.com/profiles/7656…",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )

    def __init__(self, bot: discord.Client, user: discord.abc.User):
        super().__init__()
        self.bot = bot
        self.user = user

    async def _fallback_resolve(self, raw: str) -> Optional[str]:
        """
        Härtung: kein Substring-Match mehr.
        Akzeptiere nur http/https-URLs deren hostname == steamcommunity.com
        oder *.steamcommunity.com. Extrahiere dann /profiles/<17-stellige-ID>.
        """
        s = (raw or "").strip()
        if not s:
            return None

        # 1) Reine 17-stellige SteamID64
        if re.fullmatch(r"\d{17}", s):
            return s

        # 2) Sauber geparste URL mit echtem Host-Check
        try:
            u = urlparse(s)
        except Exception as e:
            log.debug("urlparse failed for %r: %r", s, e)
            u = None

        if not u:
            return None

        # nur http/https
        if str(u.scheme).lower() not in ("http", "https"):
            return None

        host = (u.hostname or "").lower().rstrip(".")
        if not host:
            return None

        if not (host == "steamcommunity.com" or host.endswith(".steamcommunity.com")):
            return None

        path = (u.path or "").rstrip("/")
        m = re.fullmatch(r"/profiles/(\d{17})", path)
        if m:
            return m.group(1)

        # Vanity wird hier nicht aufgelöst – das macht der eigentliche Steam-Cog.
        return None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            steam_cog = interaction.client.get_cog("SteamLink")
            raw = str(self.steam_input.value).strip()

            steam_id: Optional[str] = None
            persona: Optional[str] = None

            if steam_cog:
                try:
                    steam_id = await steam_cog._resolve_steam_input(raw)  # type: ignore[attr-defined]
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("Steam resolve via cog failed: %r", e)
                    steam_id = None
                if steam_id:
                    try:
                        persona = await steam_cog._fetch_persona(steam_id)  # type: ignore[attr-defined]
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.debug("Persona fetch failed: %r", e)
                        persona = None

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
            except (discord.Forbidden, discord.HTTPException) as e:
                log.debug("DM notify failed: %r", e)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Manual Steam link failed: %r", e)
            try:
                await interaction.response.send_message("❌ Unerwarteter Fehler beim manuellen Verknüpfen.", ephemeral=True)
            except (discord.NotFound, discord.HTTPException) as e2:
                log.debug("Followup send failed: %r", e2)


# --------------------- Texte ---------------------
EMBED_TITLE = "Empfehlung für besseres Erlebnis"
EMBED_DESC = (
    "• **Wozu ist das gut?** Wir können deinen Voice-Status (z. B. **Lobby/In-Game**, **Anzahl im Match**) "
    "präziser als Kanalbeschreibung anzeigen und Events sauberer balancen.\n\n"
    "**Ablauf & Optionen:**\n"
    "• **Mit Discord verbinden:** Schnellster Weg. Wir fragen über Discord **identify + connections** ab. "
    "Ist Steam bei deinen Discord-Verknüpfungen hinterlegt → speichern wir automatisch deine **SteamID64**; "
    "falls nicht, leiten wir dich direkt zu **Steam OpenID** weiter.\n"
    "• **SteamID manuell eingeben:** Du trägst **ID/Vanity/Profil-Link** ein. Vanity klappt nur, wenn das "
    "Steam-Modul aktiv ist; sonst bitte **/profiles/<id>** nutzen.\n"
    "• **Mit Steam anmelden:** Offizielles **Steam OpenID**. Wir erhalten **nur** deine **SteamID64** (keine Passwörter) "
    "und schicken dir eine **DM-Bestätigung**.\n"
    "• **Schließen:** Bricht ab. Später kannst du `/link`, `/link_steam` oder `/addsteam` verwenden.\n\n"
    "**Wichtig:** Steam → Profil → **Datenschutzeinstellungen** → **Spieldetails = Öffentlich** "
    "(und **Gesamtspielzeit** nicht auf „immer privat“)."
)
EMBED_FOOTER = "Kurzbefehle: /link · /link_steam · /addsteam"


# --------------------- Optionen-View ---------------------
class _SteamLinkPromptView(discord.ui.View):
    """
    Optionen-View (alles grau):
      - Link-Buttons für Discord-OAuth & Steam OpenID (style=link).
      - Manuell/Weiter/Schließen als secondary.
      - „Weiter“ wird via Poll aktiv, sobald in DB irgendein Link existiert.
      - Serverseitiger Guard verhindert Weiter ohne DB-Eintrag.
      - Weiter & Schließen in Reihe 2 (row=1).
    """
    def __init__(self, bot: discord.Client, user: discord.abc.User, parent_step: SteamLinkNudgeView,
                 timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.user = user
        self.parent_step = parent_step

        # Nicht-Link Buttons jetzt anlegen
        self.btn_manual = discord.ui.Button(
            label="SteamID manuell eingeben", style=discord.ButtonStyle.secondary,
            emoji="🔢", custom_id="steam_manual_open", row=0
        )
        self.btn_manual.callback = self._click_manual  # type: ignore[assignment]

        # SCHLIEẞEN → zweite Reihe
        self.btn_close = discord.ui.Button(
            label="Schließen", style=discord.ButtonStyle.secondary,
            emoji="❌", custom_id="steam_close", row=2
        )
        self.btn_close.callback = self._click_close  # type: ignore[assignment]

        # WEITER → zweite Reihe
        self.btn_next = discord.ui.Button(
            label="Weiter", style=discord.ButtonStyle.secondary,
            emoji="➡️", custom_id="steam_next", row=2, disabled=True
        )
        self.btn_next.callback = self._click_next  # type: ignore[assignment]

        # Diese werden beim Senden dynamisch erzeugt:
        self._discord_link: Optional[discord.ui.Button] = None
        self._steam_link: Optional[discord.ui.Button] = None

        self._poll_task: Optional[asyncio.Task] = None
        self.message: Optional[discord.Message] = None

    def _steam_cog(self):
        return self.bot.get_cog("SteamLink")

    def _has_any_link(self) -> bool:
        _ensure_schema()
        row = db.query_one("SELECT 1 FROM steam_links WHERE user_id=? LIMIT 1", (int(self.user.id),))
        return bool(row)

    async def _poll_links(self):
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
                    except (discord.HTTPException, discord.NotFound) as e:
                        log.debug("poll edit failed: %r", e)
        except asyncio.CancelledError:
            # Nicht schlucken – korrekt abbrechen
            raise
        except Exception as e:
            log.exception("Steam link poll task crashed: %r", e)

    def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        super().stop()

    async def _click_manual(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Diese Aktion ist nicht für dich bestimmt.", ephemeral=True)
            return
        await interaction.response.send_modal(_ManualSteamModal(self.bot, self.user))

    async def _click_close(self, interaction: discord.Interaction):
        self.parent_step.force_finish()
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
            try:
                await interaction.message.delete()
            except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
                log.debug("delete message in close failed: %r", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("close interaction outer failed: %r", e)
            try:
                for it in self.children:
                    if isinstance(it, discord.ui.Button):
                        it.disabled = True
                await interaction.message.edit(view=self)
            except (discord.HTTPException, discord.NotFound) as e2:
                log.debug("close fallback edit failed: %r", e2)

    async def _click_next(self, interaction: discord.Interaction):
        _ensure_schema()
        row = db.query_one("SELECT 1 FROM steam_links WHERE user_id=? LIMIT 1", (int(self.user.id),))
        if not row:
            try:
                self.btn_next.disabled = True
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "⏳ Noch kein verknüpfter Steam-Account gefunden. "
                        "Bitte eine der Optionen oben nutzen.",
                        ephemeral=True,
                    )
                try:
                    await interaction.message.edit(view=self)
                except (discord.HTTPException, discord.NotFound) as e2:
                    log.debug("edit after next-guard failed: %r", e2)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("next guard response failed: %r", e)
            return

        self.parent_step.force_finish()
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
            try:
                await interaction.message.delete()
            except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
                log.debug("delete message in next failed: %r", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("next handler outer failed: %r", e)
            try:
                for it in self.children:
                    if isinstance(it, discord.ui.Button):
                        it.disabled = True
                await interaction.message.edit(view=self)
            except (discord.HTTPException, discord.NotFound) as e2:
                log.debug("next fallback edit failed: %r", e2)

    async def send(self, channel: discord.abc.Messageable) -> discord.Message:
        # Frische URLs vom Steam-Cog holen
        discord_url = ""
        steam_url = ""
        cog = self._steam_cog()
        if cog and hasattr(cog, "build_discord_link_for"):
            try:
                discord_url = cog.build_discord_link_for(self.user.id)  # type: ignore
            except Exception as e:
                log.debug("build_discord_link_for failed: %r", e)
                discord_url = ""
        if cog and hasattr(cog, "build_steam_openid_for"):
            try:
                steam_url = cog.build_steam_openid_for(self.user.id)  # type: ignore
            except Exception as e:
                log.debug("build_steam_openid_for failed: %r", e)
                steam_url = ""

        if not discord_url and cog:
            try:
                discord_url = cog._build_discord_auth_url(self.user.id)  # type: ignore[attr-defined]
            except Exception as e:
                log.debug("_build_discord_auth_url failed: %r", e)
        if not steam_url and cog:
            try:
                state = cog._mk_state(self.user.id)                     # type: ignore[attr-defined]
                steam_url = cog._build_steam_login_url(state)           # type: ignore[attr-defined]
            except Exception as e:
                log.debug("_build_steam_login_url failed: %r", e)

        # View befüllen (alles grau)
        self.clear_items()

        # 1) Discord-OAuth (Link-Button)
        if discord_url:
            self._discord_link = discord.ui.Button(
                label="Mit Discord verbinden", style=discord.ButtonStyle.link,
                emoji="🔗", url=discord_url, row=0
            )
        else:
            self._discord_link = discord.ui.Button(
                label="Mit Discord verbinden (nicht verfügbar)", style=discord.ButtonStyle.secondary,
                emoji="🔗", custom_id="steam_discord_disabled", disabled=True, row=0
            )
        self.add_item(self._discord_link)

        # 2) Manuell (secondary)
        self.add_item(self.btn_manual)

        # 3) Steam OpenID (Link-Button)
        if steam_url:
            self._steam_link = discord.ui.Button(
                label="Mit Steam anmelden", style=discord.ButtonStyle.link,
                emoji="🎮", url=steam_url, row=0
            )
        else:
            self._steam_link = discord.ui.Button(
                label="Mit Steam anmelden (nicht verfügbar)", style=discord.ButtonStyle.secondary,
                emoji="🎮", custom_id="steam_openid_disabled", disabled=True, row=0
            )
        self.add_item(self._steam_link)

        # 4) Weiter (row=1)
        self.add_item(self.btn_next)

        # 5) Schließen (row=1)
        self.add_item(self.btn_close)

        embed = discord.Embed(title=EMBED_TITLE, description=EMBED_DESC, color=discord.Color.dark_gray())
        embed.set_footer(text=EMBED_FOOTER)

        msg = await channel.send(embed=embed, view=self)
        self.message = msg
        self._poll_task = asyncio.create_task(self._poll_links())
        return msg


# --------------------- Nudge-View (Schritt im DM-Flow) ---------------------
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
                except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
                    log.debug("nudge delete failed: %r", e)
                if getattr(self, "bound_message", None):
                    try:
                        await self.bound_message.delete()
                    except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
                        log.debug("bound message delete failed: %r", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("nudge cleanup failed: %r", e)

            # Optionen-Karte senden
            channel = interaction.channel or await interaction.user.create_dm()
            view = _SteamLinkPromptView(interaction.client, interaction.user, parent_step=self, timeout=None)
            await view.send(channel)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Open SteamLinkPromptView failed: %r", e)
            try:
                await interaction.followup.send(
                    "⚠️ Konnte die Verknüpfungs-Optionen gerade nicht öffnen. "
                    "Nutze alternativ **/link** oder **/link_steam**.",
                    ephemeral=True,
                )
            except (discord.HTTPException, discord.NotFound) as e2:
                log.debug("followup after failure failed: %r", e2)

    @discord.ui.button(label="Später", style=discord.ButtonStyle.secondary, custom_id="wdm:q5:skip", emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        await self._finish(interaction)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.secondary, custom_id="wdm:q5:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        await self._finish(interaction)
