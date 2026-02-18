"""
Social Media Clip Dashboard - Web Interface.

Bietet UI für:
- Clip-Übersicht
- Upload-Management (TikTok, YouTube, Instagram)
- Analytics-Dashboard
"""
import html
import logging
from typing import Optional
from urllib.parse import urlencode

from aiohttp import web

from .clip_manager import ClipManager

log = logging.getLogger("TwitchStreams.SocialMediaDashboard")


def _sanitize_log_value(value: Optional[str]) -> str:
    """Prevent CRLF log-forging via untrusted values."""
    if value is None:
        return "<none>"
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _dashboard_url(**params: str) -> str:
    """Build internal dashboard URL with encoded query values."""
    if not params:
        return "/social-media"
    return f"/social-media?{urlencode(params)}"


class SocialMediaDashboard:
    """Web Dashboard für Social Media Clip Management."""

    def __init__(self, clip_manager: ClipManager, auth_checker=None, auth_session_getter=None):
        """
        Args:
            clip_manager: ClipManager instance
            auth_checker: Callable that checks authentication (from parent dashboard server)
            auth_session_getter: Callable that resolves dashboard OAuth session (dict)
        """
        self.clip_manager = clip_manager
        self.auth_checker = auth_checker
        self.auth_session_getter = auth_session_getter

    def _require_auth(self, request: web.Request) -> None:
        """Check authentication using parent dashboard's OAuth system."""
        # If no auth_checker provided, allow (backwards compat)
        if not self.auth_checker:
            return

        # Use parent's auth checker (supports Twitch OAuth, localhost, tokens)
        if not self.auth_checker(request):
            raise web.HTTPUnauthorized(
                text="Bitte melde dich mit deinem Twitch-Partner-Account an.",
                headers={"Location": "/twitch/auth/login?next=/social-media"}
            )

    def _get_auth_streamer_login(self, request: web.Request) -> Optional[str]:
        """Return Twitch login from dashboard OAuth session when available."""
        getter = self.auth_session_getter
        if not callable(getter):
            return None
        try:
            session = getter(request)
        except Exception:
            log.debug("Failed to resolve dashboard session for social-media", exc_info=True)
            return None
        if not isinstance(session, dict):
            return None
        login = str(session.get("twitch_login") or "").strip().lower()
        return login or None

    def _resolve_streamer_scope(
        self,
        request: web.Request,
        requested_streamer: Optional[str] = None,
        *,
        required: bool = False,
    ) -> Optional[str]:
        """Resolve effective streamer with session-based ownership enforcement."""
        requested = str(requested_streamer or "").strip().lower()
        session_streamer = self._get_auth_streamer_login(request)

        if session_streamer:
            if requested and requested.lower() != session_streamer:
                safe_requested = _sanitize_log_value(requested)
                safe_session = _sanitize_log_value(session_streamer)
                log.warning(
                    "Blocked cross-account social-media access: requested=%s session=%s",
                    safe_requested,
                    safe_session,
                )
                raise web.HTTPForbidden(
                    text="Du kannst nur auf deinen eigenen Twitch-Account zugreifen."
                )
            return session_streamer

        if required and not requested:
            raise web.HTTPBadRequest(text="streamer parameter required")

        return requested or None

    @staticmethod
    def _normalize_clip_id(raw_value) -> Optional[int]:
        """Convert user-provided clip id into positive integer."""
        try:
            clip_id = int(raw_value)
        except (TypeError, ValueError):
            return None
        return clip_id if clip_id > 0 else None

    def _clip_owned_by_streamer(self, clip_id: int, streamer_login: str) -> bool:
        from ..storage import get_conn

        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM twitch_clips_social_media
                WHERE id = ? AND LOWER(streamer_login) = LOWER(?)
                LIMIT 1
                """,
                (clip_id, streamer_login),
            ).fetchone()
        return bool(row)

    def _streamer_template_owned_by_streamer(self, template_id: int, streamer_login: str) -> bool:
        from ..storage import get_conn

        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM clip_templates_streamer
                WHERE id = ? AND LOWER(streamer_login) = LOWER(?)
                LIMIT 1
                """,
                (template_id, streamer_login),
            ).fetchone()
        return bool(row)

    def _build_app(self) -> web.Application:
        """Build aiohttp app with routes."""
        app = web.Application()

        # HTML Pages
        app.router.add_get("/social-media", self.index)
        app.router.add_get("/terms", self.page_terms)
        app.router.add_get("/privacy", self.page_privacy)

        # API Endpoints
        app.router.add_get("/social-media/api/stats", self.api_stats)
        app.router.add_get("/social-media/api/clips", self.clips_list)
        app.router.add_post("/social-media/api/upload", self.queue_upload)
        app.router.add_get("/social-media/api/analytics", self.analytics)

        # Template Management Endpoints
        app.router.add_get("/social-media/api/templates/global", self.api_templates_global)
        app.router.add_get("/social-media/api/templates/streamer", self.api_templates_streamer)
        app.router.add_post("/social-media/api/templates/streamer", self.api_create_template)
        app.router.add_post("/social-media/api/templates/apply", self.api_apply_template)

        # Batch Operations Endpoints
        app.router.add_post("/social-media/api/batch-upload", self.api_batch_upload)
        app.router.add_post("/social-media/api/mark-uploaded", self.api_mark_uploaded)

        # Clip Fetching Endpoints
        app.router.add_post("/social-media/api/fetch-clips", self.api_fetch_clips)
        app.router.add_get("/social-media/api/last-hashtags", self.api_last_hashtags)

        # OAuth & Platform Management Endpoints
        app.router.add_get("/social-media/oauth/start/{platform}", self.oauth_start)
        app.router.add_get("/social-media/oauth/callback", self.oauth_callback)
        app.router.add_post("/social-media/oauth/disconnect/{platform}", self.oauth_disconnect)
        app.router.add_get("/social-media/api/platforms/status", self.api_platforms_status)

        return app

    async def page_terms(self, request: web.Request) -> web.Response:
        """Public Terms of Service page (required for TikTok / platform OAuth apps)."""
        body = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nutzungsbedingungen – Deutsche Deadlock Community Bot</title>
