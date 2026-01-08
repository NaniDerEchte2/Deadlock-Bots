# standalone/rename_worker.py
from __future__ import annotations

import asyncio
import logging
import os
import sys
import datetime as dt
import contextlib
from typing import List, Optional
import time
from pathlib import Path

# ---------- Repo-Root robust finden, damit "service" importierbar ist ----------
def _add_repo_root_for_imports(marker="service/db.py") -> str:
    here = Path(__file__).resolve()
    # gehe aufwärts bis wir einen Ordner finden, der marker enthält
    for parent in [here.parent] + list(here.parents):
        if (parent / marker).exists():
            if str(parent) not in sys.path:
                sys.path.insert(0, str(parent))
            return str(parent)
    # Fallback: Eltern von /standalone
    fallback = here.parent.parent
    if str(fallback) not in sys.path:
        sys.path.insert(0, str(fallback))
    return str(fallback)

REPO_ROOT = _add_repo_root_for_imports()
# ---------- Ende Repo-Root Setup ----------

import discord
from discord.ext import commands
from pydantic import Field, SecretStr

from service.config import settings
from service import db as db_service # Use alias to avoid conflict with `db` for local renaming
from service.db import DB_PATH # For logging/info only, actual access via db_service

# Configure logging for the worker process first
logger = logging.getLogger("RenameWorker")
logger.setLevel(logging.INFO) # Set default level for the logger

