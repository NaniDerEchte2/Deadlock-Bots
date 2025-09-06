# =========================================
# Deadlock-Bots – Umstellung auf zentrale SQLite-DB
# =========================================
# Hinweis:
# - Alle Bots/Cogs nutzen nun EIN gemeinsames SQLite-File:
#   %USERPROFILE%/Documents/Deadlock/service/deadlock.sqlite3
#   (überschreibbar über Env: DEADLOCK_DB_DIR)
# - JSON-Dateien & Einzel-DBs sind entfernt. Ein einmaliges Migrations-
#   script ist enthalten (service/migrate_to_central_db.py).
# - WAL, FOREIGN_KEYS, Vacuum-on-shutdown aktiviert.
# - Python ≥ 3.10, discord.py ≥ 2.3
# - Stelle sicher, dass .env Tokens etc. weiterhin gepflegt sind.
#
# Dateien in diesem Refactor:
#  - shared/db.py (DB-Core, Schema, Helpers, Migrations-Infra)
#  - service/migrate_to_central_db.py (Einmaliges Merge-Script JSON→DB)
#  - main_bot.py (lädt Cogs, init DB, Admin-Kommandos)
#  - cogs/tempvoice.py (TempVoice mit zentraler DB)
#  - cogs/voice_activity_tracker.py (Voice-Tracking in DB)
#  - cogs/rank_voice_manager.py (Ranked-Voice Gatekeeping)
#  - cogs/deadlock_team_balancer.py (Team Balance, nutzt ranks-Tabelle)
#  - cogs/welcome_dm.py (Onboarding/Präferenzen in DB)
#  - Standalone/rank_bot/standalone_rank_bot.py (Ranks UI + DB)
#  - cogs/changelog_discord_bot.py (Subscriptions + Posted IDs in DB)
#
# =========================================
# File: shared/db.py
# =========================================

import os
import atexit
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

DB_ENV = "DEADLOCK_DB_DIR"
DEFAULT_DIR = os.path.expandvars(r"%USERPROFILE%/Documents/Deadlock/service")
DB_NAME = "deadlock.sqlite3"

_CONN: Optional[sqlite3.Connection] = None
_LOCK = threading.RLock()


def _db_file() -> str:
    root = os.environ.get(DB_ENV) or DEFAULT_DIR
    Path(root).mkdir(parents=True, exist_ok=True)
    return str(Path(root) / DB_NAME)


def connect() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        _CONN = sqlite3.connect(
            _db_file(), check_same_thread=False, isolation_level=None
        )
        _CONN.row_factory = sqlite3.Row
        with _LOCK:
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.execute("PRAGMA synchronous=NORMAL")
            _CONN.execute("PRAGMA foreign_keys=ON")
            init_schema(_CONN)
    return _CONN