<style>
  body { font-family: sans-serif; max-width: 800px; margin: 60px auto; padding: 0 24px; color: #222; line-height: 1.7; }
  h1 { font-size: 1.8rem; margin-bottom: 4px; }
  h2 { font-size: 1.2rem; margin-top: 32px; }
  p, li { font-size: 0.97rem; }
  footer { margin-top: 48px; font-size: 0.85rem; color: #666; }
</style>
</head>
<body>
<h1>Nutzungsbedingungen</h1>
<p><strong>Deutsche Deadlock Community Bot</strong> – zuletzt aktualisiert: Februar 2026</p>

<h2>1. Geltungsbereich</h2>
<p>Diese Nutzungsbedingungen gelten für den Einsatz des Deutsche Deadlock Community Bots
(nachfolgend „Bot"), der Twitch-Clips automatisch auf Social-Media-Plattformen
(TikTok, YouTube, Instagram) veröffentlicht. Der Bot wird von der Deutschen Deadlock
Community für interne Zwecke betrieben.</p>

<h2>2. Nutzung</h2>
<p>Der Bot ist ausschließlich für autorisierte Streamer der Deutschen Deadlock Community
bestimmt. Eine Nutzung durch Dritte ist nicht vorgesehen. Autorisierte Streamer erklären
sich einverstanden, dass ihre öffentlichen Twitch-Clips im Rahmen der Community-Aktivitäten
auf den verbundenen Social-Media-Kanälen veröffentlicht werden dürfen.</p>

<h2>3. Inhalte</h2>
<p>Es dürfen ausschließlich Clips hochgeladen werden, für die die erforderlichen Rechte
vorliegen. Insbesondere sind Clips mit urheberrechtlich geschützter Musik, beleidigenden
oder rechtswidrigen Inhalten ausgeschlossen.</p>

<h2>4. Haftungsausschluss</h2>
<p>Der Bot wird ohne Gewähr betrieben. Wir übernehmen keine Haftung für eventuelle
Fehlfunktionen, Datenverluste oder Schäden, die durch die Nutzung des Bots entstehen.</p>

<h2>5. Änderungen</h2>
<p>Diese Bedingungen können jederzeit angepasst werden. Die jeweils aktuelle Version ist
unter dieser URL abrufbar.</p>

<h2>6. Kontakt</h2>
<p>Bei Fragen: Discord-Server der Deutschen Deadlock Community.</p>

<footer>Deutsche Deadlock Community · <a href="/privacy">Datenschutzhinweis</a></footer>
</body>
</html>"""
        return web.Response(text=body, content_type="text/html", charset="utf-8")

    async def page_privacy(self, request: web.Request) -> web.Response:
        """Public Privacy Policy page (required for TikTok / platform OAuth apps)."""
        body = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Datenschutzhinweis – Deutsche Deadlock Community Bot</title>
<style>
  body { font-family: sans-serif; max-width: 800px; margin: 60px auto; padding: 0 24px; color: #222; line-height: 1.7; }
  h1 { font-size: 1.8rem; margin-bottom: 4px; }
  h2 { font-size: 1.2rem; margin-top: 32px; }
  p, li { font-size: 0.97rem; }
  footer { margin-top: 48px; font-size: 0.85rem; color: #666; }
</style>
</head>
<body>
<h1>Datenschutzhinweis</h1>
<p><strong>Deutsche Deadlock Community Bot</strong> – zuletzt aktualisiert: Februar 2026</p>

<h2>1. Verantwortliche Stelle</h2>
<p>Betrieben wird der Bot durch die Deutsche Deadlock Community (Discord-Server).
Kontakt über den Discord-Server.</p>

<h2>2. Erhobene Daten</h2>
<p>Der Bot verarbeitet ausschließlich öffentlich verfügbare Daten von der Twitch-API:</p>
<ul>
  <li>Twitch-Benutzernamen und -IDs der Community-Streamer</li>
  <li>Öffentliche Clip-Metadaten (Titel, URL, Vorschaubild, Spieldauer, Zuschauerzahl)</li>
  <li>OAuth-Zugangsdaten für verbundene Social-Media-Konten (verschlüsselt gespeichert)</li>
</ul>
<p>Es werden keine personenbezogenen Daten von Zuschauern oder Dritten gespeichert.</p>

<h2>3. Zweck der Verarbeitung</h2>
<p>Die Daten werden ausschließlich zur automatischen Veröffentlichung von
Community-Clips auf TikTok, YouTube und Instagram verwendet.</p>

<h2>4. Speicherung & Sicherheit</h2>
<p>Clip-Metadaten und OAuth-Tokens werden in einer lokalen SQLite-Datenbank gespeichert.
Zugangsdaten werden mit AES-256-GCM verschlüsselt. Ein Zugriff von außen auf die
Datenbank besteht nicht.</p>

<h2>5. Weitergabe an Dritte</h2>
<p>Daten werden nicht an Dritte weitergegeben. Für den Upload wird die jeweilige
Plattform-API (TikTok, YouTube, Instagram) verwendet; dabei gelten deren
Datenschutzbestimmungen.</p>

<h2>6. Dauer der Speicherung</h2>
<p>Clip-Einträge werden intern gespeichert bis sie manuell gelöscht werden.
OAuth-Tokens werden bei Widerruf der App-Berechtigung sofort deaktiviert.</p>

<h2>7. Betroffenenrechte</h2>
<p>Autorisierte Streamer können jederzeit die Löschung ihrer Daten per Discord-Nachricht
anfordern.</p>

<footer>Deutsche Deadlock Community · <a href="/terms">Nutzungsbedingungen</a></footer>
</body>
</html>"""
        return web.Response(text=body, content_type="text/html", charset="utf-8")

    async def index(self, request: web.Request) -> web.Response:
        """Main dashboard page with full template & batch upload UI."""
        self._require_auth(request)
        authenticated_streamer = self._resolve_streamer_scope(request)
        safe_streamer_label = html.escape(
            f"@{authenticated_streamer}" if authenticated_streamer else "nicht gesetzt"
        )
        safe_streamer_data = html.escape(authenticated_streamer or "", quote=True)

        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Social Media Clip Manager</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0e0e10;
            color: #efeff1;
            padding: 20px;
        }}
        .container {{ max-width: 1600px; margin: 0 auto; }}
        h1 {{ margin-bottom: 10px; color: #9147ff; }}
        h2 {{ color: #efeff1; margin: 20px 0; }}
        .subtitle {{ color: #adadb8; margin-bottom: 30px; }}

        .tabs {{
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
            border-bottom: 1px solid #26262c;
            padding-bottom: 10px;
        }}
        .tab {{
            padding: 10px 20px;
            background: #18181b;
            border: none;
            color: #adadb8;
            cursor: pointer;
            border-radius: 4px 4px 0 0;
            text-decoration: none;
        }}
        .tab.active, .tab:hover {{
            background: #26262c;
            color: #efeff1;
        }}

        /* Action Bar */
        .action-bar {{
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            align-items: center;
            flex-wrap: wrap;
        }}
        .action-bar select, .action-bar input {{
            padding: 10px;
            background: #18181b;
            color: #efeff1;
            border: 1px solid #26262c;
            border-radius: 4px;
            font-size: 14px;
        }}
        .action-bar select {{ min-width: 200px; }}

        /* Stats Grid */
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }}
        .stat-card {{
            background: #18181b;
            padding: 20px;
            border-radius: 8px;
            border-left: 3px solid #9147ff;
        }}
        .stat-card h3 {{ font-size: 14px; color: #adadb8; margin-bottom: 10px; text-transform: uppercase; }}
        .stat-card .value {{ font-size: 32px; font-weight: bold; color: #efeff1; }}
        .stat-card .platform {{ font-size: 12px; color: #adadb8; margin-top: 5px; }}

        /* Clip Grid */
        .clip-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 20px;
        }}
        .clip-card {{
            background: #18181b;
            border-radius: 8px;
            overflow: hidden;
            transition: transform 0.2s;
        }}
        .clip-card:hover {{ transform: translateY(-5px); }}
        .clip-thumbnail {{
            width: 100%;
            height: 169px;
            object-fit: cover;
            background: #26262c;
        }}
        .clip-info {{ padding: 15px; }}
        .clip-title {{
            font-weight: 600;
            margin-bottom: 8px;
            color: #efeff1;
            font-size: 14px;
            line-height: 1.4;
        }}
        .clip-meta {{
            font-size: 12px;
            color: #adadb8;
            margin-bottom: 10px;
        }}
        .clip-actions {{
            display: flex;
            gap: 8px;
            margin-top: 15px;
            flex-wrap: wrap;
        }}

        /* Buttons */
        .btn {{
            padding: 8px 16px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            transition: opacity 0.2s;
            text-decoration: none;
            display: inline-block;
        }}
        .btn:hover {{ opacity: 0.8; }}
        .btn-primary {{ background: #9147ff; color: white; }}
        .btn-success {{ background: #00c853; color: white; }}
        .btn-secondary {{ background: #26262c; color: #adadb8; }}
        .btn-danger {{ background: #e91e63; color: white; }}
        .btn-small {{ padding: 6px 12px; font-size: 12px; }}

        /* Platform Badges */
        .platform-badges {{
            display: flex;
            gap: 8px;
            margin-top: 10px;
        }}
        .badge {{
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .badge-tiktok {{ background: #000; color: #69c9d0; }}
        .badge-youtube {{ background: #ff0000; color: white; }}
        .badge-instagram {{ background: linear-gradient(45deg, #f09433 0%, #e6683c 25%, #dc2743 50%, #cc2366 75%, #bc1888 100%); color: white; }}
        .badge-pending {{ background: #26262c; color: #adadb8; }}

        /* Modal */
        .modal {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }}
        .modal.active {{ display: flex; }}
        .modal-content {{
            background: #18181b;
            padding: 30px;
            border-radius: 8px;
            max-width: 500px;
            width: 90%;
            max-height: 80vh;
            overflow-y: auto;
        }}
        .modal-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }}
        .modal-header h2 {{ margin: 0; }}
        .modal-close {{
            background: none;
            border: none;
            color: #adadb8;
            font-size: 24px;
            cursor: pointer;
        }}
        .form-group {{
            margin-bottom: 20px;
        }}
        .form-group label {{
            display: block;
            margin-bottom: 8px;
            color: #adadb8;
            font-size: 14px;
        }}
        .form-group input, .form-group textarea, .form-group select {{
            width: 100%;
            padding: 10px;
            background: #0e0e10;
            color: #efeff1;
            border: 1px solid #26262c;
            border-radius: 4px;
            font-size: 14px;
            font-family: inherit;
        }}
        .form-group textarea {{ min-height: 100px; resize: vertical; }}
        .checkbox-group {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .checkbox-group input[type="checkbox"] {{
            width: auto;
        }}

        /* Template Cards */
        .template-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }}
        .template-card {{
            background: #18181b;
            padding: 15px;
            border-radius: 8px;
            border-left: 3px solid #9147ff;
            cursor: pointer;
            transition: transform 0.2s;
        }}
        .template-card:hover {{ transform: translateX(5px); }}
        .template-card.default {{ border-left-color: #00c853; }}
        .template-name {{
            font-weight: 600;
            margin-bottom: 8px;
            color: #efeff1;
        }}
        .template-desc {{
            font-size: 12px;
            color: #adadb8;
            margin-bottom: 8px;
        }}
        .template-hashtags {{
            font-size: 11px;
            color: #9147ff;
        }}

        .loader {{
            text-align: center;
            padding: 40px;
            color: #adadb8;
        }}

        /* Platform Connection Cards */
        .platform-grid {{
            display: grid;
            gap: 20px;
            margin-top: 20px;
        }}
        .platform-card {{
            background: #18181b;
            border-radius: 8px;
            padding: 20px;
            display: flex;
            align-items: center;
            gap: 20px;
            border-left: 3px solid #26262c;
        }}
        .platform-card.connected {{
            border-left-color: #00c853;
        }}
        .platform-logo {{
            width: 60px;
            height: 60px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 32px;
        }}
        .platform-info {{
            flex: 1;
        }}
        .platform-name {{
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 5px;
        }}
        .platform-status {{
            font-size: 14px;
            color: #adadb8;
        }}
        .platform-status.connected {{
            color: #00c853;
        }}
        .platform-actions {{
            display: flex;
            gap: 10px;
        }}
        .btn-connect {{
            background: #9147ff;
            color: white;
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-weight: 500;
        }}
        .btn-disconnect {{
            background: #e91e63;
        }}

        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
    </style>
</head>
<body data-auth-streamer="{safe_streamer_data}">
    <div class="container">
        <h1>🎬 Social Media Clip Manager</h1>
        <p class="subtitle">Verwalte deine Twitch-Clips und veröffentliche sie auf TikTok, YouTube & Instagram</p>
        <p class="subtitle">Aktiver Streamer: <strong>{safe_streamer_label}</strong></p>

        <div class="tabs">
            <button class="tab active" onclick="switchTab('dashboard')">📊 Dashboard</button>
            <button class="tab" onclick="switchTab('clips')">🎥 Clips</button>
            <button class="tab" onclick="switchTab('templates')">📝 Templates</button>
            <button class="tab" onclick="switchTab('settings')">⚙️ Einstellungen</button>
        </div>

        <!-- Dashboard Tab -->
        <div id="tab-dashboard" class="tab-content active">
            <!-- Action Bar -->
            <div class="action-bar">
                <select id="status-filter" onchange="filterChanged()">
                    <option value="">Alle Status</option>
                    <option value="not-uploaded">Nicht hochgeladen</option>
                    <option value="uploaded">Hochgeladen</option>
                </select>
                <button class="btn btn-primary" onclick="fetchClipsManual()">🔄 Clips Aktualisieren</button>
                <button class="btn btn-success" onclick="openBatchUploadModal()">📤 Batch Upload</button>
            </div>

            <!-- Stats Grid -->
            <div class="stats-grid" id="stats-grid">
                <div class="loader">Lade Statistiken...</div>
            </div>

            <h2>Clips</h2>
            <div class="clip-grid" id="clip-grid">
                <div class="loader">Lade Clips...</div>
            </div>
        </div>

        <!-- Clips Tab -->
        <div id="tab-clips" class="tab-content">
            <h2>Alle Clips</h2>
            <div class="clip-grid" id="all-clips-grid">
                <div class="loader">Lade Clips...</div>
            </div>
        </div>

        <!-- Templates Tab -->
        <div id="tab-templates" class="tab-content">
            <h2>Empfohlene Templates</h2>
            <div class="template-grid" id="global-templates-grid">
                <div class="loader">Lade Templates...</div>
            </div>

            <h2>Meine Templates</h2>
            <button class="btn btn-primary" onclick="openCreateTemplateModal()" style="margin-bottom: 15px;">+ Neues Template</button>
            <div class="template-grid" id="streamer-templates-grid">
                <div class="loader">Lade Templates...</div>
            </div>
        </div>

        <!-- Settings Tab -->
        <div id="tab-settings" class="tab-content">
            <h2>Plattform-Verbindungen</h2>
            <p class="subtitle">Verbinde deine Social Media Accounts für automatische Uploads.</p>

            <div id="oauth-messages"></div>

            <div id="platform-connections" class="platform-grid">
                <div class="loader">Lade Plattform-Status...</div>
            </div>
        </div>
    </div>

    <!-- Batch Upload Modal -->
    <div id="batch-upload-modal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Batch Upload</h2>
                <button class="modal-close" onclick="closeBatchUploadModal()">&times;</button>
            </div>
            <p style="color: #adadb8; margin-bottom: 20px;">
                Alle nicht hochgeladenen Clips des ausgewählten Streamers werden hochgeladen.
            </p>
            <div class="form-group">
                <label>Plattformen:</label>
                <div class="checkbox-group">
                    <input type="checkbox" id="batch-tiktok" checked> TikTok
                </div>
                <div class="checkbox-group">
                    <input type="checkbox" id="batch-youtube" checked> YouTube Shorts
                </div>
                <div class="checkbox-group">
                    <input type="checkbox" id="batch-instagram" checked> Instagram Reels
                </div>
            </div>
            <div class="form-group">
                <div class="checkbox-group">
                    <input type="checkbox" id="batch-apply-template" checked>
                    <label style="margin: 0;">Standard-Template anwenden</label>
                </div>
            </div>
            <button class="btn btn-success" onclick="executeBatchUpload()" style="width: 100%;">📤 Upload Starten</button>
        </div>
    </div>

    <!-- Create Template Modal -->
    <div id="create-template-modal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Neues Template</h2>
                <button class="modal-close" onclick="closeCreateTemplateModal()">&times;</button>
            </div>
            <div class="form-group">
                <label>Template Name:</label>
                <input type="text" id="template-name" placeholder="z.B. Mein Standard-Template">
            </div>
            <div class="form-group">
                <label>Beschreibung (Placeholders: {{{{title}}}}, {{{{streamer}}}}, {{{{game}}}}):</label>
                <textarea id="template-description" placeholder="Epic {{{{game}}}} moment by {{{{streamer}}}}! 🎮"></textarea>
            </div>
            <div class="form-group">
                <label>Hashtags (komma-getrennt):</label>
                <input type="text" id="template-hashtags" placeholder="gaming, twitch, {{{{game}}}}">
            </div>
            <div class="form-group">
                <div class="checkbox-group">
                    <input type="checkbox" id="template-is-default">
                    <label style="margin: 0;">Als Standard-Template verwenden</label>
                </div>
            </div>
            <button class="btn btn-success" onclick="createTemplate()" style="width: 100%;">💾 Speichern</button>
        </div>
    </div>

    <script>
        // Global State
        let currentStreamer = document.body.dataset.authStreamer || new URLSearchParams(window.location.search).get('streamer') || '';
        let currentStatus = '';
        let allClips = [];

        // ========== Initialization ==========
        async function init() {{
            await loadStats();
            await loadClips();
        }}

        // ========== Tab Switching ==========
        function switchTab(tabName) {{
            // Update tab buttons
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            event.target.classList.add('active');

            // Update tab content
            document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
            document.getElementById(`tab-${{tabName}}`).classList.add('active');

            // Load content for tab
            if (tabName === 'templates') {{
                loadTemplates();
            }} else if (tabName === 'clips') {{
                loadAllClips();
            }} else if (tabName === 'settings') {{
                loadPlatformConnections();
            }}
        }}

        // ========== Stats Loading ==========
        async function loadStats() {{
            try {{
                const params = new URLSearchParams();
                if (currentStreamer) params.append('streamer', currentStreamer);

                const response = await fetch(`/social-media/api/stats?${{params}}`);
                const data = await response.json();

                const statsGrid = document.getElementById('stats-grid');
                const clips = data.clips || {{}};

                // Calculate not uploaded
                const total = clips.total || 0;
                const uploaded = (clips.tiktok_uploads || 0) + (clips.youtube_uploads || 0) + (clips.instagram_uploads || 0);
                const notUploaded = Math.max(0, total * 3 - uploaded);

                statsGrid.innerHTML = `
                    <div class="stat-card">
                        <h3>Total Clips</h3>
                        <div class="value">${{total}}</div>
                    </div>
                    <div class="stat-card">
                        <h3>Nicht Hochgeladen</h3>
                        <div class="value">${{notUploaded}}</div>
                        <div class="platform">⏳ Pending</div>
                    </div>
                    <div class="stat-card">
                        <h3>TikTok</h3>
                        <div class="value">${{clips.tiktok_uploads || 0}}</div>
                        <div class="platform">🎵 TikTok</div>
                    </div>
                    <div class="stat-card">
                        <h3>YouTube</h3>
                        <div class="value">${{clips.youtube_uploads || 0}}</div>
                        <div class="platform">📺 Shorts</div>
                    </div>
                    <div class="stat-card">
                        <h3>Instagram</h3>
                        <div class="value">${{clips.instagram_uploads || 0}}</div>
                        <div class="platform">📷 Reels</div>
                    </div>
                `;
            }} catch (error) {{
                console.error('Failed to load stats:', error);
            }}
        }}

        // ========== Clips Loading ==========
        async function loadClips() {{
            try {{
                const params = new URLSearchParams({{ limit: 50 }});
                if (currentStreamer) params.append('streamer', currentStreamer);

                const response = await fetch(`/social-media/api/clips?${{params}}`);
                allClips = await response.json();

                renderClips();
            }} catch (error) {{
                console.error('Failed to load clips:', error);
                document.getElementById('clip-grid').innerHTML = '<p style="color: #e91e63;">Fehler beim Laden der Clips</p>';
            }}
        }}

        function renderClips() {{
            let filteredClips = allClips;

            // Apply status filter
            if (currentStatus === 'not-uploaded') {{
                filteredClips = allClips.filter(clip =>
                    !clip.uploaded_tiktok || !clip.uploaded_youtube || !clip.uploaded_instagram
                );
            }} else if (currentStatus === 'uploaded') {{
                filteredClips = allClips.filter(clip =>
                    clip.uploaded_tiktok && clip.uploaded_youtube && clip.uploaded_instagram
                );
            }}

            const grid = document.getElementById('clip-grid');

            if (filteredClips.length === 0) {{
                grid.innerHTML = '<p style="color: #adadb8;">Keine Clips gefunden.</p>';
                return;
            }}

            grid.innerHTML = filteredClips.map(clip => `
                <div class="clip-card">
                    <img src="${{clip.clip_thumbnail_url}}" class="clip-thumbnail" alt="${{clip.clip_title}}">
                    <div class="clip-info">
                        <div class="clip-title">${{clip.clip_title}}</div>
                        <div class="clip-meta">
                            👁 ${{clip.view_count}} • ⏱ ${{Math.round(clip.duration_seconds)}}s
                            ${{clip.game_name ? `• 🎮 ${{clip.game_name}}` : ''}}
                        </div>

                        <div class="platform-badges">
                            ${{clip.uploaded_tiktok ? '<span class="badge badge-tiktok">TikTok ✓</span>' : '<span class="badge badge-pending">TikTok</span>'}}
                            ${{clip.uploaded_youtube ? '<span class="badge badge-youtube">YouTube ✓</span>' : '<span class="badge badge-pending">YouTube</span>'}}
                            ${{clip.uploaded_instagram ? '<span class="badge badge-instagram">Instagram ✓</span>' : '<span class="badge badge-pending">Instagram</span>'}}
                        </div>

                        <div class="clip-actions">
                            <button class="btn btn-primary btn-small" onclick="queueSingleClip(${{clip.id}})">
                                📤 Upload
                            </button>
                            <button class="btn btn-secondary btn-small" onclick="markAsUploaded(${{clip.id}})">
                                ✓ Mark
                            </button>
                            <a href="${{clip.clip_url}}" target="_blank" class="btn btn-secondary btn-small">
                                🔗 View
                            </a>
                        </div>
                    </div>
                </div>
            `).join('');
        }}

        async function loadAllClips() {{
            const grid = document.getElementById('all-clips-grid');
            grid.innerHTML = '<div class="loader">Lade Clips...</div>';

            try {{
                const params = new URLSearchParams({{ limit: 100 }});
                if (currentStreamer) params.append('streamer', currentStreamer);
                const response = await fetch(`/social-media/api/clips?${{params}}`);
                const clips = await response.json();

                if (clips.length === 0) {{
                    grid.innerHTML = '<p style="color: #adadb8;">Keine Clips gefunden.</p>';
                    return;
                }}

                grid.innerHTML = clips.map(clip => `
                    <div class="clip-card">
                        <img src="${{clip.clip_thumbnail_url}}" class="clip-thumbnail">
                        <div class="clip-info">
                            <div class="clip-title">${{clip.clip_title}}</div>
                            <div class="clip-meta">
                                ${{clip.streamer_login}} • ${{clip.view_count}} views
                            </div>
                            <div class="platform-badges">
                                ${{clip.uploaded_tiktok ? '<span class="badge badge-tiktok">TT ✓</span>' : ''}}
                                ${{clip.uploaded_youtube ? '<span class="badge badge-youtube">YT ✓</span>' : ''}}
                                ${{clip.uploaded_instagram ? '<span class="badge badge-instagram">IG ✓</span>' : ''}}
                            </div>
                        </div>
                    </div>
                `).join('');
            }} catch (error) {{
                console.error('Failed to load all clips:', error);
                grid.innerHTML = '<p style="color: #e91e63;">Fehler beim Laden</p>';
            }}
        }}

        // ========== Filter Handling ==========
        function filterChanged() {{
            currentStatus = document.getElementById('status-filter').value;

            loadStats();
            loadClips();
        }}

        // ========== Manual Clip Fetch ==========
        async function fetchClipsManual() {{
            if (!currentStreamer) {{
                alert('Bitte wähle einen Streamer aus.');
                return;
            }}

            try {{
                const response = await fetch('/social-media/api/fetch-clips', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ streamer: currentStreamer, limit: 20, days: 7 }})
                }});

                const result = await response.json();

                if (result.success) {{
                    alert(`Clips aktualisiert: ${{result.clips_found}} gefunden`);
                    await loadStats();
                    await loadClips();
                }} else {{
                    alert(`Fehler: ${{result.error}}`);
                }}
            }} catch (error) {{
                console.error('Failed to fetch clips:', error);
                alert('Fehler beim Fetchen der Clips');
            }}
        }}

        // ========== Upload Actions ==========
        async function queueSingleClip(clipId) {{
            try {{
                const response = await fetch('/social-media/api/upload', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        clip_id: clipId,
                        platforms: ['tiktok', 'youtube', 'instagram']
                    }})
                }});

                const result = await response.json();
                alert('Clip zur Upload-Queue hinzugefügt!');
                await loadClips();
            }} catch (error) {{
                console.error('Failed to queue upload:', error);
                alert('Fehler beim Hinzufügen zur Queue');
            }}
        }}

        async function markAsUploaded(clipId) {{
            try {{
                const response = await fetch('/social-media/api/mark-uploaded', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        clip_id: clipId,
                        platforms: ['tiktok', 'youtube', 'instagram']
                    }})
                }});

                const result = await response.json();

                if (result.success) {{
                    alert('Clip als hochgeladen markiert!');
                    await loadStats();
                    await loadClips();
                }} else {{
                    alert(`Fehler: ${{result.error}}`);
                }}
            }} catch (error) {{
                console.error('Failed to mark as uploaded:', error);
                alert('Fehler beim Markieren');
            }}
        }}

        // ========== Batch Upload Modal ==========
        function openBatchUploadModal() {{
            if (!currentStreamer) {{
                alert('Bitte wähle einen Streamer aus.');
                return;
            }}
            document.getElementById('batch-upload-modal').classList.add('active');
        }}

        function closeBatchUploadModal() {{
            document.getElementById('batch-upload-modal').classList.remove('active');
        }}

        async function executeBatchUpload() {{
            const platforms = [];
            if (document.getElementById('batch-tiktok').checked) platforms.push('tiktok');
            if (document.getElementById('batch-youtube').checked) platforms.push('youtube');
            if (document.getElementById('batch-instagram').checked) platforms.push('instagram');

            if (platforms.length === 0) {{
                alert('Bitte wähle mindestens eine Plattform aus.');
                return;
            }}

            try {{
                const response = await fetch('/social-media/api/batch-upload', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        streamer: currentStreamer,
                        platforms: platforms,
                        apply_default_template: document.getElementById('batch-apply-template').checked
                    }})
                }});

                const result = await response.json();

                if (result.success) {{
                    alert(`Batch Upload: ${{result.stats.queued}} Clips in Queue, ${{result.stats.errors}} Fehler`);
                    closeBatchUploadModal();
                    await loadStats();
                    await loadClips();
                }} else {{
                    alert(`Fehler: ${{result.error}}`);
                }}
            }} catch (error) {{
                console.error('Batch upload failed:', error);
                alert('Fehler beim Batch Upload');
            }}
        }}

        // ========== Templates ==========
        async function loadTemplates() {{
            // Load global templates
            try {{
                const response = await fetch('/social-media/api/templates/global');
                const data = await response.json();

                const grid = document.getElementById('global-templates-grid');

                if (data.templates.length === 0) {{
                    grid.innerHTML = '<p style="color: #adadb8;">Keine Templates gefunden.</p>';
                }} else {{
                    grid.innerHTML = data.templates.map(tpl => `
                        <div class="template-card">
                            <div class="template-name">${{tpl.template_name}}</div>
                            <div class="template-desc">${{tpl.description_template}}</div>
                            <div class="template-hashtags">${{tpl.hashtags.join(', ')}}</div>
                            <div style="margin-top: 10px; font-size: 11px; color: #adadb8;">
                                Verwendet: ${{tpl.usage_count}}x
                            </div>
                        </div>
                    `).join('');
                }}
            }} catch (error) {{
                console.error('Failed to load global templates:', error);
            }}

            // Load streamer templates
            if (currentStreamer) {{
                try {{
                    const response = await fetch(`/social-media/api/templates/streamer?streamer=${{currentStreamer}}`);
                    const data = await response.json();

                    const grid = document.getElementById('streamer-templates-grid');

                    if (data.templates.length === 0) {{
                        grid.innerHTML = '<p style="color: #adadb8;">Keine eigenen Templates. Erstelle eins mit dem Button oben!</p>';
                    }} else {{
                        grid.innerHTML = data.templates.map(tpl => `
                            <div class="template-card ${{tpl.is_default ? 'default' : ''}}">
                                <div class="template-name">
                                    ${{tpl.template_name}}
                                    ${{tpl.is_default ? '<span style="color: #00c853;">⭐ Default</span>' : ''}}
                                </div>
                                <div class="template-desc">${{tpl.description_template}}</div>
                                <div class="template-hashtags">${{tpl.hashtags.join(', ')}}</div>
                            </div>
                        `).join('');
                    }}
                }} catch (error) {{
                    console.error('Failed to load streamer templates:', error);
                }}
            }}
        }}

        // ========== Create Template Modal ==========
        function openCreateTemplateModal() {{
            if (!currentStreamer) {{
                alert('Bitte wähle einen Streamer aus.');
                return;
            }}
            document.getElementById('create-template-modal').classList.add('active');
        }}

        function closeCreateTemplateModal() {{
            document.getElementById('create-template-modal').classList.remove('active');
        }}

        async function createTemplate() {{
            const name = document.getElementById('template-name').value.trim();
            const description = document.getElementById('template-description').value.trim();
            const hashtagsStr = document.getElementById('template-hashtags').value.trim();
            const isDefault = document.getElementById('template-is-default').checked;

            if (!name || !description) {{
                alert('Bitte fülle alle Felder aus.');
                return;
            }}

            const hashtags = hashtagsStr.split(',').map(h => h.trim()).filter(h => h);

            try {{
                const response = await fetch('/social-media/api/templates/streamer', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        streamer: currentStreamer,
                        template_name: name,
                        description: description,
                        hashtags: hashtags,
                        is_default: isDefault
                    }})
                }});

                const result = await response.json();

                if (result.success) {{
                    alert('Template erstellt!');
                    closeCreateTemplateModal();

                    // Clear form
                    document.getElementById('template-name').value = '';
                    document.getElementById('template-description').value = '';
                    document.getElementById('template-hashtags').value = '';
                    document.getElementById('template-is-default').checked = false;

                    // Reload templates
                    loadTemplates();
                }} else {{
                    alert(`Fehler: ${{result.error}}`);
                }}
            }} catch (error) {{
                console.error('Failed to create template:', error);
                alert('Fehler beim Erstellen des Templates');
            }}
        }}

        // ========== Platform Connections ==========
        async function loadPlatformConnections() {{
            try {{
                const params = new URLSearchParams();
                if (currentStreamer) params.append('streamer', currentStreamer);

                const response = await fetch(`/social-media/api/platforms/status?${{params}}`);
                const data = await response.json();

                const container = document.getElementById('platform-connections');

                const platformConfig = {{
                    tiktok: {{ name: 'TikTok', logo: '🎵', color: '#000' }},
                    youtube: {{ name: 'YouTube', logo: '📺', color: '#ff0000' }},
                    instagram: {{ name: 'Instagram', logo: '📷', color: '#e1306c' }}
                }};

                container.innerHTML = data.platforms.map(p => {{
                    const config = platformConfig[p.platform];
                    const isConnected = p.connected;

                    return `
                        <div class="platform-card ${{isConnected ? 'connected' : ''}}">
                            <div class="platform-logo" style="background: ${{config.color}};">
                                ${{config.logo}}
                            </div>
                            <div class="platform-info">
                                <div class="platform-name">${{config.name}}</div>
                                <div class="platform-status ${{isConnected ? 'connected' : ''}}">
                                    ${{isConnected
                                        ? `✅ Konto verknüpft${{p.username ? ` (${{p.username}})` : ''}}`
                                        : '○ Konto nicht verbunden'
                                    }}
                                </div>
                            </div>
                            <div class="platform-actions">
                                ${{isConnected
                                    ? `
                                        <button class="btn btn-connect" onclick="reconnectPlatform('${{p.platform}}')">
                                            Erneut verbinden
                                        </button>
                                        <button class="btn btn-connect btn-disconnect" onclick="disconnectPlatform('${{p.platform}}')">
                                            Trennen
                                        </button>
                                    `
                                    : `
                                        <button class="btn btn-connect" onclick="connectPlatform('${{p.platform}}')">
                                            Mit ${{config.name}} verbinden
                                        </button>
                                    `
                                }}
                            </div>
                        </div>
                    `;
                }}).join('');

                // Show OAuth messages if present
                showOAuthMessages();

            }} catch (error) {{
                console.error('Failed to load platform connections:', error);
                document.getElementById('platform-connections').innerHTML =
                    '<p style="color: #e91e63;">Fehler beim Laden der Plattform-Status</p>';
            }}
        }}

        function connectPlatform(platform) {{
            const params = new URLSearchParams();
            if (currentStreamer) params.append('streamer', currentStreamer);
            window.location.href = `/social-media/oauth/start/${{platform}}?${{params}}`;
        }}

        function reconnectPlatform(platform) {{
            connectPlatform(platform);
        }}

        async function disconnectPlatform(platform) {{
            if (!confirm(`${{platform.toUpperCase()}} Verbindung wirklich trennen?`)) return;

            try {{
                const params = new URLSearchParams();
                if (currentStreamer) params.append('streamer', currentStreamer);

                const response = await fetch(`/social-media/oauth/disconnect/${{platform}}?${{params}}`, {{
                    method: 'POST'
                }});

                const result = await response.json();

                if (result.success) {{
                    alert('Verbindung getrennt!');
                    await loadPlatformConnections();
                }} else {{
                    alert(`Fehler: ${{result.error}}`);
                }}
            }} catch (error) {{
                console.error('Failed to disconnect platform:', error);
                alert('Fehler beim Trennen der Verbindung');
            }}
        }}

        function renderOAuthMessage(message, isError) {{
            const container = document.getElementById('oauth-messages');
            if (!container) return;

            container.textContent = '';
            const box = document.createElement('div');
            box.style.background = isError ? '#e91e63' : '#00c853';
            box.style.color = 'white';
            box.style.padding = '15px';
            box.style.borderRadius = '4px';
            box.style.marginBottom = '20px';
            box.textContent = isError ? `❌ Fehler: ${{message}}` : `✅ ${{message}}`;
            container.appendChild(box);
        }}

        function showOAuthMessages() {{
            const urlParams = new URLSearchParams(window.location.search);
            const success = urlParams.get('oauth_success');
            const error = urlParams.get('oauth_error');

            if (success) {{
                renderOAuthMessage(`Erfolgreich mit ${{success.toUpperCase()}} verbunden!`, false);

                // Remove URL params after showing message
                setTimeout(() => {{
                    window.history.replaceState({{}}, document.title, '/social-media');
                    const container = document.getElementById('oauth-messages');
                    if (container) container.textContent = '';
                }}, 5000);
            }} else if (error) {{
                const errorMap = {{
                    provider_error: 'OAuth-Anbieter hat den Zugriff abgelehnt.',
                    invalid_callback: 'OAuth-Antwort ist ungültig oder abgelaufen.',
                    callback_failed: 'OAuth-Verarbeitung fehlgeschlagen. Bitte erneut versuchen.',
                    oauth_start_failed: 'OAuth-Start fehlgeschlagen. Bitte erneut versuchen.'
                }};
                renderOAuthMessage(errorMap[error] || 'OAuth-Verbindung fehlgeschlagen.', true);

                setTimeout(() => {{
                    window.history.replaceState({{}}, document.title, '/social-media');
                    const container = document.getElementById('oauth-messages');
                    if (container) container.textContent = '';
                }}, 8000);
            }}
        }}

        // Close modals on background click
        document.addEventListener('click', (e) => {{
            if (e.target.classList.contains('modal')) {{
                e.target.classList.remove('active');
            }}
        }});

        // Initialize on load
        init();

        // Show OAuth messages on page load
        showOAuthMessages();
    </script>
</body>
</html>
"""

        return web.Response(text=html_content, content_type="text/html")

    async def api_stats(self, request: web.Request) -> web.Response:
        """Stats API endpoint for dashboard."""
        self._require_auth(request)

        streamer = self._resolve_streamer_scope(request, request.query.get("streamer"))
        summary = self.clip_manager.get_analytics_summary(streamer_login=streamer)

        return web.json_response(summary)

    async def clips_list(self, request: web.Request) -> web.Response:
        """Clips list API endpoint."""
        self._require_auth(request)

        limit = int(request.query.get("limit", "50"))
        streamer = self._resolve_streamer_scope(request, request.query.get("streamer"))
        status = request.query.get("status")

        clips = self.clip_manager.get_clips_for_dashboard(
            streamer_login=streamer,
            status=status,
            limit=limit,
        )

        return web.json_response(clips)

    async def queue_upload(self, request: web.Request) -> web.Response:
        """Queue upload API endpoint."""
        self._require_auth(request)

        data = await request.json()
        clip_id = self._normalize_clip_id(data.get("clip_id"))
        platforms = data.get("platforms", [])  # ['tiktok', 'youtube', 'instagram'] or 'all'

        if not clip_id:
            return web.json_response({"error": "clip_id required"}, status=400)

        streamer = self._resolve_streamer_scope(
            request,
            data.get("streamer") or request.query.get("streamer"),
        )
        if streamer and not self._clip_owned_by_streamer(clip_id, streamer):
            return web.json_response(
                {"error": "forbidden: clip does not belong to authenticated streamer"},
                status=403,
            )

        if platforms == "all":
            platforms = ["tiktok", "youtube", "instagram"]

        queued = []
        for platform in platforms:
            try:
                queue_id = self.clip_manager.queue_upload(
                    clip_db_id=clip_id,
                    platform=platform,
                    title=data.get("title"),
                    description=data.get("description"),
                    hashtags=data.get("hashtags"),
                    priority=data.get("priority", 0),
                )
                queued.append({"platform": platform, "queue_id": queue_id})
            except Exception:
                safe_platform = _sanitize_log_value(platform)
                log.exception("Failed to queue upload for platform=%s", safe_platform)
                queued.append({"platform": platform, "error": "queue_failed"})

        return web.json_response({"queued": queued})

    async def analytics(self, request: web.Request) -> web.Response:
        """Analytics dashboard."""
        self._require_auth(request)

        streamer = self._resolve_streamer_scope(request, request.query.get("streamer"))
        summary = self.clip_manager.get_analytics_summary(streamer_login=streamer)

        return web.json_response(summary)

    # ========== Template Management API ==========

    async def api_templates_global(self, request: web.Request) -> web.Response:
        """GET /api/templates/global - Get global templates."""
        self._require_auth(request)

        category = request.query.get("category")
        templates = self.clip_manager.get_global_templates(category=category)

        return web.json_response({"templates": templates})

    async def api_templates_streamer(self, request: web.Request) -> web.Response:
        """GET /api/templates/streamer - Get streamer templates."""
        self._require_auth(request)

        streamer = self._resolve_streamer_scope(
            request,
            request.query.get("streamer"),
            required=True,
        )

        templates = self.clip_manager.get_streamer_templates(streamer_login=streamer)

        return web.json_response({"templates": templates})

    async def api_create_template(self, request: web.Request) -> web.Response:
        """POST /api/templates/streamer - Create/Update streamer template."""
        self._require_auth(request)

        try:
            data = await request.json()

            streamer = self._resolve_streamer_scope(
                request,
                data.get("streamer"),
                required=True,
            )
            template_name = data.get("template_name")
            description = data.get("description")
            hashtags = data.get("hashtags", [])
            is_default = data.get("is_default", False)

            if not all([template_name, description]):
                return web.json_response(
                    {"error": "template_name and description are required"},
                    status=400
                )

            template_id = self.clip_manager.create_streamer_template(
                streamer_login=streamer,
                template_name=template_name,
                description_template=description,
                hashtags=hashtags,
                is_default=is_default,
            )

            return web.json_response({
                "success": True,
                "template_id": template_id,
                "message": "Template created/updated successfully"
            })

        except web.HTTPException:
            raise
        except Exception:
            log.exception("Failed to create template")
            return web.json_response({"error": "template_create_failed"}, status=500)

    async def api_apply_template(self, request: web.Request) -> web.Response:
        """POST /api/templates/apply - Apply template to clip."""
        self._require_auth(request)

        try:
            data = await request.json()

            clip_id = self._normalize_clip_id(data.get("clip_id"))
            template_id = self._normalize_clip_id(data.get("template_id"))
            is_global = data.get("is_global", False)

            if not clip_id or not template_id:
                return web.json_response(
                    {"error": "clip_id and template_id are required"},
                    status=400
                )

            streamer = self._resolve_streamer_scope(
                request,
                data.get("streamer") or request.query.get("streamer"),
            )
            if streamer and not self._clip_owned_by_streamer(clip_id, streamer):
                return web.json_response(
                    {"error": "forbidden: clip does not belong to authenticated streamer"},
                    status=403,
                )
            if streamer and not is_global and not self._streamer_template_owned_by_streamer(template_id, streamer):
                return web.json_response(
                    {"error": "forbidden: template does not belong to authenticated streamer"},
                    status=403,
                )

            success = self.clip_manager.apply_template_to_clip(
                clip_id=clip_id,
                template_id=template_id,
                is_global=is_global,
            )

            if success:
                return web.json_response({
                    "success": True,
                    "message": "Template applied successfully"
                })
            else:
                return web.json_response(
                    {"error": "Failed to apply template"},
                    status=500
                )

        except web.HTTPException:
            raise
        except Exception:
            log.exception("Failed to apply template")
            return web.json_response({"error": "template_apply_failed"}, status=500)

    # ========== Batch Operations API ==========

    async def api_batch_upload(self, request: web.Request) -> web.Response:
        """POST /api/batch-upload - Batch upload all new clips."""
        self._require_auth(request)

        try:
            data = await request.json()

            streamer = self._resolve_streamer_scope(
                request,
                data.get("streamer"),
                required=True,
            )
            platforms = data.get("platforms", [])
            apply_default_template = data.get("apply_default_template", True)

            if not platforms:
                return web.json_response(
                    {"error": "platforms are required"},
                    status=400
                )

            stats = await self.clip_manager.batch_upload_all_new(
                streamer_login=streamer,
                platforms=platforms,
                apply_default_template=apply_default_template,
            )

            return web.json_response({
                "success": True,
                "stats": stats,
                "message": f"Queued {stats['queued']} clips, {stats['errors']} errors"
            })

        except web.HTTPException:
            raise
        except Exception:
            log.exception("Failed to batch upload")
            return web.json_response({"error": "batch_upload_failed"}, status=500)

    async def api_mark_uploaded(self, request: web.Request) -> web.Response:
        """POST /api/mark-uploaded - Manually mark clip as uploaded."""
        self._require_auth(request)

        try:
            data = await request.json()

            clip_id = self._normalize_clip_id(data.get("clip_id"))
            platforms = data.get("platforms", [])

            if not clip_id or not platforms:
                return web.json_response(
                    {"error": "clip_id and platforms are required"},
                    status=400
                )

            streamer = self._resolve_streamer_scope(
                request,
                data.get("streamer") or request.query.get("streamer"),
            )
            if streamer and not self._clip_owned_by_streamer(clip_id, streamer):
                return web.json_response(
                    {"error": "forbidden: clip does not belong to authenticated streamer"},
                    status=403,
                )

            success = self.clip_manager.mark_clip_uploaded(
                clip_id=clip_id,
                platforms=platforms,
                manual=True,
            )

            if success:
                return web.json_response({
                    "success": True,
                    "message": "Clip marked as uploaded"
                })
            else:
                return web.json_response(
                    {"error": "Failed to mark clip as uploaded"},
                    status=500
                )

        except web.HTTPException:
            raise
        except Exception:
            log.exception("Failed to mark clip as uploaded")
            return web.json_response({"error": "mark_uploaded_failed"}, status=500)

    # ========== Clip Fetching API ==========

    async def api_fetch_clips(self, request: web.Request) -> web.Response:
        """POST /api/fetch-clips - Manually fetch clips for streamer."""
        self._require_auth(request)

        try:
            data = await request.json()

            streamer = self._resolve_streamer_scope(
                request,
                data.get("streamer"),
                required=True,
            )
            limit = data.get("limit", 20)
            days = data.get("days", 7)

            clips = await self.clip_manager.fetch_recent_clips(
                streamer_login=streamer,
                limit=limit,
                days=days,
            )

            return web.json_response({
                "success": True,
                "clips_found": len(clips),
                "message": f"Fetched {len(clips)} clips"
            })

        except web.HTTPException:
            raise
        except Exception:
            log.exception("Failed to fetch clips")
            return web.json_response({"error": "fetch_clips_failed"}, status=500)

    async def api_last_hashtags(self, request: web.Request) -> web.Response:
        """GET /api/last-hashtags - Get last used hashtags."""
        self._require_auth(request)

        streamer = self._resolve_streamer_scope(
            request,
            request.query.get("streamer"),
            required=True,
        )

        hashtags = self.clip_manager.get_last_hashtags(streamer_login=streamer)

        return web.json_response({"hashtags": hashtags})

    # ========== OAuth & Platform Management ==========

    async def oauth_start(self, request: web.Request) -> web.Response:
        """Start OAuth flow for a platform."""
        self._require_auth(request)

        platform = request.match_info["platform"]
        streamer = self._resolve_streamer_scope(
            request,
            request.query.get("streamer"),
            required=True,
        )

        if platform not in ["tiktok", "youtube", "instagram"]:
            return web.Response(text="Invalid platform", status=400)

        from .oauth_manager import SocialMediaOAuthManager
        oauth_mgr = SocialMediaOAuthManager()

        try:
            redirect_uri = str(request.url.origin()) + "/social-media/oauth/callback"
            auth_url = oauth_mgr.generate_auth_url(platform, streamer, redirect_uri)

            return web.HTTPFound(auth_url)
        except Exception:
            log.exception("OAuth start failed")
            return web.HTTPFound(_dashboard_url(oauth_error="oauth_start_failed"))

    async def oauth_callback(self, request: web.Request) -> web.Response:
        """Handle OAuth callback from platform."""
        code = request.query.get("code")
        state = request.query.get("state")
        error = request.query.get("error")

        if error:
            safe_error = _sanitize_log_value(error)
            log.error("OAuth provider returned an error: %s", safe_error)
            return web.HTTPFound(_dashboard_url(oauth_error="provider_error"))

        if not code or not state:
            return web.Response(text="Missing code or state", status=400)

        from .oauth_manager import SocialMediaOAuthManager
        oauth_mgr = SocialMediaOAuthManager()

        try:
            result = await oauth_mgr.handle_callback(code, state)

            # Redirect back to dashboard with success message
            platform = result.get("platform", "unknown")
            if platform not in {"tiktok", "youtube", "instagram"}:
                platform = "unknown"
            return web.HTTPFound(_dashboard_url(oauth_success=platform))

        except ValueError:
            log.warning("OAuth callback validation failed")
            return web.HTTPFound(_dashboard_url(oauth_error="invalid_callback"))
        except Exception:
            log.exception("OAuth callback failed")
            return web.HTTPFound(_dashboard_url(oauth_error="callback_failed"))

    async def oauth_disconnect(self, request: web.Request) -> web.Response:
        """Disconnect a platform."""
        self._require_auth(request)

        platform = request.match_info["platform"]
        streamer = self._resolve_streamer_scope(
            request,
            request.query.get("streamer"),
            required=True,
        )

        if platform not in ["tiktok", "youtube", "instagram"]:
            return web.json_response({"error": "Invalid platform"}, status=400)

        from ..storage import get_conn

        try:
            with get_conn() as conn:
                conn.execute(
                    """
                    UPDATE social_media_platform_auth
                    SET enabled = 0
                    WHERE platform = ?
                      AND (streamer_login = ? OR (streamer_login IS NULL AND ? IS NULL))
                    """,
                    (platform, streamer, streamer)
                )

            safe_platform = _sanitize_log_value(platform)
            safe_streamer = _sanitize_log_value(streamer)
            log.info("Disconnected platform=%s, streamer=%s", safe_platform, safe_streamer)
            return web.json_response({"success": True})

        except Exception:
            log.exception("Failed to disconnect platform")
            return web.json_response({"error": "disconnect_failed"}, status=500)

    async def api_platforms_status(self, request: web.Request) -> web.Response:
        """GET platform connection status."""
        self._require_auth(request)

        streamer = self._resolve_streamer_scope(
            request,
            request.query.get("streamer"),
            required=True,
        )

        from .credential_manager import SocialMediaCredentialManager
        cred_mgr = SocialMediaCredentialManager()

        try:
            platforms_status = cred_mgr.get_all_platforms_status(streamer)

            platforms = []
            for platform_name, status in platforms_status.items():
                platforms.append({
                    "platform": platform_name,
                    "connected": status["connected"],
                    "username": status.get("username"),
                    "user_id": status.get("user_id"),
                    "expires_at": status.get("expires_at"),
                    "expired": status.get("expired", False),
                })

            return web.json_response({"platforms": platforms})

        except Exception:
            log.exception("Failed to get platform status")
            return web.json_response({"error": "platform_status_failed"}, status=500)


def create_social_media_app(
    clip_manager: ClipManager,
    auth_checker=None,
    auth_session_getter=None,
) -> web.Application:
    """
    Create Social Media Dashboard aiohttp app.

    Args:
        clip_manager: ClipManager instance
        auth_checker: Callable that checks authentication (from parent dashboard server)
        auth_session_getter: Callable that resolves dashboard OAuth session

    Returns:
        aiohttp Application
    """
    dashboard = SocialMediaDashboard(
        clip_manager,
        auth_checker=auth_checker,
        auth_session_getter=auth_session_getter,
    )
    return dashboard._build_app()