# Determine the log directory based on DB_PATH
log_dir = os.path.join(os.path.dirname(DB_PATH), "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "rename_worker.log")

# Console Handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(console_handler)

# File Handler
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(file_handler)

logger.info(f"RenameWorker will log to: {log_file}")

# Load environment variables for Pydantic Settings
# This is typically done once at the application's entry point
# For a standalone script, we need to ensure .env is loaded here too
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    logger.debug("dotenv not available.")
except Exception as e:
    logger.warning(f"Error loading .env in worker: {e}")

# Override specific settings for the worker bot
class WorkerSettings(settings.__class__): # Inherit from main Settings
    discord_token: SecretStr = Field(..., alias="DISCORD_TOKEN_WORKER") # Worker uses its own token
    # Add any other worker-specific settings here if needed

# Instantiate worker-specific settings
try:
    worker_settings = WorkerSettings()
except Exception as e:
    logger.critical(f"CRITICAL: Failed to load worker settings: {e}")
    sys.exit(1)

# Rename request processing interval and retry mechanism
POLL_INTERVAL_SECONDS = 5
MAX_RETRY_COUNT = 5
RETRY_DELAY_SECONDS = 60 # Initial delay for failed requests

class RenameWorkerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True # Needed to resolve members if necessary (e.g. for logs)
        intents.guilds = True  # Needed for guild/channel access

        super().__init__(
            command_prefix="!", # Not used for commands, but required
            intents=intents,
            description="Dedicated Worker Bot for Channel Renaming",
            owner_id=settings.owner_id, # Can reuse owner_id from main settings
            case_insensitive=True,
        )
        self._db_processor_task: Optional[asyncio.Task[None]] = None
        self._db_lock = asyncio.Lock() # For DB operations by this worker
        self._last_api_rename_attempt_time: float = 0.0 # To track Discord API rate limits

    async def on_ready(self):
        logger.info(f"RenameWorkerBot logged in as {self.user} (ID: {self.user.id})")
        logger.info("Starting DB rename processor...")
        self._start_db_processor()

    async def on_error(self, event_method: str, *args, **kwargs):
        logger.exception(f"Unhandled error in {event_method}")

    def _start_db_processor(self):
        if self._db_processor_task is None or self._db_processor_task.done():
            self._db_processor_task = self.loop.create_task(self._process_db_queue())

    async def _get_and_update_global_rename_state_worker(self):
        """Helper to get/update global rename state atomically"""
        async with db_service.transaction():
            # Ensure the global state row exists
            await db_service.execute_async(
                """
                INSERT OR IGNORE INTO rename_global_state (id, last_rename_timestamp, next_worker_id)
                VALUES (1, STRFTIME('%Y-%m-%d %H:%M:%S', 'now', '-5 minutes'), 1)
                """
            )

            state_row = await db_service.query_one_async(
                "SELECT last_rename_timestamp, next_worker_id FROM rename_global_state WHERE id = 1"
            )

            last_ts_str = state_row["last_rename_timestamp"]
            last_ts = time.mktime(dt.datetime.strptime(last_ts_str, '%Y-%m-%d %H:%M:%S').timetuple())
            next_worker = state_row["next_worker_id"]

            return last_ts, next_worker

    async def _set_global_rename_timestamp_worker(self):
        """Update global rename timestamp after successful rename"""
        await db_service.execute_async(
            "UPDATE rename_global_state SET last_rename_timestamp = STRFTIME('%Y-%m-%d %H:%M:%S', 'now') WHERE id = 1"
        )

    async def _process_db_queue(self):
        worker_id = 2 # This is the dedicated Worker Bot, assigned ID 2

        while not self.is_closed():
            try:
                # --- Global Throttle Check ---
                last_rename_time, next_worker_to_process = await self._get_and_update_global_rename_state_worker()
                time_since_last_global_rename = time.time() - last_rename_time
                
                if time_since_last_global_rename < worker_settings.global_rename_throttle_seconds:
                    sleep_for = worker_settings.global_rename_throttle_seconds - time_since_last_global_rename
                    logger.debug(f"Rename Queue (Worker): Global throttle active. Sleeping for {sleep_for:.1f}s.")
                    await asyncio.sleep(sleep_for)
                    continue # Re-check everything after sleep
                
                # Check if it's this worker's turn
                if next_worker_to_process != worker_id:
                    logger.debug(f"Rename Queue (Worker): Not my turn. Next worker is {next_worker_to_process}. Sleeping.")
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue

                # Poll DB for pending requests assigned to this worker
                requests = await self._fetch_pending_requests(worker_id)

                if not requests:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue

                for req in requests:
                    await self._process_single_request(req)

            except asyncio.CancelledError:
                logger.info("DB rename processor (Worker) cancelled.")
                break
            except Exception as e:
                logger.error(f"DB rename processor (Worker) crashed: {e}", exc_info=True)
                await asyncio.sleep(POLL_INTERVAL_SECONDS * 2) # Longer sleep on crash

        logger.info("DB rename processor (Worker) stopped.")

    async def _fetch_pending_requests(self, assigned_worker_id: int) -> List[db_service.Row]:
        # Fetch up to 10 pending requests ordered by creation time and assigned to this worker
        try:
            return await db_service.query_all_async(
                """
                SELECT id, channel_id, new_name, reason, retry_count
                FROM rename_requests
                WHERE status = 'PENDING' AND assigned_worker_id = ?
                ORDER BY created_at ASC
                LIMIT 10
                """,
                (assigned_worker_id,)
            )
        except Exception as e:
            logger.error(f"Fehler beim Abrufen aus DB-Queue (Worker): {e}", exc_info=True)
            return []

    async def _process_single_request(self, req: db_service.Row):
        request_id = req["id"]
        channel_id = req["channel_id"]
        new_name = req["new_name"]
        reason = req["reason"]
        retry_count = req["retry_count"]

        # Update status to PROCESSING immediately to avoid other workers picking it up (if multiple workers)
        # Or, if only one worker, this marks intent
        await self._update_request_status(request_id, "PROCESSING", "Started processing", None, retry_count)

        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            error_msg = f"Channel {channel_id} nicht gefunden oder kein Voice Channel."
            logger.warning(f"Rename Worker: {error_msg}")
            await self._update_request_status(request_id, "FAILED", error_msg, dt.datetime.utcnow(), retry_count + 1)
            return

        if channel.name == new_name:
            logger.info(f"Rename Worker: Channel {channel.name} hat bereits den Namen '{new_name}'. Markiere als COMPLETED.")
            await self._update_request_status(request_id, "COMPLETED", "Name already set", dt.datetime.utcnow(), retry_count)
            return

        # --- Rate Limit Handling for Discord API ---
        current_time = time.monotonic()
        time_since_last_api_call = current_time - self._last_api_rename_attempt_time
        
        # Implement a safety delay between actual Discord API calls to prevent global rate limits
        # Discord allows 2 renames per 10 minutes *per channel* and also global limits.
        # A simple 2-second delay is a safe general-purpose buffer.
        MIN_API_CALL_INTERVAL = 2.0 
        if time_since_last_api_call < MIN_API_CALL_INTERVAL:
            await asyncio.sleep(MIN_API_CALL_INTERVAL - time_since_last_api_call)
            self._last_api_rename_attempt_time = time.monotonic() # Update time after potential sleep

        try:
            await channel.edit(name=new_name, reason=reason)
            logger.info(f"Rename Worker: Channel {channel.name} erfolgreich umbenannt zu '{new_name}'.")
            await self._update_request_status(request_id, "COMPLETED", "Success", dt.datetime.utcnow(), retry_count)
            await self._set_global_rename_timestamp_worker() # Update global throttle after success
        except discord.HTTPException as e:
            if e.status == 429: # Rate limit hit
                retry_after = float(e.headers.get("Retry-After", 5.0))
                error_msg = f"Rate Limit (429) getroffen. Versuche in {retry_after:.1f}s erneut."
                logger.warning(f"Rename Worker: {error_msg}")
                await self._update_request_status(request_id, "PENDING", error_msg, dt.datetime.utcnow(), retry_count + 1) # Set back to pending for retry
                await asyncio.sleep(retry_after + 1.0) # Wait a bit more than retry_after
            else:
                error_msg = f"Discord HTTP Fehler: {e.status} - {e.text}"
                logger.error(f"Rename Worker: {error_msg}", exc_info=True)
                await self._handle_failed_request(request_id, error_msg, retry_count)
        except Exception as e:
            error_msg = f"Unerwarteter Fehler beim Umbenennen: {e}"
            logger.error(f"Rename Worker: {error_msg}", exc_info=True)
            await self._handle_failed_request(request_id, error_msg, retry_count)

    async def _handle_failed_request(self, request_id: int, error_msg: str, current_retry: int):
        if current_retry >= MAX_RETRY_COUNT:
            final_status = "FAILED"
            final_error_msg = f"FINALER FEHLER nach {MAX_RETRY_COUNT} Versuchen: {error_msg}"
            logger.critical(f"Rename Worker: {final_error_msg}")
        else:
            final_status = "PENDING" # Put back to pending for retry
            final_error_msg = error_msg
            logger.warning(f"Rename Worker: Request {request_id} fehlgeschlagen, setze auf PENDING für Retry ({current_retry + 1}/{MAX_RETRY_COUNT}).")
            await asyncio.sleep(RETRY_DELAY_SECONDS) # Add a delay before next retry attempt

        await self._update_request_status(request_id, final_status, final_error_msg, dt.datetime.utcnow(), current_retry + 1)

    async def _update_request_status(self, request_id: int, status: str, error: Optional[str], processed_at: Optional[dt.datetime], retry_count: int):
        # Using a lock here to prevent race conditions if multiple processing attempts modify the same record
        async with self._db_lock:
            try:
                await db_service.execute_async(
                    """
                    UPDATE rename_requests
                    SET status = ?, last_error = ?, processed_at = ?, retry_count = ?
                    WHERE id = ?
                    """,
                    (status, error, processed_at, retry_count, request_id)
                )
            except Exception as e:
                logger.error(f"Fehler beim Aktualisieren des Rename-Request Status {request_id}: {e}", exc_info=True)

    async def close(self):
        if self._db_processor_task:
            self._db_processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._db_processor_task
        await super().close()
        logger.info("RenameWorkerBot gestoppt.")

async def main():
    logger.info(f"Starting RenameWorkerBot (DB Path: {DB_PATH})")
    bot = RenameWorkerBot()
    try:
        token = worker_settings.discord_token.get_secret_value()
        if not token:
            logger.critical("DISCORD_TOKEN_WORKER nicht gefunden. Bitte in .env setzen.")
            sys.exit(1)
        await bot.start(token)
    except KeyboardInterrupt:
        logger.info("WorkerBot: Keyboard Interrupt erhalten. Fahre herunter.")
    except Exception as e:
        logger.critical(f"WorkerBot Hauptfehler: {e}", exc_info=True)
    finally:
        if not bot.is_closed():
            await bot.close()
        logger.info("RenameWorkerBot beendet.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("WorkerBot: Prozess beendet.")
    except Exception as e:
        logger.critical(f"WorkerBot: Unerwarteter Fehler im Haupt-Loop: {e}", exc_info=True)
