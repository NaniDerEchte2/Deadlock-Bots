# ============================================================
# DL Coaching System – DB-first (aiosqlite, utils.deadlock_db.DB_PATH)
# Datei: cogs/dl_coaching.py
# Tables (werden automatisch erstellt):
#   coaching_sessions(
#       user_id INTEGER PRIMARY KEY,
#       thread_id INTEGER,
#       match_id TEXT,
#       rank TEXT, subrank TEXT, hero TEXT, comment TEXT,
#       step TEXT,
#       created_at TIMESTAMP, updated_at TIMESTAMP,
#       is_active INTEGER
#   )
#   INDEX: idx_coaching_thread (thread_id)
# ============================================================

import asyncio
import datetime
import json
import logging
import socket
from dataclasses import dataclass
from typing import Optional, Dict, Any

import discord
from discord.ext import commands, tasks
from discord.ui import Button, View, Modal, TextInput, Select

from service import db


logger = logging.getLogger(__name__)


@dataclass
class CoachingConfig:
    channel_id: int = 1357421075188813897
    # Falls vorhanden, wird eine bestehende Bot-Nachricht im Channel mit der View versehen.
    existing_message_id: Optional[int] = 1383883328385454210
    socket_host: str = "localhost"
    socket_port: int = 45680
    timeout_seconds: int = 600  # 10 min


