import asyncio
import logging
import re
import time
from pathlib import Path

import discord
from discord.ext import commands

from service import db
from service.config import settings
from service.db import db_path

try:
    from cogs.tempvoice.core import MINRANK_CATEGORY_IDS
except Exception:  # Fallback, falls TempVoice nicht geladen ist
    MINRANK_CATEGORY_IDS: set[int] = {1412804540994162789}  # Comp/Ranked

DB_PATH = Path(db_path())  # alias, damit alter Code weiterläuft

# Sub-Rang Score-System: score = tier * 6 + subrank (1-6)
# Initiate 1 = 7, Eternus 6 = 72
RANKED_SUBRANK_TOLERANCE = 9   # ±9 Sub-Rang-Punkte = ±1.5 Hauptränge
SCORE_MIN_ABSOLUTE = 7         # Initiate 1
SCORE_MAX_ABSOLUTE = 72        # Eternus 6

# Sub-Rang Rollen-Erkennung (unterstützt "Ascendant 3" und "Asc 3")
RANK_SHORT_NAMES = {
    "Initiate": "Ini",
    "Seeker": "See",
    "Alchemist": "Alc",
    "Arcanist": "Arc",
    "Ritualist": "Rit",
    "Emissary": "Emi",
    "Archon": "Arch",
    "Oracle": "Ora",
    "Phantom": "Pha",
    "Ascendant": "Asc",
    "Eternus": "Ete",
}
SHORT_NAME_TO_RANK = {v.casefold(): k for k, v in RANK_SHORT_NAMES.items()}
RANK_NAME_TO_VALUE = {
    "obscurus": 0,
    "initiate": 1,
    "seeker": 2,
    "alchemist": 3,
    "arcanist": 4,
    "ritualist": 5,
    "emissary": 6,
    "archon": 7,
    "oracle": 8,
    "phantom": 9,
    "ascendant": 10,
    "eternus": 11,
}

