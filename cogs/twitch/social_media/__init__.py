"""
Social Media Clip Publisher System.

Komponenten:
- ClipManager: Clip-Verwaltung & Upload-Queue
- Dashboard: Web-Interface
- Uploader: Platform-spezifische Upload-Worker (TODO)
"""
from .clip_manager import ClipManager
from .dashboard import create_social_media_app

__all__ = ["ClipManager", "create_social_media_app"]
