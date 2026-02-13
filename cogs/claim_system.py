import discord
from discord.ext import commands
import asyncio
import sys
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from service import db as central_db

log = logging.getLogger(__name__)

# Event Loop Policy für Windows setzen
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Automatische Pfad-Erstellung
SCRIPT_DIR = Path(__file__).parent.absolute()
BASE_DIR = SCRIPT_DIR.parent

# Verzeichnisse definieren
CLAIM_DATA_DIR = BASE_DIR / "claim_data"
LOGS_DIR = BASE_DIR / "logs"

def ensure_directories():
    for directory in [CLAIM_DATA_DIR, LOGS_DIR]:
        directory.mkdir(parents=True, exist_ok=True)

ensure_directories()

# Konfiguration
SOCKET_HOST = 'localhost'
SOCKET_PORT = 45680
STAFF_ROLE_ID = 1371929762913587292

USERS = {
    "ashty": {
        "id": 219209674019438592,
        "heroes": ["kelvin", "ivy", "lash", "sinclair", "mcginnis", "viscous", "abrams", "paradox", "mo", "holiday"]
    },
    "yourmomgosky": {
        "id": 678968588203327539,
        "heroes": ["lady_geist", "warden", "wraith", "kelvin", "ivy"]
    },
    "naniderechte": {
        "id": 662995601738170389,
        "heroes": []
    },
    "hucci1789": {
        "id": 410856005551915008,
        "heroes": ["abrams", "paradox", "ivy", "dynamo", "pocket"]
    }
}