_RANK_NAMES_FOR_REGEX = list(RANK_NAME_TO_VALUE.keys()) + list(RANK_SHORT_NAMES.values())
_SUBRANK_PATTERN = "|".join(re.escape(n) for n in _RANK_NAMES_FOR_REGEX)
SUBRANK_ROLE_RE = re.compile(rf"^({_SUBRANK_PATTERN})\s+([1-6])$", re.IGNORECASE)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RolePermissionVoiceManager(commands.Cog):
    """Rollen-basierte Sprachkanal-Verwaltung über Discord-Rollen-Berechtigungen
    Persistenz (Toggle & Anker) über zentrale DB (service.db).
    """

    def __init__(self, bot):
        self.bot = bot
        # Nur Comp/Ranked wird überwacht (kein Ranked/Grind Split mehr)
        self.monitored_categories = {
            1412804540994162789: "lane",  # Comp/Ranked Lanes
        }

        # Ausnahme-Kanäle die NICHT überwacht werden sollen
        self.excluded_channel_ids = {
            1375933460841234514,
            1375934283931451512,
            1357422958544420944,
            1412804671432818890,
            1411391356278018245,  # TempVoice Fixed Lane (nicht umbenennen/überwachen)
            1470126503252721845,  # TempVoice Fixed Lane (nicht umbenennen/überwachen)
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

        # Balancing-Regeln (Rang -> (minus, plus)); Standard: ±1 (Ranked)
        self.balancing_rules = dict.fromkeys(self.deadlock_ranks.keys(), (-1, 1))

        # Cache: {user_id:guild_id: (rank_name, rank_value, subrank)}
        self.user_rank_cache: dict[str, tuple[str, int, int]] = {}
        self.guild_roles_cache: dict[int, dict[int, discord.Role]] = {}

        # Laufzeit-State (wird beim Start aus DB geladen)
        # {channel_id: (user_id, rank_name, rank_value, allowed_min, allowed_max, anchor_subrank, score_min, score_max)}
        self.channel_anchors: dict[int, tuple[int, str, int, int, int, int, int, int]] = {}
        # {channel_id: {"enabled": bool}}
        self.channel_settings: dict[int, dict[str, bool]] = {}
        # Nach Initial-Setup dürfen Permissions manuell angepasst werden
        self.channel_permissions_initialized: set[int] = set()

        # Rename throttle
        self._channel_rename_interval = max(60, settings.rank_vs_rename_cooldown_seconds)
        self._last_channel_rename: dict[int, float] = {}
        self._pending_channel_renames: dict[int, str] = {}
        self._rename_tasks: dict[int, asyncio.Task] = {}
        self._rename_tasks_lock = asyncio.Lock()  # Schützt _rename_tasks vor Race Conditions
        # Min delay between permission writes (PUT /permissions) and rename PATCH on same channel.
        self._post_permission_rename_delay_seconds = 5.0
        self._last_permission_write: dict[int, float] = {}
        self._startup_reconciled = False

    @staticmethod
    def _is_tempvoice_lane(channel: discord.VoiceChannel) -> bool:
        try:
            name = channel.name.lower()
        except Exception:
            return False
        # Erkennt "lane 1" ODER "ascendant 3" etc.
        is_basic_lane = name.startswith("lane ")
        is_rank_lane = any(name.startswith(f"{rn.lower()} ") for rn in RANK_NAME_TO_VALUE.keys())
        is_short_rank_lane = any(name.startswith(f"{sn.lower()} ") for sn in RANK_SHORT_NAMES.values())
        
        return channel.category_id in MINRANK_CATEGORY_IDS and (is_basic_lane or is_rank_lane or is_short_rank_lane)

    # -------------------- DB Layer --------------------

    async def _db_ensure_schema(self):
        # NOTE: Using central DB connection (autocommit)
        await db.execute_async(
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
        await db.execute_async(
            """
            CREATE TABLE IF NOT EXISTS voice_channel_anchors (
                channel_id   INTEGER PRIMARY KEY,
                guild_id     INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                rank_name    TEXT NOT NULL,
                rank_value   INTEGER NOT NULL,
                allowed_min  INTEGER NOT NULL,
                allowed_max  INTEGER NOT NULL,
                anchor_subrank INTEGER DEFAULT 3,
                score_min    INTEGER,
                score_max    INTEGER,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Migration: Neue Spalten für ältere Datenbanken hinzufügen
        try:
            info_rows = await db.query_all_async("PRAGMA table_info(voice_channel_anchors)")
            existing_cols = {row["name"] for row in info_rows} if info_rows else set()

            migrations = [
                ("anchor_subrank", "ALTER TABLE voice_channel_anchors ADD COLUMN anchor_subrank INTEGER DEFAULT 3"),
                ("score_min", "ALTER TABLE voice_channel_anchors ADD COLUMN score_min INTEGER"),
                ("score_max", "ALTER TABLE voice_channel_anchors ADD COLUMN score_max INTEGER"),
            ]

            for col_name, sql in migrations:
                if col_name not in existing_cols:
                    logger.info("Migriere voice_channel_anchors: Füge Spalte %s hinzu", col_name)
                    await db.execute_async(sql)
        except Exception as e:
            logger.error("Fehler bei Schema-Migration für voice_channel_anchors: %s", e)

    async def _db_load_state_for_guild(self, guild: discord.Guild):
        """Lädt Settings & Anker der Gilde in die In-Memory-Maps."""
        # (Sichergestellt via cog_load)

        # Settings
        rows = await db.query_all_async(
            "SELECT channel_id, enabled FROM voice_channel_settings WHERE guild_id=?",
            (guild.id,),
        )
        for r in rows:
            self.channel_settings[int(r["channel_id"])] = {"enabled": bool(r["enabled"])}

        # Anchors
        rows = await db.query_all_async(
            """
            SELECT channel_id, user_id, rank_name, rank_value, allowed_min, allowed_max,
                   anchor_subrank, score_min, score_max
            FROM voice_channel_anchors WHERE guild_id=?
            """,
            (guild.id,),
        )
        for r in rows:
            rank_value = int(r["rank_value"])
            anchor_subrank = int(r["anchor_subrank"] or 3)
            score_min_raw = r["score_min"]
            score_max_raw = r["score_max"]
            if score_min_raw is None:
                # Altdaten: Score aus Rang berechnen (Ranked-Toleranz als Standard)
                anchor_score = rank_value * 6 + anchor_subrank
                score_min = max(SCORE_MIN_ABSOLUTE, anchor_score - RANKED_SUBRANK_TOLERANCE)
                score_max = min(SCORE_MAX_ABSOLUTE, anchor_score + RANKED_SUBRANK_TOLERANCE)
            else:
                score_min = int(score_min_raw)
                score_max = int(score_max_raw)
            self.channel_anchors[int(r["channel_id"])] = (
                int(r["user_id"]),
                str(r["rank_name"]),
                rank_value,
                int(r["allowed_min"]),
                int(r["allowed_max"]),
                anchor_subrank,
                score_min,
                score_max,
            )

    async def _db_upsert_setting(self, channel: discord.VoiceChannel, enabled: bool):
        await db.execute_async(
            """
            INSERT INTO voice_channel_settings(channel_id, guild_id, enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                enabled=excluded.enabled,
                updated_at=CURRENT_TIMESTAMP
            """,
            (channel.id, channel.guild.id, int(enabled)),
        )

    async def _db_upsert_anchor(
        self,
        channel: discord.VoiceChannel,
        user_id: int,
        rank_name: str,
        rank_value: int,
        allowed_min: int,
        allowed_max: int,
        anchor_subrank: int,
        score_min: int,
        score_max: int,
    ):
        await db.execute_async(
            """
            INSERT INTO voice_channel_anchors(channel_id, guild_id, user_id, rank_name, rank_value, allowed_min, allowed_max, anchor_subrank, score_min, score_max)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                user_id=excluded.user_id,
                rank_name=excluded.rank_name,
                rank_value=excluded.rank_value,
                allowed_min=excluded.allowed_min,
                allowed_max=excluded.allowed_max,
                anchor_subrank=excluded.anchor_subrank,
                score_min=excluded.score_min,
                score_max=excluded.score_max,
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
                anchor_subrank,
                score_min,
                score_max,
            ),
        )

    async def _db_delete_anchor(self, channel: discord.VoiceChannel):
        await db.execute_async(
            "DELETE FROM voice_channel_anchors WHERE channel_id=?", (channel.id,)
        )

    # -------------------- Role Parsing --------------------

    def _parse_subrank_role_name(self, role_name: str) -> tuple[int, int] | None:
        """Extrahiert Rang-Wert und Sub-Rang aus einem Rollennamen."""
        match = SUBRANK_ROLE_RE.fullmatch(str(role_name or "").strip())
        if not match:
            return None
        name_part = match.group(1).casefold()

        # Check full name first
        rank_value = RANK_NAME_TO_VALUE.get(name_part)

        # Then check short name mapping
        if rank_value is None:
            full_name = SHORT_NAME_TO_RANK.get(name_part)
            if full_name:
                rank_value = RANK_NAME_TO_VALUE.get(full_name.casefold())

        if rank_value is None:
            return None
        try:
            subrank = int(match.group(2))
        except (TypeError, ValueError):
            return None
        if subrank < 1 or subrank > 6:
            return None
        return rank_value, subrank

    # -------------------- Sub-Rang DB-Abfragen --------------------

    async def get_user_subrank_from_db(self, member: discord.Member) -> int:
        """Liest Sub-Rang des primären Steam-Accounts aus DB. Gibt 3 zurück wenn kein Link."""
        try:
            row = await db.query_one_async(
                "SELECT deadlock_subrank FROM steam_links "
                "WHERE user_id=? AND deadlock_rank IS NOT NULL AND deadlock_rank > 0 "
                "ORDER BY primary_account DESC, updated_at DESC LIMIT 1",
                (member.id,),
            )
            if row and row["deadlock_subrank"] is not None:
                return max(1, min(6, int(row["deadlock_subrank"])))
        except Exception as e:
            logger.warning("get_user_subrank_from_db Fehler für %s: %s", member.id, e)
        return 3  # Mitte als Fallback

    async def get_user_score_from_db(self, member: discord.Member) -> int | None:
        """Liest Gesamt-Score des primären Steam-Accounts. Gibt None zurück wenn kein Link."""
        try:
            row = await db.query_one_async(
                "SELECT deadlock_rank, deadlock_subrank FROM steam_links "
                "WHERE user_id=? AND deadlock_rank IS NOT NULL AND deadlock_rank > 0 "
                "ORDER BY primary_account DESC, updated_at DESC LIMIT 1",
                (member.id,),
            )
            if row and row["deadlock_rank"] is not None:
                tier = int(row["deadlock_rank"])
                subrank = max(1, min(6, int(row["deadlock_subrank"] or 3)))
                return tier * 6 + subrank
        except Exception as e:
            logger.warning("get_user_score_from_db Fehler für %s: %s", member.id, e)
        return None

    # -------------------- Lifecycle --------------------

    async def cog_load(self):
        try:
            # Schema sicherstellen
            await self._db_ensure_schema()

            # Bei Start für alle bekannten Guilds laden
            for guild in self.bot.guilds:
                await self._db_load_state_for_guild(guild)

            logger.info("RolePermissionVoiceManager Cog geladen")
            monitored_list = ", ".join(str(cid) for cid in self.monitored_categories.keys())
            logger.info(f"   Überwachte Kategorien: {monitored_list}")
            logger.info(f"   Überwachte Rollen: {len(self.discord_rank_roles)}")
            logger.info(f"   Ausgeschlossene Kanäle: {len(self.excluded_channel_ids)}")
            logger.info("   🔧 Persistenz: zentrale DB (Settings & Anker)")
        except Exception as e:
            logger.error(f"Fehler beim Laden des RolePermissionVoiceManager Cogs: {e}")
            raise

    async def cog_unload(self):
        try:
            self.user_rank_cache.clear()
            self.guild_roles_cache.clear()
            self.channel_anchors.clear()
            self.channel_settings.clear()
            for task in list(self._rename_tasks.values()):
                try:
                    task.cancel()
                except Exception as exc:
                    logger.debug("Failed to cancel rename task: %s", exc, exc_info=True)
            self._rename_tasks.clear()
            self._pending_channel_renames.clear()
            self._last_permission_write.clear()
            self._startup_reconciled = False
            logger.info("RolePermissionVoiceManager Cog entladen")
        except Exception as e:
            logger.error(f"Fehler beim Entladen des RolePermissionVoiceManager Cogs: {e}")

    # -------------------- Helpers --------------------

    def get_guild_roles(self, guild: discord.Guild) -> dict[int, discord.Role]:
        if guild.id not in self.guild_roles_cache:
            self.guild_roles_cache[guild.id] = {role.id: role for role in guild.roles}
        return self.guild_roles_cache[guild.id]

    def get_user_rank_from_roles(self, member: discord.Member) -> tuple[str, int, int | None]:
        cache_key = f"{member.id}:{member.guild.id}"
        if cache_key in self.user_rank_cache:
            return self.user_rank_cache[cache_key]

        highest_rank_name = "Obscurus"
        highest_rank_value = 0
        found_subrank = None
        highest_score = -1

        for role in member.roles:
            # 1. Check for subrank roles (e.g. "Asc 3" or "Ascendant 3")
            match = SUBRANK_ROLE_RE.fullmatch(role.name.strip())
            if match:
                name_part = match.group(1).casefold()
                sub = int(match.group(2))

                # Resolve base rank
                base_name = None
                rank_val = RANK_NAME_TO_VALUE.get(name_part)
                if rank_val is not None:
                    base_name = name_part.capitalize()
                else:
                    full_name = SHORT_NAME_TO_RANK.get(name_part)
                    if full_name:
                        rank_val = RANK_NAME_TO_VALUE.get(full_name.casefold())
                        base_name = full_name.capitalize()

                if rank_val is not None:
                    # Score calculation: tier * 6 + subrank (1-6)
                    score = rank_val * 6 + sub
                    if score > highest_score:
                        highest_score = score
                        highest_rank_name = base_name
                        highest_rank_value = rank_val
                        found_subrank = sub
                    continue

            # 2. Check for traditional major tier roles
            if role.id in self.discord_rank_roles:
                rank_name, rank_value = self.discord_rank_roles[role.id]
                # Major tier roles get a score based on tier only
                score = rank_value * 6
                if score > highest_score:
                    highest_score = score
                    highest_rank_name = rank_name
                    highest_rank_value = rank_value
                    # We don't set found_subrank here, will be fetched from DB later if needed

        result = (highest_rank_name, highest_rank_value, found_subrank)
        self.user_rank_cache[cache_key] = result
        return result

    async def get_channel_members_ranks(
        self, channel: discord.VoiceChannel
    ) -> dict[discord.Member, tuple[str, int, int]]:
        members_ranks: dict[discord.Member, tuple[str, int, int]] = {}
        for member in channel.members:
            if member.bot:
                continue
            rn, rv, rs = self.get_user_rank_from_roles(member)
            if rs is None:
                rs = await self.get_user_subrank_from_db(member)
            members_ranks[member] = (rn, rv, rs)
        return members_ranks

    def calculate_balancing_range_from_anchor(
        self, channel: discord.VoiceChannel
    ) -> tuple[int, int]:
        anchor = self.get_channel_anchor(channel)
        if anchor is None:
            return 0, 11
        _user_id, _rank_name, _rank_value, allowed_min, allowed_max, *_ = anchor
        return allowed_min, allowed_max

    def get_allowed_role_ids(self, allowed_min: int, allowed_max: int) -> set[int]:
        return {
            role_id
            for role_id, (_rn, rv) in self.discord_rank_roles.items()
            if allowed_min <= rv <= allowed_max
        }

    def _mark_permission_write(self, channel_id: int) -> None:
        self._last_permission_write[int(channel_id)] = time.monotonic()

    def _permission_to_rename_delay_remaining(self, channel_id: int) -> float:
        if self._post_permission_rename_delay_seconds <= 0:
            return 0.0
        last_write = self._last_permission_write.get(int(channel_id))
        if last_write is None:
            return 0.0
        elapsed = time.monotonic() - last_write
        return max(0.0, self._post_permission_rename_delay_seconds - elapsed)

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
                self._mark_permission_write(channel.id)
            return True
        except discord.NotFound:
            return False
        except Exception as e:
            logger.error(f"@everyone setzen fehlgeschlagen: {e}")
            return False

    async def reset_everyone_connect(self, channel: discord.VoiceChannel) -> bool:
        """Entfernt den Connect-Deny f�r @everyone, damit neue Anker den Kanal wieder betreten k�nnen."""
        try:
            if not await self.channel_exists(channel):
                return False
            everyone_role = channel.guild.default_role
            ow = channel.overwrites_for(everyone_role)
            changed = False
            if ow.connect is not None:
                ow.connect = None
                changed = True
            if ow.view_channel is None:
                ow.view_channel = True
                changed = True
            if changed:
                await channel.set_permissions(everyone_role, overwrite=ow)
                self._mark_permission_write(channel.id)
            return True
        except discord.NotFound:
            return False
        except Exception as e:
            logger.error(f"@everyone Reset fehlgeschlagen: {e}")
            return False

    async def channel_exists(self, channel: discord.VoiceChannel) -> bool:
        try:
            fresh = channel.guild.get_channel(channel.id)
            return isinstance(fresh, discord.VoiceChannel)
        except Exception:
            return False

    async def update_channel_permissions_via_roles(
        self, channel: discord.VoiceChannel, force: bool = False
    ):
        try:
            if not await self.channel_exists(channel):
                return

            if not self.is_channel_system_enabled(channel):
                return

            members_ranks = await self.get_channel_members_ranks(channel)
            # Falls force=True (z.B. bei Channel-Erstellung), machen wir weiter auch wenn Cache noch leer ist
            if not members_ranks and not force:
                # leer -> Anker entfernen + Rollen-Overwrites entfernen
                await self.remove_channel_anchor(channel)
                await self.reset_everyone_connect(channel)
                await self.clear_role_permissions(channel)
                self.channel_permissions_initialized.discard(channel.id)
                return

            if not force and channel.id in self.channel_permissions_initialized:
                return

            anchor = self.get_channel_anchor(channel)
            if not anchor:
                logger.debug(f"Kein Anker für {channel.name} ({channel.id}) gefunden.")
                return

            # Score-Bereich abrufen (±9 Punkte = ±1.5 Tiers)
            _uid, _rn, _rv, _amin, _amax, _asub, score_min, score_max = anchor
            logger.info(f"Update Permissions (Batch) für {channel.name}: Score {score_min}-{score_max}")

            # 1. Aktuelle Overwrites kopieren
            new_overwrites = dict(channel.overwrites)
            
            # 2. @everyone Deny setzen
            everyone_role = channel.guild.default_role
            everyone_ow = new_overwrites.get(everyone_role, discord.PermissionOverwrite())
            everyone_ow.connect = False
            everyone_ow.view_channel = True
            new_overwrites[everyone_role] = everyone_ow

            # 3. Alle Rollen der Gilde prüfen, welche in den Score-Bereich fallen
            allowed_role_ids = set()
            major_role_ids = set(self.discord_rank_roles.keys())
            
            # Wir iterieren über alle Rollen der Gilde, um Sub-Ränge zu finden
            for role in channel.guild.roles:
                parsed = self._parse_subrank_role_name(role.name)
                if parsed:
                    rv, rs = parsed
                    score = rv * 6 + rs
                    if score_min <= score <= score_max:
                        allowed_role_ids.add(role.id)
                        # Zu Overwrites hinzufügen
                        new_overwrites[role] = discord.PermissionOverwrite(
                            connect=True, speak=True, view_channel=True
                        )

            # 4. Alte/Nicht erlaubte Rollen aus dem Batch entfernen
            # Wir prüfen die existierenden Overwrites und werfen raus was nicht mehr erlaubt ist
            for target in list(new_overwrites.keys()):
                if isinstance(target, discord.Role) and target.id != everyone_role.id:
                    is_major = target.id in major_role_ids
                    is_subrank = self._parse_subrank_role_name(target.name) is not None
                    
                    if (is_major or is_subrank) and target.id not in allowed_role_ids:
                        # Aus den Overwrites entfernen
                        new_overwrites.pop(target)

            # 5. Alles in EINEM Call an Discord senden
            try:
                await channel.edit(overwrites=new_overwrites, reason="Rank System: Batch Permission Update")
                self._mark_permission_write(channel.id)
            except discord.HTTPException as e:
                logger.error(f"Batch Permission Update fehlgeschlagen für {channel.name}: {e}")
                # Fallback: Falls Batch fehlschlägt (selten), versuchen wir es einzeln
                await self._fallback_individual_permissions(channel, allowed_role_ids, major_role_ids)

            self.channel_permissions_initialized.add(channel.id)

        except Exception as e:
            logger.error(f"update_channel_permissions_via_roles Fehler: {e}")

    async def _fallback_individual_permissions(self, channel, allowed_role_ids, major_role_ids):
        """Fallback-Methode falls der Batch-Edit fehlschlägt."""
        logger.info(f"Starte Fallback-Einzel-Update für {channel.name}")
        # everyone deny
        await self.set_everyone_deny_connect(channel)
        
        # Erlaubte einzeln setzen
        for rid in allowed_role_ids:
            role = channel.guild.get_role(rid)
            if role:
                await channel.set_permissions(role, connect=True, speak=True, view_channel=True)
                await asyncio.sleep(0.2)
                
        # Nicht erlaubte einzeln entfernen
        for target, ow in list(channel.overwrites.items()):
            if isinstance(target, discord.Role) and target.id != channel.guild.default_role.id:
                if (target.id in major_role_ids or self._parse_subrank_role_name(target.name)) and target.id not in allowed_role_ids:
                    await channel.set_permissions(target, overwrite=None)
                    await asyncio.sleep(0.2)

    async def remove_disallowed_role_permissions(
        self, channel: discord.VoiceChannel, allowed_role_ids: set[int]
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
                    self._mark_permission_write(channel.id)
                    await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"remove_disallowed_role_permissions Fehler: {e}")

    async def clear_role_permissions(self, channel: discord.VoiceChannel):
        try:
            major_role_ids = set(self.discord_rank_roles.keys())
            for target, _ow in list(channel.overwrites.items()):
                if (
                    isinstance(target, discord.Role)
                    and target.id != channel.guild.default_role.id
                ):
                    is_major = target.id in major_role_ids
                    is_subrank = self._parse_subrank_role_name(target.name) is not None
                    
                    if is_major or is_subrank:
                        await channel.set_permissions(target, overwrite=None)
                        self._mark_permission_write(channel.id)
                        await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"clear_role_permissions Fehler: {e}")

    async def update_channel_name(self, channel: discord.VoiceChannel, *, force: bool = False):
        try:
            if not await self.channel_exists(channel):
                return

            mode = self.get_channel_mode(channel)

            if mode is None:
                return

            # TempVoice lanes only get renamed when we manage them via the rank system (ranked/grind).
            # Regular lanes keep their TempVoice naming.
            if self._is_tempvoice_lane(channel) and mode != "lane":
                return

            members_ranks = await self.get_channel_members_ranks(channel)
            if not members_ranks:
                new_name = "Rang-Sprachkanal"
            else:
                anchor = self.get_channel_anchor(channel)
                if anchor:
                    (
                        _uid,
                        anchor_rank_name,
                        anchor_rank_value,
                        allowed_min,
                        allowed_max,
                        anchor_subrank,
                        score_min,
                        score_max,
                    ) = anchor
                    new_name = f"{anchor_rank_name} {anchor_subrank}"
                else:
                    # Fallback: erster User
                    first_member = next(iter(members_ranks.keys()))
                    rank_name, _rv2, subrank = members_ranks[first_member]
                    new_name = f"{rank_name} {subrank}"

            # Prüfe zuerst ob Name schon passt - vermeidet redundante API-Calls
            if channel.name == new_name:
                # Cleanup: Entferne pending renames und tasks für diesen Channel
                self._pending_channel_renames.pop(channel.id, None)
                existing_task = self._rename_tasks.pop(channel.id, None)
                if existing_task and not existing_task.done():
                    existing_task.cancel()
                return

            now = time.monotonic()
            required_delay = 0.0
            if not force:
                last_rename = self._last_channel_rename.get(channel.id)
                if last_rename is not None:
                    elapsed = now - last_rename
                    if elapsed < self._channel_rename_interval:
                        required_delay = max(
                            required_delay, self._channel_rename_interval - elapsed
                        )
                        logger.debug(
                            "rename cooldown active for %s (%.1fs remaining)",
                            channel.name,
                            required_delay,
                        )
            perm_delay = self._permission_to_rename_delay_remaining(channel.id)
            if perm_delay > 0:
                required_delay = max(required_delay, perm_delay)
                logger.debug(
                    "rename delayed after permission update for %s (%.1fs remaining)",
                    channel.name,
                    perm_delay,
                )
            if required_delay > 0:
                self._schedule_delayed_channel_rename(channel, new_name, required_delay)
                return

            pending_task = self._rename_tasks.pop(channel.id, None)
            if pending_task and not pending_task.done():
                pending_task.cancel()
            self._pending_channel_renames.pop(channel.id, None)

            await self.bot.queue_channel_rename(
                channel.id, new_name, reason="Rank Voice Manager Rename"
            )
            self._last_channel_rename[channel.id] = now
        except discord.NotFound:
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"update_channel_name Fehler: {e}")

    def _schedule_delayed_channel_rename(
        self, channel: discord.VoiceChannel, new_name: str, delay: float
    ) -> None:
        self._pending_channel_renames[channel.id] = new_name
        delay_seconds = max(0.0, float(delay))

        # Check existing task (race-safe mit try-lock pattern)
        existing = self._rename_tasks.get(channel.id)
        if existing and not existing.done():
            return

        async def _apply_later():
            try:
                await asyncio.sleep(delay_seconds)
                latest_name = self._pending_channel_renames.get(channel.id)
                if not latest_name:
                    return
                if not await self.channel_exists(channel):
                    return
                post_perm_delay = self._permission_to_rename_delay_remaining(channel.id)
                if post_perm_delay > 0:
                    await asyncio.sleep(post_perm_delay)
                    latest_name = self._pending_channel_renames.get(channel.id)
                    if not latest_name:
                        return
                    if not await self.channel_exists(channel):
                        return
                await self.bot.queue_channel_rename(
                    channel.id, latest_name, reason="Rank Voice Manager Delayed Rename"
                )
                self._last_channel_rename[channel.id] = time.monotonic()
            except asyncio.CancelledError:
                raise
            except discord.NotFound:
                return
            except Exception as exc:
                logger.error(
                    "Verzögerter Channel-Rename fehlgeschlagen (%s): %s",
                    channel.id,
                    exc,
                )
            finally:
                self._pending_channel_renames.pop(channel.id, None)
                # Lock-geschütztes Remove (Race-Safe!)
                async with self._rename_tasks_lock:
                    self._rename_tasks.pop(channel.id, None)

        # Lock-geschütztes Add (Race-Safe!)
        async def _add_task():
            async with self._rename_tasks_lock:
                # Double-check nach Lock
                existing = self._rename_tasks.get(channel.id)
                if existing and not existing.done():
                    return
                self._rename_tasks[channel.id] = asyncio.create_task(_apply_later())

        asyncio.create_task(_add_task())

    def get_rank_name_from_value(self, rank_value: int) -> str:
        for rn, val in self.deadlock_ranks.items():
            if val == rank_value:
                return rn
        return "Obscurus"

    async def set_channel_anchor(
        self,
        channel: discord.VoiceChannel,
        user: discord.Member,
        rank_name: str,
        rank_value: int,
        anchor_subrank: int = 3,
    ):
        mode = self.get_channel_mode(channel)

        tolerance = RANKED_SUBRANK_TOLERANCE

        anchor_score = rank_value * 6 + anchor_subrank
        score_min = max(SCORE_MIN_ABSOLUTE, anchor_score - tolerance)
        score_max = min(SCORE_MAX_ABSOLUTE, anchor_score + tolerance)

        # Grobe Discord-Rollen-Berechtigungen: welche Haupt-Rang-Tiers überlappen mit Score-Bereich
        allowed_min = max(1, (score_min - 1) // 6)
        allowed_max = min(11, (score_max - 1) // 6)

        self.channel_permissions_initialized.discard(channel.id)
        self.channel_anchors[channel.id] = (
            user.id,
            rank_name,
            rank_value,
            allowed_min,
            allowed_max,
            anchor_subrank,
            score_min,
            score_max,
        )
        await self._db_upsert_anchor(
            channel, user.id, rank_name, rank_value, allowed_min, allowed_max,
            anchor_subrank, score_min, score_max,
        )
        logger.info(
            f"🔗 Anker gesetzt für {channel.name}: {user.display_name} ({rank_name} {anchor_subrank}) "
            f"→ Score {score_min}-{score_max} (Tiers {allowed_min}-{allowed_max})"
        )

    def get_channel_anchor(
        self, channel: discord.VoiceChannel
    ) -> tuple[int, str, int, int, int] | None:
        return self.channel_anchors.get(channel.id)

    async def _ensure_valid_anchor(
        self,
        channel: discord.VoiceChannel,
        members_ranks: dict[discord.Member, tuple[str, int, int]],
    ) -> bool:
        """Stellt sicher, dass der Anker zu einem aktuell anwesenden Member gehört."""
        anchor = self.get_channel_anchor(channel)
        if not members_ranks:
            if anchor is not None:
                await self.remove_channel_anchor(channel)
                return True
            return False

        if anchor is not None:
            anchor_user_id = int(anchor[0])
            if any(int(member.id) == anchor_user_id for member in members_ranks):
                return False
            logger.info(
                "🔄 Anker in %s ungültig (owner=%s nicht im Channel) – setze neu.",
                channel.name,
                anchor_user_id,
            )

        first_member = next(iter(members_ranks.keys()))
        rank_name, rank_value, subrank = members_ranks[first_member]
        await self.set_channel_anchor(channel, first_member, rank_name, rank_value, subrank)
        return True

    async def remove_channel_anchor(self, channel: discord.VoiceChannel):
        if channel.id in self.channel_anchors:
            old = self.channel_anchors.pop(channel.id)
            logger.info(f"🔗 Anker entfernt für {channel.name}: {old[1]} {old[5]} ({old[2]})")
            await self._db_delete_anchor(channel)

    def is_channel_system_enabled(self, channel: discord.VoiceChannel) -> bool:
        return self.channel_settings.get(channel.id, {}).get("enabled", True)

    async def set_channel_system_enabled(self, channel: discord.VoiceChannel, enabled: bool):
        self.channel_settings.setdefault(channel.id, {})["enabled"] = enabled
        if enabled:
            self.channel_permissions_initialized.discard(channel.id)
        await self._db_upsert_setting(channel, enabled)
        logger.info(
            f"🔧 Rang-System für {channel.name} {'aktiviert' if enabled else 'deaktiviert'}"
        )

    # -------------------- Monitoring --------------------

    def is_monitored_channel(self, channel: discord.VoiceChannel) -> bool:
        if channel.id in self.excluded_channel_ids:
            return False
        return channel.category_id in self.monitored_categories if channel.category else False

    def get_channel_mode(self, channel: discord.VoiceChannel) -> str | None:
        if channel.category:
            return self.monitored_categories.get(channel.category_id)
        return None

    async def _reconcile_live_channels(self, guild: discord.Guild):
        """Synchronisiert beim Start vorhandene Voice-Channels mit dem aktuellen Anchor-State."""
        for channel in guild.voice_channels:
            if not self.is_monitored_channel(channel):
                continue
            if not self.is_channel_system_enabled(channel):
                continue
            members_ranks = await self.get_channel_members_ranks(channel)
            if not members_ranks and self.get_channel_anchor(channel) is None:
                continue
            anchor_changed = await self._ensure_valid_anchor(channel, members_ranks)
            await self.update_channel_permissions_via_roles(channel, force=anchor_changed)
            await self.update_channel_name(channel, force=anchor_changed)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        # Falls der Bot später hinzugefügt wird – Lade DB-Status für diese Guild
        try:
            await self._db_load_state_for_guild(guild)
        except Exception as e:
            logger.warning(f"on_guild_join load state failed: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        if self._startup_reconciled:
            return
        self._startup_reconciled = True
        for guild in self.bot.guilds:
            try:
                # Sicherstellen, dass der DB-Status für diese Guild geladen wurde (falls cog_load zu früh war)
                await self._db_load_state_for_guild(guild)
                await self._reconcile_live_channels(guild)
            except Exception as e:
                logger.warning("on_ready reconcile failed for guild %s: %s", guild.id, e)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
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

            members_ranks = await self.get_channel_members_ranks(channel)
            if not members_ranks:
                return
            anchor_changed = await self._ensure_valid_anchor(channel, members_ranks)
            
            # Rank-Info für den beitretenden Member (3er-Tupel)
            member_info = members_ranks.get(member)
            if not member_info:
                rn, rv, rs = self.get_user_rank_from_roles(member)
                member_info = (rn, rv, rs or 3)
            
            rank_name, rank_value, _subrank = member_info
            anchor = self.get_channel_anchor(channel)

            if anchor:
                _uid, _arname, _arval, allowed_min, allowed_max, _asubrank, score_min, score_max = anchor
                # Nur logs – niemals kicken
                if not (allowed_min <= rank_value <= allowed_max):
                    logger.info(
                        f"ℹ️ {member.display_name} ({rank_name}) Haupt-Rang außerhalb {allowed_min}-{allowed_max}, bleibt aber."
                    )
                elif member.id != _uid:
                    user_score = await self.get_user_score_from_db(member)
                    if user_score is not None and not (score_min <= user_score <= score_max):
                        logger.info(
                            f"ℹ️ {member.display_name} (Score {user_score}) außerhalb Sub-Rang-Bereich "
                            f"{score_min}-{score_max} in {channel.name}, bleibt aber."
                        )

            await self.update_channel_permissions_via_roles(channel, force=anchor_changed)
            await self.update_channel_name(channel, force=anchor_changed)

        except Exception as e:
            logger.error(f"handle_voice_join Fehler: {e}")

    async def handle_voice_leave(self, member: discord.Member, channel: discord.VoiceChannel):
        try:
            await asyncio.sleep(1)  # etwas Luft für Discord-Events

            if not self.is_channel_system_enabled(channel):
                return

            members_ranks = await self.get_channel_members_ranks(channel)
            anchor_changed = await self._ensure_valid_anchor(channel, members_ranks)

            await self.update_channel_permissions_via_roles(channel, force=anchor_changed)
            await self.update_channel_name(channel, force=anchor_changed)

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
            rn, rv, rs = self.get_user_rank_from_roles(ctx.author)
            sub_txt = f" {rs}" if rs else ""
            embed.add_field(name="🎯 Dein Rang", value=f"{rn}{sub_txt} ({rv})", inline=True)
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

        lines: list[str] = []
        for ch_id, (
            user_id,
            rank_name,
            _rank_value,
            amin,
            amax,
            anchor_subrank,
            score_min,
            score_max,
        ) in self.channel_anchors.items():
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
                f"🔗 Anker: {user.display_name} ({rank_name} {anchor_subrank})\n"
                f"📊 Tiers: {min_rank}–{max_rank} | Score: {score_min}–{score_max}\n"
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
            await ctx.send(
                f"🔧 Rang-System für **{channel.name}**: {'✅ Aktiviert' if current else '❌ Deaktiviert'}"
            )
            return

        action_l = action.lower()
        if action_l in ["ein", "on", "aktivieren", "enable"]:
            if current:
                await ctx.send(f"ℹ️ Bereits aktiviert für **{channel.name}**.")
                return
            await self.set_channel_system_enabled(channel, True)
            await ctx.send(f"✅ Aktiviert: **{channel.name}**")
            await self.update_channel_permissions_via_roles(channel)
            await self.update_channel_name(channel, force=True)
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
        embed.add_field(
            name="👁️ Überwachung",
            value="✅ Überwacht" if is_mon else "❌ Nicht überwacht",
            inline=True,
        )

        if is_mon:
            sys_en = self.is_channel_system_enabled(channel)
            embed.add_field(
                name="🔧 Rang-System",
                value="✅ Aktiviert" if sys_en else "❌ Deaktiviert",
                inline=True,
            )
            anchor = self.get_channel_anchor(channel)
            if anchor and sys_en:
                uid, rn, _rv, amin, amax, asubrank, smin, smax = anchor
                user = ctx.guild.get_member(uid)
                min_rank = self.get_rank_name_from_value(amin)
                max_rank = self.get_rank_name_from_value(amax)
                embed.add_field(
                    name="🔗 Anker",
                    value=(
                        f"{user.display_name if user else uid} ({rn} {asubrank})\n"
                        f"Tiers: {min_rank}–{max_rank}\n"
                        f"Score: {smin}–{smax}"
                    ),
                    inline=False,
                )
            else:
                embed.add_field(
                    name="🔗 Anker",
                    value="Kein Anker gesetzt" if sys_en else "System deaktiviert",
                    inline=False,
                )

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
            rn, rv, rs = self.get_user_rank_from_roles(member)
            sub_txt = f" {rs}" if rs else ""

            embed = discord.Embed(
                title=f"🔍 Debug: {member.display_name}", color=discord.Color.orange()
            )
            embed.add_field(
                name="👤 User-Info",
                value=f"ID: {member.id}\nRollen: {len(member.roles)}",
                inline=True,
            )
            embed.add_field(name="🎯 Erkannter Rang", value=f"**{rn}{sub_txt}** ({rv})", inline=True)
            embed.add_field(
                name="🎭 Gefundene Rang-Rollen",
                value="\n".join(found) if found else "❌ Keine",
                inline=False,
            )

            all_roles_text = "\n".join([f"{rid}: {name}" for rid, name in user_roles[:10]])
            if len(user_roles) > 10:
                all_roles_text += f"\n… und {len(user_roles) - 10} weitere"
            embed.add_field(name="📋 Alle Rollen (erste 10)", value=all_roles_text, inline=False)

            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"debug_user_roles Fehler: {e}")
            await ctx.send(f"❌ Debug-Fehler: {e}")

    @rank_command.command(name="info")
    async def rank_info(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        try:
            rn, rv, rs = self.get_user_rank_from_roles(member)
            sub_txt = f" {rs}" if rs else ""
            embed = discord.Embed(
                title=f"🎭 Rang-Information: {member.display_name}",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Höchster Rang", value=f"{rn}{sub_txt}", inline=True)
            embed.add_field(name="Rang-Wert", value=rv, inline=True)
            if rn in self.balancing_rules:
                minus, plus = self.balancing_rules[rn]
                embed.add_field(
                    name="Balancing-Regel",
                    value=f"{minus:+d} bis {plus:+d} Ränge",
                    inline=True,
                )
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
                rn, rv, rs = members_ranks[first_member]
                await self.set_channel_anchor(channel, first_member, rn, rv, rs)

            await self.update_channel_permissions_via_roles(channel, force=True)
            await self.update_channel_name(channel, force=True)
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
            embed.add_field(
                name="🚫 Ausgeschlossene Kanäle",
                value="\n".join(ex_lines),
                inline=False,
            )
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
