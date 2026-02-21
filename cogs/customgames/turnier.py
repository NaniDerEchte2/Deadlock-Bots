"""Turnier-Cog – /turnier Slash Command

- Benutzer: Anmelden, Abmelden, Status (benötigt Turnier-Rolle + Steam-Verknüpfung)
- Admin: Zeitraum verwalten, Teams verwalten, Anmeldungen verwalten, Panel posten
- Rang wird direkt aus der Steam-Verknüpfung (steam_links) gelesen
- Kein Zeitraum = Anmeldung gesperrt
- Keine Doppel-Anmeldungen (DB-Constraint)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from cogs.customgames import tournament_store as tstore
from service import db

log = logging.getLogger(__name__)

# Rolle die Nutzer benötigen, um sich anzumelden
TURNIER_ROLE_ID = 1474210107255554331
TEAM_MAX_SIZE = tstore.TEAM_MAX_SIZE


# ─────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────


def _has_turnier_role(member: discord.Member) -> bool:
    return any(r.id == TURNIER_ROLE_ID for r in member.roles)


async def _get_steam_link(user_id: int) -> dict[str, Any] | None:
    """Fetch rank data from steam_links for a verified Discord user."""
    row = await db.query_one_async(
        """
        SELECT steam_id, deadlock_rank, deadlock_rank_name, deadlock_subrank
        FROM steam_links
        WHERE user_id = ? AND verified = 1
        ORDER BY primary_account DESC, deadlock_rank_updated_at DESC
        LIMIT 1
        """,
        (int(user_id),),
    )
    if row:
        return {k: row[k] for k in row.keys()}
    return None


def _rank_display(rank_name: str | None, subrank: int | None) -> str:
    if not rank_name:
        return "Unbekannt"
    sub = int(subrank or 0)
    if sub > 0:
        return f"{rank_name} {sub}"
    return str(rank_name)


def _rank_score(rank_tier: int | None, subrank: int | None) -> int:
    """Balance-Score: Initiate 1 = 7, Eternus 6 = 72, Obscurus = 3."""
    tier = int(rank_tier or 0)
    sub = max(1, min(6, int(subrank or 3)))
    if tier == 0:
        return 3
    return tier * 6 + sub


def _fmt_dt(dt_str: str | None) -> str:
    if not dt_str:
        return "—"
    try:
        dt = datetime.fromisoformat(str(dt_str))
        return dt.strftime("%d.%m.%Y %H:%M Uhr")
    except Exception:
        return str(dt_str)


def _is_period_open(period: dict[str, Any] | None) -> bool:
    if not period or not int(period.get("is_active", 0)):
        return False
    try:
        now = datetime.now()
        start = datetime.fromisoformat(str(period["registration_start"]))
        end = datetime.fromisoformat(str(period["registration_end"]))
        return start <= now <= end
    except Exception:
        return False


def _period_status_str(period: dict[str, Any] | None) -> str:
    if not period:
        return "Kein Zeitraum"
    if not int(period.get("is_active", 0)):
        return "⛔ Geschlossen"
    try:
        now = datetime.now()
        start = datetime.fromisoformat(str(period["registration_start"]))
        end = datetime.fromisoformat(str(period["registration_end"]))
        if now < start:
            return f"⏳ Startet {_fmt_dt(str(period['registration_start']))}"
        if now > end:
            return "⛔ Abgelaufen"
        return "🟢 Offen"
    except Exception:
        return "Unbekannt"


def _build_panel_embed(
    period: dict[str, Any] | None,
    summary: dict[str, Any] | None,
) -> discord.Embed:
    embed = discord.Embed(title="🏆 Deadlock Turnier-Anmeldung", color=discord.Color.gold())
    if period and int(period.get("is_active", 0)):
        embed.add_field(name="📅 Zeitraum", value=str(period.get("name", "—")), inline=False)
        embed.add_field(name="Status", value=_period_status_str(period), inline=True)
        embed.add_field(
            name="🕐 Start", value=_fmt_dt(str(period.get("registration_start"))), inline=True
        )
        embed.add_field(
            name="🕐 Ende", value=_fmt_dt(str(period.get("registration_end"))), inline=True
        )
    else:
        embed.description = "Aktuell ist **kein Anmeldezeitraum** aktiv."
    if summary:
        embed.add_field(
            name="👥 Anmeldungen",
            value=(
                f"Gesamt: **{summary.get('signups_total', 0)}** | "
                f"Solo: **{summary.get('solo_count', 0)}** | "
                f"Team: **{summary.get('team_count', 0)}**"
            ),
            inline=False,
        )
    embed.add_field(
        name="ℹ️ Voraussetzungen",
        value=(
            f"• Du benötigst die <@&{TURNIER_ROLE_ID}> Rolle\n"
            "• Steam-Konto verknüpfen: `/account_verknüpfen`"
        ),
        inline=False,
    )
    return embed


# ─────────────────────────────────────────────
# Modals
# ─────────────────────────────────────────────


class PeriodCreateModal(discord.ui.Modal, title="Anmeldezeitraum erstellen"):
    period_name = discord.ui.TextInput(
        label="Name des Zeitraums",
        placeholder="z. B. Deadlock Cup #1 März 2026",
        min_length=3,
        max_length=64,
    )
    start_dt = discord.ui.TextInput(
        label="Anmeldebeginn (DD.MM.YYYY HH:MM)",
        placeholder="01.03.2026 12:00",
        min_length=12,
        max_length=16,
    )
    end_dt = discord.ui.TextInput(
        label="Anmeldeschluss (DD.MM.YYYY HH:MM)",
        placeholder="28.03.2026 23:59",
        min_length=12,
        max_length=16,
    )
    team_size_input = discord.ui.TextInput(
        label="Max. Teamgröße (Spieler pro Team)",
        placeholder="6",
        min_length=1,
        max_length=2,
        default="6",
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        fmt = "%d.%m.%Y %H:%M"
        try:
            start = datetime.strptime(str(self.start_dt.value).strip(), fmt)
            end = datetime.strptime(str(self.end_dt.value).strip(), fmt)
        except ValueError:
            await interaction.response.send_message(
                "❌ Ungültiges Datumsformat. Bitte nutze **DD.MM.YYYY HH:MM**\nBeispiel: `28.03.2026 23:59`",
                ephemeral=True,
            )
            return
        if end <= start:
            await interaction.response.send_message(
                "❌ Das Enddatum muss nach dem Startdatum liegen.",
                ephemeral=True,
            )
            return
        try:
            tsize = int(str(self.team_size_input.value or "6").strip())
            if not 2 <= tsize <= 20:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Teamgröße muss eine Zahl zwischen 2 und 20 sein.", ephemeral=True
            )
            return
        period = await tstore.create_period_async(
            interaction.guild_id,
            name=str(self.period_name.value).strip(),
            registration_start=start.isoformat(),
            registration_end=end.isoformat(),
            team_size=tsize,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(
            f"✅ Zeitraum **{period['name']}** erstellt.\n"
            f"🕐 Start: {_fmt_dt(str(period['registration_start']))}\n"
            f"🕐 Ende: {_fmt_dt(str(period['registration_end']))}\n"
            f"👥 Max. Teamgröße: **{period['team_size']}**",
            ephemeral=True,
        )


class TeamCreateModal(discord.ui.Modal, title="Team erstellen"):
    team_name = discord.ui.TextInput(
        label="Teamname",
        placeholder="z. B. Team Alpha",
        min_length=tstore.TEAM_NAME_MIN,
        max_length=tstore.TEAM_NAME_MAX,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            team = await tstore.get_or_create_team_async(
                interaction.guild_id,
                str(self.team_name.value).strip(),
                created_by=interaction.user.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        state = "neu erstellt" if team.get("created") else "bereits vorhanden"
        await interaction.response.send_message(
            f"✅ Team **{team['name']}** ({state})  —  ID: `{team['id']}`",
            ephemeral=True,
        )


class TeamSignupCreateModal(discord.ui.Modal, title="Neues Team erstellen"):
    team_name = discord.ui.TextInput(
        label="Teamname",
        placeholder="z. B. Team Alpha",
        min_length=tstore.TEAM_NAME_MIN,
        max_length=tstore.TEAM_NAME_MAX,
    )

    def __init__(
        self,
        guild_id: int,
        user_id: int,
        rank_name: str,
        rank_tier: int,
        rank_sub: int,
        team_max_size: int = TEAM_MAX_SIZE,
    ) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.rank_name = rank_name
        self.rank_tier = rank_tier
        self.rank_sub = rank_sub
        self.team_max_size = team_max_size

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            team = await tstore.get_or_create_team_async(
                self.guild_id,
                str(self.team_name.value).strip(),
                created_by=self.user_id,
            )
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        # Check team size if team already existed
        if not team.get("created"):
            mc = int(team.get("member_count", 0) or 0)
            if mc >= self.team_max_size:
                await interaction.response.send_message(
                    f"❌ Team **{team['name']}** ist bereits voll ({mc}/{self.team_max_size}).",
                    ephemeral=True,
                )
                return

        result = await tstore.upsert_signup_async(
            self.guild_id,
            self.user_id,
            registration_mode="team",
            rank=self.rank_name.lower(),
            rank_subvalue=self.rank_sub,
            team_id=int(team["id"]),
            assigned_by_admin=False,
        )
        embed = discord.Embed(title="✅ Team-Anmeldung erfolgreich", color=discord.Color.green())
        state = "neu erstellt" if team.get("created") else "beigetreten"
        embed.add_field(name="Team", value=f"{team['name']} ({state})", inline=True)
        embed.add_field(
            name="Rang", value=_rank_display(self.rank_name, self.rank_sub), inline=True
        )
        status = str(result.get("status", ""))
        if status == "updated":
            embed.set_footer(text="Deine Anmeldung wurde aktualisiert.")
        await interaction.response.edit_message(embed=embed, view=None)


# ─────────────────────────────────────────────
# Team-Auswahl (Nutzer-Signup)
# ─────────────────────────────────────────────


class TeamPickView(discord.ui.View):
    CREATE_VALUE = "__create__"

    def __init__(
        self,
        user_id: int,
        guild_id: int,
        rank_name: str,
        rank_tier: int,
        rank_sub: int,
        teams: list[dict[str, Any]],
        team_max_size: int = TEAM_MAX_SIZE,
    ) -> None:
        super().__init__(timeout=120.0)
        self.user_id = user_id
        self.guild_id = guild_id
        self.rank_name = rank_name
        self.rank_tier = rank_tier
        self.rank_sub = rank_sub
        self.team_max_size = team_max_size
        self.teams = {int(t["id"]): t for t in teams}

        options: list[discord.SelectOption] = []
        for t in teams[:24]:
            mc = int(t.get("member_count", 0) or 0)
            full_tag = " ✗ voll" if mc >= team_max_size else f" ({mc}/{team_max_size})"
            options.append(
                discord.SelectOption(
                    label=str(t.get("name", f"Team {t['id']}"))[:100],
                    value=str(t["id"]),
                    description=f"Mitglieder: {mc}/{TEAM_MAX_SIZE}{full_tag}",
                )
            )
        options.append(
            discord.SelectOption(
                label="Neues Team erstellen",
                value=self.CREATE_VALUE,
                emoji="➕",
                description="Erstelle ein neues Team für dich",
            )
        )
        select = discord.ui.Select(placeholder="Team auswählen…", options=options)
        select.callback = self._select_cb
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Dieses Menü gehört dir nicht.", ephemeral=True)
            return False
        return True

    async def _select_cb(self, interaction: discord.Interaction) -> None:
        val = interaction.data["values"][0]
        if val == self.CREATE_VALUE:
            await interaction.response.send_modal(
                TeamSignupCreateModal(
                    self.guild_id, self.user_id, self.rank_name, self.rank_tier, self.rank_sub
                )
            )
            return

        team_id = int(val)
        team_data = self.teams.get(team_id)
        if team_data and int(team_data.get("member_count", 0) or 0) >= self.team_max_size:
            await interaction.response.send_message(
                f"❌ Dieses Team ist bereits voll ({self.team_max_size}/{self.team_max_size}).",
                ephemeral=True,
            )
            return

        result = await tstore.upsert_signup_async(
            self.guild_id,
            self.user_id,
            registration_mode="team",
            rank=self.rank_name.lower(),
            rank_subvalue=self.rank_sub,
            team_id=team_id,
            assigned_by_admin=False,
        )
        team_name = (
            str(team_data.get("name", f"Team {team_id}")) if team_data else f"Team {team_id}"
        )
        embed = discord.Embed(title="✅ Team-Anmeldung erfolgreich", color=discord.Color.green())
        embed.add_field(name="Team", value=team_name, inline=True)
        embed.add_field(
            name="Rang", value=_rank_display(self.rank_name, self.rank_sub), inline=True
        )
        if str(result.get("status")) == "updated":
            embed.set_footer(text="Deine Anmeldung wurde aktualisiert.")
        await interaction.response.edit_message(embed=embed, view=None)


class SignupModeView(discord.ui.View):
    """Anzeige nach Bestätigung des Rangs: Solo oder Team."""

    def __init__(
        self,
        user_id: int,
        guild_id: int,
        rank_name: str,
        rank_tier: int,
        rank_sub: int,
        team_max_size: int = TEAM_MAX_SIZE,
    ) -> None:
        super().__init__(timeout=120.0)
        self.user_id = user_id
        self.guild_id = guild_id
        self.rank_name = rank_name
        self.rank_tier = rank_tier
        self.rank_sub = rank_sub
        self.team_max_size = team_max_size

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Dieses Menü gehört dir nicht.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Solo anmelden", style=discord.ButtonStyle.primary, emoji="🎯")
    async def solo_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        result = await tstore.upsert_signup_async(
            self.guild_id,
            self.user_id,
            registration_mode="solo",
            rank=self.rank_name.lower(),
            rank_subvalue=self.rank_sub,
            team_id=None,
            assigned_by_admin=False,
        )
        status_map = {"inserted": "eingetragen", "updated": "aktualisiert"}
        status_txt = status_map.get(str(result.get("status")), "unverändert")
        embed = discord.Embed(title="✅ Solo-Anmeldung erfolgreich", color=discord.Color.green())
        embed.add_field(name="Status", value=status_txt.capitalize(), inline=True)
        embed.add_field(name="Modus", value="Solo", inline=True)
        embed.add_field(
            name="Rang", value=_rank_display(self.rank_name, self.rank_sub), inline=True
        )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Mit Team anmelden", style=discord.ButtonStyle.secondary, emoji="🛡️")
    async def team_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        teams = await tstore.list_teams_async(self.guild_id)
        if not teams:
            await interaction.response.send_modal(
                TeamSignupCreateModal(
                    self.guild_id,
                    self.user_id,
                    self.rank_name,
                    self.rank_tier,
                    self.rank_sub,
                    self.team_max_size,
                )
            )
            return
        embed = discord.Embed(
            title="🛡️ Team auswählen",
            description="Wähle dein Team oder erstelle ein neues.",
            color=discord.Color.blue(),
        )
        view = TeamPickView(
            self.user_id,
            self.guild_id,
            self.rank_name,
            self.rank_tier,
            self.rank_sub,
            teams,
            self.team_max_size,
        )
        await interaction.response.edit_message(embed=embed, view=view)


# ─────────────────────────────────────────────
# Persistentes Panel (in Channel postbar)
# ─────────────────────────────────────────────


class TurnierPanelView(discord.ui.View):
    """Persistentes Panel mit Anmeldung / Abmeldung / Status Buttons."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Jetzt anmelden",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="turnier_panel_anmelden",
    )
    async def signup_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog: TurnierCog | None = interaction.client.get_cog("TurnierCog")  # type: ignore[assignment]
        if not cog:
            await interaction.response.send_message(
                "❌ Turnier-System nicht verfügbar.", ephemeral=True
            )
            return
        await cog.handle_signup(interaction)

    @discord.ui.button(
        label="Abmelden",
        style=discord.ButtonStyle.danger,
        emoji="🚪",
        custom_id="turnier_panel_abmelden",
    )
    async def withdraw_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog: TurnierCog | None = interaction.client.get_cog("TurnierCog")  # type: ignore[assignment]
        if not cog:
            await interaction.response.send_message(
                "❌ Turnier-System nicht verfügbar.", ephemeral=True
            )
            return
        await cog.handle_withdraw(interaction)

    @discord.ui.button(
        label="Mein Status",
        style=discord.ButtonStyle.secondary,
        emoji="📊",
        custom_id="turnier_panel_status",
    )
    async def status_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog: TurnierCog | None = interaction.client.get_cog("TurnierCog")  # type: ignore[assignment]
        if not cog:
            await interaction.response.send_message(
                "❌ Turnier-System nicht verfügbar.", ephemeral=True
            )
            return
        await cog.handle_status(interaction)