class ClaimView(discord.ui.View):
    def __init__(self, cog: "ClaimSystem", thread_id: int, assigned_user_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.thread_id = thread_id
        self.assigned_user_id = assigned_user_id

    @discord.ui.button(label="Coaching übernehmen", style=discord.ButtonStyle.primary, custom_id="claim_button")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        has_staff_role = any(role.id == STAFF_ROLE_ID for role in interaction.user.roles)
        if not has_staff_role:
            await interaction.response.send_message("Du hast nicht die Berechtigung, das Coaching zu übernehmen.", ephemeral=True)
            return

        if str(self.thread_id) in self.cog.claimed_threads and self.cog.claimed_threads[str(self.thread_id)] is not None:
            await interaction.response.send_message(f"Dieses Coaching wurde bereits von <@{self.cog.claimed_threads[str(self.thread_id)]}> übernommen.", ephemeral=True)
            return

        if interaction.user.id != self.assigned_user_id and self.assigned_user_id != 0:
            await interaction.response.send_message("Dieses Coaching ist einem anderen Benutzer zugewiesen.", ephemeral=True)
            return

        self.cog.mark_thread_claimed(self.thread_id, interaction.user.id)
        button.disabled = True
        await interaction.message.edit(view=self)
        await interaction.channel.send(f"<@{interaction.user.id}> hat dieses Coaching übernommen.")
        await interaction.response.send_message("Du hast dieses Coaching erfolgreich übernommen.", ephemeral=True)

    @discord.ui.button(label="Freigeben", style=discord.ButtonStyle.secondary, custom_id="release_button")
    async def release_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        has_staff_role = any(role.id == STAFF_ROLE_ID for role in interaction.user.roles)
        if not has_staff_role:
            await interaction.response.send_message("Du hast nicht die Berechtigung, dieses Coaching freizugeben.", ephemeral=True)
            return

        self.assigned_user_id = 0
        self.cog.release_thread(self.thread_id)
        self.children[0].disabled = False
        await interaction.message.edit(view=self)
        await interaction.channel.send(f"<@&{STAFF_ROLE_ID}> Dieses Coaching wurde freigegeben und kann von jedem übernommen werden.")
        await interaction.response.send_message("Du hast dieses Coaching erfolgreich freigegeben.", ephemeral=True)

class ClaimSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.claimed_threads: Dict[str, Optional[int]] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._server_task: Optional[asyncio.Task] = None
        self._init_db()
        self.load_claimed_threads()

    def _init_db(self):
        with central_db.get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS claimed_threads (
                    thread_id         BIGINT PRIMARY KEY,
                    assigned_user_id  BIGINT,
                    claimed_by_id     BIGINT,
                    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def load_claimed_threads(self):
        with central_db.get_conn() as conn:
            rows = conn.execute("SELECT thread_id, claimed_by_id FROM claimed_threads").fetchall()
        self.claimed_threads = {str(r[0]): (int(r[1]) if r[1] is not None else None) for r in rows}

    def mark_thread_processed(self, thread_id: int, assigned_user_id: int):
        with central_db.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO claimed_threads (thread_id, assigned_user_id, claimed_by_id) VALUES (?, ?, NULL)",
                (thread_id, assigned_user_id),
            )
        self.claimed_threads[str(thread_id)] = None

    def mark_thread_claimed(self, thread_id: int, claimer_id: int):
        with central_db.get_conn() as conn:
            conn.execute("UPDATE claimed_threads SET claimed_by_id=? WHERE thread_id=?", (claimer_id, thread_id))
        self.claimed_threads[str(thread_id)] = claimer_id

    def release_thread(self, thread_id: int):
        with central_db.get_conn() as conn:
            conn.execute("UPDATE claimed_threads SET assigned_user_id = 0 WHERE thread_id = ?", (thread_id,))

    async def cog_load(self):
        # Start socket server
        self._server_task = asyncio.create_task(self._start_socket_server())
        log.info("ClaimSystem: Socket server task started")

    async def cog_unload(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                log.debug("ClaimSystem: Socket server task cancelled during unload")
        log.info("ClaimSystem: Socket server stopped")

    async def _start_socket_server(self):
        max_retries = 5
        retry_delay = 0.5
        
        for attempt in range(max_retries):
            try:
                self._server = await asyncio.start_server(self._handle_client, SOCKET_HOST, SOCKET_PORT)
                log.info(f"ClaimSystem: Socket-Server gestartet auf {SOCKET_HOST}:{SOCKET_PORT}")
                async with self._server:
                    await self._server.serve_forever()
                return
            except OSError as e:
                import errno
                if e.errno == 10048 or e.errno == errno.EADDRINUSE:
                    if attempt < max_retries - 1:
                        log.debug(f"ClaimSystem port {SOCKET_PORT} belegt, retry in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                log.exception("ClaimSystem: Socket-Server konnte nicht starten")
                break
            except Exception:
                log.exception("ClaimSystem: Unerwarteter Fehler im Socket-Server")
                break

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info('peername')
        log.debug(f"ClaimSystem: Verbindung von {addr}")
        try:
            length_data = await reader.readexactly(4)
            data_length = int.from_bytes(length_data, byteorder='big')
            payload_data = await reader.readexactly(data_length)
            data = json.loads(payload_data.decode('utf-8'))
            log.info(f"ClaimSystem: Daten empfangen: {data}")
            await self.process_notification_data(data)
        except Exception as e:
            log.error(f"ClaimSystem: Fehler beim Empfang: {e}")
        finally:
            writer.close()
            await writer.wait_closed()

    async def process_notification_data(self, data: Dict[str, Any]):
        try:
            thread_id = data.get('thread_id')
            if not thread_id: return

            if str(thread_id) in self.claimed_threads:
                log.info(f"ClaimSystem: Thread {thread_id} bereits bearbeitet.")
                return

            thread = self.bot.get_channel(int(thread_id))
            if not thread:
                log.warning(f"ClaimSystem: Thread {thread_id} nicht gefunden.")
                return

            assigned_user_id = data.get('assigned_user_id')
            if not assigned_user_id:
                # Fallback Logic (simplified)
                assigned_user_id = USERS["hucci1789"]["id"]

            try:
                assigned_user = self.bot.get_user(int(assigned_user_id)) or await self.bot.fetch_user(int(assigned_user_id))
                if assigned_user:
                    await thread.add_user(assigned_user)
            except Exception as e:
                log.error(f"ClaimSystem: Fehler beim Hinzufügen des Users: {e}")

            view = ClaimView(self, int(thread_id), int(assigned_user_id))
            await thread.send(
                f"Dieses Coaching wurde <@{assigned_user_id}> zugewiesen. Bitte übernimm das Coaching, um es zu bearbeiten.",
                view=view
            )
            self.mark_thread_processed(int(thread_id), int(assigned_user_id))
        except Exception as e:
            log.exception(f"ClaimSystem: Fehler bei Verarbeitung: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(ClaimSystem(bot))
