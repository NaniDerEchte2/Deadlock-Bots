# cogs/steam_verified_role.py
# Kurzfassung: identisch zur letzten Version, plus Diagnose & bessere Zusammenfassungen.

import os, logging, asyncio
from typing import Set, List, Tuple
import discord
from discord.ext import commands, tasks

from service import db as central_db

log = logging.getLogger(__name__)

class SteamVerifiedRole(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_id = int(os.getenv("GUILD_ID", "1289721245281292288"))
        self.verified_role_id = int(os.getenv("VERIFIED_ROLE_ID", "1419608095533043774"))
        self.log_channel_id = int(os.getenv("VERIFIED_LOG_CHANNEL_ID", "1374364800817303632"))
        self.db_path = central_db.db_path()
        self.dry_run = os.getenv("DRY_RUN", "0") == "1"
        interval_min = int(os.getenv("POLL_INTERVAL_MINUTES", "180"))
        self._interval_seconds = max(60, interval_min * 60)
        self._task = None
        log.info("SteamVerifiedRole init: guild=%s role=%s db=%s every=%ss dry_run=%s log_ch=%s",
                 self.guild_id, self.verified_role_id, self.db_path, self._interval_seconds, self.dry_run, self.log_channel_id)

    # ---------- DB ----------
    def _fetch_verified_discord_ids(self) -> Set[int]:
        try:
            with central_db.get_conn() as con:
                cur = con.execute("""SELECT user_id FROM steam_links WHERE verified=1 GROUP BY user_id""")
                rows = cur.fetchall()
        except Exception as e:
            log.exception("DB-Fehler beim Lesen verifizierter IDs: %s", e)
            return set()

        ids: Set[int] = set()
        for r in rows:
            if r["user_id"] is None:
                continue
            # nur plausible Discord-Snowflakes durchlassen (>= 10^16)
            try:
                val = int(r["user_id"])
                if val >= 10_000_000_000_000_000:  # 1e16 ~ 17 Stellen
                    ids.add(val)
            except Exception:
                continue
        return ids

    # ---------- Helpers ----------
    def _http_session_closed(self) -> bool:
        http = getattr(self.bot, "http", None)
        session = getattr(http, "_HTTPClient__session", None)
        return session is None or getattr(session, "closed", False)

    def _is_session_closed_error(self, exc: BaseException) -> bool:
        if not isinstance(exc, RuntimeError):
            return False
        text = str(exc)
        return "Session is closed" in text or "ClientSession is closed" in text

    async def _resolve_guild_and_role(self) -> Tuple[discord.Guild, discord.Role]:
        if not self.guild_id:
            log.error("GUILD_ID ist nicht konfiguriert.")
            return None, None
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(self.guild_id)
            except discord.HTTPException as exc:
                log.warning("Konnte Guild nicht abrufen (%s): %s", self.guild_id, exc)
        if guild is None:
            log.error("Guild %s nicht gefunden/zugreifbar.", self.guild_id)
            return None, None
        role = guild.get_role(self.verified_role_id)
        if role is None:
            log.error("Rolle %s in Guild %s nicht gefunden.", self.verified_role_id, guild.id)
            return None, None
        return guild, role

    async def _get_log_channel(self, guild: discord.Guild):
        ch = self.bot.get_channel(self.log_channel_id)
        if isinstance(ch, discord.TextChannel) and ch.guild.id == guild.id:
            return ch
        try:
            ch = await guild.fetch_channel(self.log_channel_id)
            if isinstance(ch, discord.TextChannel):
                return ch
        except discord.HTTPException as exc:
            log.debug("Konnte Log-Channel nicht abrufen (%s): %s", self.log_channel_id, exc)
        return None

    async def _announce_assignments(self, guild: discord.Guild, lines: List[str]):
        if not lines or self.dry_run:
            return
        ch = await self._get_log_channel(guild)
        if not ch:
            log.warning("Kein Log-Channel oder kein Zugriff: %s", self.log_channel_id)
            return
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 1900:
                try: await ch.send(chunk)
                except discord.HTTPException as e: log.warning("Konnte Log nicht senden: %s", e)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            try: await ch.send(chunk)
            except discord.HTTPException as e: log.warning("Konnte Log nicht senden: %s", e)

    # ---------- Core ----------
    async def _run_once(self) -> int:
        if self.bot.is_closed() or self._http_session_closed():
            log.info("Bot oder HTTP-Session geschlossen -> Verified-Loop wird beendet.")
            return 0

        guild, role = await self._resolve_guild_and_role()
        if not guild or not role: return 0

        # Rechte/Höhe vorab prüfen
        me = guild.me
        if not me:
            try:
                me = await guild.fetch_member(self.bot.user.id)
            except RuntimeError as exc:
                if self._is_session_closed_error(exc):
                    log.info("HTTP-Session geschlossen beim Bot-Member-Lookup.")
                    return 0
                raise
            except discord.HTTPException as exc:
                log.warning("Konnte Bot-Member nicht abrufen (%s): %s", self.bot.user.id if self.bot.user else "?", exc)
        if not me:
            log.error("Konnte Bot-Member in Guild nicht bestimmen.")
            return 0

        if not guild.me.guild_permissions.manage_roles:
            log.error("Bot hat kein 'Manage Roles' in Guild %s.", guild.id)
            return 0
        # Rolle über der Verified-Rolle?
        top_pos = max((r.position for r in me.roles), default=0)
        if top_pos <= role.position:
            log.error("Rollen-Hierarchie: Bot-Top(%s) <= Verified(%s) – kann nicht zuweisen.",
                      top_pos, role.position)
            return 0

        verified_ids = self._fetch_verified_discord_ids()
        if not verified_ids: return 0

        changes, lines = 0, []
        not_found = 0
        for uid in verified_ids:
            if self.bot.is_closed() or self._http_session_closed():
                log.info("HTTP-Session oder Bot geschlossen waehrend Lauf -> Abbruch.")
                break
            member = guild.get_member(uid)
            if member is None:
                try: member = await guild.fetch_member(uid)
                except discord.NotFound:
                    not_found += 1
                    continue
                except RuntimeError as exc:
                    if self._is_session_closed_error(exc):
                        log.info("HTTP-Session geschlossen beim fetch_member (%s) -> Abbruch Lauf.", uid)
                        break
                    raise
                except discord.HTTPException:
                    continue
            if role in member.roles:
                continue
            if self.dry_run:
                log.info("[DRY] Würde Rolle vergeben an %s (%s)", uid, member.display_name)
                changes += 1
                continue
            try:
                await member.add_roles(role, reason="Steam verified = 1 (automatisch)")
                changes += 1
                lines.append(f"✅ <@{uid}> ({member.display_name}) ist jetzt **Verified** - Rolle zugewiesen.")
                await asyncio.sleep(0.25)
            except RuntimeError as exc:
                if self._is_session_closed_error(exc):
                    log.info("HTTP-Session geschlossen beim add_roles -> Abbruch Lauf.")
                    break
                raise
            except discord.Forbidden:
                log.error("Forbidden: Rolle %s an %s (%s) - Hierarchie/Berechtigung?",
                          role.id, uid, getattr(member, 'display_name', '?'))
            except discord.HTTPException as e:
                log.warning("HTTP-Fehler bei %s: %s", uid, e)

        if lines and not self._http_session_closed():
            await self._announce_assignments(guild, lines)
        log.info("Verified-Check: %s Rollen vergeben, %s IDs nicht auf Server.", changes, not_found)
        return changes

    # ---------- Loop ----------
    @tasks.loop(seconds=5.0, count=1)
    async def _start_loop_once_ready(self):
        while not self.bot.is_ready():
            await asyncio.sleep(1)
        async def loop_body():
            try:
                while True:
                    if self.bot.is_closed() or self._http_session_closed():
                        log.info("Beende Verified-Loop (Bot/Session geschlossen).")
                        break
                    try:
                        await self._run_once()
                    except asyncio.CancelledError:
                        raise
                    except RuntimeError as exc:
                        if self._is_session_closed_error(exc):
                            log.info("HTTP-Session geschlossen -> Loop endet sauber.")
                            break
                        log.exception("Unerwarteter Fehler im Verified-Rollenlauf.")
                    except Exception:
                        log.exception("Unerwarteter Fehler im Verified-Rollenlauf.")
                    if self.bot.is_closed() or self._http_session_closed():
                        log.info("Beende Verified-Loop (Bot/Session geschlossen).")
                        break
                    await asyncio.sleep(self._interval_seconds)
            except asyncio.CancelledError:
                log.info("Verified-Loop wurde abgebrochen.")
                raise
            finally:
                self._task = None
        if self._task is None:
            self._task = self.bot.loop.create_task(loop_body())
            log.info("Periodischer Verified-Checker gestartet (alle %ss).", self._interval_seconds)

    @commands.Cog.listener()
    async def on_ready(self):
        if self._task is None:
            self._start_loop_once_ready.start()

    # ---------- Commands ----------
    @commands.command(name="verifyrole_run", help="Manueller Lauf (loggt nur Zuweisungen).")
    @commands.has_permissions(administrator=True)
    async def verifyrole_run(self, ctx: commands.Context):
        changes = await self._run_once()
        await ctx.reply(f"Fertig. {changes} Nutzer(n) die Verified-Rolle vergeben.", mention_author=False)

    @commands.command(name="verifyrole_diag", help="Diagnose: prüft IDs, Rechte, DB & Hierarchie.")
    @commands.has_permissions(administrator=True)
    async def verifyrole_diag(self, ctx: commands.Context):
        guild, role = await self._resolve_guild_and_role()
        db_exists = os.path.exists(self.db_path)
        ids = self._fetch_verified_discord_ids()
        ids_sample = list(ids)[:5]
        manage_roles = guild.me.guild_permissions.manage_roles if guild else False
        top_pos = max((r.position for r in guild.me.roles), default=0) if guild else -1
        role_pos = role.position if role else -1
        members_present = sum(1 for i in ids if guild and guild.get_member(i))
        embed = discord.Embed(title="Verified-Role Diagnose", color=0x2ecc71)
        embed.add_field(name="Guild ID", value=str(self.guild_id), inline=True)
        embed.add_field(name="Role ID", value=str(self.verified_role_id), inline=True)
        embed.add_field(name="DB Pfad", value=self.db_path, inline=False)
        embed.add_field(name="DB vorhanden", value=str(db_exists), inline=True)
        embed.add_field(name="Verifizierte IDs (DB)", value=str(len(ids)), inline=True)
        embed.add_field(name="Davon aktuell im Cache (get_member)", value=str(members_present), inline=True)
        embed.add_field(name="Bot Manage Roles", value=str(manage_roles), inline=True)
        embed.add_field(name="Bot TopPos vs Role Pos", value=f"{top_pos} vs {role_pos}", inline=True)
        if ids_sample:
            embed.add_field(name="Beispiel-IDs", value="\n".join(str(x) for x in ids_sample), inline=False)
        await ctx.reply(embed=embed, mention_author=False)

    def cog_unload(self):
        if self._task:
            self._task.cancel()
            self._task = None
        try:
            self._start_loop_once_ready.cancel()
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(SteamVerifiedRole(bot))