class DlCoachingCog(commands.Cog):
    """DL Coaching System (Dropdown-UI) – DB-basiert, resilient über Restarts."""

    RANKS = {
        "initiate": "<:initiate:1316457822518775869>",
        "seeker": "<:seeker:1316458138886475876>",
        "alchemist": "<:alchemist:1316455291629342750>",
        "arcanist": "<:arcanist:1316455305315352587>",
        "ritualist": "<:ritualist:1316458203298660533>",
        "emissary": "<:emissary:1316457650367496306>",
        "archon": "<:archon:1316457293801324594>",
        "oracle": "<:oracle:1316457885743579317>",
        "phantom": "<:phantom:1316457982363701278>",
        "ascendant": "<:ascendant:1316457367818338385>",
        "eternus": "<:eternus:1316457737621868574>",
    }
    SUBRANKS = ["i", "ii", "iii", "iv", "v", "✶"]

    HEROES_PAGE_1 = {
        "abrams": "<:abrams:1371194882483294280>",
        "bebot": "<:bebot:1371194884547023080>",
        "calico": "<:calico:1371194886845632582>",
        "dynamo": "<:dynamo:1371194889592766514>",
        "grey_talon": "<:grey_talon:1371194891362898002>",
        "haze": "<:haze:1371194893640142858>",
        "holiday": "<:holiday:1371194895686963304>",
        "infernus": "<:Infernus:1371194897939566663>",
        "ivy": "<:ivy:1371194899432476722>",
        "kelvin": "<:kelvin:1371194901391474860>",
        "lady_geist": "<:lady_geist:1371194903018733758>",
        "lash": "<:lash:1371194904545333428>",
        "mirage": "<:mirage:1371194910232809552>",
    }
    HEROES_PAGE_2 = {
        "mo": "<:mo:1371194912489472091>",
        "paradox": "<:paradox:1371194915551182858>",
        "pocket": "<:pocket:1371194917627494420>",
        "seven": "<:seven:1371209369177427989>",
        "mcginnis": "<:mcginnis:1371209373350629428>",
        "sinclair": "<:sinclair:1371209380117968976>",
        "viscous": "<:viscous:1371209383586785380>",
        "viper": "<:viper:1371209397506406411>",
        "vyper": "<:vyper:1371209401519575192>",
        "warden": "<:warden:1371209405068214442>",
        "wraith": "<:wraith:1371209407781666826>",
        "yamato": "<:yamato:1371209416258359376>",
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = CoachingConfig()

        # DB connection
        self.db: Optional[aiosqlite.Connection] = None

        # persistente View (Start-Button)
        self.bot.add_view(self.StartView(self))

        # Timeout loop (arbeitet DB-basiert)
        self._timeout_loop.start()

    # ----------------- DB helpers -----------------
    async def _db_connect(self):
        if self.db:
            return
        # NOTE: PRAGMAs (journal_mode, cache_size, etc.) are already set by the
        # central DB manager (service/db.py). Setting them again causes connection
        # corruption. DO NOT add PRAGMA calls here.
        # TODO: Refactor to use service.db async API (see REFACTORING_PLAN.md)
        self.db = await aiosqlite.connect(str(db.db_path()))
        self.db.row_factory = aiosqlite.Row
        await self._db_ensure_schema()

    async def _db_ensure_schema(self):
        assert self.db
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS coaching_sessions (
                user_id     INTEGER PRIMARY KEY,
                thread_id   INTEGER,
                match_id    TEXT,
                rank        TEXT,
                subrank     TEXT,
                hero        TEXT,
                comment     TEXT,
                step        TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active   INTEGER DEFAULT 1
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_coaching_thread ON coaching_sessions (thread_id)"
        )
        await self.db.commit()

    async def _db_upsert(self, user_id: int, **fields: Any):
        """Insert/Update Session row for user."""
        assert self.db
        allowed_fields = (
            "thread_id",
            "match_id",
            "rank",
            "subrank",
            "hero",
            "comment",
            "step",
            "is_active",
        )
        sanitized_fields = {k: v for k, v in fields.items() if k in allowed_fields}
        if not sanitized_fields:
            return

        existing = await self._db_get(user_id)
        merged: Dict[str, Any] = {}
        for key in allowed_fields:
            if key in sanitized_fields:
                merged[key] = sanitized_fields[key]
            elif existing is not None and key in existing.keys():
                merged[key] = existing[key]
            elif key == "is_active":
                merged[key] = 1
            else:
                merged[key] = None

        await self.db.execute(
            """
            INSERT INTO coaching_sessions (
                user_id, thread_id, match_id, rank, subrank, hero, comment, step, is_active, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                match_id = excluded.match_id,
                rank = excluded.rank,
                subrank = excluded.subrank,
                hero = excluded.hero,
                comment = excluded.comment,
                step = excluded.step,
                is_active = excluded.is_active,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                merged["thread_id"],
                merged["match_id"],
                merged["rank"],
                merged["subrank"],
                merged["hero"],
                merged["comment"],
                merged["step"],
                merged["is_active"],
            ),
        )
        await self.db.commit()

    async def _db_get(self, user_id: int) -> Optional[aiosqlite.Row]:
        assert self.db
        cur = await self.db.execute(
            "SELECT * FROM coaching_sessions WHERE user_id=?", (user_id,)
        )
        return await cur.fetchone()

    async def _db_get_by_thread(self, thread_id: int) -> Optional[aiosqlite.Row]:
        assert self.db
        cur = await self.db.execute(
            "SELECT * FROM coaching_sessions WHERE thread_id=?", (thread_id,)
        )
        return await cur.fetchone()

    async def _db_close_session(self, user_id: int):
        assert self.db
        await self.db.execute(
            "UPDATE coaching_sessions SET is_active=0, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (user_id,),
        )
        await self.db.commit()

    # ----------------- Emoji helpers -----------------
    @staticmethod
    def _safe_option_emoji(guild: Optional[discord.Guild], mention: str) -> Optional[discord.PartialEmoji]:
        try:
            if not guild or not mention or not mention.startswith("<:"):
                return None
            inner = mention[2:-1]
            _name, sid = inner.split(":", 1)
            em = guild.get_emoji(int(sid))
            if em:
                return discord.PartialEmoji(name=em.name, id=em.id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("safe_option_emoji failed: %r", e)
            return None
        return None

    # ----------------- Socket notify -----------------
    def _notify_claim_bot(self, thread_data: Dict[str, Any]) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((self.cfg.socket_host, self.cfg.socket_port))
                payload = json.dumps(thread_data).encode("utf-8")
                s.sendall(len(payload).to_bytes(4, byteorder="big"))
                s.sendall(payload)
        except Exception as e:
            logger.debug("notify claim bot failed: %r", e)

    # ----------------- UI -----------------
    class StartView(View):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(timeout=None)
            self.cog = cog

        @discord.ui.button(label="Match-Coaching starten", style=discord.ButtonStyle.primary, custom_id="dl_start")
        async def start(self, interaction: discord.Interaction, _button: Button):
            await interaction.response.send_modal(DlCoachingCog.MatchIDModal(self.cog))

    class MatchIDModal(Modal):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(title="Match ID Eingeben")
            self.cog = cog
            self.match_id = TextInput(label="Match ID", placeholder="z.B. 12345-ABCDE", max_length=50)
            self.add_item(self.match_id)

        async def on_submit(self, interaction: discord.Interaction):
            await self.cog._db_connect()

            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("match modal defer failed: %r", e)

            base_channel = interaction.channel
            if not isinstance(base_channel, discord.TextChannel):
                try:
                    await interaction.followup.send("❌ Bitte im Textkanal ausführen.", ephemeral=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("match modal followup failed: %r", e)
                return

            thread = await base_channel.create_thread(
                name=f"Match-Coaching: {interaction.user.display_name}",
                type=discord.ChannelType.private_thread,
            )
            try:
                await thread.add_user(interaction.user)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("add_user to thread failed: %r", e)

            await self.cog._db_upsert(
                interaction.user.id,
                thread_id=thread.id,
                match_id=str(self.match_id.value),
                step="rank",
                is_active=1,
            )

            emb = discord.Embed(
                title="Deadlock Match-Coaching",
                description=f"Match-ID: **{self.match_id.value}**\n\nBitte wähle deinen Rang.",
                color=discord.Color.blue(),
            )
            await thread.send(embed=emb, view=DlCoachingCog.RankView(self.cog, thread.guild))

            try:
                await interaction.followup.send(f"Thread erstellt: {thread.mention}", ephemeral=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("match modal followup send failed: %r", e)

    class RankSelect(Select):
        def __init__(self, cog: "DlCoachingCog", guild: Optional[discord.Guild]):
            self.cog = cog
            self.guild = guild
            options = []
            for key, mention in cog.RANKS.items():
                em = DlCoachingCog._safe_option_emoji(guild, mention)
                options.append(discord.SelectOption(label=key.title(), value=key, emoji=em))
            super().__init__(placeholder="Wähle deinen Rang", min_values=1, max_values=1, options=options, custom_id="dl_rank")

        async def callback(self, interaction: discord.Interaction):
            await self.cog._db_connect()
            uid = interaction.user.id
            await self.cog._db_upsert(uid, rank=self.values[0], step="subrank")

            row = await self.cog._db_get(uid)
            emb = discord.Embed(
                title="Deadlock Match-Coaching",
                description=f"Match-ID: {row['match_id']}\nRang: {row['rank']} {self.cog.RANKS.get(row['rank'],'')}\n\nBitte wähle deinen Subrang.",
                color=discord.Color.blue(),
            )
            await interaction.response.edit_message(embed=emb, view=DlCoachingCog.SubrankView(self.cog))

    class RankView(View):
        def __init__(self, cog: "DlCoachingCog", guild: Optional[discord.Guild]):
            super().__init__(timeout=None)
            self.add_item(DlCoachingCog.RankSelect(cog, guild))

    class SubrankSelect(Select):
        def __init__(self, cog: "DlCoachingCog"):
            self.cog = cog
            options = [
                discord.SelectOption(label="I", value="i"),
                discord.SelectOption(label="II", value="ii"),
                discord.SelectOption(label="III", value="iii"),
                discord.SelectOption(label="IV", value="iv"),
                discord.SelectOption(label="V", value="v"),
                discord.SelectOption(label="✶", value="✶"),
            ]
            super().__init__(placeholder="Wähle deinen Subrang", min_values=1, max_values=1, options=options, custom_id="dl_subrank")

        async def callback(self, interaction: discord.Interaction):
            await self.cog._db_connect()
            uid = interaction.user.id
            await self.cog._db_upsert(uid, subrank=self.values[0], step="hero")

            row = await self.cog._db_get(uid)
            emb = discord.Embed(
                title="Deadlock Match-Coaching",
                description=f"Match-ID: {row['match_id']}\nRang: {row['rank']} {self.cog.RANKS.get(row['rank'],'')}\nSubrang: {row['subrank']}\n\nBitte wähle deinen Helden.",
                color=discord.Color.blue(),
            )
            await interaction.response.edit_message(embed=emb, view=DlCoachingCog.HeroView(self.cog, interaction.guild))

    class SubrankView(View):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(timeout=None)
            self.add_item(DlCoachingCog.SubrankSelect(cog))

    class HeroSelectPage1(Select):
        def __init__(self, cog: "DlCoachingCog", guild: Optional[discord.Guild]):
            self.cog = cog
            options = []
            for name, mention in cog.HEROES_PAGE_1.items():
                em = DlCoachingCog._safe_option_emoji(guild, mention)
                options.append(discord.SelectOption(label=name.replace('_',' ').title(), value=name, emoji=em))
            super().__init__(placeholder="Helden (A–M)", min_values=1, max_values=1, options=options, custom_id="dl_hero_p1")

        async def callback(self, interaction: discord.Interaction):
            await DlCoachingCog._hero_selected(self.cog, interaction, self.values[0])

    class HeroSelectPage2(Select):
        def __init__(self, cog: "DlCoachingCog", guild: Optional[discord.Guild]):
            self.cog = cog
            options = []
            for name, mention in cog.HEROES_PAGE_2.items():
                em = DlCoachingCog._safe_option_emoji(guild, mention)
                options.append(discord.SelectOption(label=name.replace('_',' ').title(), value=name, emoji=em))
            super().__init__(placeholder="Helden (N–Z)", min_values=1, max_values=1, options=options, custom_id="dl_hero_p2")

        async def callback(self, interaction: discord.Interaction):
            await DlCoachingCog._hero_selected(self.cog, interaction, self.values[0])

    class HeroView(View):
        def __init__(self, cog: "DlCoachingCog", guild: Optional[discord.Guild]):
            super().__init__(timeout=None)
            self.add_item(DlCoachingCog.HeroSelectPage1(cog, guild))
            self.add_item(DlCoachingCog.HeroSelectPage2(cog, guild))

    @staticmethod
    async def _hero_selected(cog: "DlCoachingCog", interaction: discord.Interaction, hero_value: str):
        await cog._db_connect()
        uid = interaction.user.id
        await cog._db_upsert(uid, hero=hero_value, step="comment")

        row = await cog._db_get(uid)
        emb = discord.Embed(
            title="Deadlock Match-Coaching",
            description=(
                f"Match-ID: {row['match_id']}\n"
                f"Rang: {row['rank']} {cog.RANKS.get(row['rank'],'')}\n"
                f"Subrang: {row['subrank']}\n"
                f"Held: {row['hero']}\n\n"
                f"Klicke auf den Button, um deinen Kommentar einzugeben."
            ),
            color=discord.Color.blue(),
        )
        await interaction.response.edit_message(embed=emb, view=DlCoachingCog.CommentView(cog))

    class CommentModal(Modal):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(title="Kommentar eingeben")
            self.cog = cog
            self.comment = TextInput(label="Kommentar", style=discord.TextStyle.paragraph, max_length=1000)
            self.add_item(self.comment)

        async def on_submit(self, interaction: discord.Interaction):
            try:
                await self.cog._db_connect()
                uid = interaction.user.id

                # Sofort deferren → stabilere UX
                try:
                    if not interaction.response.is_done():
                        await interaction.response.defer(ephemeral=True, thinking=False)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("comment modal defer failed: %r", e)

                await self.cog._db_upsert(uid, comment=str(self.comment.value), step="finish")
                row = await self.cog._db_get(uid)

                rank_key = row["rank"] or ""
                rank_emoji = self.cog.RANKS.get(rank_key, "")

                emb = discord.Embed(title="Zusammenfassung deines Coachings", color=discord.Color.green())
                emb.add_field(name="Match ID", value=row["match_id"], inline=False)
                emb.add_field(name="Rang", value=f"{row['rank']} {rank_emoji}", inline=False)
                emb.add_field(name="Subrang", value=row["subrank"], inline=False)
                emb.add_field(name="Held", value=row["hero"], inline=False)
                emb.add_field(name="Kommentar", value=row["comment"] or "-", inline=False)

                await interaction.followup.send(embed=emb, view=DlCoachingCog.FinishView(self.cog), ephemeral=True)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Comment submit failed: %s", e)
                # Fallback-Fehlerausgabe
                sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
                try:
                    await sender("❌ Etwas ist schiefgelaufen. Bitte erneut versuchen.", ephemeral=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e2:
                    logger.debug("comment modal error reply failed: %r", e2)

    class CommentView(View):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(timeout=None)
            self.cog = cog

        @discord.ui.button(label="Kommentar eingeben", style=discord.ButtonStyle.primary, custom_id="dl_comment")
        async def open_comment(self, interaction: discord.Interaction, _button: Button):
            await interaction.response.send_modal(DlCoachingCog.CommentModal(self.cog))

    class FinishView(View):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(timeout=None)
            self.cog = cog

        @discord.ui.button(label="Abschließen", style=discord.ButtonStyle.success, custom_id="dl_finish")
        async def finish(self, interaction: discord.Interaction, _button: Button):
            await self.cog._db_connect()
            uid = interaction.user.id
            row = await self.cog._db_get(uid)
            if not row:
                await interaction.response.send_message("Keine Daten gefunden.", ephemeral=True)
                return  # <- kein Wert zurückgeben (CodeQL fix)

            channel = interaction.channel
            if not isinstance(channel, (discord.Thread, discord.TextChannel)):
                await interaction.response.send_message("Ungültiger Kanal.", ephemeral=True)
                return  # <- kein Wert zurückgeben (CodeQL fix)

            content = (
                f"**Match-Coaching**\n\n"
                f"**Match ID:** {row['match_id']}\n"
                f"**Rang:** {row['rank']} {self.cog.RANKS.get(row['rank'], '')}\n"
                f"**Subrang:** {row['subrank']}\n"
                f"**Held:** {row['hero']}\n"
                f"**Kommentar:** {row['comment'] or '-'}\n\n"
                f"_______________________________\n"
                f"Analysiert von: <@{uid}>\n"
                f"Coaching abgeschlossen! Danke für deine Eingaben."
            )
            try:
                if not interaction.response.is_done():
                    await interaction.response.edit_message(view=None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("finish edit clear view failed: %r", e)

            await channel.send(content)

            # Optional: externen Bot informieren
            notif = {
                "thread_id": getattr(channel, "id", None),
                "match_id": row["match_id"],
                "rank": row["rank"],
                "subrank": row["subrank"],
                "hero": row["hero"],
                "user_id": uid,
            }
            self.cog._notify_claim_bot(notif)

            # Session schließen & Thread archivieren
            await self.cog._db_close_session(uid)
            try:
                if isinstance(channel, discord.Thread):
                    await channel.edit(archived=True, locked=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("thread archive failed: %r", e)

            try:
                await interaction.followup.send("✅ Coaching abgeschlossen.", ephemeral=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("finish followup failed: %r", e)

    # ----------------- Timeout & Lifecycle -----------------
    @tasks.loop(seconds=5)
    async def _timeout_loop(self):
        # DB-basiert: inaktive Sessions schließen
        try:
            await self._db_connect()
            assert self.db
            cur = await self.db.execute(
                "SELECT user_id, thread_id, updated_at FROM coaching_sessions WHERE is_active=1"
            )
            rows = await cur.fetchall()
            now = datetime.datetime.utcnow()
            for r in rows:
                try:
                    updated = _parse_ts(r["updated_at"])
                    if (now - updated).total_seconds() > self.cfg.timeout_seconds:
                        thread = self.bot.get_channel(int(r["thread_id"]))
                        if isinstance(thread, discord.Thread):
                            try:
                                await thread.send("⏱️ Timeout erreicht. Thread wird geschlossen.")
                                await thread.edit(archived=True, locked=True)
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                logger.debug("timeout thread notify/edit failed: %r", e)
                        await self._db_close_session(int(r["user_id"]))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Timeout check row failed: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Timeout loop error: {e}")

    @_timeout_loop.before_loop
    async def _before_timeout(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        # Coaching-Nachricht im Zielkanal mit StartView versehen
        await self._db_connect()
        ch = self.bot.get_channel(self.cfg.channel_id)
        if isinstance(ch, discord.TextChannel):
            msg = None
            if self.cfg.existing_message_id:
                try:
                    msg = await ch.fetch_message(self.cfg.existing_message_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("fetch existing coaching message failed: %r", e)
                    msg = None
            if not msg:
                try:
                    async for m in ch.history(limit=50):
                        if (
                            m.author.id == self.bot.user.id
                            and m.embeds
                            and "Deadlock Match-Coaching" in (m.embeds[0].title or "")
                        ):
                            msg = m
                            break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("scan channel history failed: %r", e)
                    msg = None
            try:
                if msg:
                    await msg.edit(view=self.StartView(self))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("attach StartView to message failed: %r", e)

    def cog_unload(self):
        try:
            if self._timeout_loop.is_running():
                self._timeout_loop.cancel()
        except Exception as e:
            logger.debug("timeout loop cancel failed: %r", e)
        if self.db:
            asyncio.create_task(self.db.close())


# ----------------- kleine Utils -----------------
def _parse_ts(val) -> datetime.datetime:
    """
    aiosqlite liefert TIMESTAMP als str (SQLite). Wir interpretieren beide Varianten.
    """
    if isinstance(val, datetime.datetime):
        return val
    if isinstance(val, str):
        # SQLite default CURRENT_TIMESTAMP -> "YYYY-MM-DD HH:MM:SS"
        try:
            return datetime.datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                # ISO fallback
                return datetime.datetime.fromisoformat(val.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception as e:
                logger.debug("parse_ts iso parse failed: %r", e)
    return datetime.datetime.utcnow()


# ----------------- Setup -----------------
async def setup(bot: commands.Bot):
    await bot.add_cog(DlCoachingCog(bot))
    logger.info("DlCoachingCog (DB-first) geladen")
