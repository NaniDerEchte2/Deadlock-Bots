"""
Abstract Base Class für Platform-Uploader.

Definiert die gemeinsame Schnittstelle für alle Platform-spezifischen Uploader.
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp


class PlatformUploader(ABC):
    """Abstract base class für Platform-Uploader."""

    def __init__(self, platform_name: str):
        """
        Args:
            platform_name: Name der Plattform (tiktok, youtube, instagram)
        """
        self.platform_name = platform_name
        self.log = logging.getLogger(f"Uploader.{platform_name}")

    @abstractmethod
    async def authenticate(self, credentials: Dict) -> bool:
        """
        Authenticate with platform OAuth.

        Args:
            credentials: Dict mit Auth-Credentials (platform-abhängig)

        Returns:
            True wenn erfolgreich
        """
        pass

    @abstractmethod
    async def upload_video(
        self,
        video_path: str,
        title: str,
        description: str,
        hashtags: List[str],
        **kwargs,
    ) -> str:
        """
        Upload video to platform.

        Args:
            video_path: Pfad zur Video-Datei
            title: Video title
            description: Video description
            hashtags: Liste von Hashtags (ohne #)
            **kwargs: Platform-spezifische Optionen

        Returns:
            External video ID

        Raises:
            Exception bei Upload-Fehler
        """
        pass

    @abstractmethod
    async def get_video_status(self, video_id: str) -> Dict:
        """
        Check upload/processing status.

        Args:
            video_id: External video ID

        Returns:
            Dict mit Status-Informationen
        """
        pass

    @abstractmethod
    def validate_video(self, video_path: str) -> bool:
        """
        Validate video meets platform requirements.

        Args:
            video_path: Pfad zur Video-Datei

        Returns:
            True wenn Video valid ist

        Raises:
            ValueError bei Validation-Fehlern
        """
        pass

    def format_hashtags(self, hashtags: List[str]) -> str:
        """
        Format hashtags for platform.

        Args:
            hashtags: Liste von Hashtags (ohne #)

        Returns:
            Formatierte Hashtag-String
        """
        return " ".join([f"#{tag}" for tag in hashtags if tag])

    async def download_clip(self, clip_url: str, output_path: str) -> bool:
        """
        Download Twitch clip.

        Args:
            clip_url: Twitch clip URL
            output_path: Ausgabe-Pfad

        Returns:
            True wenn erfolgreich
        """
        try:
            # Use yt-dlp subprocess to download
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp",
                "-f", "best",
                "-o", output_path,
                clip_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                self.log.error("yt-dlp failed: %s", stderr.decode())
                return False

            if not Path(output_path).exists():
                self.log.error("Downloaded file not found: %s", output_path)
                return False

            self.log.info("Downloaded clip: %s -> %s", clip_url, output_path)
            return True

        except FileNotFoundError:
            self.log.error("yt-dlp not installed or not in PATH")
            raise
        except Exception:
            self.log.exception("Failed to download clip")
            return False

    async def get_video_duration(self, video_path: str) -> float:
        """
        Get video duration in seconds.

        Args:
            video_path: Pfad zur Video-Datei

        Returns:
            Duration in seconds
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                raise Exception(f"ffprobe failed: {stderr.decode()}")

            duration = float(stdout.decode().strip())
            return duration

        except FileNotFoundError:
            self.log.error("ffprobe not installed or not in PATH")
            raise
        except Exception:
            self.log.exception("Failed to get video duration")
            raise

    async def get_video_resolution(self, video_path: str) -> tuple[int, int]:
        """
        Get video resolution (width, height).

        Args:
            video_path: Pfad zur Video-Datei

        Returns:
            Tuple (width, height)
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0",
                video_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                raise Exception(f"ffprobe failed: {stderr.decode()}")

            width, height = stdout.decode().strip().split("x")
            return int(width), int(height)

        except FileNotFoundError:
            self.log.error("ffprobe not installed or not in PATH")
            raise
        except Exception:
            self.log.exception("Failed to get video resolution")
            raise
