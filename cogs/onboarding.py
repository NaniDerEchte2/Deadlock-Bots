# -*- coding: utf-8 -*-
"""Statisches Multi-Step Onboarding – kein AI, kein Role-Gate, 7 klare Schritte."""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

GUILD_ID = 1289721245281292288

# Channel-IDs für klickbare Mentions in Embeds (<#ID>)
CH_LFG             = 1376335502919335936  # #spieler-suche
CH_TEMPVOICE       = 1371927143537315890  # #sprach-kanal-verwalten
CH_RULES           = 1315684135175716975  # #regelwerk
CH_FEEDBACK        = 1289721245281292291  # #feedback-kanal
CH_CLIPS           = 1425215762460835931  # #clip-submission
CH_COACHING        = 1357421075188813897  # #ich-brauch-einen-coach
CH_TICKET          = None                 # #ticket-eröffnen (Mention via Text)
CH_BETA            = 1428745737323155679  # #beta-zugang


# ---------------------------------------------------------------------------
# Schritt-Definitionen
# ---------------------------------------------------------------------------

def _c(channel_id: int) -> str:
    """Gibt einen klickbaren Channel-Mention zurück."""
    return f"<#{channel_id}>"


STEPS: list[dict] = [
    # ── 0 ─────────────────────────────────────────────────────────────────
    {
        "title": "Hey, willkommen auf dem Server! 👋",
        "description": (
            "Schön dass du dabei bist – wir sind die **Deutsche Deadlock Community**.\n\n"
            "Egal ob du gerade erst anfängst oder schon ein paar hundert Stunden drinhast, "
            "hier findest du Leute zum Zocken, Tipps, Coaching und alles rund ums Game.\n\n"
            "Kurz durchklicken lohnt sich – **7 Schritte**, dann weißt du alles was du brauchst. Los geht's."
        ),
        "color": 0x5865F2,
        "footer": "Schritt 1 / 7",
    },
    # ── 1 ─────────────────────────────────────────────────────────────────
    {
        "title": "📋 Regeln & Verhalten",
        "description": (
            "Kurz & knapp – kein Roman:\n\n"
            "🚫 **Was gar nicht geht:**\n"
            "- Beleidigungen, Hassrede, Diskriminierung\n"
            "- Spam oder Werbung für andere Server\n"
            "- NSFW-Content irgendwo auf dem Server\n"
            "- Leute in Chill-Lanes wegen ihrem Skill anmachen\n\n"
            "✅ **Was wir erwarten:**\n"
            "- Respekt gegenüber allen – egal Rang, Erfahrung oder Spielstil\n"
            "- Im Voice **kommunizieren**: Lane-Gegner fehlen? Callt es. Ihr macht ne Box? Callt es. "
            "Ihr wechselt die Lane? Callt es. Niemand hat Bock auf 1v2 weil keiner redet.\n"
            "- Bei Problemen: kein Stress machen sondern ein Ticket öffnen -> "
            "#ticket-eroeffnen\n\n"
            "**Faustregel:** Behandel andere so wie du selbst behandelt werden willst."
        ),
        "color": 0xED4245,
        "footer": "Schritt 2 / 7",
    },
    # ── 2 ─────────────────────────────────────────────────────────────────
    {
        "title": "🎙️ Voice Lanes – was ist was?",
        "description": (
            "Es gibt verschiedene Lane-Typen und die unterscheiden sich wirklich:\n\n"

            "🏆 **Ranked / Competitive Lanes**\n"
            "Nur für Leute in deinem Rang-Bereich (±2 Ränge). Max. 6 Spieler pro Lane. "
            "Willst du den Skill-Diff noch enger halten? In "
            f"{_c(CH_TEMPVOICE)} kannst du den Mindestrang für deine Lane anpassen.\n"
            "→ Hier kann der Ton mal direkter sein – aber respektvoll bleibt ihr trotzdem.\n\n"

            "🎮 **Chill / Spaß Lanes**\n"
            "Kein Rang-Limit. Der Rang-Hinweis an der Lane ist nur eine grobe Orientierung – "
            "du kannst trotzdem joinen.\n"
            "→ **WICHTIG:** Hier nervt sich NIEMAND über Skill. Wer wegen schlechtem Gameplay "
            "flamet, kann das in Ranked Lanes machen. In Chill-Lanes ist das ein No-Go.\n\n"

            "🆕 **Neue Spieler Lane**\n"
            "Primär für Leute die noch neu im Game sind. Jeder kann joinen, aber kein Flame, "
            "kein 'warum weißt du das nicht' - neue Spieler lernen noch das Game, "
            "nicht auch noch den Server.\n\n"

            "🥊 **Street Brawl Lanes**\n"
            "Eigene Kategorie speziell für den Street Brawl Modus."
        ),
        "color": 0xFEE75C,
        "footer": "Schritt 3 / 7",
    },
    # ── 3 ─────────────────────────────────────────────────────────────────
    {
        "title": "🚧 Lane öffnen & verwalten",
        "description": (
            f"**Lane öffnen:** Geh in {_c(CH_TEMPVOICE)} – dort ist ein Dropdown-Menü. "
            "Lane-Typ auswählen, fertig. Du bist automatisch der **Owner** der Lane.\n\n"

            "**Als Owner hast du folgende Tools:**\n\n"
            "👢 **Kick** – Jemand ist AFK oder nervt und Reden hilft nicht? Raus damit.\n"
            "🚫 **Ban** – Willst du jemanden dauerhaft aus deiner Lane ausschließen? Ban setzen. "
            "Die Person kann nicht mehr beitreten solange du Owner bist.\n"
            "↩️ **Unban** – Ban wieder aufheben.\n"
            "👥 **Duo / Trio** – Nur zu zweit oder dritt? Aktivieren, dann kommt niemand anderes rein.\n"
            "🔄 **Normale Lane** – Duo/Trio aufheben, alles zurück auf Standard.\n"
            "👁️ **Lurker-Rolle** – Du schaust nur zu und spielst nicht mit? "
            "Lurker-Rolle annehmen = du schaffst einen extra Platz für jemanden der mitspielen will.\n\n"

            "**Owner-Wechsel:** Wenn der Owner die Lane verlässt kannst du die Lane übernehmen "
            "und bist dann der neue Owner."
        ),
        "color": 0x57F287,
        "footer": "Schritt 4 / 7",
    },
    # ── 4 ─────────────────────────────────────────────────────────────────
    {
        "title": "🎮 Mitspieler finden – so geht's richtig",
        "description": (
            "Das machen leider die meisten falsch, deswegen einmal klar erklärt:\n\n"

            "**Schritt 1 – Lanes checken (das Wichtigste!)**\n"
            "Schau im Seiten-Panel unter den Sprachkanälen was gerade offen ist. "
            "Gibt's eine Lane die halbwegs passt? → Einfach joinen und schauen. "
            "90% der Zeit passt es.\n\n"

            "**Schritt 2 – Erst wenn wirklich nix passt:**\n"
            f"Eigene Lane in {_c(CH_TEMPVOICE)} aufmachen und dann in "
            f"{_c(CH_LFG)} schreiben was du suchst. "
            "Der Bot schaut dann automatisch wer von den aktiven Spielern vom Rang her passt "
            "und zeigt dir das an – mit Status (Lobby / Match) und ob noch Platz ist.\n\n"

            "**Bitte nicht:** Direkt in spieler-suche schreiben ohne vorher zu schauen "
            "ob schon was offen ist. Die Lanes sind sichtbar – einfach kurz hinschauen.\n\n"

            "💡 **Tipp:** Wenn du die **LFG Ping Rolle** hast (Discord Onboarding bei Rollen-Auswahl), "
            "wirst du gepingt wenn jemand Mitspieler sucht."
        ),
        "color": 0x5865F2,
        "footer": "Schritt 5 / 7",
    },
    # ── 5 ─────────────────────────────────────────────────────────────────
    {
        "title": "🔗 Account verknüpfen & Rang-System",
        "description": (
            "**Warum verknüpfen?**\n"
            "Dein In-Game Rang wird automatisch hier auf dem Server angezeigt und immer aktuell gehalten – "
            "ganz ohne manuelles Updaten. Außerdem funktioniert der Live-Status in den Voice Lanes "
            "nur mit verknüpften Accounts richtig.\n\n"

            "**So geht's:**\n"
            "Tippe einfach `/account_verknüpfen` irgendwo auf dem Server. "
            "Der Bot schickt dir dann eine **Freundschaftsanfrage** auf Steam → annehmen → fertig. "
            "Alternativ geht auch Steam OAuth.\n"
            "> Mehrere Accounts? Kein Problem – einfach mehrfach `/account_verknüpfen` ausführen.\n\n"

            "**Live-Status in Voice Lanes:**\n"
            "Sobald du im Voice bist siehst du über der Lane automatisch:\n"
            "> `Lane Name · Im Match · 14 Min · (4/6)`\n"
            "Die Zahl zeigt wie viele Leute aus dem Call gerade im Match sind und ob noch Platz "
            "in der Lobby ist. Update alle ~6 Minuten.\n\n"

            "⚠️ **Wichtig:** Funktioniert nur korrekt wenn **alle im Call** ihren Account verknüpft haben – "
            "sonst sind die Angaben unvollständig."
        ),
        "color": 0x00AEEF,
        "footer": "Schritt 6 / 7",
    },
    # ── 6 ─────────────────────────────────────────────────────────────────
    {
        "title": "🛠️ Was der Server sonst noch so hat",
        "description": (
            f"**📺 Clips & YouTube** – {_c(CH_CLIPS)}\n"
            "Deine besten Highlights dort einreichen. Wir bauen daraus YouTube Videos. "
            "Bester Clip der Woche wird von der Community gevotet – "
            "manchmal gibt's was zu gewinnen.\n\n"

            f"**🎓 Coaching** – {_c(CH_COACHING)}\n"
            "Du willst besser werden oder brauchst Hilfe? Dort anfragen, "
            "dann gehts in die **Coaching Lane** im Voice.\n\n"

            "**🧩 Custom Games** – #📍Sammelpunkt\n"
            "Wir machen regelmäßig Custom Games. Treffpunkt ist der **Sammelpunkt** Voice Channel, "
            "Koordination läuft über **#custom-games-chat**. Mit `/customgame` Befehlen kannst du Games erstellen. "
            "Wer die **Custom Games Ping Rolle** hat (Discord Onboarding → Rollen auswählen) "
            "wird gepingt wenn was läuft.\n\n"

            "**📝 Patchnotes** – #patchnotes\n"
            "Alle Patches auf Deutsch. Mit der **Patchnotes Ping Rolle** bekommst du sofort eine Benachrichtigung.\n\n"

            "**🎥 Streamer**\n"
            "Streamst du Deadlock? Mit `/streamer` beantragst du die Streamer-Partner-Rolle – läuft automatisch.\n\n"

            f"**🗝️ Kein Deadlock-Zugang?** – {_c(CH_BETA)}\n"
            "Dort einfach melden, wir helfen weiter.\n\n"

            "**Das war's – viel Spaß auf dem Server! 🎮**"
        ),
        "color": 0x57F287,
        "footer": "Schritt 7 / 7",
    },
]


