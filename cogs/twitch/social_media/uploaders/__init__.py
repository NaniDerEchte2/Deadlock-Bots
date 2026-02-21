"""
Social Media Platform Uploaders.

Exportiert alle Platform-Uploader und Utilities.
"""

from .base import PlatformUploader
from .instagram import InstagramUploader

# Platform-specific uploaders
from .tiktok import TikTokUploader
from .video_processor import VideoProcessor
from .youtube import YouTubeUploader

__all__ = [
    "PlatformUploader",
    "VideoProcessor",
    "TikTokUploader",
    "YouTubeUploader",
    "InstagramUploader",
]


def create_uploader(platform: str, **kwargs):
    """
    Factory function to create platform-specific uploader.

    Args:
        platform: 'tiktok', 'youtube', or 'instagram'
        **kwargs: Platform-specific credentials

    Returns:
        PlatformUploader instance

    Raises:
        ValueError: If platform is unknown
    """
    platform = platform.lower()

    if platform == "tiktok":
        return TikTokUploader(
            client_key=kwargs.get("client_key"),
            client_secret=kwargs.get("client_secret"),
        )
    elif platform == "youtube":
        return YouTubeUploader(
            client_id=kwargs.get("client_id"),
            client_secret=kwargs.get("client_secret"),
        )
    elif platform == "instagram":
        return InstagramUploader(
            access_token=kwargs.get("access_token"),
            business_account_id=kwargs.get("business_account_id"),
        )
    else:
        raise ValueError(f"Unknown platform: {platform}")