# ─────────────────────────────────────────────
# Signup-Verwaltung (Admin)
# ─────────────────────────────────────────────


class SignupManageView(discord.ui.View):
    PAGE_SIZE = 10

    def __init__(
        self,
        guild_id: int,
        user_id: int,
        signups: list[dict[str, Any]],
        guild: discord.Guild | None,
    ) -> None:
        super().__init__(timeout=300.0)
        self.guild_id = guild_id
        self.user_id = user_id
        self.signups = list(signups)
        self.guild = guild
        self.page = 0
        self._rebuild_select()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Dieses Dashboard gehört dir nicht.", ephemeral=True
            )
            return False
        return True

    def _page_signups(self) -> list[dict[str, Any]]:
        start = self.page * self.PAGE_SIZE
        return self.signups[start : start + self.PAGE_SIZE]

    def _rebuild_select(self) -> None:
        for item in list(self.children):
            if isinstance(item, discord.ui.Select):
                self.remove_item(item)
        page_sups = self._page_signups()
        if not page_sups:
            return
        options: list[discord.SelectOption] = []
        for sup in page_sups:
            uid = int(sup.get("user_id", 0))
            member = self.guild.get_member(uid) if self.guild else None
            display = member.display_name if member else str(uid)
            rank_str = tstore.rank_label(str(sup.get("rank", "initiate")))
            sub = int(sup.get("rank_subvalue") or 0)
            if sub > 0:
                rank_str += f" {sub}"
            team = str(sup.get("team_name") or "Solo")
            options.append(
                discord.SelectOption(
                    label=display[:100],
                    value=str(uid),
                    description=f"{rank_str} | {team}"[:100],
                )
            )
        select = discord.ui.Select(
            placeholder="Spieler auswählen zum Entfernen…",
            options=options,
            row=0,
        )
        select.callback = self._select_cb
        self.add_item(select)

    async def _select_cb(self, interaction: discord.Interaction) -> None:
        uid = int(interaction.data["values"][0])
        removed = await tstore.remove_signup_async(self.guild_id, uid)
        member = self.guild.get_member(uid) if self.guild else None
        display = member.mention if member else str(uid)
        if removed:
            self.signups = [s for s in self.signups if int(s.get("user_id", 0)) != uid]
            self._rebuild_select()
            embed = self.build_embed()
            await interaction.response.edit_message(
                content=f"✅ {display} aus dem Turnier entfernt.",
                embed=embed,
                view=self,
            )
        else:
            await interaction.response.send_message("❌ Spieler nicht gefunden.", ephemeral=True)

    def build_embed(self) -> discord.Embed:
        total = len(self.signups)
        max_page = max(0, (total - 1) // self.PAGE_SIZE)
        embed = discord.Embed(
            title=f"📋 Anmeldungen ({total})",
            color=discord.Color.teal(),
        )
        page_sups = self._page_signups()
        lines: list[str] = []
        start_idx = self.page * self.PAGE_SIZE
        for i, sup in enumerate(page_sups, start_idx + 1):
            uid = int(sup.get("user_id", 0))
            member = self.guild.get_member(uid) if self.guild else None
            display = member.display_name if member else str(uid)
            rank_str = tstore.rank_label(str(sup.get("rank", "initiate")))
            sub = int(sup.get("rank_subvalue") or 0)
            if sub > 0:
                rank_str += f" {sub}"
            mode = "Team" if str(sup.get("registration_mode")) == "team" else "Solo"
            team = str(sup.get("team_name") or "—")
            lines.append(f"{i}. **{display}** | {rank_str} | {mode} | {team}")
        embed.description = "\n".join(lines) if lines else "Keine Anmeldungen."
        embed.set_footer(text=f"Seite {self.page + 1}/{max_page + 1} • Dropdown: Spieler entfernen")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.page > 0:
            self.page -= 1
            self._rebuild_select()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        max_page = max(0, (len(self.signups) - 1) // self.PAGE_SIZE)
        if self.page < max_page:
            self.page += 1
            self._rebuild_select()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


# ─────────────────────────────────────────────
# Team-Verwaltung (Admin)
# ─────────────────────────────────────────────


class TeamDeleteSelectView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        user_id: int,
        options: list[discord.SelectOption],
    ) -> None:
        super().__init__(timeout=60.0)
        self.guild_id = guild_id
        self.user_id = user_id
        select = discord.ui.Select(placeholder="Team zum Löschen auswählen…", options=options)
        select.callback = self._cb
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _cb(self, interaction: discord.Interaction) -> None:
        team_id = int(interaction.data["values"][0])
        deleted = await tstore.delete_team_async(self.guild_id, team_id)
        msg = f"✅ Team ID `{team_id}` wurde gelöscht." if deleted else "❌ Team nicht gefunden."
        await interaction.response.edit_message(content=msg, view=None)


class TeamAdminView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, teams: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300.0)
        self.guild_id = guild_id
        self.user_id = user_id
        self.teams = teams

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Dieses Dashboard gehört dir nicht.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Neues Team erstellen", style=discord.ButtonStyle.primary, emoji="➕")
    async def create_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(TeamCreateModal())

    @discord.ui.button(label="Team löschen", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.teams:
            await interaction.response.send_message("Keine Teams vorhanden.", ephemeral=True)
            return
        options = [
            discord.SelectOption(
                label=str(t.get("name", f"Team {t['id']}"))[:100],
                value=str(t["id"]),
                description=f"ID: {t['id']} | Mitglieder: {t.get('member_count', 0)}",
            )
            for t in self.teams[:25]
        ]
        view = TeamDeleteSelectView(self.guild_id, self.user_id, options)
        await interaction.response.send_message(
            "Welches Team soll gelöscht werden?", view=view, ephemeral=True
        )


# ─────────────────────────────────────────────
# Admin Dashboard
# ─────────────────────────────────────────────


class ConfirmClearView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int) -> None:
        super().__init__(timeout=30.0)
        self.guild_id = guild_id
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Ja, alle löschen", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        count = await tstore.clear_all_signups_async(self.guild_id)
        await interaction.response.edit_message(
            content=f"✅ {count} Anmeldungen gelöscht.", view=None
        )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Abgebrochen.", view=None)


