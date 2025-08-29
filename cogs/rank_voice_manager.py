import discord
from discord.ext import commands
import asyncio
import logging
from typing import Dict, Tuple, List, Optional, Set
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RolePermissionVoiceManager(commands.Cog):
    """Rollen-basierte Sprachkanal-Verwaltung über Discord-Rollen-Berechtigungen"""

    def __init__(self, bot):
        self.bot = bot
        self.monitored_category_id = 1357422957017698478
        
        # Ausnahme-Kanäle die NICHT überwacht werden sollen
        self.excluded_channel_ids = {
            1375933460841234514,
            1375934283931451512,
            1357422958544420944
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
            1331458087349129296: ("Eternus", 11)
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
            "Eternus": 11
        }

        # Balancing-Regeln (Rang -> (minus, plus))
        self.balancing_rules = {
            "Initiate": (-2, 2),
            "Seeker": (-2, 2),
            "Alchemist": (-2, 2),
            "Arcanist": (-2, 2),
            "Ritualist": (-2, 2),
            "Emissary": (-2, 1),
            "Archon": (-1, 1),
            "Oracle": (-1, 2),
            "Phantom": (-1, 2),
            "Ascendant": (-1, 1),
            "Eternus": (-1, 1)
        }

        # Cache für Performance
        self.user_rank_cache = {}
        self.guild_roles_cache = {}
        
        # Channel-Anker System: Speichert ersten User pro Kanal
        self.channel_anchors = {}  # {channel_id: (user_id, rank_name, rank_value, allowed_min, allowed_max)}
        
        # Channel-spezifische Einstellungen
        self.channel_settings = {}  # {channel_id: {"enabled": True/False}}

    async def cog_load(self):
        """Wird beim Laden des Cogs aufgerufen"""
        try:
            logger.info("RolePermissionVoiceManager Cog erfolgreich geladen")
            print(f"✅ RolePermissionVoiceManager Cog geladen")
            print(f"   Überwachte Kategorie: {self.monitored_category_id}")
            print(f"   Überwachte Rollen: {len(self.discord_rank_roles)}")
            print(f"   Ausgeschlossene Kanäle: {len(self.excluded_channel_ids)}")
            print(f"   🔧 Arbeitet mit Rollen-Berechtigungen (nicht User-Berechtigungen)")
        except Exception as e:
            logger.error(f"Fehler beim Laden des RolePermissionVoiceManager Cogs: {e}")
            print(f"❌ Fehler beim Laden von RolePermissionVoiceManager: {e}")
            raise

    async def cog_unload(self):
        """Wird beim Entladen des Cogs aufgerufen"""
        try:
            self.user_rank_cache.clear()
            self.guild_roles_cache.clear()
            self.channel_anchors.clear()
            self.channel_settings.clear()
            logger.info("RolePermissionVoiceManager Cog erfolgreich entladen")
            print("✅ RolePermissionVoiceManager Cog entladen")
        except Exception as e:
            logger.error(f"Fehler beim Entladen des RolePermissionVoiceManager Cogs: {e}")
            print(f"❌ Fehler beim Entladen von RolePermissionVoiceManager: {e}")

    def get_guild_roles(self, guild: discord.Guild) -> Dict[int, discord.Role]:
        """Cached Guild-Rollen für Performance"""
        if guild.id not in self.guild_roles_cache:
            self.guild_roles_cache[guild.id] = {role.id: role for role in guild.roles}
        return self.guild_roles_cache[guild.id]

    def get_user_rank_from_roles(self, member: discord.Member) -> Tuple[str, int]:
        """Ermittelt Benutzer-Rang basierend auf Discord-Rollen mit Debug-Ausgabe"""
        cache_key = f"{member.id}:{member.guild.id}"
        
        if cache_key in self.user_rank_cache:
            return self.user_rank_cache[cache_key]

        # Debug: Alle Rollen des Users loggen
        user_role_ids = [role.id for role in member.roles]
        logger.debug(f"User {member.display_name} hat Rollen: {user_role_ids}")

        # Prüfe alle Rollen des Benutzers
        highest_rank = ("Obscurus", 0)
        highest_rank_value = 0
        found_rank_roles = []

        for role in member.roles:
            if role.id in self.discord_rank_roles:
                rank_name, rank_value = self.discord_rank_roles[role.id]
                found_rank_roles.append(f"{rank_name}({rank_value})")
                if rank_value > highest_rank_value:
                    highest_rank = (rank_name, rank_value)
                    highest_rank_value = rank_value

        # Debug-Ausgabe
        if found_rank_roles:
            logger.info(f"User {member.display_name}: Gefundene Ränge={found_rank_roles}, Höchster={highest_rank[0]}")
        else:
            logger.debug(f"User {member.display_name}: Keine Rang-Rollen gefunden")

        # Cache aktualisieren
        self.user_rank_cache[cache_key] = highest_rank
        return highest_rank

    async def get_channel_members_ranks(self, channel: discord.VoiceChannel) -> Dict[discord.Member, Tuple[str, int]]:
        """Holt Ränge aller Mitglieder in einem Sprachkanal"""
        members_ranks = {}
        
        for member in channel.members:
            if member.bot:
                continue
                
            rank_name, rank_value = self.get_user_rank_from_roles(member)
            members_ranks[member] = (rank_name, rank_value)
        
        logger.debug(f"Kanal {channel.name}: {len(members_ranks)} Mitglieder mit Rängen")
        return members_ranks

    def calculate_balancing_range_from_anchor(self, channel: discord.VoiceChannel) -> Tuple[int, int]:
        """Berechnet Balancing-Bereich basierend auf Anker-User (NICHT alle User)"""
        anchor = self.get_channel_anchor(channel)
        
        if anchor is None:
            # Kein Anker gesetzt - Kanal ist leer oder System gerade gestartet
            return 0, 11
        
        user_id, rank_name, rank_value, allowed_min, allowed_max = anchor
        logger.debug(f"Anker-basierte Berechnung für {channel.name}: {rank_name}({rank_value}) → {allowed_min}-{allowed_max}")
        return allowed_min, allowed_max

    def get_allowed_role_ids(self, allowed_min: int, allowed_max: int) -> Set[int]:
        """Ermittelt welche Discord-Rollen im erlaubten Bereich liegen"""
        allowed_roles = set()
        
        for role_id, (rank_name, rank_value) in self.discord_rank_roles.items():
            if allowed_min <= rank_value <= allowed_max:
                allowed_roles.add(role_id)
        
        return allowed_roles

    async def set_everyone_deny_connect(self, channel: discord.VoiceChannel):
        """Setzt @everyone auf Connect=False"""
        try:
            # Prüfe ob Kanal noch existiert
            if not await self.channel_exists(channel):
                logger.warning(f"Kanal {channel.id} existiert nicht mehr - Überspringe @everyone Update")
                return False

            everyone_role = channel.guild.default_role
            current_overwrites = channel.overwrites_for(everyone_role)
            
            # Nur setzen wenn nicht bereits Connect=False
            if current_overwrites.connect is not False:
                await channel.set_permissions(
                    everyone_role, 
                    overwrite=discord.PermissionOverwrite(
                        connect=False,
                        view_channel=True
                    )
                )
                logger.debug(f"@everyone auf Connect=False gesetzt für {channel.name}")
            return True
        except discord.NotFound:
            logger.warning(f"Kanal {channel.id} wurde gelöscht - Überspringe @everyone Update")
            return False
        except Exception as e:
            logger.error(f"Fehler beim Setzen der @everyone Berechtigung: {e}")
            return False

    async def channel_exists(self, channel: discord.VoiceChannel) -> bool:
        """Prüft ob Kanal noch existiert"""
        try:
            # Versuche den Kanal vom Guild zu holen
            fresh_channel = channel.guild.get_channel(channel.id)
            return fresh_channel is not None and isinstance(fresh_channel, discord.VoiceChannel)
        except:
            return False

    async def update_channel_permissions_via_roles(self, channel: discord.VoiceChannel):
        """Aktualisiert Kanal-Berechtigungen über Discord-Rollen (nicht User)"""
        try:
            # Prüfe ob Kanal noch existiert
            if not await self.channel_exists(channel):
                logger.warning(f"Kanal {channel.id} existiert nicht mehr - Überspringe Update")
                return

            # Prüfe ob System für diesen Kanal aktiviert ist
            if not self.is_channel_system_enabled(channel):
                logger.debug(f"Rang-System für {channel.name} deaktiviert - Überspringe Update")
                return

            # 1. @everyone auf Connect=False setzen
            everyone_success = await self.set_everyone_deny_connect(channel)
            if not everyone_success:
                return  # Kanal existiert nicht mehr

            members_ranks = await self.get_channel_members_ranks(channel)
            
            if not members_ranks:
                # Kanal ist leer - Anker entfernen und alle Rollen-Berechtigungen entfernen
                self.remove_channel_anchor(channel)
                await self.clear_role_permissions(channel)
                return

            # 2. Berechne erlaubten Bereich basierend auf ANKER (nicht alle User)
            allowed_min, allowed_max = self.calculate_balancing_range_from_anchor(channel)
            allowed_role_ids = self.get_allowed_role_ids(allowed_min, allowed_max)

            logger.info(f"Kanal {channel.name}: Anker-basierte Ränge {allowed_min}-{allowed_max}, Rollen-IDs: {allowed_role_ids}")

            # 3. Hole Guild-Rollen für Performance
            guild_roles = self.get_guild_roles(channel.guild)

            # 4. Setze Connect=True für erlaubte Rollen
            updated_roles = []
            for role_id in allowed_role_ids:
                if role_id in guild_roles:
                    role = guild_roles[role_id]
                    current_overwrites = channel.overwrites_for(role)
                    
                    # Nur setzen wenn nicht bereits Connect=True
                    if current_overwrites.connect is not True:
                        await channel.set_permissions(
                            role, 
                            overwrite=discord.PermissionOverwrite(
                                connect=True,
                                speak=True,
                                view_channel=True
                            )
                        )
                        updated_roles.append(role.name)
                        
                        # Rate-Limiting
                        await asyncio.sleep(0.5)

            # 5. Entferne Connect=True von nicht mehr erlaubten Rollen
            await self.remove_disallowed_role_permissions(channel, allowed_role_ids)

            if updated_roles:
                logger.info(f"Connect=True gesetzt für Rollen: {updated_roles}")

        except Exception as e:
            logger.error(f"Fehler beim Aktualisieren der Rollen-Berechtigungen: {e}")

    async def remove_disallowed_role_permissions(self, channel: discord.VoiceChannel, allowed_role_ids: Set[int]):
        """Entfernt Connect-Berechtigungen von Rollen die nicht mehr erlaubt sind"""
        try:
            removed_roles = []
            
            for overwrite_target, overwrite in channel.overwrites.items():
                # Nur Discord-Rollen prüfen (nicht @everyone, nicht User)
                if (isinstance(overwrite_target, discord.Role) and 
                    overwrite_target.id != channel.guild.default_role.id and
                    overwrite_target.id in self.discord_rank_roles):
                    
                    # Wenn Rolle nicht mehr erlaubt ist, entferne Berechtigung
                    if overwrite_target.id not in allowed_role_ids:
                        await channel.set_permissions(overwrite_target, overwrite=None)
                        removed_roles.append(overwrite_target.name)
                        await asyncio.sleep(0.5)  # Rate-Limiting

            if removed_roles:
                logger.info(f"Berechtigungen entfernt von Rollen: {removed_roles}")

        except Exception as e:
            logger.error(f"Fehler beim Entfernen von Rollen-Berechtigungen: {e}")

    async def clear_role_permissions(self, channel: discord.VoiceChannel):
        """Entfernt alle Rang-Rollen-Berechtigungen (Kanal leer)"""
        try:
            cleared_roles = []
            
            for overwrite_target, overwrite in channel.overwrites.items():
                if (isinstance(overwrite_target, discord.Role) and 
                    overwrite_target.id != channel.guild.default_role.id and
                    overwrite_target.id in self.discord_rank_roles):
                    
                    await channel.set_permissions(overwrite_target, overwrite=None)
                    cleared_roles.append(overwrite_target.name)
                    await asyncio.sleep(0.5)

            if cleared_roles:
                logger.info(f"Alle Rollen-Berechtigungen entfernt: {cleared_roles}")

        except Exception as e:
            logger.error(f"Fehler beim Leeren der Rollen-Berechtigungen: {e}")

    async def update_channel_name(self, channel: discord.VoiceChannel):
        """Aktualisiert Kanal-Name basierend auf ANKER-User (erster User), nicht allen Usern"""
        try:
            # Prüfe ob Kanal noch existiert
            if not await self.channel_exists(channel):
                logger.warning(f"Kanal {channel.id} existiert nicht mehr - Überspringe Name-Update")
                return

            members_ranks = await self.get_channel_members_ranks(channel)
            
            if not members_ranks:
                new_name = "Rang-Sprachkanal"
            else:
                # Verwende ANKER-USER für Kanal-Namen, nicht alle User
                anchor = self.get_channel_anchor(channel)
                
                if anchor:
                    user_id, anchor_rank_name, anchor_rank_value, allowed_min, allowed_max = anchor
                    
                    # Kanal-Name basiert auf Anker-User und erlaubtem Bereich
                    min_rank_name = self.get_rank_name_from_value(allowed_min)
                    max_rank_name = self.get_rank_name_from_value(allowed_max)
                    
                    if allowed_min == allowed_max:
                        # Nur ein Rang erlaubt
                        new_name = f"{anchor_rank_name} Lobby"
                    elif allowed_max - allowed_min <= 1:
                        # Enger Bereich
                        new_name = f"{anchor_rank_name} Elo"
                    else:
                        # Breiter Bereich - zeige Spanne mit Anker als Basis
                        new_name = f"{min_rank_name}-{max_rank_name} ({anchor_rank_name})"
                    
                    logger.debug(f"Anker-basierter Name: {anchor_rank_name} → {new_name}")
                else:
                    # Fallback: Verwende ersten User im Kanal
                    first_member = next(iter(members_ranks.keys()))
                    rank_name, rank_value = members_ranks[first_member]
                    new_name = f"{rank_name} Lobby"
                    logger.warning(f"Kein Anker gefunden für {channel.name}, verwende ersten User: {rank_name}")

            if channel.name != new_name:
                try:
                    await channel.edit(name=new_name)
                    logger.info(f"Kanal-Name aktualisiert: {new_name}")
                except discord.NotFound:
                    logger.warning(f"Kanal {channel.id} wurde während Name-Update gelöscht")
                    return
                
        except Exception as e:
            logger.error(f"Fehler beim Aktualisieren des Kanal-Namens: {e}")

    def get_rank_name_from_value(self, rank_value: int) -> str:
        """Konvertiert Rang-Wert zu Rang-Name"""
        for rank_name, value in self.deadlock_ranks.items():
            if value == rank_value:
                return rank_name
        return "Obscurus"

    def set_channel_anchor(self, channel: discord.VoiceChannel, user: discord.Member, rank_name: str, rank_value: int):
        """Setzt den Anker-User für einen Kanal (erster User bestimmt Regeln)"""
        if rank_name in self.balancing_rules:
            minus_range, plus_range = self.balancing_rules[rank_name]
            allowed_min = max(0, rank_value + minus_range)
            allowed_max = min(11, rank_value + plus_range)
        else:
            allowed_min = allowed_max = rank_value
        
        self.channel_anchors[channel.id] = (user.id, rank_name, rank_value, allowed_min, allowed_max)
        logger.info(f"🔗 Anker gesetzt für {channel.name}: {user.display_name} ({rank_name}) → Bereich {allowed_min}-{allowed_max}")

    def get_channel_anchor(self, channel: discord.VoiceChannel) -> Optional[Tuple[int, str, int, int, int]]:
        """Holt den Anker-User für einen Kanal"""
        return self.channel_anchors.get(channel.id)

    def remove_channel_anchor(self, channel: discord.VoiceChannel):
        """Entfernt den Anker für einen Kanal (wenn leer)"""
        if channel.id in self.channel_anchors:
            anchor_data = self.channel_anchors[channel.id]
            del self.channel_anchors[channel.id]
            logger.info(f"🔗 Anker entfernt für {channel.name}: {anchor_data[1]} ({anchor_data[2]})")

    def is_channel_system_enabled(self, channel: discord.VoiceChannel) -> bool:
        """Prüft ob das Rang-System für einen Kanal aktiviert ist"""
        return self.channel_settings.get(channel.id, {}).get("enabled", True)  # Default: Aktiviert

    def set_channel_system_enabled(self, channel: discord.VoiceChannel, enabled: bool):
        """Aktiviert/Deaktiviert das Rang-System für einen Kanal"""
        if channel.id not in self.channel_settings:
            self.channel_settings[channel.id] = {}
        
        self.channel_settings[channel.id]["enabled"] = enabled
        status = "aktiviert" if enabled else "deaktiviert"
        logger.info(f"🔧 Rang-System für {channel.name} {status}")

    def get_channel_system_status(self, channel: discord.VoiceChannel) -> str:
        """Holt den System-Status für einen Kanal"""
        enabled = self.is_channel_system_enabled(channel)
        return "✅ Aktiviert" if enabled else "❌ Deaktiviert"

    def is_monitored_channel(self, channel: discord.VoiceChannel) -> bool:
        """Prüft ob Kanal überwacht wird (ausgenommen Ausnahme-Kanäle)"""
        if channel.id in self.excluded_channel_ids:
            return False
        return (channel.category_id == self.monitored_category_id if channel.category else False)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Behandelt Sprachkanal-Änderungen"""
        try:
            # Cache bei Änderung löschen
            cache_key = f"{member.id}:{member.guild.id}"
            if cache_key in self.user_rank_cache:
                del self.user_rank_cache[cache_key]

            # Guild-Rollen-Cache bei Änderung aktualisieren
            if member.guild.id in self.guild_roles_cache:
                del self.guild_roles_cache[member.guild.id]

            # Kanal beigetreten oder gewechselt
            if after.channel and self.is_monitored_channel(after.channel):
                await self.handle_voice_join(member, after.channel)
            
            # Kanal verlassen
            if before.channel and self.is_monitored_channel(before.channel):
                await self.handle_voice_leave(member, before.channel)
                
        except Exception as e:
            logger.error(f"Fehler bei voice_state_update: {e}")

    async def handle_voice_join(self, member: discord.Member, channel: discord.VoiceChannel):
        """Behandelt Beitritt zu überwachtem Sprachkanal mit Anker-System (OHNE Kicks)"""
        try:
            # Prüfe ob System für diesen Kanal aktiviert ist
            if not self.is_channel_system_enabled(channel):
                logger.debug(f"Rang-System für {channel.name} deaktiviert - User {member.display_name} darf bleiben")
                return

            # Rang des Benutzers prüfen
            rank_name, rank_value = self.get_user_rank_from_roles(member)
            logger.info(f"User {member.display_name} betritt {channel.name} mit Rang {rank_name}({rank_value})")

            # Prüfe ob Anker existiert
            anchor = self.get_channel_anchor(channel)
            
            if anchor is None:
                # Kein Anker → Dieser User ist der ERSTE und wird zum Anker
                self.set_channel_anchor(channel, member, rank_name, rank_value)
                logger.info(f"🔗 {member.display_name} wird Anker für {channel.name} ({rank_name})")
                
                # Berechtigungen und Name sofort aktualisieren
                await self.update_channel_permissions_via_roles(channel)
                await self.update_channel_name(channel)
                return
            
            # Anker existiert → User darf bleiben, aber logge ob er "passt"
            user_id, anchor_rank_name, anchor_rank_value, allowed_min, allowed_max = anchor
            
            if not (allowed_min <= rank_value <= allowed_max):
                # User passt NICHT in Anker-Bereich → ABER KEIN KICK! Nur Info-Log
                logger.info(f"ℹ️ {member.display_name} ({rank_name}) passt nicht in Anker-Bereich {allowed_min}-{allowed_max}, bleibt aber (durch Admin-Move?)")
            else:
                # User passt in Anker-Bereich → Alles OK
                logger.info(f"✅ {member.display_name} ({rank_name}) passt in Anker-Bereich {allowed_min}-{allowed_max}")
            
            # Berechtigungen aktualisieren (Name ändert sich NICHT, da Anker-basiert)
            await self.update_channel_permissions_via_roles(channel)
            await self.update_channel_name(channel)
            
        except Exception as e:
            logger.error(f"Fehler beim Behandeln des Sprachkanal-Beitritts: {e}")

    async def handle_voice_leave(self, member: discord.Member, channel: discord.VoiceChannel):
        """Behandelt Verlassen von überwachtem Sprachkanal mit Anker-Management"""
        try:
            logger.info(f"User {member.display_name} verlässt {channel.name}")
            
            # Kurze Verzögerung für konsistente Daten
            await asyncio.sleep(1)
            
            # Prüfe ob System für diesen Kanal aktiviert ist
            if not self.is_channel_system_enabled(channel):
                logger.debug(f"Rang-System für {channel.name} deaktiviert - Überspringe Leave-Update")
                return
            
            # Prüfe ob Kanal leer ist
            members_ranks = await self.get_channel_members_ranks(channel)
            
            if not members_ranks:
                # Kanal ist leer → Anker entfernen
                self.remove_channel_anchor(channel)
                logger.info(f"🔗 Kanal {channel.name} ist leer - Anker entfernt")
            else:
                # Kanal nicht leer → Prüfe ob der ANKER-USER verlassen hat
                anchor = self.get_channel_anchor(channel)
                if anchor and anchor[0] == member.id:
                    # Der Anker-User hat verlassen! → Anker an nächsten User übertragen
                    logger.info(f"🔗 Anker-User {member.display_name} hat {channel.name} verlassen - Übertrage Anker")
                    
                    # Entferne alten Anker
                    self.remove_channel_anchor(channel)
                    
                    # Setze ersten verbleibenden User als neuen Anker
                    first_remaining_member = next(iter(members_ranks.keys()))
                    rank_name, rank_value = members_ranks[first_remaining_member]
                    self.set_channel_anchor(channel, first_remaining_member, rank_name, rank_value)
                    
                    logger.info(f"🔗 Neuer Anker gesetzt: {first_remaining_member.display_name} ({rank_name})")
            
            # Berechtigungen und Name aktualisieren
            await self.update_channel_permissions_via_roles(channel)
            await self.update_channel_name(channel)
            
        except Exception as e:
            logger.error(f"Fehler beim Behandeln des Sprachkanal-Verlassens: {e}")

    # Admin-Befehle
    @commands.group(name="rrang", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def rank_command(self, ctx):
        """Rollen-basierte Rang-Management-Befehle"""
        embed = discord.Embed(
            title="🎭 Rollen-Berechtigungen Rang-System",
            description="Verwaltet Sprachkanäle über Discord-Rollen-Berechtigungen",
            color=0x0099ff
        )
        
        embed.add_field(
            name="📋 Verfügbare Befehle",
            value="`info` - Zeigt Rang-Info eines Benutzers\n"
                  "`debug` - Debug-Info für User-Rollen\n"
                  "`anker` - Zeigt aktuelle Kanal-Anker\n"
                  "`toggle` - Aktiviert/Deaktiviert System für aktuellen VC\n"
                  "`vcstatus` - Status des aktuellen Voice Channels\n"
                  "`status` - System-Status und Version\n"
                  "`rollen` - Zeigt alle überwachten Rollen\n"
                  "`kanäle` - Zeigt überwachte/ausgeschlossene Kanäle\n"
                  "`aktualisieren` - Erzwingt Kanal-Update\n"
                  "`system` - Aktiviert/Deaktiviert System",
            inline=False
        )
        
        await ctx.send(embed=embed)

    @rank_command.command(name="status")
    async def system_status(self, ctx):
        """Zeigt System-Status und Version"""
        embed = discord.Embed(
            title="📊 System-Status",
            description="Rollen-Berechtigungen Voice Manager",
            color=discord.Color.green()
        )
        
        embed.add_field(
            name="🔧 System-Version",
            value="**Sanftes Anker-System v4.0**\n✅ Keine User-Kicks (nur Rollen-Berechtigungen)\n✅ Pro-Kanal Toggle-System\n✅ Erster-User-Anker System\n✅ Kanal-Löschung-Schutz\n✅ Rate-Limiting optimiert",
            inline=False
        )
        
        embed.add_field(
            name="📁 Überwachung",
            value=f"Kategorie: {self.monitored_category_id}\nAusgeschlossen: {len(self.excluded_channel_ids)} Kanäle\nRollen: {len(self.discord_rank_roles)}",
            inline=True
        )
        
        embed.add_field(
            name="💾 Cache",
            value=f"User-Ränge: {len(self.user_rank_cache)}\nGuild-Rollen: {len(self.guild_roles_cache)}\nKanal-Anker: {len(self.channel_anchors)}\nKanal-Settings: {len(self.channel_settings)}",
            inline=True
        )
        
        # Teste aktuellen Benutzer
        try:
            rank_name, rank_value = self.get_user_rank_from_roles(ctx.author)
            embed.add_field(
                name="🎯 Ihr Rang",
                value=f"{rank_name} (Wert: {rank_value})",
                inline=True
            )
        except Exception as e:
            embed.add_field(
                name="⚠️ Rang-Test",
                value=f"Fehler: {e}",
                inline=True
            )
        
        await ctx.send(embed=embed)

    @rank_command.command(name="anker")
    async def show_channel_anchors(self, ctx):
        """Zeigt aktuelle Kanal-Anker"""
        embed = discord.Embed(
            title="🔗 Kanal-Anker Übersicht",
            description="Aktive Erst-User-Anker in überwachten Kanälen",
            color=discord.Color.purple()
        )
        
        if not self.channel_anchors:
            embed.description = "❌ Keine aktiven Kanal-Anker"
            await ctx.send(embed=embed)
            return
        
        anchor_info = []
        for channel_id, (user_id, rank_name, rank_value, allowed_min, allowed_max) in self.channel_anchors.items():
            channel = ctx.guild.get_channel(channel_id)
            user = ctx.guild.get_member(user_id)
            
            if channel and user:
                min_rank = self.get_rank_name_from_value(allowed_min)
                max_rank = self.get_rank_name_from_value(allowed_max)
                
                # Aktuelle Member-Anzahl
                current_members = len([m for m in channel.members if not m.bot])
                
                anchor_info.append(
                    f"**{channel.name}**\n"
                    f"🔗 Anker: {user.display_name} ({rank_name})\n"
                    f"📊 Bereich: {min_rank}-{max_rank} ({allowed_min}-{allowed_max})\n"
                    f"👥 Aktuelle User: {current_members}\n"
                )
            else:
                # Kanal oder User existiert nicht mehr
                anchor_info.append(f"❓ **Veralteter Anker** (Kanal: {channel_id}, User: {user_id})")
        
        embed.description = "\n".join(anchor_info)
        
        if len(anchor_info) > 10:
            embed.set_footer(text="Zeige erste 10 Anker")
        
        await ctx.send(embed=embed)

    @rank_command.command(name="toggle")
    async def toggle_channel_system(self, ctx, action: str = None):
        """Aktiviert/Deaktiviert das Rang-System für den aktuellen Voice Channel"""
        # Prüfe ob User in Voice Channel ist
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("❌ Sie müssen sich in einem Voice Channel befinden um das System zu togglen.")
            return
        
        channel = ctx.author.voice.channel
        
        # Prüfe ob Channel überwacht wird
        if not self.is_monitored_channel(channel):
            await ctx.send(f"❌ **{channel.name}** wird nicht vom Rang-System überwacht.")
            return
        
        # Aktueller Status
        current_status = self.is_channel_system_enabled(channel)
        
        if action is None:
            # Nur Status anzeigen
            status_text = "✅ Aktiviert" if current_status else "❌ Deaktiviert"
            await ctx.send(f"🔧 Rang-System für **{channel.name}**: {status_text}")
            return
        
        # Action verarbeiten
        if action.lower() in ["ein", "on", "aktivieren", "enable"]:
            if current_status:
                await ctx.send(f"ℹ️ Rang-System für **{channel.name}** ist bereits aktiviert.")
            else:
                self.set_channel_system_enabled(channel, True)
                await ctx.send(f"✅ Rang-System für **{channel.name}** aktiviert.")
                
                # Sofort aktualisieren
                await self.update_channel_permissions_via_roles(channel)
                await self.update_channel_name(channel)
                
        elif action.lower() in ["aus", "off", "deaktivieren", "disable"]:
            if not current_status:
                await ctx.send(f"ℹ️ Rang-System für **{channel.name}** ist bereits deaktiviert.")
            else:
                self.set_channel_system_enabled(channel, False)
                await ctx.send(f"❌ Rang-System für **{channel.name}** deaktiviert.")
                
                # Anker entfernen und Berechtigungen zurücksetzen
                self.remove_channel_anchor(channel)
                await self.clear_role_permissions(channel)
                
        else:
            await ctx.send("❌ Verwenden Sie: `ein`/`on` oder `aus`/`off`")

    @rank_command.command(name="vcstatus")
    async def voice_channel_status(self, ctx):
        """Zeigt Status des aktuellen Voice Channels"""
        # Prüfe ob User in Voice Channel ist
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("❌ Sie müssen sich in einem Voice Channel befinden.")
            return
        
        channel = ctx.author.voice.channel
        
        embed = discord.Embed(
            title=f"🔊 Status: {channel.name}",
            color=discord.Color.blue()
        )
        
        # Basis-Infos
        embed.add_field(
            name="📊 Kanal-Info",
            value=f"ID: {channel.id}\nKategorie: {channel.category.name if channel.category else 'Keine'}\nMitglieder: {len(channel.members)}",
            inline=True
        )
        
        # Überwachung
        is_monitored = self.is_monitored_channel(channel)
        embed.add_field(
            name="👁️ Überwachung",
            value="✅ Überwacht" if is_monitored else "❌ Nicht überwacht",
            inline=True
        )
        
        if is_monitored:
            # System-Status
            system_enabled = self.is_channel_system_enabled(channel)
            embed.add_field(
                name="🔧 Rang-System",
                value="✅ Aktiviert" if system_enabled else "❌ Deaktiviert",
                inline=True
            )
            
            # Anker-Info
            anchor = self.get_channel_anchor(channel)
            if anchor and system_enabled:
                user_id, rank_name, rank_value, allowed_min, allowed_max = anchor
                anchor_user = ctx.guild.get_member(user_id)
                anchor_name = anchor_user.display_name if anchor_user else f"User ID {user_id}"
                
                min_rank = self.get_rank_name_from_value(allowed_min)
                max_rank = self.get_rank_name_from_value(allowed_max)
                
                embed.add_field(
                    name="🔗 Anker",
                    value=f"{anchor_name} ({rank_name})\nBereich: {min_rank}-{max_rank}",
                    inline=False
                )
            else:
                embed.add_field(
                    name="🔗 Anker",
                    value="Kein Anker gesetzt" if system_enabled else "System deaktiviert",
                    inline=False
                )
        
        await ctx.send(embed=embed)

    @rank_command.command(name="debug")
    async def debug_user_roles(self, ctx, member: discord.Member = None):
        """Debug-Informationen für User-Rollen-Erkennung"""
        if member is None:
            member = ctx.author
        
        try:
            # Cache leeren für frische Daten
            cache_key = f"{member.id}:{member.guild.id}"
            if cache_key in self.user_rank_cache:
                del self.user_rank_cache[cache_key]
            
            # Alle Rollen des Users
            user_roles = [(role.id, role.name) for role in member.roles]
            
            # Rang-Rollen finden
            found_rank_roles = []
            for role in member.roles:
                if role.id in self.discord_rank_roles:
                    rank_name, rank_value = self.discord_rank_roles[role.id]
                    found_rank_roles.append(f"**{role.name}** (ID: {role.id}) -> {rank_name} ({rank_value})")
            
            # Höchsten Rang ermitteln
            rank_name, rank_value = self.get_user_rank_from_roles(member)
            
            embed = discord.Embed(
                title=f"🔍 Debug: {member.display_name}",
                color=discord.Color.orange()
            )
            
            embed.add_field(
                name="👤 User-Info",
                value=f"ID: {member.id}\nRollen-Anzahl: {len(member.roles)}",
                inline=True
            )
            
            embed.add_field(
                name="🎯 Erkannter Rang",
                value=f"**{rank_name}** (Wert: {rank_value})",
                inline=True
            )
            
            if found_rank_roles:
                embed.add_field(
                    name="🎭 Gefundene Rang-Rollen",
                    value="\n".join(found_rank_roles),
                    inline=False
                )
            else:
                embed.add_field(
                    name="🎭 Rang-Rollen",
                    value="❌ Keine Rang-Rollen gefunden!",
                    inline=False
                )
            
            # Alle Rollen (gekürzt)
            all_roles_text = "\n".join([f"{role_id}: {role_name}" for role_id, role_name in user_roles[:10]])
            if len(user_roles) > 10:
                all_roles_text += f"\n... und {len(user_roles) - 10} weitere"
            
            embed.add_field(
                name="📋 Alle Rollen (erste 10)",
                value=all_roles_text,
                inline=False
            )
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Fehler bei Debug-Ausgabe: {e}")
            await ctx.send(f"❌ Debug-Fehler: {e}")

    @rank_command.command(name="info")
    async def rank_info(self, ctx, member: discord.Member = None):
        """Zeigt Rang-Informationen basierend auf Discord-Rollen"""
        if member is None:
            member = ctx.author
        
        try:
            rank_name, rank_value = self.get_user_rank_from_roles(member)
            
            embed = discord.Embed(
                title=f"🎭 Rang-Information für {member.display_name}",
                color=discord.Color.blue()
            )
            embed.add_field(name="Höchster Rang", value=rank_name, inline=True)
            embed.add_field(name="Rang-Wert", value=rank_value, inline=True)

            # Balancing-Regeln anzeigen
            if rank_name in self.balancing_rules:
                minus, plus = self.balancing_rules[rank_name]
                embed.add_field(
                    name="Balancing-Regel",
                    value=f"{minus:+d} bis {plus:+d} Ränge",
                    inline=True
                )
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Rang-Information: {e}")
            await ctx.send("❌ Fehler beim Abrufen der Rang-Information.")

    @rank_command.command(name="aktualisieren")
    @commands.has_permissions(manage_guild=True)
    async def force_update(self, ctx, channel: discord.VoiceChannel = None):
        """Erzwingt Aktualisierung von Kanal-Berechtigungen und -Name"""
        if channel is None:
            if not ctx.author.voice or not ctx.author.voice.channel:
                await ctx.send("❌ Sie müssen sich in einem Sprachkanal befinden oder einen Kanal angeben.")
                return
            channel = ctx.author.voice.channel
        
        if not self.is_monitored_channel(channel):
            await ctx.send("❌ Dieser Kanal wird nicht überwacht.")
            return
        
        try:
            # Cache leeren
            self.user_rank_cache.clear()
            self.guild_roles_cache.clear()
            
            # Anker neu setzen falls Kanal nicht leer
            members_ranks = await self.get_channel_members_ranks(channel)
            if members_ranks:
                # Entferne alten Anker
                self.remove_channel_anchor(channel)
                
                # Setze ersten User als neuen Anker
                first_member = next(iter(members_ranks.keys()))
                rank_name, rank_value = members_ranks[first_member]
                self.set_channel_anchor(channel, first_member, rank_name, rank_value)
                logger.info(f"🔄 Anker neu gesetzt bei Force-Update: {first_member.display_name} ({rank_name})")
            
            await self.update_channel_permissions_via_roles(channel)
            await self.update_channel_name(channel)
            await ctx.send(f"✅ Kanal **{channel.name}** erfolgreich aktualisiert.")
            
        except Exception as e:
            logger.error(f"Fehler beim Aktualisieren des Kanals: {e}")
            await ctx.send("❌ Fehler beim Aktualisieren des Kanals.")

    @rank_command.command(name="rollen")
    async def show_tracked_roles(self, ctx):
        """Zeigt alle überwachten Discord-Rollen"""
        embed = discord.Embed(
            title="🎭 Überwachte Rang-Rollen",
            description="Discord-Rollen für das Rang-System",
            color=discord.Color.gold()
        )
        
        role_info = []
        for role_id, (rank_name, rank_value) in self.discord_rank_roles.items():
            role = ctx.guild.get_role(role_id)
            if role:
                member_count = len(role.members)
                role_info.append(f"**{rank_name}** ({rank_value}): {role.mention} - {member_count} Mitglieder")
            else:
                role_info.append(f"**{rank_name}** ({rank_value}): ❌ Rolle nicht gefunden (ID: {role_id})")
        
        embed.description = "\n".join(role_info)
        await ctx.send(embed=embed)

    @rank_command.command(name="kanäle")
    async def show_channel_config(self, ctx):
        """Zeigt überwachte und ausgeschlossene Kanäle"""
        embed = discord.Embed(
            title="🔊 Kanal-Konfiguration",
            description="Übersicht der Sprachkanal-Überwachung",
            color=discord.Color.blue()
        )
        
        category = ctx.guild.get_channel(self.monitored_category_id)
        if category:
            voice_channels = [ch for ch in category.channels if isinstance(ch, discord.VoiceChannel)]
            monitored_channels = [ch for ch in voice_channels if ch.id not in self.excluded_channel_ids]
            
            embed.add_field(
                name=f"📁 Überwachte Kategorie: {category.name}",
                value=f"Gesamt-Kanäle: {len(voice_channels)}\nÜberwacht: {len(monitored_channels)}",
                inline=False
            )
        
        excluded_info = []
        for channel_id in self.excluded_channel_ids:
            channel = ctx.guild.get_channel(channel_id)
            if channel:
                excluded_info.append(f"🔇 {channel.name}")
            else:
                excluded_info.append(f"❓ Unbekannter Kanal (ID: {channel_id})")
        
        if excluded_info:
            embed.add_field(
                name="🚫 Ausgeschlossene Kanäle",
                value="\n".join(excluded_info),
                inline=False
            )
        
        await ctx.send(embed=embed)

    async def cog_command_error(self, ctx, error):
        """Behandelt Befehl-Fehler innerhalb des Cogs"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Unzureichende Berechtigungen für diesen Befehl.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("❌ Ungültige Argumente angegeben.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("❌ Benutzer nicht gefunden.")
        else:
            logger.error(f"Unerwarteter Fehler in {ctx.command}: {error}")
            await ctx.send("❌ Ein unerwarteter Fehler ist aufgetreten.")

async def setup(bot):
    """Setup-Funktion für das Cog"""
    await bot.add_cog(RolePermissionVoiceManager(bot))
    logger.info("RolePermissionVoiceManager Cog hinzugefügt")

async def teardown(bot):
    """Teardown-Funktion für das Cog"""
    try:
        cog = bot.get_cog("RolePermissionVoiceManager")
        if cog:
            await bot.remove_cog("RolePermissionVoiceManager")
        logger.info("RolePermissionVoiceManager Cog entfernt")
    except Exception as e:
        logger.error(f"Fehler beim Entfernen des RolePermissionVoiceManager Cogs: {e}")