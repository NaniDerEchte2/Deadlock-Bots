"""
TikTok Uploader - TikTok Content Posting API Integration.

Verwendet TikTok Content Posting API (Business Account erforderlich).
Docs: https://developers.tiktok.com/doc/content-posting-api-overview/
"""

import aiohttp
from pathlib import Path
from typing import Dict, List, Optional

from .base import PlatformUploader


class TikTokUploader(PlatformUploader):
    """TikTok API uploader."""

    def __init__(self, client_key: str, client_secret: str):
        """
        Args:
            client_key: TikTok API Client Key
            client_secret: TikTok API Client Secret
        """
        super().__init__("tiktok")
        self.client_key = client_key
        self.client_secret = client_secret
        self.access_token: Optional[str] = None
        self.api_base = "https://open.tiktokapis.com/v2"

    async def authenticate(self, credentials: Dict) -> bool:
        """
        OAuth 2.0 authentication.

        Args:
            credentials: Dict with 'code' (authorization code) or 'access_token'

        Returns:
            True wenn erfolgreich
        """
        try:
            # If access_token provided directly, use it
            if "access_token" in credentials:
                self.access_token = credentials["access_token"]
                self.log.info("TikTok: Using provided access token")
                return True

            # Otherwise, exchange authorization code for token
            code = credentials.get("code")
            if not code:
                self.log.error("TikTok auth: Missing 'code' or 'access_token'")
                return False

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.api_base}/oauth/token/",
                    data={
                        "client_key": self.client_key,
                        "client_secret": self.client_secret,
                        "code": code,
                        "grant_type": "authorization_code",
                    },
                ) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        self.log.error("TikTok auth failed: %s", error)
                        return False

                    data = await resp.json()
                    self.access_token = data.get("data", {}).get("access_token")

                    if not self.access_token:
                        self.log.error("TikTok auth: No access_token in response")
                        return False

                    self.log.info("TikTok authentication successful")
                    return True

        except Exception:
            self.log.exception("TikTok authentication failed")
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
        Upload video to TikTok.

        Args:
            video_path: Pfad zur Video-Datei
            title: Video title (max 150 chars)
            description: Video description
            hashtags: Liste von Hashtags
            **kwargs: Optional: privacy_level ('PUBLIC_TO_EVERYONE', 'SELF_ONLY', 'MUTUAL_FOLLOW_FRIENDS')

        Returns:
            TikTok publish_id

        Raises:
            Exception bei Upload-Fehler
        """
        if not self.access_token:
            raise Exception("Not authenticated. Call authenticate() first.")

        try:
            self.validate_video(video_path)

            # Step 1: Initialize upload
            self.log.info("TikTok: Initializing upload...")
            upload_id = await self._init_upload()

            # Step 2: Upload video chunks
            self.log.info("TikTok: Uploading video chunks...")
            await self._upload_chunks(video_path, upload_id)

            # Step 3: Publish post
            caption = f"{description}\n\n{self.format_hashtags(hashtags)}"[:2200]
            privacy_level = kwargs.get("privacy_level", "PUBLIC_TO_EVERYONE")

            self.log.info("TikTok: Publishing post...")
            video_id = await self._publish_post(
                upload_id=upload_id,
                title=title[:150],
                caption=caption,
                privacy_level=privacy_level,
            )

            self.log.info("TikTok upload successful: %s", video_id)
            return video_id

        except Exception:
            self.log.exception("TikTok upload failed")
            raise

    async def _init_upload(self) -> str:
        """Initialize upload session."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.api_base}/post/publish/video/init/",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json={"source_info": {"source": "FILE_UPLOAD"}},
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    raise Exception(f"TikTok init upload failed: {error}")

                data = await resp.json()
                upload_id = data.get("data", {}).get("upload_id")

                if not upload_id:
                    raise Exception("No upload_id in response")

                return upload_id

    async def _upload_chunks(self, video_path: str, upload_id: str):
        """Upload video in chunks."""
        chunk_size = 10 * 1024 * 1024  # 10MB chunks

        with open(video_path, "rb") as f:
            chunk_index = 0

            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break

                async with aiohttp.ClientSession() as session:
                    form = aiohttp.FormData()
                    form.add_field(
                        "video",
                        chunk,
                        filename=f"chunk_{chunk_index}.mp4",
                        content_type="video/mp4",
                    )

                    async with session.post(
                        f"{self.api_base}/post/publish/video/chunk/",
                        headers={"Authorization": f"Bearer {self.access_token}"},
                        data=form,
                        params={"upload_id": upload_id, "chunk_index": chunk_index},
                    ) as resp:
                        if resp.status != 200:
                            error = await resp.text()
                            raise Exception(f"TikTok chunk upload failed: {error}")

                self.log.debug("TikTok: Uploaded chunk %d", chunk_index)
                chunk_index += 1

    async def _publish_post(
        self, upload_id: str, title: str, caption: str, privacy_level: str
    ) -> str:
        """Publish uploaded video."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.api_base}/post/publish/",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json={
                    "post_info": {
                        "title": title,
                        "description": caption,
                        "privacy_level": privacy_level,
                        "disable_comment": False,
                        "disable_duet": False,
                        "disable_stitch": False,
                    },
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "upload_id": upload_id,
                    },
                },
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    raise Exception(f"TikTok publish failed: {error}")

                data = await resp.json()
                publish_id = data.get("data", {}).get("publish_id")

                if not publish_id:
                    raise Exception("No publish_id in response")

                return publish_id

    async def get_video_status(self, video_id: str) -> Dict:
        """
        Check video processing status.

        Args:
            video_id: TikTok publish_id

        Returns:
            Dict mit Status-Informationen
        """
        if not self.access_token:
            raise Exception("Not authenticated")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base}/post/publish/status/fetch/",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    params={"publish_id": video_id},
                ) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        raise Exception(f"Status fetch failed: {error}")

                    data = await resp.json()
                    return data.get("data", {})

        except Exception:
            self.log.exception("Failed to get video status")
            return {}

    def validate_video(self, video_path: str) -> bool:
        """
        Validate video for TikTok (max 60s, 9:16 aspect ratio).

        TikTok Requirements:
        - Max 60 seconds
        - 9:16 aspect ratio (portrait)
        - Max 287.6 MB file size

        Args:
            video_path: Pfad zur Video-Datei

        Returns:
            True wenn valid

        Raises:
            ValueError bei Validation-Fehlern
        """
        path = Path(video_path)

        if not path.exists():
            raise ValueError(f"Video file not found: {video_path}")

        # Check file size (max 287.6 MB)
        file_size_mb = path.stat().st_size / (1024 * 1024)
        if file_size_mb > 287.6:
            raise ValueError(f"Video too large: {file_size_mb:.1f}MB (max 287.6MB)")

        # Note: Duration and aspect ratio checks require ffprobe
        # These are performed by VideoProcessor before upload
        self.log.info(
            "TikTok video validation passed: %s (%.1f MB)", video_path, file_size_mb
        )
        return True