class AdminDashboardView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int) -> None:
        super().__init__(timeout=300.0)
        self.guild_id = guild_id
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Dieses Dashboard gehört dir nicht.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Zeitraum erstellen",
        style=discord.ButtonStyle.primary,
        emoji="📅",
        row=0,
    )
    async def create_period_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(PeriodCreateModal())

    @discord.ui.button(
        label="Zeitraum beenden",
        style=discord.ButtonStyle.danger,
        emoji="🔴",
        row=0,
    )
    async def close_period_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        period = await tstore.get_active_period_async(self.guild_id)
        if not period:
            await interaction.response.send_message("ℹ️ Kein aktiver Zeitraum.", ephemeral=True)
            return
        await tstore.close_period_async(self.guild_id, int(period["id"]))
        await interaction.response.send_message(
            f"✅ Zeitraum **{period['name']}** wurde beendet.", ephemeral=True
        )

    @discord.ui.button(
        label="Anmeldungen",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
        row=0,
    )
    async def signups_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        signups = await tstore.list_signups_async(self.guild_id)
        if not signups:
            await interaction.followup.send("📭 Keine Anmeldungen.", ephemeral=True)
            return
        view = SignupManageView(self.guild_id, self.user_id, signups, interaction.guild)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="Teams verwalten",
        style=discord.ButtonStyle.secondary,
        emoji="🛡️",
        row=1,
    )
    async def teams_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        teams = await tstore.list_teams_async(self.guild_id)
        embed = discord.Embed(title=f"🛡️ Teams ({len(teams)})", color=discord.Color.blue())
        if teams:
            lines = []
            for t in teams:
                mc = int(t.get("member_count", 0) or 0)
                lines.append(f"• **{t['name']}** — {mc}/{TEAM_MAX_SIZE} Spieler  (ID: `{t['id']}`)")
            embed.description = "\n".join(lines[:20])
            if len(teams) > 20:
                embed.set_footer(text=f"… und {len(teams) - 20} weitere Teams")
        else:
            embed.description = "Noch keine Teams erstellt."
        view = TeamAdminView(self.guild_id, self.user_id, teams)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="Panel posten",
        style=discord.ButtonStyle.success,
        emoji="📢",
        row=1,
    )
    async def post_panel_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        period = await tstore.get_active_period_async(self.guild_id)
        summary = await tstore.summary_async(self.guild_id)
        embed = _build_panel_embed(period, summary)
        panel_view = TurnierPanelView()
        await interaction.channel.send(embed=embed, view=panel_view)
        await interaction.response.send_message(
            "✅ Panel in diesem Channel gepostet.", ephemeral=True
        )

    @discord.ui.button(
        label="Alle Anmeldungen löschen",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        row=1,
    )
    async def clear_all_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = ConfirmClearView(self.guild_id, self.user_id)
        await interaction.response.send_message(
            "⚠️ Wirklich **alle Anmeldungen** löschen? (Teams bleiben erhalten)",
            view=view,
            ephemeral=True,
        )


