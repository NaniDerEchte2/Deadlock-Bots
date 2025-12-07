# ============================================================
# Deadlock Team Balancer – volle Parität, neue DB (aiosqlite)
# Datei: cogs/deadlock_team_balancer.py
# Erfordert: utils.deadlock_db.DB_PATH  (SQLite, Tabelle: user_ranks(user_id INTEGER PRIMARY KEY, rank TEXT))
# Kategorie-ID (Pflicht, 2 Team-VCs): 1289721245281292290
# ============================================================

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from itertools import combinations
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from service import db


logger = logging.getLogger(__name__)

# --------- KONFIG ---------
MATCH_CATEGORY_ID = 1289721245281292290  # Pflichtkategorie für die zwei Team-VCs
TEAM_SIZE_CAP = 6                        # max 6 pro Team => 6v6
MOVE_SLEEP = 0.35                        # Rate-Limit-Schoner beim Move
SELECTION_TIMEOUT = 90.0                 # Sekunden für interaktive Auswahl
# --------------------------

# Rang-Mapping (Name -> Wert)
DEADLOCK_RANKS: Dict[str, int] = {
    "Obscurus": 0,
    "Initiate": 1,
    "Seeker": 2,
    "Alchemist": 3,
    "Arcanist": 4,
    "Ritualist": 5,
    "Emissary": 6,
    "Archon": 7,
    "Oracle": 8,
    "Phantom": 9,
    "Ascendant": 10,
    "Eternus": 11,
}

# Rollen-ID -> (RankName, RankValue)
DISCORD_RANK_ROLES: Dict[int, Tuple[str, int]] = {
    1331457571118387210: ("Initiate", 1),
    1331457652877955072: ("Seeker", 2),
    1331457699992436829: ("Alchemist", 3),
    1331457724848017539: ("Arcanist", 4),
    1331457879345070110: ("Ritualist", 5),
    1331457898781474836: ("Emissary", 6),
    1331457949654319114: ("Archon", 7),
    1316966867033653338: ("Oracle", 8),
    1331458016356208680: ("Phantom", 9),
    1331458049637875785: ("Ascendant", 10),
    1331458087349129296: ("Eternus", 11),
}

# ================= Helper & DB =================

def _normalize_rank_name(raw: str) -> str:
    return (raw or "").strip().title()

@lru_cache(maxsize=10000)
def _rank_value_from_name_cached(name: str) -> int:
    return DEADLOCK_RANKS.get(_normalize_rank_name(name), 0)

async def _fetch_rank_from_db(user_id: int) -> Tuple[str, int]:
    """Fetch user rank from central DB using async wrapper."""
    try:
        row = await db.query_one_async(
            "SELECT rank FROM user_ranks WHERE user_id = ?",
            (int(user_id),),
        )
        if row and row[0]:
            nm = _normalize_rank_name(str(row[0]))
            return nm, _rank_value_from_name_cached(nm)
    except Exception as e:
        logger.warning(f"DB rank fetch failed for {user_id}: {e}")
    return "Obscurus", 0

def _rank_from_roles(member: discord.Member) -> Tuple[str, int]:
    best = ("Obscurus", 0)
    for role in member.roles:
        meta = DISCORD_RANK_ROLES.get(role.id)
        if meta and meta[1] > best[1]:
            best = meta
    return best

def _balance_score(team_a: List[int], team_b: List[int]) -> float:
    # Kombiniert Summe-/Ø-Differenz + Varianz – je kleiner desto besser
    if not team_a or not team_b:
        return float("inf")
    avg_a = sum(team_a) / len(team_a)
    avg_b = sum(team_b) / len(team_b)
    var_a = sum((x - avg_a) ** 2 for x in team_a) / len(team_a)
    var_b = sum((x - avg_b) ** 2 for x in team_b) / len(team_b)
    diff_sum = abs(sum(team_a) - sum(team_b))
    diff_avg = abs(avg_a - avg_b)
    return diff_sum * 1.0 + diff_avg * 2.0 + (var_a + var_b) * 0.5