# Index des Account-Verknüpfen-Schritts (STEPS[5])
_ACCOUNT_STEP_INDEX = 5

# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class AccountLinkStepView(discord.ui.View):
    """Schritt 6: Steam-Account verknüpfen + Weiter ➜ zum letzten Schritt."""

    def __init__(self, cog: "StaticOnboarding", user_id: int):
        super().__init__(timeout=3600)
        self.cog = cog
        self.user_id = user_id
        # OAuth-Modul lazy laden – kann fehlen
        self._oauth = None
        try:
            from cogs.steam import steam_link_oauth as _m  # type: ignore
            if hasattr(_m, "start_urls_for"):
                self._oauth = _m
        except Exception:
            pass

    def _check_user(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(
        label="Steam Account verknüpfen",
        style=discord.ButtonStyle.success,
        emoji="🔗",
        row=0,
    )
    async def link_steam(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._check_user(interaction):
            await interaction.response.send_message(
                "Dieses Onboarding gehört jemand anderem.", ephemeral=True
            )
            return

        if self._oauth is None:
            await interaction.response.send_message(
                "Die Verknüpfung ist gerade nicht verfügbar. "
                "Tippe einfach `/account_verknüpfen` auf dem Server.",
                ephemeral=True,
            )
            return

        try:
            urls = self._oauth.start_urls_for(interaction.user.id)
        except Exception:
            urls = {}

        discord_url = urls.get("discord_start")
        steam_url = urls.get("steam_openid_start")

        if not discord_url or not steam_url:
            await interaction.response.send_message(
                "❌ Link konnte nicht generiert werden. Nutze `/account_verknüpfen` direkt.",
                ephemeral=True,
            )
            return

        sheet = discord.ui.View(timeout=120)
        sheet.add_item(discord.ui.Button(
            label="Via Discord bei Steam anmelden",
            style=discord.ButtonStyle.link,
            url=discord_url,
            emoji="🔗",
        ))
        sheet.add_item(discord.ui.Button(
            label="Direkt bei Steam anmelden",
            style=discord.ButtonStyle.link,
            url=steam_url,
            emoji="🎮",
        ))
        await interaction.response.send_message(
            "🔐 Wähle eine Option – nach dem Login schickt der Bot dir eine Steam-Freundschaftsanfrage:",
            view=sheet,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Weiter ➜",
        style=discord.ButtonStyle.primary,
        row=1,
    )
    async def next_step(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._check_user(interaction):
            await interaction.response.send_message(
                "Dieses Onboarding gehört jemand anderem.", ephemeral=True
            )
            return

        embed = _build_embed(len(STEPS) - 1)  # letzter Schritt
        await interaction.response.send_message(embed=embed, view=DoneView(self.user_id))
        self.stop()


class NextStepView(discord.ui.View):
    """Zeigt einen 'Weiter ➜' Button für alle Schritte außer dem letzten."""

    def __init__(self, cog: "StaticOnboarding", step_index: int, user_id: int):
        super().__init__(timeout=3600)  # 1 Stunde – kein Reboot-Persist nötig
        self.cog = cog
        self.step_index = step_index
        self.user_id = user_id

    @discord.ui.button(label="Weiter ➜", style=discord.ButtonStyle.primary)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Dieses Onboarding gehört jemand anderem.", ephemeral=True
            )
            return

        next_index = self.step_index + 1
        embed = _build_embed(next_index)

        # Schritt 6 (Account verknüpfen) bekommt eigene View mit Steam-Button
        if next_index == _ACCOUNT_STEP_INDEX:
            view = AccountLinkStepView(self.cog, self.user_id)
        elif next_index >= len(STEPS) - 1:
            view = DoneView(self.user_id)
        else:
            view = NextStepView(self.cog, next_index, self.user_id)

        await interaction.response.send_message(embed=embed, view=view)
        self.stop()


class DoneView(discord.ui.View):
    """Letzter Schritt: Abschluss-Button."""

    def __init__(self, user_id: int):
        super().__init__(timeout=3600)
        self.user_id = user_id

    @discord.ui.button(label="Alles klar, viel Spaß! 🎮", style=discord.ButtonStyle.success)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Dieses Onboarding gehört jemand anderem.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Nice, jetzt weißt du alles! Falls doch mal Fragen sind: "
            "einfach ein Ticket aufmachen oder einen Mod fragen. Have fun! 🎮",
            ephemeral=True,
        )
        self.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_embed(step_index: int) -> discord.Embed:
    step = STEPS[step_index]
    embed = discord.Embed(
        title=step["title"],
        description=step["description"],
        color=step["color"],
    )
    embed.set_footer(text=f"Deutsche Deadlock Community · {step['footer']}")
    return embed


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class StaticOnboarding(commands.Cog):
    """Statisches Multi-Step Onboarding – 7 Schritte, kein AI."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        log.info("StaticOnboarding geladen (%d Schritte).", len(STEPS))

    # Öffentliche API – kompatibel mit rules_channel.py
    async def start_in_channel(
        self, channel: discord.abc.Messageable, member: discord.Member
    ) -> bool:
        """Postet Schritt 0 in den Thread/Channel und startet den Flow."""
        try:
            embed = _build_embed(0)
            view = NextStepView(self, step_index=0, user_id=member.id)
            await channel.send(embed=embed, view=view)
            return True
        except Exception:
            log.exception("StaticOnboarding konnte nicht gestartet werden")
            return False


async def setup(bot: commands.Bot):
    await bot.add_cog(StaticOnboarding(bot))