def init_schema(conn: Optional[sqlite3.Connection] = None) -> None:
    c = conn or connect()
    with _LOCK:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version(
              version INTEGER NOT NULL
            );
            INSERT INTO schema_version(version)
              SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);

            -- generische KV-Ablage (für migrierte/sonstige Einstellungen)
            CREATE TABLE IF NOT EXISTS kv_store(
              ns TEXT NOT NULL,
              k  TEXT NOT NULL,
              v  TEXT NOT NULL,
              PRIMARY KEY(ns, k)
            );

            -- Nutzer/Guild Basis
            CREATE TABLE IF NOT EXISTS users(
              user_id    INTEGER PRIMARY KEY,
              name       TEXT,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS guild_settings(
              guild_id INTEGER PRIMARY KEY,
              key      TEXT,
              value    TEXT
            );

            -- Onboarding & Präferenzen
            CREATE TABLE IF NOT EXISTS onboarding_sessions(
              user_id   INTEGER PRIMARY KEY,
              step      TEXT,
              data_json TEXT,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_preferences(
              user_id      INTEGER PRIMARY KEY,
              funny_custom INTEGER DEFAULT 0,
              grind_custom INTEGER DEFAULT 0,
              patch_notes  INTEGER DEFAULT 0,
              rank         INTEGER,
              updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Ranks
            CREATE TABLE IF NOT EXISTS ranks(
              user_id   INTEGER PRIMARY KEY,
              rank      INTEGER NOT NULL,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- TempVoice
            CREATE TABLE IF NOT EXISTS temp_voice_channels(
              channel_id INTEGER PRIMARY KEY,
              owner_id   INTEGER NOT NULL,
              name       TEXT,
              user_limit INTEGER,
              privacy    TEXT DEFAULT 'public',
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              deleted_at DATETIME
            );

            -- Voice Tracking
            CREATE TABLE IF NOT EXISTS voice_sessions(
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id    INTEGER NOT NULL,
              channel_id INTEGER NOT NULL,
              joined_at  DATETIME NOT NULL,
              left_at    DATETIME,
              seconds    INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_voice_sessions_user_open
              ON voice_sessions(user_id, left_at);
            CREATE TABLE IF NOT EXISTS voice_stats(
              user_id       INTEGER PRIMARY KEY,
              total_seconds INTEGER NOT NULL DEFAULT 0,
              last_update   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Team Balancer / Matches
            CREATE TABLE IF NOT EXISTS matches(
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              guild_id   INTEGER,
              created_by INTEGER,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              data_json  TEXT
            );

            -- Changelog Bot
            CREATE TABLE IF NOT EXISTS changelog_subscriptions(
              guild_id      INTEGER PRIMARY KEY,
              channel_id    INTEGER NOT NULL,
              role_ping_id  INTEGER
            );
            CREATE TABLE IF NOT EXISTS posted_changelogs(
              id        TEXT PRIMARY KEY,
              posted_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with _LOCK:
        connect().execute(sql, params)


def executemany(sql: str, seq_of_params: Iterable[Iterable[Any]]) -> None:
    with _LOCK:
        connect().executemany(sql, seq_of_params)


def query_one(sql: str, params: Iterable[Any] = ()):  # -> sqlite3.Row | None
    with _LOCK:
        cur = connect().execute(sql, params)
        return cur.fetchone()


def query_all(sql: str, params: Iterable[Any] = ()):  # -> list[sqlite3.Row]
    with _LOCK:
        cur = connect().execute(sql, params)
        return cur.fetchall()


def set_kv(ns: str, k: str, v: str) -> None:
    execute(
        """
        INSERT INTO kv_store(ns,k,v) VALUES(?,?,?)
        ON CONFLICT(ns,k) DO UPDATE SET v=excluded.v
        """,
        (ns, k, v),
    )


def get_kv(ns: str, k: str) -> Optional[str]:
    row = query_one("SELECT v FROM kv_store WHERE ns=? AND k=?", (ns, k))
    return row[0] if row else None


@atexit.register
def _vacuum_on_shutdown() -> None:
    try:
        with _LOCK:
            c = connect()
            c.execute("VACUUM")
    except Exception:
        pass


# =========================================
# File: service/migrate_to_central_db.py
# =========================================

if __name__ == "__main__" and False:
    # Dieser Block wird nur als Marker im Canvas gezeigt –
    # die eigentliche Datei folgt unten als eigenständige Datei.
    pass

# --- separate Datei beginnt hier ---
# service/migrate_to_central_db.py

import json
import os
from pathlib import Path
from typing import Any

from shared import db

ROOT = Path(__file__).resolve().parents[1]

IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except Exception:
        return str(p)


def import_generic_json(json_path: Path) -> None:
    ns = f"legacy_json:{_rel(json_path)}"
    try:
        text = json_path.read_text(encoding="utf-8")
        db.set_kv(ns, "raw", text)
        print(f"[OK] JSON in kv_store: {ns}")
    except Exception as e:
        print(f"[WARN] Konnte {_rel(json_path)} nicht lesen: {e}")


def migrate_tempvoice_json(json_path: Path) -> None:
    # Falls bekannte Struktur erkennbar ist, mappen wir direkt in temp_voice_channels,
    # sonst landet der Dump in kv_store.
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        # Erwartete Minimal-Struktur (flexibel gehalten):
        # {"channels": [{"channel_id": 123, "owner_id": 456, "name": "...", "user_limit": 4, "privacy": "public"}, ...]}
        items = []
        if isinstance(data, dict) and "channels" in data and isinstance(data["channels"], list):
            for it in data["channels"]:
                ch_id = it.get("channel_id")
                owner = it.get("owner_id")
                if not ch_id or not owner:
                    continue
                name = it.get("name")
                limit = it.get("user_limit")
                privacy = it.get("privacy") or "public"
                items.append((ch_id, owner, name, limit, privacy))
        if items:
            db.executemany(
                """
                INSERT INTO temp_voice_channels(channel_id,owner_id,name,user_limit,privacy)
                VALUES(?,?,?,?,?)
                ON CONFLICT(channel_id) DO UPDATE SET
                  owner_id=excluded.owner_id,
                  name=excluded.name,
                  user_limit=excluded.user_limit,
                  privacy=excluded.privacy
                """,
                items,
            )
            print(f"[OK] {len(items)} TempVoice-Einträge aus {_rel(json_path)} übernommen")
        else:
            import_generic_json(json_path)
    except Exception as e:
        print(f"[WARN] TempVoice-JSON nicht erkannt, speichere roh: {e}")
        import_generic_json(json_path)


def run() -> None:
    db.connect()  # stellt Schema sicher

    # Spezifisch bekannter Alt-Pfad aus Repo-Root
    tv = ROOT / "tempvoice_data.json"
    if tv.exists():
        migrate_tempvoice_json(tv)
        # Nach erfolgreicher Migration kannst du die Datei manuell löschen/archivieren.

    # Generische JSON-Übernahme (alles außer IGNORED_DIRS)
    for p in ROOT.rglob("*.json"):
        if any(part in IGNORED_DIRS for part in p.parts):
            continue
        if p == tv:
            continue  # bereits verarbeitet
        import_generic_json(p)

    print("[DONE] Migration abgeschlossen. Prüfe kv_store für ungemappte Daten.")


if __name__ == "__main__":
    run()


# =========================================
# File: main_bot.py
# =========================================

import asyncio
import logging
import os
from typing import List

import discord
from discord.ext import commands

from shared import db

INTENTS = discord.Intents(
    guilds=True,
    members=True,
    voice_states=True,
    messages=True,
    message_content=True,
)

COGS: List[str] = [
    "cogs.tempvoice",
    "cogs.voice_activity_tracker",
    "cogs.rank_voice_manager",
    "cogs.deadlock_team_balancer",
    "cogs.welcome_dm",
    "cogs.changelog_discord_bot",
]


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


class MasterBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!master ", intents=INTENTS)
        self.remove_command("help")

    async def setup_hook(self) -> None:
        # DB initialisieren
        db.connect()
        # Cogs laden
        for cog in COGS:
            try:
                await self.load_extension(cog)
                logging.info("🔍 Auto-loaded cog: %s", cog)
            except Exception as e:
                logging.exception("Fehler beim Laden von %s: %s", cog, e)

    async def on_ready(self) -> None:
        logging.info("Master Bot online als %s", self.user)


bot = MasterBot()


@bot.command(name="reload")
@commands.is_owner()
async def reload_cog(ctx: commands.Context, cog_name: str):
    try:
        await bot.reload_extension(cog_name)
        await ctx.reply(f"✅ `{cog_name}` neu geladen")
    except Exception as e:
        await ctx.reply(f"❌ Fehler: {e}")


@bot.command(name="cog_status")
@commands.is_owner()
async def cog_status(ctx: commands.Context):
    loaded = sorted(bot.extensions.keys())
    await ctx.reply("Geladene Cogs:\n" + "\n".join(f"- {c}" for c in loaded))


@bot.command(name="cleanup")
@commands.is_owner()
async def cleanup(ctx: commands.Context):
    # Platzhalter – TempVoice hat eigenes Cleanup
    await ctx.reply("Nix wildes zu tun – TempVoice Cleanup separat.")


if __name__ == "__main__":
    setup_logging()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN fehlt in der Umgebung/.env")
    bot.run(token)


# =========================================
# File: cogs/tempvoice.py
# =========================================

import logging
from typing import Optional

import discord
from discord.ext import commands, tasks

from shared import db

log = logging.getLogger("TempVoice")

# Env für Quell-/Eltern-Channel optional
CREATE_CHANNEL_ID = int(os.getenv("TEMPVOICE_CREATE_CHANNEL_ID", "0"))  # optional
PARENT_CATEGORY_ID = int(os.getenv("TEMPVOICE_PARENT_CATEGORY_ID", "0"))  # optional


class TempVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cleanup_old_channels.start()

    def cog_unload(self) -> None:
        self.cleanup_old_channels.cancel()

    @tasks.loop(minutes=10)
    async def cleanup_old_channels(self):
        # Löscht verwaiste Channels (existiert in Guild nicht mehr, aber in DB)
        for row in db.query_all(
            "SELECT channel_id FROM temp_voice_channels WHERE deleted_at IS NULL"
        ):
            ch_id = int(row[0])
            ch = self.bot.get_channel(ch_id)
            if ch is None:
                db.execute(
                    "UPDATE temp_voice_channels SET deleted_at=CURRENT_TIMESTAMP WHERE channel_id=?",
                    (ch_id,),
                )
                log.info("TempVoice: Markiere gelöschten Channel %s", ch_id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        # User joint in Create-Channel → eigenen Temp-Channel erzeugen
        if after.channel and CREATE_CHANNEL_ID and after.channel.id == CREATE_CHANNEL_ID:
            await self._ensure_temp_channel(member)

        # TempChannel wird leer → als gelöscht markieren
        if before.channel and before.channel != after.channel:
            await self._check_channel_empty(before.channel)
        if after.channel and before.channel != after.channel:
            await self._check_channel_empty(after.channel)

    async def _ensure_temp_channel(self, owner: discord.Member) -> None:
        guild = owner.guild
        parent = guild.get_channel(PARENT_CATEGORY_ID) if PARENT_CATEGORY_ID else None
        name = f"{owner.display_name}'s Room"
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=True, view_channel=True)
        }
        ch = await guild.create_voice_channel(name=name, category=parent, overwrites=overwrites)
        db.execute(
            """
            INSERT INTO temp_voice_channels(channel_id,owner_id,name,user_limit,privacy)
            VALUES(?,?,?,?,?)
            ON CONFLICT(channel_id) DO UPDATE SET owner_id=excluded.owner_id, name=excluded.name
            """,
            (ch.id, owner.id, name, None, "public"),
        )
        try:
            await owner.move_to(ch)
        except Exception:
            pass
        log.info("TempVoice: Created %s for %s", ch.id, owner.id)

    async def _check_channel_empty(self, channel: discord.VoiceChannel) -> None:
        row = db.query_one(
            "SELECT channel_id FROM temp_voice_channels WHERE channel_id=? AND deleted_at IS NULL",
            (channel.id,),
        )
        if not row:
            return
        if len(channel.members) == 0:
            await channel.delete(reason="TempVoice empty – cleanup")
            db.execute(
                "UPDATE temp_voice_channels SET deleted_at=CURRENT_TIMESTAMP WHERE channel_id=?",
                (channel.id,),
            )
            log.info("TempVoice: Deleted empty %s", channel.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TempVoiceCog(bot))


# =========================================
# File: cogs/voice_activity_tracker.py
# =========================================

import logging
import datetime as dt

import discord
from discord.ext import commands

from shared import db

log = logging.getLogger("VoiceTracker")


class VoiceTracker(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        now = dt.datetime.utcnow()
        # Leave-Event
        if before.channel and before.channel != after.channel:
            row = db.query_one(
                "SELECT id, joined_at FROM voice_sessions WHERE user_id=? AND left_at IS NULL ORDER BY id DESC LIMIT 1",
                (member.id,),
            )
            if row:
                joined_at = dt.datetime.fromisoformat(row[1]) if isinstance(row[1], str) else row[1]
                seconds = int((now - joined_at).total_seconds())
                db.execute(
                    "UPDATE voice_sessions SET left_at=?, seconds=? WHERE id=?",
                    (now.isoformat(), seconds, row[0]),
                )
                stat = db.query_one("SELECT total_seconds FROM voice_stats WHERE user_id=?", (member.id,))
                total = (stat[0] if stat else 0) + max(0, seconds)
                db.execute(
                    "INSERT INTO voice_stats(user_id,total_seconds) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET total_seconds=excluded.total_seconds, last_update=CURRENT_TIMESTAMP",
                    (member.id, total),
                )
                log.info("Voice: %s +%ss (total %ss)", member.id, seconds, total)
        # Join-Event
        if after.channel and before.channel != after.channel:
            db.execute(
                "INSERT INTO voice_sessions(user_id,channel_id,joined_at) VALUES(?,?,?)",
                (member.id, after.channel.id, now.isoformat()),
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceTracker(bot))


# =========================================
# File: cogs/rank_voice_manager.py
# =========================================

import os
import logging

import discord
from discord.ext import commands

from shared import db

log = logging.getLogger("RankVoiceMgr")

RANK_CATEGORY_ID = int(os.getenv("RANK_CATEGORY_ID", "1357422957017698478"))
MIN_RANK_ALLOWED = int(os.getenv("MIN_RANK_ALLOWED", "0"))  # z.B. 0 = Obscurus


class RankVoiceManager(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _user_rank(self, user_id: int) -> int:
        row = db.query_one("SELECT rank FROM ranks WHERE user_id=?", (user_id,))
        return int(row[0]) if row and row[0] is not None else 0

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # Gatekeeping nur in Rank-Kategorie – User mit zu niedrigem Rank kicken
        if after.channel and after.channel.category and after.channel.category.id == RANK_CATEGORY_ID:
            rank = self._user_rank(member.id)
            if rank < MIN_RANK_ALLOWED:
                try:
                    await member.move_to(None, reason="Rank zu niedrig für Ranked-Voice")
                except Exception:
                    pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RankVoiceManager(bot))


# =========================================
# File: cogs/deadlock_team_balancer.py
# =========================================

import json
import logging
from typing import List

import discord
from discord.ext import commands

from shared import db

log = logging.getLogger("TeamBalancer")

RANK_NAMES = [
    "Obscurus", "Initiate", "Seeker", "Alchemist", "Arcanist",
    "Ritualist", "Emissary", "Archon", "Oracle", "Phantom",
    "Ascendant", "Eternus",
]


class TeamBalancer(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def rank_of(self, user_id: int) -> int:
        row = db.query_one("SELECT rank FROM ranks WHERE user_id=?", (user_id,))
        return int(row[0]) if row and row[0] is not None else 0

    @commands.command(name="balance")
    @commands.has_permissions(administrator=True)
    async def balance(self, ctx: commands.Context):
        members = [m for m in ctx.author.voice.channel.members] if (ctx.author.voice and ctx.author.voice.channel) else []
        if len(members) < 2:
            return await ctx.reply("Zu wenig Spieler im Voice.")
        scored = sorted([(self.rank_of(m.id), m) for m in members], key=lambda x: x[0], reverse=True)
        team_a, team_b = [], []
        sum_a = sum_b = 0
        for score, m in scored:
            if sum_a <= sum_b:
                team_a.append(m)
                sum_a += score
            else:
                team_b.append(m)
                sum_b += score
        embed = discord.Embed(title="⚖️ Team Balance")
        embed.add_field(name=f"Team A ({sum_a})", value="\n".join(m.mention for m in team_a), inline=True)
        embed.add_field(name=f"Team B ({sum_b})", value="\n".join(m.mention for m in team_b), inline=True)
        await ctx.reply(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TeamBalancer(bot))


# =========================================
# File: cogs/welcome_dm.py
# =========================================

import json
import logging

import discord
from discord.ext import commands

from shared import db

log = logging.getLogger("WelcomeDM")

FUNNY_CUSTOM_ROLE_ID = int(os.getenv("FUNNY_CUSTOM_ROLE_ID", "1407085699374649364"))
GRIND_CUSTOM_ROLE_ID = int(os.getenv("GRIND_CUSTOM_ROLE_ID", "1407086020331311144"))
PATCH_ROLE_ID = int(os.getenv("PATCH_ROLE_ID", "1330994309524357140"))


class WelcomeDMCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="onboard")
    @commands.has_permissions(administrator=True)
    async def onboard(self, ctx: commands.Context, member: discord.Member | None = None):
        member = member or ctx.author
        try:
            dm = await member.create_dm()
            await dm.send(
                "Willkommen! Wähle bitte deine Präferenzen (Funny/Grind/Patch). Antworte mit JSON, z.B.: {\"funny\":true,\"grind\":false,\"patch\":true}"
            )
        except Exception:
            await ctx.reply("Konnte keine DM senden.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not isinstance(message.channel, discord.DMChannel):
            return
        if message.author.bot:
            return
        # Einfaches JSON-Parsing – deine vorherige komplexe UI kann aufgesetzt werden,
        # die Speicherung liegt nun in der zentralen DB.
        try:
            data = json.loads(message.content)
            funny = 1 if data.get("funny") else 0
            grind = 1 if data.get("grind") else 0
            patch = 1 if data.get("patch") else 0
            db.execute(
                """
                INSERT INTO user_preferences(user_id,funny_custom,grind_custom,patch_notes)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                  funny_custom=excluded.funny_custom,
                  grind_custom=excluded.grind_custom,
                  patch_notes=excluded.patch_notes,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (message.author.id, funny, grind, patch),
            )
            await message.channel.send("Danke, gespeichert.")
        except Exception as e:
            await message.channel.send(f"Ungültiges Format: {e}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeDMCog(bot))


# =========================================
# File: Standalone/rank_bot/standalone_rank_bot.py
# =========================================

import asyncio
import logging
import os

import discord
from discord.ext import commands

from shared import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

INTENTS = discord.Intents(guilds=True, members=True, messages=True, message_content=True)

RANKS = [
    "Obscurus", "Initiate", "Seeker", "Alchemist", "Arcanist",
    "Ritualist", "Emissary", "Archon", "Oracle", "Phantom",
    "Ascendant", "Eternus",
]


class RankSelect(discord.ui.Select):
    def __init__(self):
        opts = [discord.SelectOption(label=f"{i} – {name}", value=str(i)) for i, name in enumerate(RANKS)]
        super().__init__(placeholder="Wähle deinen Rank", options=opts, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        sel = int(self.values[0])
        db.execute(
            "INSERT INTO ranks(user_id,rank) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET rank=excluded.rank, updated_at=CURRENT_TIMESTAMP",
            (interaction.user.id, sel),
        )
        await interaction.response.send_message(f"Gespeichert: Rank {sel}", ephemeral=True)


class RankView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RankSelect())


class RankBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!r ", intents=INTENTS)

    async def setup_hook(self) -> None:
        db.connect()
        self.add_view(RankView())  # persistent

    async def on_ready(self) -> None:
        logging.info("Standalone Rank Bot online als %s", self.user)


bot = RankBot()


@bot.command(name="deploy")
@commands.has_permissions(administrator=True)
async def deploy(ctx: commands.Context):
    await ctx.send("Wähle deinen Rank:", view=RankView())


if __name__ == "__main__":
    token = os.getenv("RANK_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("RANK_BOT_TOKEN/BOT_TOKEN fehlt")
    bot.run(token)


# =========================================
# File: cogs/changelog_discord_bot.py
# =========================================

import logging
import discord
from discord.ext import commands, tasks
from shared import db

log = logging.getLogger("ChangelogBot")


class ChangelogBot(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Beispiel: Poll-Task disabled – implementiere deine echte Quelle
        # self.poll.start()

    def cog_unload(self) -> None:
        pass  # ggf. self.poll.cancel()

    @commands.command(name="chlog_set")
    @commands.has_permissions(administrator=True)
    async def chlog_set(self, ctx: commands.Context, channel: discord.TextChannel, role_ping: discord.Role | None = None):
        db.execute(
            """
            INSERT INTO changelog_subscriptions(guild_id,channel_id,role_ping_id)
            VALUES(?,?,?)
            ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, role_ping_id=excluded.role_ping_id
            """,
            (ctx.guild.id, channel.id, role_ping.id if role_ping else None),
        )
        await ctx.reply("Changelog-Channel gespeichert.")

    @commands.command(name="chlog_post")
    @commands.has_permissions(administrator=True)
    async def chlog_post(self, ctx: commands.Context, patch_id: str, *, text: str):
        # Dedupe via posted_changelogs
        if db.query_one("SELECT 1 FROM posted_changelogs WHERE id=?", (patch_id,)):
            return await ctx.reply("Schon gepostet.")
        row = db.query_one("SELECT channel_id, role_ping_id FROM changelog_subscriptions WHERE guild_id=?", (ctx.guild.id,))
        if not row:
            return await ctx.reply("Kein Changelog-Channel konfiguriert.")
        channel = ctx.guild.get_channel(int(row[0]))
        mention = f"<@&{int(row[1])}> " if row[1] else ""
        await channel.send(f"{mention}{text}")
        db.execute("INSERT INTO posted_changelogs(id) VALUES(?)", (patch_id,))
        await ctx.reply("Gepostet.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChangelogBot(bot))
