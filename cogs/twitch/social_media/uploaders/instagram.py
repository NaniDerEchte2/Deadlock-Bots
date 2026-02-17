"""
Instagram Reels Uploader - Instagram Graph API Integration.

WICHTIG: Instagram benötigt eine öffentliche Video-URL (kein direkter Upload).
Docs: https://developers.facebook.com/docs/instagram-api/guides/content-publishing
"""
import aiohttp
from pathlib import Path
from typing import Dict, List, Optional

from .base import PlatformUploader


class InstagramUploader(PlatformUploader):
    """Instagram Reels uploader."""

    def __init__(self, access_token: str, business_account_id: str):
        """
        Args:
            access_token: Instagram/Facebook Access Token (long-lived)
            business_account_id: Instagram Business Account ID
        """
        super().__init__("instagram")
        self.access_token = access_token
        self.business_account_id = business_account_id
        self.api_base = "https://graph.facebook.com/v21.0"

    async def authenticate(self, credentials: Dict) -> bool:
        """
        Verify access token.

        Args:
            credentials: Ignored (Token bereits im __init__)

        Returns:
            True wenn Token valid
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base}/me",
                    params={"access_token": self.access_token},
                ) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        self.log.error("Instagram auth check failed: %s", error)
                        return False

                    data = await resp.json()
                    self.log.info("Instagram authentication valid: %s", data.get("name"))
                    return True

        except Exception:
            self.log.exception("Instagram authentication failed")
            return False

    async def upload_video(
        self,
        video_path: str,
        title: str,
        description: str,
        hashtags: List[str],
        **kwargs,
    ) -> str:
        """
        Upload Reel to Instagram.

        WICHTIG: Instagram benötigt eine öffentlich zugängliche Video-URL.
        Der video_path muss bereits zu einer öffentlichen URL hochgeladen sein.

        Args:
            video_path: Öffentliche Video-URL (!) oder lokaler Pfad
            title: Wird ignoriert (Instagram hat keinen Titel)
            description: Video caption
            hashtags: Liste von Hashtags
            **kwargs: Optional:
                - share_to_feed (bool): Post auch im Feed (default: True)
                - video_url (str): Öffentliche Video-URL (falls video_path lokal)

        Returns:
            Instagram media_id

        Raises:
            Exception bei Upload-Fehler
        """
        try:
            # Determine video URL
            video_url = kwargs.get("video_url")

            if not video_url:
                # If video_path looks like URL, use it
                if video_path.startswith("http://") or video_path.startswith("https://"):
                    video_url = video_path
                else:
                    raise ValueError(
                        "Instagram requires a public video URL. "
                        "Provide 'video_url' kwarg or upload video to public hosting first."
                    )

            self.validate_video(video_path if not video_url else video_url)

            caption = f"{description}\n\n{self.format_hashtags(hashtags)}"[:2200]
            share_to_feed = kwargs.get("share_to_feed", True)

            # Step 1: Create media container
            self.log.info("Instagram: Creating media container...")
            container_id = await self._create_media_container(
                video_url=video_url,
                caption=caption,
                share_to_feed=share_to_feed,
            )

            # Step 2: Publish container
            self.log.info("Instagram: Publishing media container...")
            media_id = await self._publish_container(container_id)

            self.log.info("Instagram upload successful: %s", media_id)
            return media_id

        except Exception:
            self.log.exception("Instagram upload failed")
            raise

    async def _create_media_container(
        self, video_url: str, caption: str, share_to_feed: bool
    ) -> str:
        """Create Reel media container."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.api_base}/{self.business_account_id}/media",
                params={
                    "access_token": self.access_token,
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption,
                    "share_to_feed": share_to_feed,
                },
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    raise Exception(f"Instagram create container failed: {error}")

                data = await resp.json()
                container_id = data.get("id")

                if not container_id:
                    raise Exception("No container ID in response")

                return container_id

    async def _publish_container(self, container_id: str) -> str:
        """Publish media container."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.api_base}/{self.business_account_id}/media_publish",
                params={
                    "access_token": self.access_token,
                    "creation_id": container_id,
                },
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    raise Exception(f"Instagram publish failed: {error}")

                data = await resp.json()
                media_id = data.get("id")

                if not media_id:
                    raise Exception("No media ID in response")

                return media_id

    async def get_video_status(self, media_id: str) -> Dict:
        """
        Check Reel status.

        Args:
            media_id: Instagram media_id

        Returns:
            Dict mit Status-Informationen
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base}/{media_id}",
                    params={
                        "access_token": self.access_token,
                        "fields": "status_code,media_type,timestamp",
                    },
                ) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        raise Exception(f"Status fetch failed: {error}")

                    return await resp.json()

        except Exception:
            self.log.exception("Failed to get video status")
            return {}

    def validate_video(self, video_path: str) -> bool:
        """
        Validate for Instagram Reels (max 90s, 9:16).

        Instagram Reels Requirements:
        - Max 90 seconds
        - 9:16 aspect ratio (portrait)
        - Max 1 GB file size

        Args:
            video_path: Pfad zur Video-Datei oder URL

        Returns:
            True wenn valid

        Raises:
            ValueError bei Validation-Fehlern
        """
        # If URL, skip file validation
        if video_path.startswith("http://") or video_path.startswith("https://"):
            self.log.info("Instagram video URL validation passed: %s", video_path)
            return True

        path = Path(video_path)

        if not path.exists():
            raise ValueError(f"Video file not found: {video_path}")

        # Check file size (max 1 GB)
        file_size_mb = path.stat().st_size / (1024 * 1024)
        if file_size_mb > 1024:
            raise ValueError(f"Video too large: {file_size_mb:.1f}MB (max 1024MB)")

        self.log.info("Instagram video validation passed: %s (%.1f MB)", video_path, file_size_mb)
        return True

    async def upload_to_temporary_host(self, video_path: str) -> str:
        """
        Upload video to temporary hosting (placeholder).

        TODO: Implement video hosting (Cloudflare R2, AWS S3, etc.)

        Args:
            video_path: Lokaler Video-Pfad

        Returns:
            Öffentliche URL

        Raises:
            NotImplementedError: Hosting nicht implementiert
        """
        raise NotImplementedError(
            "Video hosting not implemented. "
            "Upload video to public hosting (Cloudflare R2, S3, etc.) "
            "and pass the URL via 'video_url' kwarg."
        )
