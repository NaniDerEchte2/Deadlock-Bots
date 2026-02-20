import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands

LOG = logging.getLogger(__name__)

# Lies die Channel-ID aus .env (z.B. BOT_LOG_CHANNEL_ID=123456789012345678)
# ENV_CHANNEL_ID = os.getenv("BOT_LOG_CHANNEL_ID")
# DEFAULT_CHANNEL_ID: Optional[int] = int(ENV_CHANNEL_ID) if ENV_CHANNEL_ID and ENV_CHANNEL_ID.isdigit() else None
DEFAULT_CHANNEL_ID = 1374364800817303632


class _DiscordChannelHandler(logging.Handler):
    """Schickt Logeinträge gesammelt in einen Discord-Channel (Rate-Limit-freundlich)."""

    def __init__(self, bot: commands.Bot, channel_id: int, level=logging.INFO) -> None:
        super().__init__(level)
        self.bot = bot
        self.channel_id = channel_id
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.task: Optional[asyncio.Task] = None
        # dezente Formatierung
        self.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        )

    def start(self) -> None:
        if self.task is None:
            self.task = self.bot.loop.create_task(self._worker())

    async def aclose(self) -> None:
        """Stoppt den Worker sauber, damit beim Shutdown keine Pending-Tasks übrig bleiben."""
        if self.task:
            self.task.cancel()
            try:
                await asyncio.wait_for(self.task, timeout=5)
            except asyncio.CancelledError:
                LOG.debug("LogBridge handler stop cancelled")
            except Exception:
                LOG.debug("LogBridge handler stop wait failed", exc_info=True)
            self.task = None

    async def _worker(self) -> None:
        await self.bot.wait_until_ready()
        # Channel holen
        ch = self.bot.get_channel(self.channel_id)
        if ch is None:
            ch = await self.bot.fetch_channel(self.channel_id)
        # Sende-Schleife mit leichter Bündelung
        buffer: list[str] = []
        try:
            while True:
                msg = await self.queue.get()
                buffer.append(msg)
                # kurze Sammelwartezeit
                try:
                    await asyncio.sleep(0.8)
                    while (
                        not self.queue.empty()
                        and len("\n".join(buffer)) < 1800
                        and len(buffer) < 12
                    ):
                        buffer.append(self.queue.get_nowait())
                except Exception as exc:
                    LOG.debug("LogBridge queue bundling failed: %s", exc, exc_info=True)
                text = "```\n" + "\n".join(buffer)
                if len(text) > 1950:
                    text = text[-1950:]  # letzter Block
                    if not text.startswith("```"):
                        text = "```\n" + text
                if not text.endswith("```"):
                    text += "\n```"
                try:
                    await ch.send(text)
                except Exception as e:
                    # Falls der Channel weg ist o.ä., nicht craschen
                    LOG.warning("Failed to send log batch to Discord: %r", e)
                buffer.clear()
        except asyncio.CancelledError:
            # Shutdown: verbleibende Nachrichten verwerfen und sauber beenden
            buffer.clear()
            raise

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            # optionale Redaktionen, falls nötig:
            # msg = re.sub(r'(STEAM_(?:PASSWORD|GUARD_CODE|TOTP_SECRET))=\S+', r'\1=***', msg, flags=re.I)
            self.queue.put_nowait(msg)
        except Exception:
            self.handleError(record)


class LogBridgeCog(commands.Cog):
    """Leitet Logs (v.a. steam.presence) in einen Discord-Channel weiter."""

    def __init__(self, bot: commands.Bot, channel_id: Optional[int]) -> None:
        self.bot = bot
        self.channel_id = channel_id
        self.handler: Optional[_DiscordChannelHandler] = None

    async def cog_load(self) -> None:
        if self.channel_id is None:
            LOG.info(
                "LogBridgeCog loaded (kein BOT_LOG_CHANNEL_ID gesetzt) – bleibt passiv."
            )
            return
        # Nur bestimmte Logger weiterleiten (hier: steam.presence + Warnungen+ allgemein)
        steam_logger = logging.getLogger("steam.presence")
        self.handler = _DiscordChannelHandler(
            self.bot, self.channel_id, level=logging.INFO
        )

        class _OnlySteamStdout(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                msg = record.getMessage()
                # Nimm nur stdout-Zeilen und alle WARN/ERROR
                return ("[steam stdout]" in msg) or (record.levelno >= logging.WARNING)

        self.handler.addFilter(_OnlySteamStdout())
        steam_logger.addHandler(self.handler)
        steam_logger.setLevel(logging.INFO)
        self.handler.start()
        LOG.info(
            "LogBridgeCog active -> forwarding 'steam.presence' to channel %s",
            self.channel_id,
        )

    async def cog_unload(self) -> None:
        if self.handler:
            try:
                logging.getLogger("steam.presence").removeHandler(self.handler)
            except Exception as exc:
                LOG.debug(
                    "Failed to remove steam.presence handler: %s", exc, exc_info=True
                )
            try:
                await self.handler.aclose()
            except Exception as exc:
                LOG.debug(
                    "Failed to close LogBridge handler task: %s", exc, exc_info=True
                )
            self.handler = None

    # Optionaler Command, um ad hoc einen anderen Channel zu nutzen (nur Admins)
    @commands.command(name="set_log_channel")
    @commands.has_permissions(administrator=True)
    async def set_log_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ):
        self.channel_id = channel.id
        # neu starten des Handlers:
        if self.handler:
            logging.getLogger("steam.presence").removeHandler(self.handler)
            self.handler = None
        steam_logger = logging.getLogger("steam.presence")
        self.handler = _DiscordChannelHandler(
            self.bot, self.channel_id, level=logging.INFO
        )
        steam_logger.addHandler(self.handler)
        self.handler.start()
        await ctx.reply(f"✅ Log-Channel gesetzt auf {channel.mention}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LogBridgeCog(bot, DEFAULT_CHANNEL_ID))
