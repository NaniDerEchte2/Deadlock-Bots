"""
Upload Worker - Processes Upload Queue and Orchestrates Uploads.

Workflow:
1. Fetch pending uploads from queue
2. Download Twitch clip (if not already downloaded)
3. Convert video to platform-specific format (9:16, max duration)
4. Upload to platform
5. Update queue status (completed/failed)
"""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from discord.ext import commands

from ..storage import get_conn
from .clip_manager import ClipManager
from .uploaders import VideoProcessor

log = logging.getLogger("TwitchStreams.UploadWorker")


class UploadWorker(commands.Cog):
    """Processes upload queue and uploads to platforms."""

    def __init__(self, bot, clip_manager: ClipManager):
        """
        Args:
            bot: Discord bot instance
            clip_manager: ClipManager instance
        """
        self.bot = bot
        self.clip_manager = clip_manager
        self.enabled = True
        self.interval_seconds = 60  # Check queue every minute
        self.max_parallel = 2  # Max parallel uploads per run

        # Initialize uploaders from env
        self.uploaders = self._init_uploaders()
        self.video_processor = VideoProcessor()

        # Start background worker
        self._task = bot.loop.create_task(self._worker_loop())
        log.info(
            "Upload worker started (interval=%ss, max_parallel=%s, uploaders=%s)",
            self.interval_seconds,
            self.max_parallel,
            list(self.uploaders.keys()),
        )

    def cog_unload(self):
        """Cleanup on cog unload."""
        if self._task:
            self._task.cancel()

    def _init_uploaders(self) -> dict:
        """Initialize platform uploaders from encrypted database credentials."""
        from .credential_manager import SocialMediaCredentialManager

        uploaders = {}
        cred_mgr = SocialMediaCredentialManager()

        # TikTok
        try:
            creds = cred_mgr.get_credentials("tiktok")
            if creds and creds.get("client_id") and creds.get("access_token"):
                from .uploaders import TikTokUploader

                uploader = TikTokUploader(creds["client_id"], creds.get("client_secret", ""))
                uploader.access_token = creds["access_token"]
                uploaders["tiktok"] = uploader
                log.info("TikTok uploader initialized (encrypted credentials)")
        except Exception:
            log.exception("Failed to initialize TikTok uploader")

        # YouTube
        try:
            creds = cred_mgr.get_credentials("youtube")
            if creds and creds.get("client_id") and creds.get("access_token"):
                from .uploaders import YouTubeUploader

                uploader = YouTubeUploader(creds["client_id"], creds.get("client_secret", ""))

                # Authenticate with encrypted tokens
                if creds.get("refresh_token"):
                    asyncio.create_task(
                        uploader.authenticate(
                            {
                                "access_token": creds["access_token"],
                                "refresh_token": creds["refresh_token"],
                            }
                        )
                    )
                else:
                    uploader.access_token = creds["access_token"]

                uploaders["youtube"] = uploader
                log.info("YouTube uploader initialized (encrypted credentials)")
        except Exception:
            log.exception("Failed to initialize YouTube uploader")

        # Instagram
        try:
            creds = cred_mgr.get_credentials("instagram")
            if creds and creds.get("access_token") and creds.get("platform_user_id"):
                from .uploaders import InstagramUploader

                uploaders["instagram"] = InstagramUploader(
                    creds["access_token"], creds["platform_user_id"]
                )
                log.info("Instagram uploader initialized (encrypted credentials)")
        except Exception:
            log.exception("Failed to initialize Instagram uploader")

        if not uploaders:
            log.debug(
                "No platform uploaders initialized. "
                "Use Social Media Dashboard to connect platforms via OAuth."
            )

        return uploaders

    async def _worker_loop(self):
        """Main worker loop - runs every minute."""
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)  # Initial delay

        while not self.bot.is_closed() and self.enabled:
            try:
                await self._process_queue()
            except Exception:
                log.exception("Upload worker run failed")

            await asyncio.sleep(self.interval_seconds)

    async def _process_queue(self):
        """Process pending uploads from queue."""
        stats = {"processed": 0, "success": 0, "failed": 0}

        for platform, uploader in self.uploaders.items():
            # Get pending uploads for this platform
            queue = self.clip_manager.get_upload_queue(
                platform=platform,
                status="pending",
                limit=self.max_parallel,
            )

            if not queue:
                continue

            log.info("Processing %s uploads for %s", len(queue), platform)

            # Process uploads (in parallel within limit)
            tasks = [self._process_upload(item, uploader) for item in queue]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Count results
            for result in results:
                stats["processed"] += 1
                if result is True:
                    stats["success"] += 1
                else:
                    stats["failed"] += 1

        if stats["processed"] > 0:
            log.info(
                "Upload batch complete: %s processed, %s success, %s failed",
                stats["processed"],
                stats["success"],
                stats["failed"],
            )

    async def _process_upload(self, queue_item: dict, uploader) -> bool:
        """
        Process single upload.

        Args:
            queue_item: Queue item dict
            uploader: PlatformUploader instance

        Returns:
            True wenn erfolgreich
        """
        queue_id = queue_item["id"]
        clip_id = queue_item["clip_id"]
        platform = queue_item["platform"]

        try:
            # Mark as processing
            self.clip_manager.update_upload_status(queue_id, "processing")

            # Get clip details
            clip_url = queue_item["clip_url"]
            clip_title = queue_item["clip_title"]
            local_path = queue_item.get("local_file_path")

            # Download clip if not already downloaded
            if not local_path or not Path(local_path).exists():
                local_path = await self._download_clip(clip_url, clip_id)

            # Convert to vertical format (9:16)
            converted_path = await self._convert_to_vertical(local_path, platform)

            # Upload to platform
            title = queue_item.get("title") or clip_title
            description = queue_item.get("description") or ""
            hashtags = queue_item.get("hashtags")

            if hashtags:
                import json

                hashtags = json.loads(hashtags) if isinstance(hashtags, str) else hashtags
            else:
                hashtags = []

            external_id = await uploader.upload_video(
                video_path=converted_path,
                title=title,
                description=description,
                hashtags=hashtags,
            )

            # Mark as completed
            self.clip_manager.update_upload_status(
                queue_id,
                "completed",
                external_video_id=external_id,
            )

            log.info("Upload successful: Clip %s -> %s (%s)", clip_id, platform, external_id)
            return True

        except Exception as e:
            log.exception("Upload failed: Clip %s -> %s", clip_id, platform)

            self.clip_manager.update_upload_status(
                queue_id,
                "failed",
                error=str(e),
            )

            return False

    async def _download_clip(self, clip_url: str, clip_id: int) -> str:
        """
        Download Twitch clip.

        Args:
            clip_url: Twitch clip URL
            clip_id: Clip DB ID

        Returns:
            Lokaler Pfad zur heruntergeladenen Datei
        """
        output_dir = Path("data/clips")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{clip_id}.mp4"

        # Skip if already downloaded
        if output_path.exists():
            log.debug("Clip already downloaded: %s", output_path)
            return str(output_path)

        # Use yt-dlp to download
        log.info("Downloading clip: %s", clip_url)

        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "-f",
            "best",
            "-o",
            str(output_path),
            clip_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error = stderr.decode()
            log.error("Clip download failed: %s", error)
            raise Exception(f"yt-dlp failed: {error}")

        if not output_path.exists():
            raise Exception(f"Downloaded file not found: {output_path}")

        # Update DB with local path
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE twitch_clips_social_media
                   SET local_file_path = ?, downloaded_at = ?
                 WHERE id = ?
                """,
                (str(output_path), datetime.now(UTC).isoformat(), clip_id),
            )

        log.info("Clip downloaded: %s", output_path)
        return str(output_path)

    async def _convert_to_vertical(self, input_path: str, platform: str) -> str:
        """
        Convert video to 9:16 format.

        Args:
            input_path: Input video path
            platform: Platform name (for duration limits)

        Returns:
            Pfad zur konvertierten Datei
        """
        output_path = input_path.replace(".mp4", f"_{platform}_vertical.mp4")

        # Skip if already converted
        if Path(output_path).exists():
            log.debug("Video already converted: %s", output_path)
            return output_path

        # Platform-specific duration limits
        max_duration = {
            "tiktok": 60,
            "youtube": 60,
            "instagram": 90,
        }.get(platform, 60)

        # Convert (trim + crop to 9:16)
        log.info("Converting video to 9:16 (max %ss): %s", max_duration, input_path)

        await self.video_processor.convert_and_trim(
            input_path=input_path,
            output_path=output_path,
            max_duration=max_duration,
            target_width=1080,
            target_height=1920,
        )

        log.info("Video converted: %s", output_path)
        return output_path


async def setup(bot):
    """Setup function for Discord.py cog."""
    # This cog is loaded by TwitchCog, not directly
    pass