def _best_split(players: List[Tuple[discord.Member, int]]) -> Tuple[List[Tuple[discord.Member, int]], List[Tuple[discord.Member, int]]]:
    """
    players: [(member, rank_value)]
    -> zwei Teams (gleiche Größe, max TEAM_SIZE_CAP) mit bester Balance
    """
    n = len(players)
    team_size = min(TEAM_SIZE_CAP, n // 2)
    team_size = max(2, team_size)  # Safety

    best = None
    best_score = float("inf")
    idx = list(range(n))

    for comb in combinations(idx, team_size):
        a_idx = set(comb)
        team_a = [players[i] for i in a_idx]
        rest = [players[i] for i in idx if i not in a_idx]
        if len(rest) < team_size:
            continue
        team_b = rest[:team_size]
        score = _balance_score([r for _, r in team_a], [r for _, r in team_b])
        if score < best_score:
            best_score = score
            best = (team_a, team_b)

    return best if best else (players[:team_size], players[team_size:team_size*2])

def _team_embed(team_a: List[Tuple[discord.Member, int]], team_b: List[Tuple[discord.Member, int]], title: str) -> discord.Embed:
    def fmt(team: List[Tuple[discord.Member, int]]) -> Tuple[str, float, float]:
        vals = [v for _, v in team]
        avg = sum(vals)/len(vals) if vals else 0.0
        var = (sum((x-avg)**2 for x in vals)/len(vals)) if vals else 0.0
        lines = []
        for m, v in team:
            nm = next((k for k, val in DEADLOCK_RANKS.items() if val == v), "Obscurus")
            lines.append(f"• **{m.display_name}** — {nm} ({v})")
        return "\n".join(lines) if lines else "—", avg, var

    emb = discord.Embed(title=title, color=0x00CC88)
    a_txt, a_avg, a_var = fmt(team_a)
    b_txt, b_avg, b_var = fmt(team_b)

    emb.add_field(name=f"🟠 Team Amber (Ø {a_avg:.1f})", value=a_txt, inline=True)
    emb.add_field(name=f"🔵 Team Sapphire (Ø {b_avg:.1f})", value=b_txt, inline=True)

    diff = abs(a_avg - b_avg)
    mark = "✅" if diff < 1.0 else ("⚠️" if diff < 2.0 else "❌")
    emb.add_field(
        name="📊 Balance",
        value=f"{mark} Ø-Unterschied: **{diff:.2f}**\nVarianz A: {a_var:.2f} | Varianz B: {b_var:.2f}",
        inline=False,
    )
    return emb

# ================= Auswahl-UI (>12 Spieler) =================

class PlayerButton(discord.ui.Button):
    def __init__(self, member: discord.Member, rank_val: int, idx: int):
        super().__init__(
            label=f"{member.display_name}",
            style=discord.ButtonStyle.secondary,
            row=(idx % 20) // 5  # 5 Buttons je Reihe (0..3), Reihe 4 = Control
        )
        self.member = member
        self.rank_val = rank_val
        self.idx = idx

    def set_selected(self, selected: bool):
        self.style = discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary

    async def callback(self, interaction: discord.Interaction):
        view: "SelectionView" = self.view  # type: ignore
        if self.idx in view.selected:
            view.selected.remove(self.idx)
            self.set_selected(False)
        else:
            if len(view.selected) >= view.max_players:
                await interaction.response.send_message(
                    f"Maximale Auswahl erreicht ({view.max_players}).", ephemeral=True
                )
                return
            view.selected.add(self.idx)
            self.set_selected(True)
        view.refresh_confirm()
        await interaction.response.edit_message(view=view)

class SelectionView(discord.ui.View):
    MAX_COMPONENTS = 25
    CONTROL_COUNT = 2  # confirm + random

    def __init__(self, ctx: commands.Context, players: List[Tuple[discord.Member, str, int]], max_players: int = 12):
        super().__init__(timeout=SELECTION_TIMEOUT)
        self.ctx = ctx
        self.players = players
        self.max_players = max_players
        self.selected: set[int] = set()

        allowed = min(len(players), self.MAX_COMPONENTS - self.CONTROL_COUNT)  # 23 Buttons
        for i, (m, _nm, rv) in enumerate(players[:allowed]):
            btn = PlayerButton(m, rv, i)
            self.add_item(btn)

        self.confirm_btn = discord.ui.Button(
            label=f"Match starten (0/{self.max_players})",
            style=discord.ButtonStyle.success,
            emoji="🎮",
            row=4,
            disabled=True
        )
        self.confirm_btn.callback = self._confirm_cb
        self.add_item(self.confirm_btn)

        self.random_btn = discord.ui.Button(
            label="Zufällige Auswahl",
            style=discord.ButtonStyle.secondary,
            emoji="🎲",
            row=4
        )
        self.random_btn.callback = self._random_cb
        self.add_item(self.random_btn)

    def refresh_confirm(self):
        c = len(self.selected)
        self.confirm_btn.label = f"Match starten ({c}/{self.max_players})"
        self.confirm_btn.disabled = c < 4 or c > self.max_players

    async def _confirm_cb(self, interaction: discord.Interaction):
        chosen = [self.players[i] for i in sorted(self.selected) if i < len(self.players)]
        self.stop()
        await interaction.response.edit_message(content=f"🎮 Starte Match mit {len(chosen)} ausgewählten Spielern …", view=None, embed=None)
        cog: "DeadlockTeamBalancer" = interaction.client.get_cog("DeadlockTeamBalancer")  # type: ignore
        if not cog:
            return
        await cog._run_balance_and_start(self.ctx, chosen)

    async def _random_cb(self, interaction: discord.Interaction):
        self.selected.clear()
        count = min(self.max_players, len(self.players), self.MAX_COMPONENTS - self.CONTROL_COUNT)
        for i in random.sample(range(count), k=min(count, self.max_players)):
            self.selected.add(i)
        for child in self.children:
            if isinstance(child, PlayerButton):
                child.set_selected(child.idx in self.selected)
        self.refresh_confirm()
        await interaction.response.edit_message(content=f"🎲 {len(self.selected)} Spieler zufällig ausgewählt.", view=self)

# ================= Haupt-Cog =================

@dataclass
class MatchInfo:
    guild_id: int
    team1_channel_id: int
    team2_channel_id: int
    players: List[int]
    started_at: datetime
    original_channel_id: Optional[int] = None

class DeadlockTeamBalancer(commands.Cog):
    """Teambalancer: 2 Teams nach Rängen, strikt 2 VC in Kategorie MATCH_CATEGORY_ID, Auto-Move, volle Befehlsparität."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._guild_locks: Dict[int, asyncio.Lock] = {}
        self.active_matches: Dict[str, MatchInfo] = {}
        self._match_counter = 0

    # ---------- Lifecycle ----------
    async def cog_load(self):
        # DB is managed centrally by service.db - no setup needed
        logger.info("DeadlockTeamBalancer bereit (DB verbunden)")
        print("✅ DeadlockTeamBalancer Cog geladen")

    # ---------- Rank-Ermittlung ----------
    async def get_user_rank(self, member: discord.Member) -> Tuple[str, int]:
        rn, rv = _rank_from_roles(member)
        if rv > 0:
            return rn, rv
        # Fallback to DB lookup using central DB
        return await _fetch_rank_from_db(member.id)

    # ---------- Commands ----------
    @commands.group(name="balance", aliases=["bal", "teams"], invoke_without_command=True)
    async def balance_root(self, ctx: commands.Context):
        emb = discord.Embed(
            title="⚖️ Deadlock Team Balancer",
            description="Teilt Spieler in **2 Teams** nach Rang auf und **erstellt immer 2 Voice-Channels** in der Match-Kategorie.",
            color=0x0099FF
        )
        emb.add_field(
            name="Befehle",
            value=(
                "`!balance auto` – nur Anzeige (keine Channels)\n"
                "`!balance start` – Channels erstellen & Spieler moven\n"
                "`!balance manual @u1 …` – manuelle Auswahl\n"
                "`!balance voice` – Alias von auto\n"
                "`!balance status [@user]` – Rank-Status\n"
                "`!balance matches` – aktive Matches\n"
                "`!balance end <id> [skip_debrief]` – Match beenden (+ Nachbesprechung)\n"
                "`!balance cleanup <hours>` – alte Matches löschen"
            ),
            inline=False
        )
        emb.add_field(
            name="Spielerzahl",
            value="Min. 4 Spieler, max. 12 (6v6). Bei >12: interaktive Auswahl.",
            inline=False
        )
        await ctx.send(embed=emb)

    @balance_root.command(name="auto")
    async def balance_auto(self, ctx: commands.Context):
        members = self._voice_members(ctx)
        if len(members) < 4:
            await ctx.send(f"❌ Mindestens 4 Spieler benötigt (aktuell: {len(members)})")
            return
        players: List[Tuple[discord.Member, str, int]] = []
        for m in members:
            nm, val = await self.get_user_rank(m)
            players.append((m, nm, val))
        players.sort(key=lambda x: x[2], reverse=True)
        team_a, team_b = _best_split([(m, v) for (m, _n, v) in players])
        embed = _team_embed(team_a, team_b, "🎯 Vorschau – Team Balance (ohne Move)")
        await ctx.send(embed=embed)

    @balance_root.command(name="voice")
    async def balance_voice(self, ctx: commands.Context):
        await self.balance_auto(ctx)

    @balance_root.command(name="manual")
    async def balance_manual(self, ctx: commands.Context, *members: discord.Member):
        if len(members) < 4:
            await ctx.send(f"❌ Mindestens 4 Spieler benötigt (angegeben: {len(members)})")
            return
        if len(members) > TEAM_SIZE_CAP * 2:
            await ctx.send(f"❌ Maximal {TEAM_SIZE_CAP*2} Spieler unterstützt (angegeben: {len(members)})")
            return
        players: List[Tuple[discord.Member, str, int]] = []
        for m in members:
            nm, val = await self.get_user_rank(m)
            players.append((m, nm, val))
        await self._run_balance_and_start(ctx, players)

    @balance_root.command(name="start")
    async def balance_start(self, ctx: commands.Context):
        """Erstellt ZWINGEND zwei Voice-Channels in der Match-Kategorie und moved die Spieler in 2 Teams."""
        members = self._voice_members(ctx)
        if len(members) < 4:
            await ctx.send(f"❌ Mindestens 4 Spieler benötigt (aktuell: {len(members)})")
            return

        players: List[Tuple[discord.Member, str, int]] = []
        for m in members:
            nm, val = await self.get_user_rank(m)
            players.append((m, nm, val))

        if len(players) > TEAM_SIZE_CAP * 2:
            embed = discord.Embed(
                title="👥 Zu viele Spieler",
                description=f"Es sind **{len(players)}** Spieler im Channel – wähle bis zu **{TEAM_SIZE_CAP*2}** Spieler aus.",
                color=discord.Color.orange()
            )
            lines = []
            for i, (mem, nm, val) in enumerate(players[:20], 1):
                lines.append(f"{i}. **{mem.display_name}** — {nm} ({val})")
            if len(players) > 20:
                lines.append(f"... und {len(players)-20} weitere")
            embed.add_field(name="Spieler (Ausschnitt)", value="\n".join(lines), inline=False)
            view = SelectionView(ctx, players, max_players=TEAM_SIZE_CAP*2)
            await ctx.send(embed=embed, view=view)
            return

        await self._run_balance_and_start(ctx, players)

    @balance_root.command(name="status")
    async def balance_status(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        nm, val = await self.get_user_rank(member)
        role_nm, role_val = _rank_from_roles(member)
        db_nm, db_val = ("Obscurus", 0)
        if self.db:
            db_nm, db_val = await _fetch_rank_from_db(self.db, member.id)
        emb = discord.Embed(title=f"📊 Rank-Status: {member.display_name}", color=discord.Color.blue())
        emb.add_field(name="🎯 Aktueller Rank", value=f"**{nm}** ({val})", inline=True)
        emb.add_field(name="🎭 Discord-Rollen", value=f"{role_nm} ({role_val})" if role_val else "Keine Rank-Rolle", inline=True)
        emb.add_field(name="🗃️ DB-Fallback", value=f"{db_nm} ({db_val})" if db_val else "Nicht in DB", inline=True)
        await ctx.send(embed=emb)

    @balance_root.command(name="matches")
    async def balance_matches(self, ctx: commands.Context):
        if not self.active_matches:
            await ctx.send("📭 Keine aktiven Matches")
            return
        emb = discord.Embed(title="🎮 Aktive Deadlock Matches", color=discord.Color.green())
        for match_id, info in self.active_matches.items():
            guild = self.bot.get_guild(info.guild_id)
            if not guild:
                continue
            lines = []
            for ch_id in (info.team1_channel_id, info.team2_channel_id):
                ch = guild.get_channel(ch_id)
                if ch:
                    count = len([m for m in ch.members if not m.bot])
                    lines.append(f"{ch.name}: {count} Spieler")
                else:
                    lines.append(f"{ch_id}: gelöscht")
            dur = datetime.utcnow() - info.started_at
            emb.add_field(
                name=f"Match {match_id}",
                value=f"Dauer: {dur.seconds//60}min {dur.seconds%60}s\n" + "\n".join(lines),
                inline=False
            )
        await ctx.send(embed=emb)

    @balance_root.command(name="cleanup")
    @commands.has_permissions(manage_channels=True)
    async def balance_cleanup(self, ctx: commands.Context, hours: int = 2):
        if hours < 1 or hours > 24:
            await ctx.send("❌ Stunden müssen zwischen 1–24 liegen")
            return
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        targets = [(mid, mi) for mid, mi in self.active_matches.items() if mi.started_at < cutoff]
        if not targets:
            await ctx.send(f"🧹 Keine Matches älter als {hours}h gefunden")
            return
        deleted_ch = 0
        for match_id, info in targets:
            for ch_id in (info.team1_channel_id, info.team2_channel_id):
                ch = ctx.guild.get_channel(ch_id)
                if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                    try:
                        await ch.delete(reason=f"Auto-Cleanup (> {hours}h)")
                        deleted_ch += 1
                        await asyncio.sleep(0.4)
                    except discord.Forbidden:
                        logger.debug("Cleanup: delete forbidden for channel %s", ch_id)
                    except discord.HTTPException as e:
                        logger.debug("Cleanup: HTTP error deleting %s: %r", ch_id, e)
                    except Exception as e:
                        logger.debug("Cleanup: unexpected error deleting %s: %r", ch_id, e)
            self.active_matches.pop(match_id, None)
        await ctx.send(f"🧹 {len(targets)} Matches bereinigt ({deleted_ch} Channels gelöscht)")

    @balance_root.command(name="end")
    async def balance_end(self, ctx: commands.Context, match_id: Optional[str] = None, skip_debrief: Optional[bool] = False):
        if not match_id:
            await ctx.send("❌ Bitte Match-ID angeben: `!balance end <id>`")
            return
        info = self.active_matches.get(match_id)
        if not info:
            await ctx.send(f"❌ Match `{match_id}` nicht gefunden.")
            return

        # 1) Optionale Nachbesprechungs-Lane in gleicher Kategorie
        debrief_ch = None
        moved = 0
        move_fail: List[str] = []
        if not skip_debrief:
            cat = ctx.guild.get_channel(MATCH_CATEGORY_ID)
            if isinstance(cat, discord.CategoryChannel):
                try:
                    debrief_ch = await ctx.guild.create_voice_channel(
                        name=f"💬 Nachbesprechung • {match_id}",
                        category=cat,
                        reason=f"Match {match_id} Nachbesprechung"
                    )
                except Exception as e:
                    logger.warning(f"Debrief-VC create failed: {e}")

            if debrief_ch:
                for uid in info.players:
                    try:
                        m = ctx.guild.get_member(uid)
                        if m and m.voice and m.voice.channel and m.voice.channel.id in (info.team1_channel_id, info.team2_channel_id):
                            await m.move_to(debrief_ch, reason=f"Match {match_id} beendet – Debrief")
                            moved += 1
                            await asyncio.sleep(0.3)
                    except discord.Forbidden:
                        move_fail.append(f"<@{uid}> (keine Berechtigung)")
                    except discord.HTTPException as e:
                        move_fail.append(f"<@{uid}> (HTTP: {e})")
                    except Exception as e:
                        move_fail.append(f"<@{uid}> (Fehler: {e})")

        # 2) Team-Channels löschen
        deleted = []
        failed = []
        for ch_id in (info.team1_channel_id, info.team2_channel_id):
            ch = ctx.guild.get_channel(ch_id)
            if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                try:
                    await ch.delete(reason=f"Match {match_id} beendet")
                    deleted.append(ch.name)
                    await asyncio.sleep(0.4)
                except Exception as e:
                    failed.append(f"{ch.mention if hasattr(ch,'mention') else ch.id} ({e})")
            else:
                failed.append(f"{ch_id} (nicht gefunden)")

        self.active_matches.pop(match_id, None)

        # 3) Ergebnis
        emb = discord.Embed(title=f"🏁 Match {match_id} beendet", color=discord.Color.green() if debrief_ch else discord.Color.orange())
        if debrief_ch:
            emb.add_field(
                name="💬 Nachbesprechungs-Lane",
                value=f"{debrief_ch.mention}\nSpieler bewegt: {moved}/{len(info.players)}",
                inline=False
            )
            if move_fail:
                emb.add_field(
                    name="⚠️ Nicht bewegt",
                    value="\n".join(move_fail[:6]) + (f"\n… und {len(move_fail)-6} weitere" if len(move_fail) > 6 else ""),
                    inline=False
                )
        if deleted:
            emb.add_field(name="🗑️ Gelöschte Team-Channels", value="\n".join(deleted), inline=False)
        if failed:
            emb.add_field(name="⚠️ Nicht gelöscht", value="\n".join(failed), inline=False)
        dur = datetime.utcnow() - info.started_at
        emb.add_field(name="📊 Dauer", value=f"{dur.seconds//60}min {dur.seconds%60}s", inline=True)
        emb.add_field(name="👥 Spieler", value=str(len(info.players)), inline=True)
        await ctx.send(embed=emb)

    # ---------- Kernablauf ----------
    async def _run_balance_and_start(self, ctx: commands.Context, players_in: List[Tuple[discord.Member, str, int]]):
        players_mv: List[Tuple[discord.Member, int]] = sorted(
            [(m, v) for (m, _nm, v) in players_in],
            key=lambda t: t[1],
            reverse=True
        )

        team_a, team_b = _best_split(players_mv)

        # Immer zwei Channels in der Kategorie erstellen (Pflicht)
        cat = ctx.guild.get_channel(MATCH_CATEGORY_ID)
        if not isinstance(cat, discord.CategoryChannel):
            await ctx.send(f"❌ Kategorie `{MATCH_CATEGORY_ID}` nicht gefunden oder keine Kategorie.")
            return

        match_id = self._next_match_id()
        team1_name = f"🟠 Team Amber • {match_id}"
        team2_name = f"🔵 Team Sapphire • {match_id}"
        lock = self._guild_locks.setdefault(ctx.guild.id, asyncio.Lock())

        async with lock:
            try:
                ch1 = await ctx.guild.create_voice_channel(name=team1_name, category=cat, reason=f"Match {match_id}")
                await asyncio.sleep(0.4)
                ch2 = await ctx.guild.create_voice_channel(name=team2_name, category=cat, reason=f"Match {match_id}")
            except discord.Forbidden:
                await ctx.send("❌ Keine Berechtigung, Voice-Channels zu erstellen.")
                return
            except Exception as e:
                await ctx.send(f"❌ Konnte Channels nicht erstellen: {e}")
                return

        # Spieler bewegen
        moved_a, moved_b, fail = await self._move_teams(team_a, team_b, ch1, ch2)

        # persistieren
        orig = ctx.author.voice.channel.id if ctx.author.voice and ctx.author.voice.channel else None
        self.active_matches[match_id] = MatchInfo(
            guild_id=ctx.guild.id,
            team1_channel_id=ch1.id,
            team2_channel_id=ch2.id,
            players=[m.id for m, _ in (team_a + team_b)],
            started_at=datetime.utcnow(),
            original_channel_id=orig
        )

        # Embed mit Balance & Move-Ergebnis
        embed = _team_embed(team_a, team_b, f"🎮 Match {match_id} – Teams erstellt & Spieler verschoben")
        embed.add_field(
            name="Move",
            value=(f"{ch1.mention}: {moved_a}/{len(team_a)}\n"
                   f"{ch2.mention}: {moved_b}/{len(team_b)}"),
            inline=False
        )
        if fail:
            embed.add_field(
                name="⚠️ Nicht bewegt",
                value="\n".join(fail[:6]) + (f"\n… und {len(fail)-6} weitere" if len(fail) > 6 else ""),
                inline=False
            )
        embed.set_footer(text=f"Match-ID: {match_id} • Beenden: !balance end {match_id}")
        await ctx.send(embed=embed)

    async def _move_teams(
        self,
        team_a: List[Tuple[discord.Member, int]],
        team_b: List[Tuple[discord.Member, int]],
        ch1: discord.VoiceChannel,
        ch2: discord.VoiceChannel
    ) -> Tuple[int, int, List[str]]:
        moved_a = moved_b = 0
        failed: List[str] = []

        order = [(team_a[i], ch1) for i in range(len(team_a))] + [(team_b[i], ch2) for i in range(len(team_b))]
        for (m, _), target in order:
            try:
                if m.voice and m.voice.channel:
                    await m.move_to(target, reason="Deadlock Team Balance")
                    if target.id == ch1.id:
                        moved_a += 1
                    else:
                        moved_b += 1
                else:
                    failed.append(f"{m.display_name} (nicht in Voice)")
            except discord.Forbidden:
                failed.append(f"{m.display_name} (keine Berechtigung)")
            except discord.HTTPException as e:
                failed.append(f"{m.display_name} (HTTP: {e})")
            except Exception as e:
                failed.append(f"{m.display_name} (Fehler: {e})")
            await asyncio.sleep(MOVE_SLEEP)

        return moved_a, moved_b, failed

    # ---------- Utils ----------
    def _voice_members(self, ctx: commands.Context) -> List[discord.Member]:
        if not ctx.author.voice or not isinstance(ctx.author.voice.channel, discord.VoiceChannel):
            return []
        return [m for m in ctx.author.voice.channel.members if not m.bot]

    def _next_match_id(self) -> str:
        self._match_counter += 1
        return f"{self._match_counter:03d}"

# ================= Setup =================

async def setup(bot: commands.Bot):
    await bot.add_cog(DeadlockTeamBalancer(bot))
    logger.info("DeadlockTeamBalancer (paritäts-komplett) geladen")
