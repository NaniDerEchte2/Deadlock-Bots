import asyncio
import logging
from typing import Dict, Tuple, List, Optional, Set
import aiosqlite
import discord
from discord.ext import commands
from service.db import db_path
from pathlib import Path
DB_PATH = Path(db_path())  # alias, damit alter Code weiterläuft


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RolePermissionVoiceManager(commands.Cog):
    """Rollen-basierte Sprachkanal-Verwaltung über Discord-Rollen-Berechtigungen
    Persistenz (Toggle & Anker) über zentrale DB (aiosqlite / DB_PATH).
    """

    def __init__(self, bot):
        self.bot = bot
        self.monitored_categories = {
            1357422957017698478: "ranked",
            1412804540994162789: "grind",
        }

        # Ausnahme-Kanäle die NICHT überwacht werden sollen
        self.excluded_channel_ids = {
            1375933460841234514,
            1375934283931451512,
            1357422958544420944,
            1412804671432818890,
        }

        # Discord Rollen-IDs zu Rang-Mapping
        self.discord_rank_roles = {
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

        # Deadlock Rang-System (für interne Berechnungen)
        self.deadlock_ranks = {
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

        # Balancing-Regeln (Rang -> (minus, plus))
        self.balancing_rules = {
            "Initiate": (-3, 3),
            "Seeker": (-3, 3),
            "Alchemist": (-3, 3),
            "Arcanist": (-3, 3),
            "Ritualist": (-3, 2),
            "Emissary": (-3, 2),
            "Archon": (-2, 2),
            "Oracle": (-2, 2),
            "Phantom": (-2, 2),
            "Ascendant": (-1, 1),
            "Eternus": (-1, 1),
        }

        # Cache
        self.user_rank_cache: Dict[str, Tuple[str, int]] = {}
        self.guild_roles_cache: Dict[int, Dict[int, discord.Role]] = {}

        # Laufzeit-State (wird beim Start aus DB geladen)
        # {channel_id: (user_id, rank_name, rank_value, allowed_min, allowed_max)}
        self.channel_anchors: Dict[int, Tuple[int, str, int, int, int]] = {}
        # {channel_id: {"enabled": bool}}
        self.channel_settings: Dict[int, Dict[str, bool]] = {}

        # DB
        self.db: Optional[aiosqlite.Connection] = None

    # -------------------- DB Layer --------------------

    async def _db_connect(self):
        if self.db:
            return
        self.db = await aiosqlite.connect(str(DB_PATH))
        self.db.row_factory = aiosqlite.Row
        await self._db_ensure_schema()

    async def _db_ensure_schema(self):
        assert self.db
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_channel_settings (
                channel_id  INTEGER PRIMARY KEY,
                guild_id    INTEGER NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_channel_anchors (
                channel_id   INTEGER PRIMARY KEY,
                guild_id     INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                rank_name    TEXT NOT NULL,
                rank_value   INTEGER NOT NULL,
                allowed_min  INTEGER NOT NULL,
                allowed_max  INTEGER NOT NULL,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await self.db.commit()

    async def _db_load_state_for_guild(self, guild: discord.Guild):
        """Lädt Settings & Anker der Gilde in die In-Memory-Maps."""
        await self._db_connect()
        assert self.db

        # Settings
        cur = await self.db.execute(
            "SELECT channel_id, enabled FROM voice_channel_settings WHERE guild_id=?",
            (guild.id,),
        )
        rows = await cur.fetchall()
        for r in rows:
            self.channel_settings[int(r["channel_id"])] = {"enabled": bool(r["enabled"])}

        # Anchors
        cur = await self.db.execute(
            """
            SELECT channel_id, user_id, rank_name, rank_value, allowed_min, allowed_max
            FROM voice_channel_anchors WHERE guild_id=?
            """,
            (guild.id,),
        )
        rows = await cur.fetchall()
        for r in rows:
            self.channel_anchors[int(r["channel_id"])] = (
                int(r["user_id"]),
                str(r["rank_name"]),
                int(r["rank_value"]),
                int(r["allowed_min"]),
                int(r["allowed_max"]),
            )

    async def _db_upsert_setting(self, channel: discord.VoiceChannel, enabled: bool):
        await self._db_connect()
        assert self.db
        await self.db.execute(
            """
            INSERT INTO voice_channel_settings(channel_id, guild_id, enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                enabled=excluded.enabled,
                updated_at=CURRENT_TIMESTAMP
            """,
            (channel.id, channel.guild.id, int(enabled)),
        )
        await self.db.commit()

    async def _db_upsert_anchor(
        self,
        channel: discord.VoiceChannel,
        user_id: int,
        rank_name: str,
        rank_value: int,
        allowed_min: int,
        allowed_max: int,
    ):
        await self._db_connect()
        assert self.db
        await self.db.execute(
            """
            INSERT INTO voice_channel_anchors(channel_id, guild_id, user_id, rank_name, rank_value, allowed_min, allowed_max)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                user_id=excluded.user_id,
                rank_name=excluded.rank_name,
                rank_value=excluded.rank_value,
                allowed_min=excluded.allowed_min,
                allowed_max=excluded.allowed_max,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                channel.id,
                channel.guild.id,
                user_id,
                rank_name,
                rank_value,
                allowed_min,
                allowed_max,
            ),
        )
        await self.db.commit()

    async def _db_delete_anchor(self, channel: discord.VoiceChannel):
        await self._db_connect()
        assert self.db
        await self.db.execute(
            "DELETE FROM voice_channel_anchors WHERE channel_id=?", (channel.id,)
        )
        await self.db.commit()

    # -------------------- Lifecycle --------------------

    async def cog_load(self):
        try:
            await self._db_connect()
            # Bei Start für alle bekannten Guilds laden
            for guild in self.bot.guilds:
                await self._db_load_state_for_guild(guild)

            logger.info("RolePermissionVoiceManager Cog erfolgreich geladen")
            print("✅ RolePermissionVoiceManager Cog geladen")
            monitored_list = ", ".join(str(cid) for cid in self.monitored_categories.keys())
            print(f"   Überwachte Kategorien: {monitored_list}")
            print(f"   Überwachte Rollen: {len(self.discord_rank_roles)}")
            print(f"   Ausgeschlossene Kanäle: {len(self.excluded_channel_ids)}")
            print("   🔧 Persistenz: zentrale DB (Settings & Anker)")
        except Exception as e:
            logger.error(f"Fehler beim Laden des RolePermissionVoiceManager Cogs: {e}")
            raise

    async def cog_unload(self):
        try:
            self.user_rank_cache.clear()
            self.guild_roles_cache.clear()
            self.channel_anchors.clear()
            self.channel_settings.clear()
            if self.db:
                await self.db.close()
            logger.info("RolePermissionVoiceManager Cog erfolgreich entladen")
            print("✅ RolePermissionVoiceManager Cog entladen")
        except Exception as e:
            logger.error(f"Fehler beim Entladen des RolePermissionVoiceManager Cogs: {e}")

    # -------------------- Helpers --------------------

    def get_guild_roles(self, guild: discord.Guild) -> Dict[int, discord.Role]:
        if guild.id not in self.guild_roles_cache:
            self.guild_roles_cache[guild.id] = {role.id: role for role in guild.roles}
        return self.guild_roles_cache[guild.id]

    def get_user_rank_from_roles(self, member: discord.Member) -> Tuple[str, int]:
        cache_key = f"{member.id}:{member.guild.id}"
        if cache_key in self.user_rank_cache:
            return self.user_rank_cache[cache_key]

        highest_rank = ("Obscurus", 0)
        highest_rank_value = 0

        for role in member.roles:
            if role.id in self.discord_rank_roles:
                rank_name, rank_value = self.discord_rank_roles[role.id]
                if rank_value > highest_rank_value:
                    highest_rank = (rank_name, rank_value)
                    highest_rank_value = rank_value

        self.user_rank_cache[cache_key] = highest_rank
        return highest_rank

    async def get_channel_members_ranks(
        self, channel: discord.VoiceChannel
    ) -> Dict[discord.Member, Tuple[str, int]]:
        members_ranks: Dict[discord.Member, Tuple[str, int]] = {}
        for member in channel.members:
            if member.bot:
                continue
            members_ranks[member] = self.get_user_rank_from_roles(member)
        return members_ranks

    def calculate_balancing_range_from_anchor(
        self, channel: discord.VoiceChannel
    ) -> Tuple[int, int]:
        anchor = self.get_channel_anchor(channel)
        if anchor is None:
            return 0, 11
        _user_id, _rank_name, _rank_value, allowed_min, allowed_max = anchor
        return allowed_min, allowed_max

    def get_allowed_role_ids(self, allowed_min: int, allowed_max: int) -> Set[int]:
        return {
            role_id
            for role_id, (_rn, rv) in self.discord_rank_roles.items()
            if allowed_min <= rv <= allowed_max
        }

    async def set_everyone_deny_connect(self, channel: discord.VoiceChannel) -> bool:
        try:
            if not await self.channel_exists(channel):
                return False
            everyone_role = channel.guild.default_role
            ow = channel.overwrites_for(everyone_role)
            if ow.connect is not False:
                await channel.set_permissions(
                    everyone_role,
                    overwrite=discord.PermissionOverwrite(connect=False, view_channel=True),
                )
            return True
        except discord.NotFound:
            return False
        except Exception as e:
            logger.error(f"@everyone setzen fehlgeschlagen: {e}")
            return False

    async def channel_exists(self, channel: discord.VoiceChannel) -> bool:
        try:
            fresh = channel.guild.get_channel(channel.id)
            return isinstance(fresh, discord.VoiceChannel)
        except Exception:
            return False

    async def update_channel_permissions_via_roles(self, channel: discord.VoiceChannel):
        try:
            if not await self.channel_exists(channel):
                return

            if not self.is_channel_system_enabled(channel):
                return

            ok = await self.set_everyone_deny_connect(channel)
            if not ok:
                return

            members_ranks = await self.get_channel_members_ranks(channel)
            if not members_ranks:
                # leer -> Anker entfernen + Rollen-Overwrites entfernen
                await self.remove_channel_anchor(channel)
                await self.clear_role_permissions(channel)
                return

            allowed_min, allowed_max = self.calculate_balancing_range_from_anchor(channel)
            allowed_role_ids = self.get_allowed_role_ids(allowed_min, allowed_max)

            guild_roles = self.get_guild_roles(channel.guild)

            # allow für erlaubte Rollen
            for role_id in allowed_role_ids:
                role = guild_roles.get(role_id)
                if not role:
                    continue
                ow = channel.overwrites_for(role)
                if ow.connect is not True:
                    await channel.set_permissions(
                        role,
                        overwrite=discord.PermissionOverwrite(
                            connect=True, speak=True, view_channel=True
                        ),
                    )
                    await asyncio.sleep(0.4)

            # remove von nicht erlaubten Rollen
            await self.remove_disallowed_role_permissions(channel, allowed_role_ids)

        except Exception as e:
            logger.error(f"update_channel_permissions_via_roles Fehler: {e}")

    async def remove_disallowed_role_permissions(
        self, channel: discord.VoiceChannel, allowed_role_ids: Set[int]
    ):
        try:
            for target, _ow in list(channel.overwrites.items()):
                if (
                    isinstance(target, discord.Role)
                    and target.id != channel.guild.default_role.id
                    and target.id in self.discord_rank_roles
                    and target.id not in allowed_role_ids
                ):
                    await channel.set_permissions(target, overwrite=None)
                    await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"remove_disallowed_role_permissions Fehler: {e}")

    async def clear_role_permissions(self, channel: discord.VoiceChannel):
        try:
            for target, _ow in list(channel.overwrites.items()):
                if (
                    isinstance(target, discord.Role)
                    and target.id != channel.guild.default_role.id
                    and target.id in self.discord_rank_roles
                ):
                    await channel.set_permissions(target, overwrite=None)
                    await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"clear_role_permissions Fehler: {e}")

    async def update_channel_name(self, channel: discord.VoiceChannel):
        try:
            if not await self.channel_exists(channel):
                return

            members_ranks = await self.get_channel_members_ranks(channel)
            if not members_ranks:
                new_name = "Rang-Sprachkanal"
            else:
                anchor = self.get_channel_anchor(channel)
                if anchor:
                    _uid, anchor_rank_name, _rv, allowed_min, allowed_max = anchor
                    min_rank_name = self.get_rank_name_from_value(allowed_min)
                    max_rank_name = self.get_rank_name_from_value(allowed_max)
                    if allowed_min == allowed_max:
                        new_name = f"{anchor_rank_name} Lobby"
                    elif allowed_max - allowed_min <= 1:
                        new_name = f"{anchor_rank_name} Elo"
                    else:
                        new_name = f"{min_rank_name}-{max_rank_name} ({anchor_rank_name})"
                else:
                    # Fallback: erster User
                    first_member = next(iter(members_ranks.keys()))
                    rank_name, _rv2 = members_ranks[first_member]
                    new_name = f"{rank_name} Lobby"

            if channel.name != new_name:
                await channel.edit(name=new_name)
        except discord.NotFound:
            return
        except Exception as e:
            logger.error(f"update_channel_name Fehler: {e}")

    def get_rank_name_from_value(self, rank_value: int) -> str:
        for rn, val in self.deadlock_ranks.items():
            if val == rank_value:
                return rn
        return "Obscurus"

    async def set_channel_anchor(
        self, channel: discord.VoiceChannel, user: discord.Member, rank_name: str, rank_value: int
    ):
        minus = plus = 0
        mode = self.get_channel_mode(channel)
        if mode == "grind":
            minus, plus = -2, 2
        else:
            minus, plus = self.balancing_rules.get(rank_name, (-1, 1))

        initiate_value = self.deadlock_ranks["Initiate"]
        emissary_value = self.deadlock_ranks["Emissary"]
        archon_value = self.deadlock_ranks["Archon"]
        phantom_value = self.deadlock_ranks["Phantom"]
        eternus_value = self.deadlock_ranks["Eternus"]

        allowed_min = max(0, rank_value + minus)
        allowed_max = min(eternus_value, rank_value + plus)

        if mode != "grind":
            if rank_value <= emissary_value:
                allowed_min = initiate_value
                allowed_max = emissary_value
            elif archon_value <= rank_value <= phantom_value:
                allowed_min = max(self.deadlock_ranks["Ritualist"], allowed_min)
                allowed_max = min(phantom_value, allowed_max)

        self.channel_anchors[channel.id] = (
            user.id,
            rank_name,
            rank_value,
            allowed_min,
            allowed_max,
        )
        await self._db_upsert_anchor(channel, user.id, rank_name, rank_value, allowed_min, allowed_max)
        logger.info(
            f"🔗 Anker gesetzt für {channel.name}: {user.display_name} ({rank_name}) → {allowed_min}-{allowed_max}"
        )

    def get_channel_anchor(
        self, channel: discord.VoiceChannel
    ) -> Optional[Tuple[int, str, int, int, int]]:
        return self.channel_anchors.get(channel.id)

    async def remove_channel_anchor(self, channel: discord.VoiceChannel):
        if channel.id in self.channel_anchors:
            old = self.channel_anchors.pop(channel.id)
            logger.info(f"🔗 Anker entfernt für {channel.name}: {old[1]} ({old[2]})")
            await self._db_delete_anchor(channel)

    def is_channel_system_enabled(self, channel: discord.VoiceChannel) -> bool:
        return self.channel_settings.get(channel.id, {}).get("enabled", True)

    async def set_channel_system_enabled(self, channel: discord.VoiceChannel, enabled: bool):
        self.channel_settings.setdefault(channel.id, {})["enabled"] = enabled
        await self._db_upsert_setting(channel, enabled)
        logger.info(f"🔧 Rang-System für {channel.name} {'aktiviert' if enabled else 'deaktiviert'}")

    # -------------------- Monitoring --------------------

    def is_monitored_channel(self, channel: discord.VoiceChannel) -> bool:
        if channel.id in self.excluded_channel_ids:
            return False
        return (
            channel.category_id in self.monitored_categories
            if channel.category
            else False
        )

    def get_channel_mode(self, channel: discord.VoiceChannel) -> Optional[str]:
        if channel.category:
            return self.monitored_categories.get(channel.category_id)
        return None

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        # Falls der Bot später hinzugefügt wird – Lade DB-Status für diese Guild
        try:
            await self._db_load_state_for_guild(guild)
        except Exception as e:
            logger.warning(f"on_guild_join load state failed: {e}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        try:
            # Cache invalidieren
            cache_key = f"{member.id}:{member.guild.id}"
            self.user_rank_cache.pop(cache_key, None)
            self.guild_roles_cache.pop(member.guild.id, None)

            # Join/Move
            if after.channel and self.is_monitored_channel(after.channel):
                await self.handle_voice_join(member, after.channel)

            # Leave
            if before.channel and self.is_monitored_channel(before.channel):
                await self.handle_voice_leave(member, before.channel)

        except Exception as e:
            logger.error(f"voice_state_update Fehler: {e}")

    async def handle_voice_join(self, member: discord.Member, channel: discord.VoiceChannel):
        try:
            if not self.is_channel_system_enabled(channel):
                return

            rank_name, rank_value = self.get_user_rank_from_roles(member)

            anchor = self.get_channel_anchor(channel)
            if anchor is None:
                await self.set_channel_anchor(channel, member, rank_name, rank_value)
                await self.update_channel_permissions_via_roles(channel)
                await self.update_channel_name(channel)
                return

            # Nur logs – niemals kicken
            _uid, _arname, _arval, allowed_min, allowed_max = anchor
            if not (allowed_min <= rank_value <= allowed_max):
                logger.info(
                    f"ℹ️ {member.display_name} ({rank_name}) passt nicht in {allowed_min}-{allowed_max}, bleibt aber."
                )

            await self.update_channel_permissions_via_roles(channel)
            await self.update_channel_name(channel)

        except Exception as e:
            logger.error(f"handle_voice_join Fehler: {e}")

    async def handle_voice_leave(self, member: discord.Member, channel: discord.VoiceChannel):
        try:
            await asyncio.sleep(1)  # etwas Luft für Discord-Events

            if not self.is_channel_system_enabled(channel):
                return

            members_ranks = await self.get_channel_members_ranks(channel)
            if not members_ranks:
                await self.remove_channel_anchor(channel)
            else:
                anchor = self.get_channel_anchor(channel)
                if anchor and anchor[0] == member.id:
                    # Anker übertragen
                    await self.remove_channel_anchor(channel)
                    first_remaining = next(iter(members_ranks.keys()))
                    rn, rv = members_ranks[first_remaining]
                    await self.set_channel_anchor(channel, first_remaining, rn, rv)

            await self.update_channel_permissions_via_roles(channel)
            await self.update_channel_name(channel)

        except Exception as e:
            logger.error(f"handle_voice_leave Fehler: {e}")

    # -------------------- Admin Commands --------------------

    @commands.group(name="rrang", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def rank_command(self, ctx):
        embed = discord.Embed(
            title="🎭 Rollen-Berechtigungen Rang-System",
            description="Verwaltet Sprachkanäle über Discord-Rollen-Berechtigungen (mit DB-Persistenz)",
            color=0x0099FF,
        )
        embed.add_field(
            name="📋 Befehle",
            value=(
                "`info` • Rang-Info eines Users\n"
                "`debug` • Debug zu User-Rollen\n"
                "`anker` • Zeigt Kanal-Anker\n"
                "`toggle [ein/aus]` • System für aktuellen VC\n"
                "`vcstatus` • Status des aktuellen VC\n"
                "`status` • Systemstatus\n"
                "`rollen` • Liste der Rang-Rollen\n"
                "`kanäle` • Überwachte/ausgeschlossene Kanäle\n"
                "`aktualisieren [#vc]` • Forced Update"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @rank_command.command(name="status")
    async def system_status(self, ctx):
        embed = discord.Embed(
            title="📊 System-Status",
            description="Rollen-Berechtigungen Voice Manager",
            color=discord.Color.green(),
        )
        enabled_cnt = sum(1 for st in self.channel_settings.values() if st.get("enabled", True))
        embed.add_field(
            name="🔧 Version",
            value="Sanftes Anker-System v4.0 (DB-persistiert)",
            inline=False,
        )
        embed.add_field(
            name="📁 Überwachung",
            value=(
                f"Kategorien: {', '.join(str(cid) for cid in self.monitored_categories.keys())}\n"
                f"Ausgeschlossen: {len(self.excluded_channel_ids)}\n"
                f"Rollen: {len(self.discord_rank_roles)}"
            ),
            inline=True,
        )
        embed.add_field(
            name="💾 Cache/State",
            value=(
                f"User-Ränge: {len(self.user_rank_cache)}\n"
                f"Guild-Rollen: {len(self.guild_roles_cache)}\n"
                f"Anker: {len(self.channel_anchors)}\n"
                f"Channel-Settings: {len(self.channel_settings)} (enabled: {enabled_cnt})"
            ),
            inline=True,
        )

        try:
            rn, rv = self.get_user_rank_from_roles(ctx.author)
            embed.add_field(name="🎯 Dein Rang", value=f"{rn} ({rv})", inline=True)
        except Exception as e:
            embed.add_field(name="🎯 Dein Rang", value=f"Fehler: {e}", inline=True)

        await ctx.send(embed=embed)

    @rank_command.command(name="anker")
    async def show_channel_anchors(self, ctx):
        embed = discord.Embed(
            title="🔗 Kanal-Anker Übersicht",
            description="Aktive Erst-User-Anker (DB-persistiert)",
            color=discord.Color.purple(),
        )

        if not self.channel_anchors:
            embed.description = "❌ Keine aktiven Kanal-Anker"
            await ctx.send(embed=embed)
            return

        lines: List[str] = []
        for ch_id, (user_id, rank_name, rank_value, amin, amax) in self.channel_anchors.items():
            ch = ctx.guild.get_channel(ch_id)
            user = ctx.guild.get_member(user_id)
            if not ch or not user:
                lines.append(f"❓ Veralteter Eintrag (Kanal {ch_id}, User {user_id})")
                continue
            min_rank = self.get_rank_name_from_value(amin)
            max_rank = self.get_rank_name_from_value(amax)
            cur_members = len([m for m in ch.members if not m.bot])
            lines.append(
                f"**{ch.name}**\n"
                f"🔗 Anker: {user.display_name} ({rank_name})\n"
                f"📊 Bereich: {min_rank}-{max_rank} ({amin}-{amax})\n"
                f"👥 Aktuelle User: {cur_members}\n"
            )
        embed.description = "\n".join(lines[:10])
        if len(lines) > 10:
            embed.set_footer(text=f"{len(lines) - 10} weitere …")
        await ctx.send(embed=embed)

    @rank_command.command(name="toggle")
    async def toggle_channel_system(self, ctx, action: str = None):
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("❌ Du musst in einem Voice Channel sein.")
            return
        channel = ctx.author.voice.channel

        if not self.is_monitored_channel(channel):
            await ctx.send(f"❌ **{channel.name}** wird nicht überwacht.")
            return

        current = self.is_channel_system_enabled(channel)
        if action is None:
            await ctx.send(f"🔧 Rang-System für **{channel.name}**: {'✅ Aktiviert' if current else '❌ Deaktiviert'}")
            return

        action_l = action.lower()
        if action_l in ["ein", "on", "aktivieren", "enable"]:
            if current:
                await ctx.send(f"ℹ️ Bereits aktiviert für **{channel.name}**.")
                return
            await self.set_channel_system_enabled(channel, True)
            await ctx.send(f"✅ Aktiviert: **{channel.name}**")
            await self.update_channel_permissions_via_roles(channel)
            await self.update_channel_name(channel)
        elif action_l in ["aus", "off", "deaktivieren", "disable"]:
            if not current:
                await ctx.send(f"ℹ️ Bereits deaktiviert für **{channel.name}**.")
                return
            await self.set_channel_system_enabled(channel, False)
            await ctx.send(f"❌ Deaktiviert: **{channel.name}**")
            await self.remove_channel_anchor(channel)
            await self.clear_role_permissions(channel)
        else:
            await ctx.send("❌ Verwende: `ein`/`on` oder `aus`/`off`")

    @rank_command.command(name="vcstatus")
    async def voice_channel_status(self, ctx):
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("❌ Du musst in einem Voice Channel sein.")
            return
        channel = ctx.author.voice.channel

        embed = discord.Embed(title=f"🔊 Status: {channel.name}", color=discord.Color.blue())
        embed.add_field(
            name="📊 Kanal-Info",
            value=f"ID: {channel.id}\nKategorie: {channel.category.name if channel.category else '–'}\nMitglieder: {len(channel.members)}",
            inline=True,
        )
        is_mon = self.is_monitored_channel(channel)
        embed.add_field(name="👁️ Überwachung", value="✅ Überwacht" if is_mon else "❌ Nicht überwacht", inline=True)

        if is_mon:
            sys_en = self.is_channel_system_enabled(channel)
            embed.add_field(name="🔧 Rang-System", value="✅ Aktiviert" if sys_en else "❌ Deaktiviert", inline=True)
            anchor = self.get_channel_anchor(channel)
            if anchor and sys_en:
                uid, rn, _rv, amin, amax = anchor
                user = ctx.guild.get_member(uid)
                min_rank = self.get_rank_name_from_value(amin)
                max_rank = self.get_rank_name_from_value(amax)
                embed.add_field(
                    name="🔗 Anker",
                    value=f"{user.display_name if user else uid} ({rn})\nBereich: {min_rank}-{max_rank}",
                    inline=False,
                )
            else:
                embed.add_field(name="🔗 Anker", value="Kein Anker gesetzt" if sys_en else "System deaktiviert", inline=False)

        await ctx.send(embed=embed)

    @rank_command.command(name="debug")
    async def debug_user_roles(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        try:
            self.user_rank_cache.pop(f"{member.id}:{member.guild.id}", None)
            user_roles = [(r.id, r.name) for r in member.roles]
            found = []
            for role in member.roles:
                if role.id in self.discord_rank_roles:
                    rn, rv = self.discord_rank_roles[role.id]
                    found.append(f"**{role.name}** (ID {role.id}) -> {rn} ({rv})")
            rn, rv = self.get_user_rank_from_roles(member)

            embed = discord.Embed(title=f"🔍 Debug: {member.display_name}", color=discord.Color.orange())
            embed.add_field(name="👤 User-Info", value=f"ID: {member.id}\nRollen: {len(member.roles)}", inline=True)
            embed.add_field(name="🎯 Erkannter Rang", value=f"**{rn}** ({rv})", inline=True)
            embed.add_field(name="🎭 Gefundene Rang-Rollen", value="\n".join(found) if found else "❌ Keine", inline=False)

            all_roles_text = "\n".join([f"{rid}: {name}" for rid, name in user_roles[:10]])
            if len(user_roles) > 10:
                all_roles_text += f"\n… und {len(user_roles)-10} weitere"
            embed.add_field(name="📋 Alle Rollen (erste 10)", value=all_roles_text, inline=False)

            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"debug_user_roles Fehler: {e}")
            await ctx.send(f"❌ Debug-Fehler: {e}")

    @rank_command.command(name="info")
    async def rank_info(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        try:
            rn, rv = self.get_user_rank_from_roles(member)
            embed = discord.Embed(title=f"🎭 Rang-Information: {member.display_name}", color=discord.Color.blue())
            embed.add_field(name="Höchster Rang", value=rn, inline=True)
            embed.add_field(name="Rang-Wert", value=rv, inline=True)
            if rn in self.balancing_rules:
                minus, plus = self.balancing_rules[rn]
                embed.add_field(name="Balancing-Regel", value=f"{minus:+d} bis {plus:+d} Ränge", inline=True)
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"rank_info Fehler: {e}")
            await ctx.send("❌ Fehler beim Abrufen der Rang-Information.")

    @rank_command.command(name="aktualisieren")
    @commands.has_permissions(manage_guild=True)
    async def force_update(self, ctx, channel: discord.VoiceChannel = None):
        if channel is None:
            if not ctx.author.voice or not ctx.author.voice.channel:
                await ctx.send("❌ In einem Sprachkanal sein oder Kanal angeben.")
                return
            channel = ctx.author.voice.channel

        if not self.is_monitored_channel(channel):
            await ctx.send("❌ Dieser Kanal wird nicht überwacht.")
            return

        try:
            self.user_rank_cache.clear()
            self.guild_roles_cache.clear()

            members_ranks = await self.get_channel_members_ranks(channel)
            if members_ranks:
                # alten Anker verwerfen & ersten User setzen (persistiert)
                await self.remove_channel_anchor(channel)
                first_member = next(iter(members_ranks.keys()))
                rn, rv = members_ranks[first_member]
                await self.set_channel_anchor(channel, first_member, rn, rv)

            await self.update_channel_permissions_via_roles(channel)
            await self.update_channel_name(channel)
            await ctx.send(f"✅ Kanal **{channel.name}** aktualisiert.")
        except Exception as e:
            logger.error(f"force_update Fehler: {e}")
            await ctx.send("❌ Fehler beim Aktualisieren des Kanals.")

    @rank_command.command(name="rollen")
    async def show_tracked_roles(self, ctx):
        embed = discord.Embed(
            title="🎭 Überwachte Rang-Rollen",
            description="Discord-Rollen für das Rang-System",
            color=discord.Color.gold(),
        )
        lines = []
        for role_id, (rn, rv) in self.discord_rank_roles.items():
            role = ctx.guild.get_role(role_id)
            if role:
                lines.append(f"**{rn}** ({rv}): {role.mention} – {len(role.members)} Mitglieder")
            else:
                lines.append(f"**{rn}** ({rv}): ❌ Rolle nicht gefunden (ID {role_id})")
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @rank_command.command(name="kanäle")
    async def show_channel_config(self, ctx):
        embed = discord.Embed(
            title="🔊 Kanal-Konfiguration",
            description="Sprachkanal-Überwachung",
            color=discord.Color.blue(),
        )
        for cat_id, mode in self.monitored_categories.items():
            category = ctx.guild.get_channel(cat_id)
            if not category:
                embed.add_field(
                    name=f"📁 Kategorie (ID {cat_id})",
                    value="❌ Kategorie nicht gefunden",
                    inline=False,
                )
                continue

            vcs = [c for c in category.channels if isinstance(c, discord.VoiceChannel)]
            monitored = [c for c in vcs if c.id not in self.excluded_channel_ids]
            embed.add_field(
                name=f"📁 {category.name} ({mode})",
                value=f"Gesamt: {len(vcs)}\nÜberwacht: {len(monitored)}",
                inline=False,
            )
        ex_lines = []
        for cid in self.excluded_channel_ids:
            ch = ctx.guild.get_channel(cid)
            ex_lines.append(f"🔇 {ch.name}" if ch else f"❓ Unbekannt (ID {cid})")
        if ex_lines:
            embed.add_field(name="🚫 Ausgeschlossene Kanäle", value="\n".join(ex_lines), inline=False)
        await ctx.send(embed=embed)

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Unzureichende Berechtigungen.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("❌ Ungültige Argumente.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("❌ Benutzer nicht gefunden.")
        else:
            logger.error(f"Unerwarteter Fehler in {ctx.command}: {error}")
            await ctx.send("❌ Ein unerwarteter Fehler ist aufgetreten.")


async def setup(bot):
    await bot.add_cog(RolePermissionVoiceManager(bot))
    logger.info("RolePermissionVoiceManager Cog hinzugefügt")


async def teardown(bot):
    try:
        cog = bot.get_cog("RolePermissionVoiceManager")
        if cog:
            await bot.remove_cog("RolePermissionVoiceManager")
        logger.info("RolePermissionVoiceManager Cog entfernt")
    except Exception as e:
        logger.error(f"Fehler beim Entfernen des RolePermissionVoiceManager Cogs: {e}")