# ─────────────────────────────────────────────
# Nutzer-Ansicht
# ─────────────────────────────────────────────


class TurnierUserView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
        guild_id: int,
        cog: TurnierCog,
        is_admin: bool,
        period_open: bool,
        is_signed_up: bool,
    ) -> None:
        super().__init__(timeout=120.0)
        self.user_id = user_id
        self.guild_id = guild_id
        self.cog = cog

        anmelden = discord.ui.Button(
            label="Anmelden",
            style=discord.ButtonStyle.success,
            emoji="✅",
            disabled=not period_open or is_signed_up,
            row=0,
        )
        anmelden.callback = self._anmelden_cb
        self.add_item(anmelden)

        abmelden = discord.ui.Button(
            label="Abmelden",
            style=discord.ButtonStyle.danger,
            emoji="🚪",
            disabled=not is_signed_up,
            row=0,
        )
        abmelden.callback = self._abmelden_cb
        self.add_item(abmelden)

        if is_admin:
            admin_btn = discord.ui.Button(
                label="Admin Dashboard",
                style=discord.ButtonStyle.primary,
                emoji="🛠️",
                row=1,
            )
            admin_btn.callback = self._admin_cb
            self.add_item(admin_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Dieses Menü gehört dir nicht.", ephemeral=True)
            return False
        return True

    async def _anmelden_cb(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_signup(interaction)

    async def _abmelden_cb(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_withdraw(interaction)

    async def _admin_cb(self, interaction: discord.Interaction) -> None:
        period = await tstore.get_active_period_async(self.guild_id)
        summary = await tstore.summary_async(self.guild_id)
        embed = discord.Embed(title="🛠️ Admin Dashboard", color=discord.Color.red())
        if period:
            embed.add_field(
                name="📅 Aktueller Zeitraum", value=str(period.get("name", "—")), inline=False
            )
            embed.add_field(name="Status", value=_period_status_str(period), inline=True)
            embed.add_field(
                name="Ende",
                value=_fmt_dt(str(period.get("registration_end"))),
                inline=True,
            )
        else:
            embed.description = "Kein aktiver Zeitraum. Erstelle einen über **Zeitraum erstellen**."
        embed.add_field(
            name="👥 Anmeldungen",
            value=(
                f"**{summary.get('signups_total', 0)}** gesamt  |  "
                f"Solo: {summary.get('solo_count', 0)}  |  "
                f"Team: {summary.get('team_count', 0)}  |  "
                f"Teams: {summary.get('teams_count', 0)}"
            ),
            inline=False,
        )
        view = AdminDashboardView(self.guild_id, self.user_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ─────────────────────────────────────────────
# Haupt-Cog
# ─────────────────────────────────────────────


class TurnierCog(commands.Cog, name="TurnierCog"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._balance_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        await tstore.ensure_schema_async()
        if not getattr(self.bot, "_turnier_panel_registered", False):
            self.bot.add_view(TurnierPanelView())
            self.bot._turnier_panel_registered = True
        self._balance_task = asyncio.create_task(self._auto_balance_loop())
        log.info("TurnierCog bereit (persistente Panel-Buttons registriert)")

    async def cog_unload(self) -> None:
        if self._balance_task and not self._balance_task.done():
            self._balance_task.cancel()
            try:
                await self._balance_task
            except asyncio.CancelledError:
                pass

    # ── Auto-balance background task ──────────────────────────────────────────

    async def _auto_balance_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            for guild in self.bot.guilds:
                try:
                    await self._run_auto_balance(guild.id)
                except Exception:
                    log.exception("Auto-Balance fehlgeschlagen (guild_id=%s)", guild.id)

    async def _run_auto_balance(self, guild_id: int) -> None:
        period = await tstore.get_active_period_async(guild_id)
        if not period:
            return

        team_size = int(period.get("team_size") or TEAM_MAX_SIZE)

        teams = await tstore.list_teams_async(guild_id)
        signups = await tstore.list_signups_async(guild_id)

        if not signups:
            return

        # Identify full teams (never touch these)
        full_team_ids: set = set()
        team_member_counts: dict = {}
        for t in teams:
            tid = int(t["id"])
            mc = int(t.get("member_count") or 0)
            team_member_counts[tid] = mc
            if mc >= team_size:
                full_team_ids.add(tid)

        # Build pool: unassigned + in non-full teams
        pool = []
        for s in signups:
            tid = s.get("team_id")
            if tid is None:
                pool.append(s)
            elif int(tid) not in full_team_ids:
                pool.append(s)

        if not pool:
            return

        # Sort pool by rank score descending
        pool.sort(
            key=lambda s: _rank_score(s.get("rank_value"), s.get("rank_subvalue")),
            reverse=True,
        )

        # Fill partial (non-full) teams first
        non_full_teams = [t for t in teams if int(t["id"]) not in full_team_ids]
        for team in non_full_teams:
            tid = int(team["id"])
            current_count = team_member_counts.get(tid, 0)
            slots_available = team_size - current_count
            if slots_available <= 0:
                continue
            # Find players in pool not already in this team
            to_add = [s for s in pool if s.get("team_id") is None or int(s.get("team_id")) != tid][
                :slots_available
            ]
            for s in to_add:
                try:
                    await tstore.assign_signup_team_async(guild_id, int(s["user_id"]), team_id=tid)
                    pool.remove(s)
                    team_member_counts[tid] = team_member_counts.get(tid, 0) + 1
                except Exception:
                    log.exception(
                        "assign_signup_team_async fehlgeschlagen (user=%s, team=%s)",
                        s.get("user_id"),
                        tid,
                    )

        # Re-filter pool: only truly unassigned now
        pool = [s for s in pool if s.get("team_id") is None]
        if len(pool) < team_size:
            return

        # Snake draft into new teams
        # Find next available auto-name ("Team A", "Team B", ...)
        existing_names = {str(t.get("name", "")).casefold() for t in teams}
        num_new_teams = len(pool) // team_size
        auto_names: list[str] = []
        letter_idx = 0
        while len(auto_names) < num_new_teams:
            name = f"Team {chr(65 + letter_idx)}"
            if name.casefold() not in existing_names:
                auto_names.append(name)
            letter_idx += 1
            if letter_idx > 25:
                # Fallback to numbered names
                n = letter_idx - 25
                name = f"Team {n}"
                if name.casefold() not in existing_names:
                    auto_names.append(name)
                letter_idx += 1

        new_team_ids: list[int] = []
        for name in auto_names:
            try:
                team_data = await tstore.get_or_create_team_async(guild_id, name)
                new_team_ids.append(int(team_data["id"]))
            except Exception:
                log.exception("get_or_create_team_async fehlgeschlagen (name=%s)", name)

        if not new_team_ids:
            return

        # Snake draft assignment: indices 0,S-1,S,2S-1,2S,...
        n_teams = len(new_team_ids)
        for i, player in enumerate(pool[: n_teams * team_size]):
            # Snake draft: determine team index
            cycle = i // n_teams
            pos_in_cycle = i % n_teams
            if cycle % 2 == 0:
                team_idx = pos_in_cycle
            else:
                team_idx = n_teams - 1 - pos_in_cycle
            if team_idx >= len(new_team_ids):
                continue
            tid = new_team_ids[team_idx]
            try:
                await tstore.assign_signup_team_async(guild_id, int(player["user_id"]), team_id=tid)
            except Exception:
                log.exception(
                    "Snake-draft assign fehlgeschlagen (user=%s, team=%s)",
                    player.get("user_id"),
                    tid,
                )

        log.info(
            "Auto-Balance abgeschlossen (guild=%s): %d neue Teams, %d Spieler zugewiesen",
            guild_id,
            len(new_team_ids),
            min(len(pool), n_teams * team_size),
        )

    def _is_admin(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        return perms.administrator or perms.manage_guild

    # ── Shared handlers (panel buttons + slash command) ──────────────

    async def handle_signup(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ Nur auf einem Server verfügbar.", ephemeral=True
            )
            return

        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        if not _has_turnier_role(member):
            await interaction.response.send_message(
                f"❌ Du benötigst die <@&{TURNIER_ROLE_ID}> Rolle, um dich anzumelden.",
                ephemeral=True,
            )
            return

        period = await tstore.get_active_period_async(interaction.guild_id)
        if not _is_period_open(period):
            await interaction.response.send_message(
                "❌ Die Anmeldung ist aktuell **nicht geöffnet**.", ephemeral=True
            )
            return

        link = await _get_steam_link(interaction.user.id)
        if not link:
            await interaction.response.send_message(
                "❌ Dein Steam-Konto ist noch nicht mit Discord verknüpft.\n"
                "Nutze `/account_verknüpfen` auf dem Server, um dein Konto zu verbinden.",
                ephemeral=True,
            )
            return

        rank_tier = int(link.get("deadlock_rank") or 0)
        rank_sub = int(link.get("deadlock_subrank") or 0)
        rank_name = str(link.get("deadlock_rank_name") or "Obscurus")
        period_team_size = int(period.get("team_size") or TEAM_MAX_SIZE)

        embed = discord.Embed(
            title="🏆 Turnier-Anmeldung",
            description=(
                f"Dein aktueller Rang: **{_rank_display(rank_name, rank_sub)}**\n"
                f"Max. Teamgröße: **{period_team_size}** Spieler\n\n"
                "Möchtest du dich **solo** oder **mit einem Team** anmelden?"
            ),
            color=discord.Color.gold(),
        )
        view = SignupModeView(
            interaction.user.id,
            interaction.guild_id,
            rank_name,
            rank_tier,
            rank_sub,
            period_team_size,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def handle_withdraw(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ Nur auf einem Server verfügbar.", ephemeral=True
            )
            return
        removed = await tstore.remove_signup_async(interaction.guild_id, interaction.user.id)
        if removed:
            await interaction.response.send_message(
                "✅ Du wurdest aus dem Turnier abgemeldet.", ephemeral=True
            )
        else:
            await interaction.response.send_message("ℹ️ Du warst nicht angemeldet.", ephemeral=True)

    async def handle_status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ Nur auf einem Server verfügbar.", ephemeral=True
            )
            return
        signup = await tstore.get_signup_async(interaction.guild_id, interaction.user.id)
        if not signup:
            await interaction.response.send_message(
                "ℹ️ Du bist aktuell **nicht** für das Turnier angemeldet.", ephemeral=True
            )
            return
        rank_key = str(signup.get("rank", "initiate"))
        rank_sub = int(signup.get("rank_subvalue") or 0)
        rank_name = tstore.rank_label(rank_key)
        mode = "Team" if str(signup.get("registration_mode")) == "team" else "Solo"
        team = str(signup.get("team_name") or "—")
        embed = discord.Embed(title="📊 Mein Turnierstatus", color=discord.Color.blue())
        embed.add_field(
            name="Rang",
            value=_rank_display(rank_name, rank_sub if rank_sub > 0 else None),
            inline=True,
        )
        embed.add_field(name="Modus", value=mode, inline=True)
        embed.add_field(name="Team", value=team, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Slash Command ────────────────────────────────────────────────

    @app_commands.command(
        name="turnier",
        description="Turnier-Dashboard: Anmelden, Status und Admin-Verwaltung",
    )
    async def turnier_cmd(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ Nur auf einem Server verfügbar.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        is_admin = self._is_admin(member)

        period = await tstore.get_active_period_async(interaction.guild_id)
        summary = await tstore.summary_async(interaction.guild_id)
        signup = await tstore.get_signup_async(interaction.guild_id, interaction.user.id)
        period_open = _is_period_open(period)

        embed = discord.Embed(title="🏆 Deadlock Turnier", color=discord.Color.gold())

        # Period info
        if period and int(period.get("is_active", 0)):
            embed.add_field(name="📅 Zeitraum", value=str(period.get("name", "—")), inline=False)
            embed.add_field(name="Status", value=_period_status_str(period), inline=True)
            embed.add_field(
                name="🕐 Ende",
                value=_fmt_dt(str(period.get("registration_end"))),
                inline=True,
            )
            embed.add_field(
                name="👥 Anmeldungen",
                value=(
                    f"**{summary.get('signups_total', 0)}** gesamt  |  "
                    f"Solo: {summary.get('solo_count', 0)}  |  "
                    f"Team: {summary.get('team_count', 0)}"
                ),
                inline=False,
            )
        else:
            desc = "Kein aktiver Anmeldezeitraum."
            if is_admin:
                desc += "\nNutze das **Admin Dashboard** um einen Zeitraum zu erstellen."
            embed.description = desc

        # User status
        if signup:
            rank_key = str(signup.get("rank", "initiate"))
            rank_sub = int(signup.get("rank_subvalue") or 0)
            rank_name = tstore.rank_label(rank_key)
            mode = "Team" if str(signup.get("registration_mode")) == "team" else "Solo"
            team = str(signup.get("team_name") or "—")
            embed.add_field(
                name="✅ Du bist angemeldet",
                value=(
                    f"Rang: **{_rank_display(rank_name, rank_sub if rank_sub > 0 else None)}**  |  "
                    f"Modus: {mode}  |  Team: {team}"
                ),
                inline=False,
            )
        else:
            has_role = _has_turnier_role(member)
            if not has_role:
                embed.add_field(
                    name="⚠️ Fehlende Rolle",
                    value=f"Du benötigst die <@&{TURNIER_ROLE_ID}> Rolle.",
                    inline=False,
                )
            elif period_open:
                embed.add_field(
                    name="📋 Status",
                    value="Noch nicht angemeldet. Klicke **Anmelden**.",
                    inline=False,
                )

        view = TurnierUserView(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            cog=self,
            is_admin=is_admin,
            period_open=period_open,
            is_signed_up=bool(signup),
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ─────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TurnierCog(bot))
    log.info("TurnierCog geladen")
