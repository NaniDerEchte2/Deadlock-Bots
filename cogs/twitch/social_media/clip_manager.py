"""
Social Media Clip Manager.

Verwaltet Twitch-Clips und deren Upload auf TikTok, YouTube Shorts, Instagram Reels.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..storage import get_conn

log = logging.getLogger("TwitchStreams.ClipManager")

# Konfiguration
CLIPS_DOWNLOAD_DIR = Path("data/clips")
CLIPS_CONVERTED_DIR = Path("data/clips/converted")

# Sicherstellen dass Verzeichnisse existieren
CLIPS_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_CONVERTED_DIR.mkdir(parents=True, exist_ok=True)


class ClipManager:
    """Verwaltet Twitch-Clips und deren Social-Media-Uploads."""

    def __init__(self, twitch_api=None):
        """
        Args:
            twitch_api: TwitchAPI instance für Clip-Fetching
        """
        self.api = twitch_api

    async def register_clip(
        self,
        clip_id: str,
        clip_url: str,
        title: str,
        thumbnail_url: str,
        streamer_login: str,
        twitch_user_id: str,
        created_at: str,
        duration: float,
        view_count: int = 0,
        game_name: Optional[str] = None,
    ) -> int:
        """
        Registriert einen neuen Clip im System.

        Returns:
            Database ID des Clips
        """
        try:
            with get_conn() as conn:
                # Prüfe ob Clip bereits existiert
                existing = conn.execute(
                    "SELECT id FROM twitch_clips_social_media WHERE clip_id = ?",
                    (clip_id,),
                ).fetchone()

                if existing:
                    log.debug(
                        "Clip %s bereits registriert (ID: %s)", clip_id, existing[0]
                    )
                    return existing[0]

                # Sicherstellen dass Streamer in twitch_streamers existiert (FK-Anforderung).
                # Race Condition: Scout kann Streamer löschen während ClipFetcher läuft.
                conn.execute(
                    "INSERT OR IGNORE INTO twitch_streamers (twitch_login, twitch_user_id) VALUES (?, ?)",
                    (streamer_login, twitch_user_id),
                )

                # Neuen Clip erstellen
                cursor = conn.execute(
                    """
                    INSERT INTO twitch_clips_social_media
                        (clip_id, clip_url, clip_title, clip_thumbnail_url,
                         streamer_login, twitch_user_id, created_at, duration_seconds,
                         view_count, game_name, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        clip_id,
                        clip_url,
                        title,
                        thumbnail_url,
                        streamer_login,
                        twitch_user_id,
                        created_at,
                        duration,
                        view_count,
                        game_name,
                    ),
                )

                clip_db_id = cursor.lastrowid
                log.debug(
                    "Clip registriert: %s (ID: %s) - %s",
                    clip_id,
                    clip_db_id,
                    title[:50],
                )
                return clip_db_id

        except Exception:
            log.exception("Fehler beim Registrieren von Clip %s", clip_id)
            raise

    async def fetch_recent_clips(
        self,
        streamer_login: str,
        limit: int = 20,
        days: int = 7,
    ) -> List[Dict]:
        """
        Fetcht neueste Clips eines Streamers und registriert sie.

        Returns:
            Liste von Clip-Dicts
        """
        if not self.api:
            raise ValueError("TwitchAPI nicht verfügbar")

        try:
            # Hole User-ID
            users = await self.api.get_users([streamer_login])
            user_data = users.get(streamer_login.lower())
            if not user_data:
                log.warning("User nicht gefunden: %s", streamer_login)
                return []

            user_id = user_data["id"]

            # Fetch Clips via Twitch API
            clips = []
            cursor = None

            while len(clips) < limit:
                params = {
                    "broadcaster_id": user_id,
                    "first": min(100, limit - len(clips)),
                }
                if cursor:
                    params["after"] = cursor

                try:
                    data = await self.api._get("/clips", params=params)
                except Exception:
                    log.warning(
                        "Clips fetch failed for %s", streamer_login, exc_info=True
                    )
                    break

                page_clips = data.get("data", [])
                if not page_clips:
                    break

                clips.extend(page_clips)

                # Pagination
                pagination = data.get("pagination", {})
                cursor = pagination.get("cursor")
                if not cursor:
                    break

            # Registriere Clips in DB
            registered_clips = []
            for clip in clips[:limit]:
                try:
                    clip_id = clip.get("id")
                    if not clip_id:
                        continue

                    db_id = await self.register_clip(
                        clip_id=clip_id,
                        clip_url=clip.get("url"),
                        title=clip.get("title"),
                        thumbnail_url=clip.get("thumbnail_url"),
                        streamer_login=streamer_login,
                        twitch_user_id=user_id,
                        created_at=clip.get("created_at"),
                        duration=clip.get("duration", 0),
                        view_count=clip.get("view_count", 0),
                        game_name=clip.get("game_name"),
                    )

                    clip["db_id"] = db_id
                    registered_clips.append(clip)

                except Exception:
                    log.exception(
                        "Fehler beim Registrieren von Clip %s", clip.get("id")
                    )
                    continue

            log.info(
                "Fetched %d clips für %s, davon %d registriert",
                len(clips),
                streamer_login,
                len(registered_clips),
            )
            return registered_clips

        except Exception:
            log.exception("Fehler beim Fetchen von Clips für %s", streamer_login)
            return []

    def get_clips_for_dashboard(
        self,
        streamer_login: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """
        Gibt Clips für Dashboard-Anzeige zurück.

        Args:
            streamer_login: Optional filter by streamer
            status: Optional filter by status (pending, processing, ready, failed)
            limit: Max results

        Returns:
            Liste von Clip-Dicts
        """
        try:
            with get_conn() as conn:
                query = """
                    SELECT c.*,
                           COALESCE(
                               (SELECT COUNT(*) FROM twitch_clips_upload_queue q
                                WHERE q.clip_id = c.id AND q.status = 'pending'),
                               0
                           ) as pending_uploads
                      FROM twitch_clips_social_media c
                     WHERE 1=1
                """
                params = []

                if streamer_login:
                    query += " AND LOWER(c.streamer_login) = LOWER(?)"
                    params.append(streamer_login)

                if status:
                    query += " AND c.status = ?"
                    params.append(status)

                query += " ORDER BY c.created_at DESC LIMIT ?"
                params.append(limit)

                rows = conn.execute(query, params).fetchall()
                return [dict(row) for row in rows]

        except Exception:
            log.exception("Fehler beim Laden von Clips für Dashboard")
            return []

    def queue_upload(
        self,
        clip_db_id: int,
        platform: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        hashtags: Optional[List[str]] = None,
        scheduled_at: Optional[str] = None,
        priority: int = 0,
    ) -> int:
        """
        Fügt einen Upload zur Queue hinzu.

        Args:
            clip_db_id: Database ID des Clips
            platform: 'tiktok', 'youtube', or 'instagram'
            title: Custom title (optional)
            description: Custom description (optional)
            hashtags: List of hashtags (optional)
            scheduled_at: ISO timestamp for scheduled post (optional)
            priority: Upload priority (higher = first)

        Returns:
            Queue ID
        """
        if platform not in {"tiktok", "youtube", "instagram"}:
            raise ValueError(f"Invalid platform: {platform}")

        try:
            with get_conn() as conn:
                # Prüfe ob Upload bereits in Queue
                existing = conn.execute(
                    """
                    SELECT id FROM twitch_clips_upload_queue
                     WHERE clip_id = ? AND platform = ? AND status IN ('pending', 'processing')
                    """,
                    (clip_db_id, platform),
                ).fetchone()

                if existing:
                    log.debug(
                        "Upload bereits in Queue: Clip %s -> %s (Queue ID: %s)",
                        clip_db_id,
                        platform,
                        existing[0],
                    )
                    return existing[0]

                # Neue Queue eintragen
                cursor = conn.execute(
                    """
                    INSERT INTO twitch_clips_upload_queue
                        (clip_id, platform, title, description, hashtags,
                         scheduled_at, priority, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        clip_db_id,
                        platform,
                        title,
                        description,
                        json.dumps(hashtags) if hashtags else None,
                        scheduled_at,
                        priority,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

                queue_id = cursor.lastrowid
                log.info(
                    "Upload queued: Clip %s -> %s (Queue ID: %s, Priority: %s)",
                    clip_db_id,
                    platform,
                    queue_id,
                    priority,
                )
                return queue_id

        except Exception:
            log.exception(
                "Fehler beim Queue-Upload für Clip %s -> %s", clip_db_id, platform
            )
            raise

    def get_upload_queue(
        self,
        platform: Optional[str] = None,
        status: str = "pending",
        limit: int = 10,
    ) -> List[Dict]:
        """
        Gibt Upload-Queue zurück.

        Args:
            platform: Optional filter by platform
            status: Filter by status (default: 'pending')
            limit: Max results

        Returns:
            Liste von Queue-Items mit Clip-Daten
        """
        try:
            with get_conn() as conn:
                query = """
                    SELECT q.*, c.clip_id, c.clip_url, c.clip_title, c.streamer_login,
                           c.local_file_path, c.converted_file_path
                      FROM twitch_clips_upload_queue q
                      JOIN twitch_clips_social_media c ON c.id = q.clip_id
                     WHERE q.status = ?
                """
                params = [status]

                if platform:
                    query += " AND q.platform = ?"
                    params.append(platform)

                query += " ORDER BY q.priority DESC, q.created_at ASC LIMIT ?"
                params.append(limit)

                rows = conn.execute(query, params).fetchall()
                return [dict(row) for row in rows]

        except Exception:
            log.exception("Fehler beim Laden der Upload-Queue")
            return []

    def update_upload_status(
        self,
        queue_id: int,
        status: str,
        external_video_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """
        Updated Upload-Status in Queue.

        Args:
            queue_id: Queue ID
            status: New status ('processing', 'completed', 'failed')
            external_video_id: Platform video ID (bei success)
            error: Error message (bei failure)
        """
        try:
            with get_conn() as conn:
                now = datetime.now(timezone.utc).isoformat()

                if status == "completed":
                    # Markiere als completed
                    conn.execute(
                        """
                        UPDATE twitch_clips_upload_queue
                           SET status = 'completed',
                               completed_at = ?
                         WHERE id = ?
                        """,
                        (now, queue_id),
                    )

                    # Update Clip-Tabelle
                    queue_item = conn.execute(
                        "SELECT clip_id, platform FROM twitch_clips_upload_queue WHERE id = ?",
                        (queue_id,),
                    ).fetchone()

                    if queue_item:
                        clip_id, platform = queue_item[0], queue_item[1]

                        if platform == "tiktok":
                            conn.execute(
                                """
                                UPDATE twitch_clips_social_media
                                   SET uploaded_tiktok = 1,
                                       tiktok_video_id = ?,
                                       tiktok_uploaded_at = ?
                                 WHERE id = ?
                                """,
                                (external_video_id, now, clip_id),
                            )
                        elif platform == "youtube":
                            conn.execute(
                                """
                                UPDATE twitch_clips_social_media
                                   SET uploaded_youtube = 1,
                                       youtube_video_id = ?,
                                       youtube_uploaded_at = ?
                                 WHERE id = ?
                                """,
                                (external_video_id, now, clip_id),
                            )
                        elif platform == "instagram":
                            conn.execute(
                                """
                                UPDATE twitch_clips_social_media
                                   SET uploaded_instagram = 1,
                                       instagram_media_id = ?,
                                       instagram_uploaded_at = ?
                                 WHERE id = ?
                                """,
                                (external_video_id, now, clip_id),
                            )

                    log.info(
                        "Upload completed: Queue %s -> %s", queue_id, external_video_id
                    )

                elif status == "failed":
                    conn.execute(
                        """
                        UPDATE twitch_clips_upload_queue
                           SET status = 'failed',
                               attempts = attempts + 1,
                               last_error = ?,
                               last_attempt_at = ?
                         WHERE id = ?
                        """,
                        (error, now, queue_id),
                    )
                    log.warning("Upload failed: Queue %s - %s", queue_id, error)

                else:
                    # processing oder anderer Status
                    conn.execute(
                        """
                        UPDATE twitch_clips_upload_queue
                           SET status = ?,
                               last_attempt_at = ?
                         WHERE id = ?
                        """,
                        (status, now, queue_id),
                    )

        except Exception:
            log.exception("Fehler beim Update von Upload-Status (Queue %s)", queue_id)
            raise

    def get_analytics_summary(self, streamer_login: Optional[str] = None) -> Dict:
        """
        Gibt Analytics-Zusammenfassung zurück.

        Returns:
            Dict mit Metriken
        """
        try:
            with get_conn() as conn:
                if streamer_login:
                    params = (streamer_login,)

                    # Clip Stats
                    clip_stats = conn.execute(
                        """
                        SELECT COUNT(*) as total,
                               SUM(CASE WHEN uploaded_tiktok = 1 THEN 1 ELSE 0 END) as tiktok_uploads,
                               SUM(CASE WHEN uploaded_youtube = 1 THEN 1 ELSE 0 END) as youtube_uploads,
                               SUM(CASE WHEN uploaded_instagram = 1 THEN 1 ELSE 0 END) as instagram_uploads
                          FROM twitch_clips_social_media c
                         WHERE c.streamer_login = ?
                        """,
                        params,
                    ).fetchone()

                    # Queue Stats
                    queue_stats = conn.execute(
                        """
                        SELECT q.platform, COUNT(*) as pending
                          FROM twitch_clips_upload_queue q
                          JOIN twitch_clips_social_media c ON c.id = q.clip_id
                         WHERE q.status = 'pending'
                           AND c.streamer_login = ?
                         GROUP BY q.platform
                        """,
                        params,
                    ).fetchall()

                    # Analytics Stats (letzte 30 Tage)
                    analytics_stats = conn.execute(
                        """
                        SELECT a.platform,
                               COUNT(DISTINCT a.clip_id) as clips,
                               SUM(a.views) as total_views,
                               SUM(a.likes) as total_likes,
                               SUM(a.comments) as total_comments,
                               SUM(a.shares) as total_shares
                          FROM twitch_clips_social_analytics a
                          JOIN twitch_clips_social_media c ON c.id = a.clip_id
                         WHERE a.synced_at > datetime('now', '-30 days')
                           AND c.streamer_login = ?
                         GROUP BY a.platform
                        """,
                        params,
                    ).fetchall()
                else:
                    # Clip Stats
                    clip_stats = conn.execute(
                        """
                        SELECT COUNT(*) as total,
                               SUM(CASE WHEN uploaded_tiktok = 1 THEN 1 ELSE 0 END) as tiktok_uploads,
                               SUM(CASE WHEN uploaded_youtube = 1 THEN 1 ELSE 0 END) as youtube_uploads,
                               SUM(CASE WHEN uploaded_instagram = 1 THEN 1 ELSE 0 END) as instagram_uploads
                          FROM twitch_clips_social_media c
                        """
                    ).fetchone()

                    # Queue Stats
                    queue_stats = conn.execute(
                        """
                        SELECT q.platform, COUNT(*) as pending
                          FROM twitch_clips_upload_queue q
                          JOIN twitch_clips_social_media c ON c.id = q.clip_id
                         WHERE q.status = 'pending'
                         GROUP BY q.platform
                        """
                    ).fetchall()

                    # Analytics Stats (letzte 30 Tage)
                    analytics_stats = conn.execute(
                        """
                        SELECT a.platform,
                               COUNT(DISTINCT a.clip_id) as clips,
                               SUM(a.views) as total_views,
                               SUM(a.likes) as total_likes,
                               SUM(a.comments) as total_comments,
                               SUM(a.shares) as total_shares
                          FROM twitch_clips_social_analytics a
                          JOIN twitch_clips_social_media c ON c.id = a.clip_id
                         WHERE a.synced_at > datetime('now', '-30 days')
                         GROUP BY a.platform
                        """
                    ).fetchall()

                return {
                    "clips": dict(clip_stats) if clip_stats else {},
                    "queue": {row[0]: row[1] for row in queue_stats},
                    "analytics": {
                        row[0]: {
                            "clips": row[1],
                            "views": row[2] or 0,
                            "likes": row[3] or 0,
                            "comments": row[4] or 0,
                            "shares": row[5] or 0,
                        }
                        for row in analytics_stats
                    },
                }

        except Exception:
            log.exception("Fehler beim Laden von Analytics-Summary")
            return {}

    # ========== Template Management ==========

    def create_global_template(
        self,
        template_name: str,
        description_template: str,
        hashtags: List[str],
        category: Optional[str] = None,
        created_by: str = "admin",
    ) -> int:
        """
        Erstellt ein globales Template (Admin-Only).

        Args:
            template_name: Eindeutiger Template-Name
            description_template: Beschreibung mit Placeholdern ({{title}}, {{streamer}}, {{game}})
            hashtags: Liste von Hashtags (können {{game}} enthalten)
            category: Kategorie (Gaming, Entertainment, Competitive)
            created_by: Ersteller (User ID oder "admin")

        Returns:
            Template ID
        """
        try:
            with get_conn() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO clip_templates_global
                        (template_name, description_template, hashtags, category, created_by)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        template_name,
                        description_template,
                        json.dumps(hashtags),
                        category,
                        created_by,
                    ),
                )
                template_id = cursor.lastrowid
                log.info(
                    "Global template created: %s (ID: %s)", template_name, template_id
                )
                return template_id

        except Exception:
            log.exception(
                "Fehler beim Erstellen von Global Template: %s", template_name
            )
            raise

    def get_global_templates(self, category: Optional[str] = None) -> List[Dict]:
        """
        Holt alle globalen Templates.

        Args:
            category: Optional filter by category

        Returns:
            Liste von Template-Dicts
        """
        try:
            with get_conn() as conn:
                if category:
                    rows = conn.execute(
                        """
                        SELECT id, template_name, description_template, hashtags,
                               category, usage_count, created_at, created_by
                          FROM clip_templates_global
                         WHERE category = ?
                         ORDER BY usage_count DESC, template_name ASC
                        """,
                        (category,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, template_name, description_template, hashtags,
                               category, usage_count, created_at, created_by
                          FROM clip_templates_global
                         ORDER BY usage_count DESC, template_name ASC
                        """
                    ).fetchall()

                templates = []
                for row in rows:
                    template = dict(row)
                    template["hashtags"] = json.loads(template["hashtags"])
                    templates.append(template)

                return templates

        except Exception:
            log.exception("Fehler beim Laden von Global Templates")
            return []

    def create_streamer_template(
        self,
        streamer_login: str,
        template_name: str,
        description_template: str,
        hashtags: List[str],
        is_default: bool = False,
    ) -> int:
        """
        Erstellt oder updated ein Streamer-Template.

        Args:
            streamer_login: Streamer login
            template_name: Template name
            description_template: Description mit Placeholdern
            hashtags: Liste von Hashtags
            is_default: Ob dies das Default-Template ist (nur 1 pro Streamer)

        Returns:
            Template ID
        """
        try:
            with get_conn() as conn:
                now = datetime.now(timezone.utc).isoformat()

                # If is_default, unset other default templates for this streamer
                if is_default:
                    conn.execute(
                        """
                        UPDATE clip_templates_streamer
                           SET is_default = 0
                         WHERE streamer_login = ?
                        """,
                        (streamer_login,),
                    )

                # Try to update existing template
                existing = conn.execute(
                    """
                    SELECT id FROM clip_templates_streamer
                     WHERE streamer_login = ? AND template_name = ?
                    """,
                    (streamer_login, template_name),
                ).fetchone()

                if existing:
                    # Update
                    conn.execute(
                        """
                        UPDATE clip_templates_streamer
                           SET description_template = ?,
                               hashtags = ?,
                               is_default = ?,
                               updated_at = ?
                         WHERE id = ?
                        """,
                        (
                            description_template,
                            json.dumps(hashtags),
                            1 if is_default else 0,
                            now,
                            existing[0],
                        ),
                    )
                    template_id = existing[0]
                    log.info(
                        "Streamer template updated: %s/%s (ID: %s)",
                        streamer_login,
                        template_name,
                        template_id,
                    )
                else:
                    # Insert
                    cursor = conn.execute(
                        """
                        INSERT INTO clip_templates_streamer
                            (streamer_login, template_name, description_template, hashtags, is_default)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            streamer_login,
                            template_name,
                            description_template,
                            json.dumps(hashtags),
                            1 if is_default else 0,
                        ),
                    )
                    template_id = cursor.lastrowid
                    log.info(
                        "Streamer template created: %s/%s (ID: %s)",
                        streamer_login,
                        template_name,
                        template_id,
                    )

                return template_id

        except Exception:
            log.exception(
                "Fehler beim Erstellen von Streamer Template: %s/%s",
                streamer_login,
                template_name,
            )
            raise

    def get_streamer_templates(self, streamer_login: str) -> List[Dict]:
        """
        Holt alle Templates eines Streamers.

        Returns:
            Liste von Template-Dicts
        """
        try:
            with get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT id, streamer_login, template_name, description_template,
                           hashtags, is_default, created_at, updated_at
                      FROM clip_templates_streamer
                     WHERE streamer_login = ?
                     ORDER BY is_default DESC, template_name ASC
                    """,
                    (streamer_login,),
                ).fetchall()

                templates = []
                for row in rows:
                    template = dict(row)
                    template["hashtags"] = json.loads(template["hashtags"])
                    templates.append(template)

                return templates

        except Exception:
            log.exception("Fehler beim Laden von Streamer Templates")
            return []

    def apply_template_to_clip(
        self,
        clip_id: int,
        template_id: int,
        is_global: bool = False,
    ) -> bool:
        """
        Wendet ein Template auf einen Clip an (substituiert Placeholders).

        Args:
            clip_id: Clip DB ID
            template_id: Template ID
            is_global: True wenn Global Template, False wenn Streamer Template

        Returns:
            True wenn erfolgreich
        """
        try:
            with get_conn() as conn:
                # Get clip data
                clip = conn.execute(
                    """
                    SELECT clip_title, streamer_login, game_name
                      FROM twitch_clips_social_media
                     WHERE id = ?
                    """,
                    (clip_id,),
                ).fetchone()

                if not clip:
                    log.warning("Clip nicht gefunden: %s", clip_id)
                    return False

                # Get template
                if is_global:
                    template = conn.execute(
                        """
                        SELECT description_template, hashtags
                          FROM clip_templates_global
                         WHERE id = ?
                        """,
                        (template_id,),
                    ).fetchone()

                    # Increment usage count
                    conn.execute(
                        "UPDATE clip_templates_global SET usage_count = usage_count + 1 WHERE id = ?",
                        (template_id,),
                    )
                else:
                    template = conn.execute(
                        """
                        SELECT description_template, hashtags
                          FROM clip_templates_streamer
                         WHERE id = ?
                        """,
                        (template_id,),
                    ).fetchone()

                if not template:
                    log.warning(
                        "Template nicht gefunden: %s (global=%s)",
                        template_id,
                        is_global,
                    )
                    return False

                # Substitute placeholders
                description = template["description_template"]
                hashtags_raw = json.loads(template["hashtags"])

                description = description.replace("{{title}}", clip["clip_title"] or "")
                description = description.replace(
                    "{{streamer}}", clip["streamer_login"] or ""
                )
                description = description.replace(
                    "{{game}}", clip["game_name"] or "Unknown"
                )

                hashtags = [
                    tag.replace(
                        "{{game}}", (clip["game_name"] or "Unknown").replace(" ", "")
                    )
                    for tag in hashtags_raw
                ]

                # Update clip
                conn.execute(
                    """
                    UPDATE twitch_clips_social_media
                       SET custom_description = ?,
                           hashtags = ?
                     WHERE id = ?
                    """,
                    (description, json.dumps(hashtags), clip_id),
                )

                log.info("Template applied to clip %s: %s", clip_id, description[:50])
                return True

        except Exception:
            log.exception("Fehler beim Anwenden von Template auf Clip %s", clip_id)
            return False

    def save_last_hashtags(self, streamer_login: str, hashtags: List[str]) -> None:
        """
        Speichert zuletzt verwendete Hashtags für einen Streamer.

        Args:
            streamer_login: Streamer login
            hashtags: Liste von Hashtags
        """
        try:
            with get_conn() as conn:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO clip_last_hashtags (streamer_login, hashtags, last_used_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(streamer_login) DO UPDATE SET
                        hashtags = excluded.hashtags,
                        last_used_at = excluded.last_used_at
                    """,
                    (streamer_login, json.dumps(hashtags), now),
                )
                log.debug("Last hashtags saved for %s: %s", streamer_login, hashtags)

        except Exception:
            log.exception("Fehler beim Speichern von Last Hashtags")

    def get_last_hashtags(self, streamer_login: str) -> List[str]:
        """
        Holt zuletzt verwendete Hashtags für einen Streamer.

        Returns:
            Liste von Hashtags (leer wenn keine vorhanden)
        """
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT hashtags FROM clip_last_hashtags WHERE streamer_login = ?",
                    (streamer_login,),
                ).fetchone()

                if row:
                    return json.loads(row["hashtags"])
                return []

        except Exception:
            log.exception("Fehler beim Laden von Last Hashtags")
            return []

    # ========== Batch Operations ==========

    async def batch_upload_all_new(
        self,
        streamer_login: str,
        platforms: List[str],
        apply_default_template: bool = True,
    ) -> Dict[str, int]:
        """
        Queued alle nicht hochgeladenen Clips eines Streamers.

        Args:
            streamer_login: Streamer login
            platforms: Liste von Platforms ('tiktok', 'youtube', 'instagram')
            apply_default_template: Ob Default-Template angewendet werden soll

        Returns:
            Stats Dict: {queued: int, skipped: int, errors: int}
        """
        stats = {"queued": 0, "skipped": 0, "errors": 0}

        try:
            with get_conn() as conn:
                # Get default template if requested
                default_template = None
                if apply_default_template:
                    template_row = conn.execute(
                        """
                        SELECT id, description_template, hashtags
                          FROM clip_templates_streamer
                         WHERE streamer_login = ? AND is_default = 1
                        """,
                        (streamer_login,),
                    ).fetchone()

                    if template_row:
                        default_template = dict(template_row)
                        default_template["hashtags"] = json.loads(
                            default_template["hashtags"]
                        )

                # For each platform, queue uploads
                for platform in platforms:
                    if platform == "tiktok":
                        clips = conn.execute(
                            """
                            SELECT id, clip_id, clip_title, streamer_login, game_name,
                                   custom_description, hashtags
                              FROM twitch_clips_social_media
                             WHERE streamer_login = ? AND uploaded_tiktok = 0
                             ORDER BY created_at DESC
                            """,
                            (streamer_login,),
                        ).fetchall()
                    elif platform == "youtube":
                        clips = conn.execute(
                            """
                            SELECT id, clip_id, clip_title, streamer_login, game_name,
                                   custom_description, hashtags
                              FROM twitch_clips_social_media
                             WHERE streamer_login = ? AND uploaded_youtube = 0
                             ORDER BY created_at DESC
                            """,
                            (streamer_login,),
                        ).fetchall()
                    elif platform == "instagram":
                        clips = conn.execute(
                            """
                            SELECT id, clip_id, clip_title, streamer_login, game_name,
                                   custom_description, hashtags
                              FROM twitch_clips_social_media
                             WHERE streamer_login = ? AND uploaded_instagram = 0
                             ORDER BY created_at DESC
                            """,
                            (streamer_login,),
                        ).fetchall()
                    else:
                        log.warning("Invalid platform: %s", platform)
                        continue

                    log.info(
                        "Found %d non-uploaded clips for %s -> %s",
                        len(clips),
                        streamer_login,
                        platform,
                    )

                    for clip in clips:
                        try:
                            clip_dict = dict(clip)
                            clip_db_id = clip_dict["id"]
                            description = clip_dict["custom_description"]
                            hashtags_str = clip_dict["hashtags"]

                            # Apply default template if needed
                            if default_template and not description:
                                # Substitute placeholders
                                description = default_template["description_template"]
                                description = description.replace(
                                    "{{title}}", clip_dict["clip_title"] or ""
                                )
                                description = description.replace(
                                    "{{streamer}}", clip_dict["streamer_login"] or ""
                                )
                                description = description.replace(
                                    "{{game}}", clip_dict["game_name"] or "Unknown"
                                )

                                hashtags = [
                                    tag.replace(
                                        "{{game}}",
                                        (clip_dict["game_name"] or "Unknown").replace(
                                            " ", ""
                                        ),
                                    )
                                    for tag in default_template["hashtags"]
                                ]
                            else:
                                hashtags = (
                                    json.loads(hashtags_str) if hashtags_str else []
                                )

                            # Queue upload
                            self.queue_upload(
                                clip_db_id=clip_db_id,
                                platform=platform,
                                title=clip_dict["clip_title"],
                                description=description,
                                hashtags=hashtags,
                                priority=0,
                            )

                            stats["queued"] += 1

                        except Exception:
                            log.exception(
                                "Fehler beim Queuen von Clip %s -> %s",
                                clip.get("id"),
                                platform,
                            )
                            stats["errors"] += 1

            log.info(
                "Batch upload queued: %s clips for %s (%s errors)",
                stats["queued"],
                streamer_login,
                stats["errors"],
            )
            return stats

        except Exception:
            log.exception("Fehler beim Batch Upload für %s", streamer_login)
            stats["errors"] += 1
            return stats

    def mark_clip_uploaded(
        self,
        clip_id: int,
        platforms: List[str],
        manual: bool = True,
    ) -> bool:
        """
        Markiert einen Clip als hochgeladen (manuell).

        Args:
            clip_id: Clip DB ID
            platforms: Liste von Platforms ('tiktok', 'youtube', 'instagram')
            manual: Ob dies ein manueller Mark ist

        Returns:
            True wenn erfolgreich
        """
        try:
            with get_conn() as conn:
                now = datetime.now(timezone.utc).isoformat()

                for platform in platforms:
                    if platform == "tiktok":
                        conn.execute(
                            """
                            UPDATE twitch_clips_social_media
                               SET uploaded_tiktok = 1,
                                   tiktok_uploaded_at = ?
                             WHERE id = ?
                            """,
                            (now, clip_id),
                        )
                    elif platform == "youtube":
                        conn.execute(
                            """
                            UPDATE twitch_clips_social_media
                               SET uploaded_youtube = 1,
                                   youtube_uploaded_at = ?
                             WHERE id = ?
                            """,
                            (now, clip_id),
                        )
                    elif platform == "instagram":
                        conn.execute(
                            """
                            UPDATE twitch_clips_social_media
                               SET uploaded_instagram = 1,
                                   instagram_uploaded_at = ?
                             WHERE id = ?
                            """,
                            (now, clip_id),
                        )

                log.info(
                    "Clip %s marked as uploaded to %s (manual=%s)",
                    clip_id,
                    platforms,
                    manual,
                )
                return True

        except Exception:
            log.exception("Fehler beim Markieren von Clip %s als uploaded", clip_id)
            return False
