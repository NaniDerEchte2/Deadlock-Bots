import discord
from discord.ext import commands, tasks
import asyncio
import json
import queue
import re
import socket
import threading

from utils.deadlock_db import Database


class ClaimSystemCog(commands.Cog):
    """Claim-System als vollwertiger Cog (portiert aus original_scripts/Claim-System.py)."""

    SOCKET_HOST = "localhost"
    SOCKET_PORT = 45680
    STAFF_ROLE_ID = 1371929762913587292

    USERS = {
        "ashty": {
            "id": 219209674019438592,
            "heroes": [
                "kelvin",
                "ivy",
                "lash",
                "sinclair",
                "mcginnis",
                "viscous",
                "abrams",
                "paradox",
                "mo",
                "holiday",
            ],
        },
        "yourmomgosky": {
            "id": 678968588203327539,
            "heroes": ["lady_geist", "warden", "wraith", "kelvin", "ivy"],
        },
        "naniderechte": {
            "id": 662995601738170389,
            "heroes": [],
        },
        "hucci1789": {
            "id": 410856005551915008,
            "heroes": ["abrams", "paradox", "ivy", "dynamo", "pocket"],
        },
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # State
        self.claimed_threads: dict[str, int | None] = {}
        self.notification_queue: "queue.Queue[dict]" = queue.Queue()

        # DB init
        self.db: Database | None = None
        self.bot.loop.create_task(self._init_db())

        # Background loop
        self._queue_loop = self._build_queue_loop()
        self._socket_thread: threading.Thread | None = None

    async def _init_db(self) -> None:
        self.db = await Database.instance()
        data = await self.db.kv_get("claim_system_claimed_threads")
        self.claimed_threads = json.loads(data) if data else {}

    async def _save_claimed_threads(self) -> None:
        if self.db is None:
            self.db = await Database.instance()
        await self.db.kv_set("claim_system_claimed_threads", json.dumps(self.claimed_threads))

    # ---------- Routing ----------
    @staticmethod
    def _rank_value(rank: str) -> int:
        mapping = {
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
        return mapping.get((rank or "").lower(), 0)

    @staticmethod
    def _subrank_value(subrank: str) -> int:
        mapping = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "✶": 6}
        key = (subrank or "").lower()
        return mapping.get(key, 0)

    def _get_assigned_user(self, rank: str, subrank: str, hero: str) -> int:
        rv = self._rank_value(rank)
        sv = self._subrank_value(subrank)
        hero_l = (hero or "").lower()

        if rv <= 8 and sv <= 1 and hero_l in self.USERS["hucci1789"]["heroes"]:
            return self.USERS["hucci1789"]["id"]
        if (rv >= 8 and sv >= 4 and hero_l in self.USERS["ashty"]["heroes"]) or (rv >= 9 and sv >= 1):
            return self.USERS["ashty"]["id"]
        if 6 <= rv <= 8 and 5 <= sv <= 4 and hero_l in self.USERS["yourmomgosky"]["heroes"]:
            return self.USERS["yourmomgosky"]["id"]
        if rv <= 6 and sv >= 5:
            return self.USERS["naniderechte"]["id"]
        return self.USERS["naniderechte"]["id"]

    # ---------- Views ----------
    class ClaimView(discord.ui.View):
        def __init__(self, cog: "ClaimSystemCog", thread_id: int, assigned_user_id: int) -> None:
            super().__init__(timeout=None)
            self.cog = cog
            self.thread_id = thread_id
            self.assigned_user_id = assigned_user_id

        @discord.ui.button(label="Coaching übernehmen", style=discord.ButtonStyle.primary, custom_id="claim_button")
        async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not any(getattr(role, "id", 0) == self.cog.STAFF_ROLE_ID for role in getattr(interaction.user, "roles", [])):
                await interaction.response.send_message("Du hast keine Berechtigung, dieses Coaching zu übernehmen.", ephemeral=True)
                return
            if str(self.thread_id) in self.cog.claimed_threads and self.cog.claimed_threads[str(self.thread_id)] is not None:
                await interaction.response.send_message(
                    f"Dieses Coaching wurde bereits von <@{self.cog.claimed_threads[str(self.thread_id)]}> übernommen.",
                    ephemeral=True,
                )
                return

            self.cog.claimed_threads[str(self.thread_id)] = interaction.user.id
            await self.cog._save_claimed_threads()
            thread = interaction.client.get_channel(self.thread_id)
            if thread:
                await thread.send(f"<@{interaction.user.id}> hat dieses Coaching übernommen.")
            await interaction.response.send_message("Du hast dieses Coaching erfolgreich übernommen.", ephemeral=True)

        @discord.ui.button(label="Freigeben", style=discord.ButtonStyle.secondary, custom_id="release_button")
        async def release_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not any(getattr(role, "id", 0) == self.cog.STAFF_ROLE_ID for role in getattr(interaction.user, "roles", [])):
                await interaction.response.send_message("Du hast keine Berechtigung, dieses Coaching freizugeben.", ephemeral=True)
                return
            if str(self.thread_id) in self.cog.claimed_threads:
                self.cog.claimed_threads[self.thread_id] = None
                await self.cog._save_claimed_threads()
            thread = interaction.client.get_channel(self.thread_id)
            if thread:
                await thread.send(
                    f"<@&{self.cog.STAFF_ROLE_ID}> Dieses Coaching wurde freigegeben und kann von jedem übernommen werden."
                )
            await interaction.response.send_message("Dieses Coaching wurde freigegeben.", ephemeral=True)

    # ---------- Socket server ----------
    def _start_socket_server(self) -> None:
        def _serve():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((self.SOCKET_HOST, self.SOCKET_PORT))
                s.listen()
                while True:
                    conn, _ = s.accept()
                    with conn:
                        try:
                            length_bytes = conn.recv(4)
                            if not length_bytes:
                                continue
                            length = int.from_bytes(length_bytes, byteorder="big")
                            payload = b""
                            while len(payload) < length:
                                chunk = conn.recv(min(4096, length - len(payload)))
                                if not chunk:
                                    break
                                payload += chunk
                            if payload:
                                try:
                                    data = json.loads(payload.decode("utf-8"))
                                    self.notification_queue.put(data)
                                except Exception:
                                    pass
                        except Exception:
                            continue

        self._socket_thread = threading.Thread(target=_serve, daemon=True)
        self._socket_thread.start()

    def _build_queue_loop(self):
        @tasks.loop(seconds=1)
        async def _loop():
            try:
                while not self.notification_queue.empty():
                    data = self.notification_queue.get_nowait()
                    await self._process_notification_data(data)
            except Exception:
                pass
        return _loop

    async def _process_notification_data(self, data: dict) -> None:
        try:
            thread_id = int(data.get("thread_id", 0))
            rank = data.get("rank", "Unbekannt")
            subrank = data.get("subrank", "Unbekannt")
            hero = data.get("hero", "Unbekannt")
            if not thread_id:
                return
            if str(thread_id) in self.claimed_threads:
                return
            thread = self.bot.get_channel(thread_id)
            if not thread:
                return

            assigned_user_id = self._get_assigned_user(rank, subrank, hero)
            try:
                assigned_user = await self.bot.fetch_user(assigned_user_id)
                await thread.add_user(assigned_user)
            except Exception:
                pass

            view = self.ClaimView(self, thread_id, assigned_user_id)
            await thread.send(
                f"Dieses Coaching wurde <@{assigned_user_id}> zugewiesen. Bitte übernimm das Coaching, um es zu bearbeiten.",
                view=view,
            )
            self.claimed_threads[str(thread_id)] = None
            await self._save_claimed_threads()
        except Exception:
            pass

    # ---------- Events ----------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._queue_loop.is_running():
            self._start_socket_server()
            self._queue_loop.start()

    # ---------- Commands ----------
    @commands.command(name="process_notification")
    @commands.has_guild_permissions(administrator=True)
    async def process_notification(self, ctx: commands.Context, *, notification_text: str):
        try:
            if notification_text.startswith("{") and notification_text.endswith("}"):
                data = json.loads(notification_text)
            else:
                thread_id_match = re.search(r"'thread_id':\s*(\d+)", notification_text)
                match_id_match = re.search(r"'match_id':\s*'([^']+)'", notification_text)
                rank_match = re.search(r"'rank':\s*'([^']+)'", notification_text)
                subrank_match = re.search(r"'subrank':\s*'([^']+)'", notification_text)
                hero_match = re.search(r"'hero':\s*'([^']+)'", notification_text)
                user_id_match = re.search(r"'user_id':\s*(\d+)", notification_text)
                if not thread_id_match:
                    await ctx.send("Thread ID nicht gefunden!")
                    return
                data = {
                    "thread_id": int(thread_id_match.group(1)),
                    "match_id": match_id_match.group(1) if match_id_match else "Unbekannt",
                    "rank": rank_match.group(1) if rank_match else "Unbekannt",
                    "subrank": subrank_match.group(1) if subrank_match else "Unbekannt",
                    "hero": hero_match.group(1) if hero_match else "Unbekannt",
                    "user_id": int(user_id_match.group(1)) if user_id_match else 0,
                }
            await self._process_notification_data(data)
            await ctx.send("Benachrichtigung erfolgreich verarbeitet.")
        except Exception as e:
            await ctx.send(f"Fehler: {e}")

    # ---------- Teardown ----------
    def cog_unload(self) -> None:
        try:
            if self._queue_loop.is_running():
                self._queue_loop.cancel()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ClaimSystemCog(bot))

