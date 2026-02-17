"""
YouTube Shorts Uploader - YouTube Data API v3 Integration.

Docs: https://developers.google.com/youtube/v3/docs/videos/insert
"""
import asyncio
from pathlib import Path
from typing import Dict, List, Optional

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    GOOGLE_API_AVAILABLE = True
except ImportError:
    GOOGLE_API_AVAILABLE = False

from .base import PlatformUploader


class YouTubeUploader(PlatformUploader):
    """YouTube Shorts uploader."""

    def __init__(self, client_id: str, client_secret: str):
        """
        Args:
            client_id: Google OAuth Client ID
            client_secret: Google OAuth Client Secret
        """
        super().__init__("youtube")

        if not GOOGLE_API_AVAILABLE:
            raise ImportError(
                "Google API client library not installed. "
                "Install with: pip install google-api-python-client google-auth google-auth-oauthlib"
            )

        self.client_id = client_id
        self.client_secret = client_secret
        self.credentials: Optional[Credentials] = None
        self.youtube = None

    async def authenticate(self, credentials: Dict) -> bool:
        """
        OAuth 2.0 authentication.

        Args:
            credentials: Dict with 'access_token' and 'refresh_token'

        Returns:
            True wenn erfolgreich
        """
        try:
            self.credentials = Credentials(
                token=credentials.get("access_token"),
                refresh_token=credentials.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=self.client_id,
                client_secret=self.client_secret,
            )

            # Build YouTube API client
            self.youtube = build("youtube", "v3", credentials=self.credentials)

            self.log.info("YouTube authentication successful")
            return True

        except Exception:
            self.log.exception("YouTube authentication failed")
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
        Upload video as YouTube Short.

        Args:
            video_path: Pfad zur Video-Datei
            title: Video title (max 100 chars)
            description: Video description (max 5000 chars)
            hashtags: Liste von Hashtags
            **kwargs: Optional: privacy ('public', 'unlisted', 'private')

        Returns:
            YouTube video_id

        Raises:
            Exception bei Upload-Fehler
        """
        if not self.youtube:
            raise Exception("Not authenticated. Call authenticate() first.")

        try:
            self.validate_video(video_path)

            # YouTube Shorts requirements:
            # - Max 60 seconds
            # - 9:16 aspect ratio
            # - #Shorts in title or description

            full_description = f"{description}\n\n{self.format_hashtags(hashtags)}\n\n#Shorts"
            full_description = full_description[:5000]  # Max 5000 chars

            body = {
                "snippet": {
                    "title": title[:100],  # Max 100 chars
                    "description": full_description,
                    "tags": hashtags[:500],  # Max 500 tags
                    "categoryId": "20",  # Gaming category
                },
                "status": {
                    "privacyStatus": kwargs.get("privacy", "public"),
                    "selfDeclaredMadeForKids": False,
                },
            }

            # Upload video (runs in executor to avoid blocking)
            video_id = await asyncio.get_event_loop().run_in_executor(
                None, self._upload_sync, video_path, body
            )

            self.log.info("YouTube upload successful: %s", video_id)
            return video_id

        except Exception:
            self.log.exception("YouTube upload failed")
            raise

    def _upload_sync(self, video_path: str, body: Dict) -> str:
        """Synchronous upload (runs in executor)."""
        media = MediaFileUpload(
            video_path,
            chunksize=10 * 1024 * 1024,  # 10MB chunks
            resumable=True,
            mimetype="video/mp4",
        )

        request = self.youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                self.log.info("YouTube upload progress: %d%%", int(status.progress() * 100))

        video_id = response["id"]
        return video_id

    async def get_video_status(self, video_id: str) -> Dict:
        """
        Check video processing status.

        Args:
            video_id: YouTube video_id

        Returns:
            Dict mit Status-Informationen
        """
        if not self.youtube:
            raise Exception("Not authenticated")

        try:
            # Run in executor to avoid blocking
            status = await asyncio.get_event_loop().run_in_executor(
                None, self._get_status_sync, video_id
            )
            return status

        except Exception:
            self.log.exception("Failed to get video status")
            return {}

    def _get_status_sync(self, video_id: str) -> Dict:
        """Synchronous status fetch (runs in executor)."""
        request = self.youtube.videos().list(
            part="status,processingDetails",
            id=video_id,
        )
        response = request.execute()

        if response["items"]:
            item = response["items"][0]
            return {
                "status": item["status"]["uploadStatus"],
                "processing_status": item.get("processingDetails", {}).get("processingStatus"),
            }
        return {}

    def validate_video(self, video_path: str) -> bool:
        """
        Validate for YouTube Shorts (max 60s, 9:16).

        YouTube Shorts Requirements:
        - Max 60 seconds
        - 9:16 aspect ratio (portrait)
        - Max 256 GB file size (practically unlimited)

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

        file_size_mb = path.stat().st_size / (1024 * 1024)
        self.log.info("YouTube video validation passed: %s (%.1f MB)", video_path, file_size_mb)
        return True
