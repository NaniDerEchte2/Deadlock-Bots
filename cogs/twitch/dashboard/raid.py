"""Raid Bot Dashboard Mixin."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Optional

from aiohttp import web

if TYPE_CHECKING:
    from ..raid_manager import RaidBot

log = logging.getLogger("TwitchStreams.Dashboard")


class DashboardRaidMixin:
    """Dashboard endpoints fuer Raid-Bot-Verwaltung."""

    # Wird vom Cog gesetzt
    _raid_bot: Optional[RaidBot] = None
    _redirect_uri: str = ""

    async def raid_auth_start(self, request: web.Request) -> web.Response:
        """Initiiert OAuth-Flow fuer einen Streamer."""
        token = request.query.get("token", "")
        if not self._check_token(token):
            return web.Response(text="Unauthorized", status=401)

        login = request.query.get("login", "").strip().lower()
        if not login:
            return web.Response(text="Missing login parameter", status=400)

        if not self._raid_bot:
            return web.Response(text="Raid bot not initialized", status=503)

        auth_url = self._raid_bot.auth_manager.generate_auth_url(login)
        return web.Response(
            text=f"""
            <html>
            <head><title>Raid Bot Autorisierung</title></head>
            <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto;">
                <h1>Raid Bot Autorisierung</h1>
                <p>Streamer: <strong>{html.escape(login, quote=True)}</strong></p>
                <p>Klicke auf den Link unten, um den Raid Bot zu autorisieren:</p>
                <p><a href="{auth_url}" style="padding: 10px 20px; background: #9146FF; color: white; text-decoration: none; border-radius: 5px;">Auf Twitch autorisieren</a></p>
                <p style="color: #666; font-size: 0.9em;">
                    Der Raid Bot kann dann automatisch in deinem Namen raiden, wenn du offline gehst.
                </p>
            </body>
            </html>
            """,
            content_type="text/html",
        )

    async def raid_oauth_callback(self, request: web.Request) -> web.Response:
        """OAuth Callback fuer Twitch-Autorisierung."""
        code = request.query.get("code")
        state = request.query.get("state")
        error = request.query.get("error")

        if error:
            return web.Response(
                text=f"""
                <html>
                <head><title>Autorisierung fehlgeschlagen</title></head>
                <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto;">
                    <h1>Autorisierung fehlgeschlagen</h1>
                    <p>Fehler: {html.escape(error, quote=True)}</p>
                    <p>Bitte schliesse dieses Fenster und starte den Vorgang erneut.</p>
                </body>
                </html>
                """,
                content_type="text/html",
            )

        if not code or not state:
            return web.Response(text="Missing code or state parameter", status=400)

        if not self._raid_bot:
            return web.Response(text="Raid bot not initialized", status=503)

        # State verifizieren
        login = self._raid_bot.auth_manager.verify_state(state)
        if not login:
            return web.Response(text="Invalid state token", status=400)

        try:
            # Code gegen Token tauschen
            session = request.app["_http_session"]
            token_data = await self._raid_bot.auth_manager.exchange_code_for_token(
                code, session
            )

            # User-Info holen (fuer twitch_user_id)
            access_token = token_data["access_token"]
            headers = {
                "Client-ID": self._raid_bot.auth_manager.client_id,
                "Authorization": f"Bearer {access_token}",
            }
            async with session.get(
                "https://api.twitch.tv/helix/users", headers=headers
            ) as r:
                if r.status != 200:
                    raise Exception(f"Failed to get user info: {r.status}")
                user_data = await r.json()
                user_info = user_data["data"][0]
                twitch_user_id = user_info["id"]
                twitch_login = user_info["login"]

            # Token speichern
            self._raid_bot.auth_manager.save_auth(
                twitch_user_id=twitch_user_id,
                twitch_login=twitch_login,
                access_token=token_data["access_token"],
                refresh_token=token_data["refresh_token"],
                expires_in=token_data.get("expires_in", 3600),
                scopes=token_data.get("scope", []),
            )

            log.info("Raid auth successful for %s", twitch_login)

            # Post-Auth Aktionen (Mod + Nachricht) - Hintergrund-Task um Response nicht zu blockieren
            import asyncio
            asyncio.create_task(self._raid_bot.complete_setup_for_streamer(twitch_user_id, twitch_login))

            return web.Response(
                text=f"""
                <html>
                <head><title>Autorisierung erfolgreich</title></head>
                <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto;">
                    <h1>Autorisierung erfolgreich!</h1>
                    <p>Der Raid Bot wurde erfolgreich fuer <strong>{twitch_login}</strong> autorisiert.</p>
                    <p>Auto-Raids sind jetzt aktiviert. Wenn du offline gehst, wird der Bot automatisch
                       einen anderen Online-Partner raiden.</p>
                    <p>Du kannst dieses Fenster schliessen.</p>
                </body>
                </html>
                """,
                content_type="text/html",
            )

        except Exception:
            log.exception("OAuth callback error for %s", login)
            return web.Response(
                text="""
                <html>
                <head><title>Fehler</title></head>
                <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto;">
                    <h1>Fehler bei der Autorisierung</h1>
                    <p>Ein interner Fehler ist aufgetreten. Bitte versuche es spaeter erneut.</p>
                    <p>Bitte schliesse dieses Fenster und versuche es spaeter erneut.</p>
                </body>
                </html>
                """,
                content_type="text/html",
            )

    async def raid_toggle(self, request: web.Request) -> web.Response:
        """Aktiviert/Deaktiviert Auto-Raid fuer einen Streamer."""
        token = (await request.post()).get("token", "")
        if not self._check_token(token):
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.post()
        user_id = data.get("user_id", "").strip()
        enabled = data.get("enabled", "1") == "1"

        if not user_id:
            return web.json_response({"error": "Missing user_id"}, status=400)

        if not self._raid_bot:
            return web.json_response({"error": "Raid bot not initialized"}, status=503)

        try:
            self._raid_bot.auth_manager.set_raid_enabled(user_id, enabled)
            return web.json_response(
                {"success": True, "message": f"Auto-Raid {'aktiviert' if enabled else 'deaktiviert'}"}
            )
        except Exception as exc:
            safe_user_id = user_id.replace("\r", "").replace("\n", "")
            log.exception("Failed to toggle raid for %s", safe_user_id)
            return web.json_response({"error": str(exc)}, status=500)

    async def raid_requirements(self, request: web.Request) -> web.Response:
        """
        Liefert eine kompakte Requirements-Seite und einen frischen OAuth-Link.
        Kein Pre-Gen-Link: jedes Öffnen erzeugt einen neuen State.
        """
        token = request.query.get("token", "")
        if not self._check_token(token):
            return web.Response(text="Unauthorized", status=401)

        login = request.query.get("login", "").strip().lower()
        if not login:
            return web.Response(text="Missing login parameter", status=400)

        if not self._raid_bot or not getattr(self._raid_bot, "auth_manager", None):
            return web.Response(text="Raid bot not initialized", status=503)

        auth_url = self._raid_bot.auth_manager.generate_auth_url(login)
        escaped_login = html.escape(login, quote=True)
        escaped_url = html.escape(auth_url, quote=True)

        body = f"""
<!DOCTYPE html>
<html lang="de">
<head><meta charset="utf-8"><title>Raid-Bot Anforderungen für {escaped_login}</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 2rem; color: #111; }}
.card {{ border:1px solid #ddd; border-radius:10px; padding:1.5rem; max-width:720px; }}
ul {{ padding-left:1.2rem; }}
.actions {{ margin-top:1rem; display:flex; gap:.6rem; flex-wrap:wrap; }}
.btn {{ padding:.65rem 1.1rem; border-radius:8px; border:none; cursor:pointer; font-weight:600; text-decoration:none; }}
.primary {{ background:#9146FF; color:white; }}
.ghost {{ background:#f4f4f6; color:#222; }}
</style>
</head>
<body>
  <div class="card">
    <h2>Raid-Bot Anforderungen für @{escaped_login}</h2>
    <p>Das braucht der Streamer, damit Auto-Raids laufen:</p>
    <ul>
      <li>Auf „Autorisiere den Raid-Bot“ klicken (öffnet Twitch OAuth).</li>
      <li>Twitch-Berechtigungen akzeptieren (Raids & Chat).</li>
      <li>Bot als Moderator behalten (wird automatisch gesetzt).</li>
    </ul>
    <div class="actions">
      <a class="btn primary" href="{escaped_url}" target="_blank" rel="noopener">Jetzt autorisieren</a>
      <button class="btn ghost" onclick="navigator.clipboard.writeText('{escaped_url}');this.textContent='Link kopiert';">Link kopieren</button>
    </div>
  </div>
</body>
</html>
"""
        return web.Response(text=body, content_type="text/html")

    async def raid_history(self, request: web.Request) -> web.Response:
        """Zeigt Raid-History an."""
        token = request.query.get("token", "")
        if not self._check_token(token):
            return web.Response(text="Unauthorized", status=401)

        limit = int(request.query.get("limit", "50"))
        from_broadcaster = request.query.get("from", "").strip()

        if hasattr(self, "_raid_history_cb") and self._raid_history_cb:
            history = await self._raid_history_cb(limit=limit, from_broadcaster=from_broadcaster)
        else:
            history = []

        # HTML-Tabelle generieren
        rows_html = ""
        for entry in history:
            success_icon = "OK" if entry.get("success") else "X"
            executed_at = entry.get("executed_at", "")[:19]  # Timestamp kuerzen
            rows_html += f"""
                <tr>
                    <td>{success_icon}</td>
                    <td>{executed_at}</td>
                    <td><strong>{entry.get('from_broadcaster_login')}</strong></td>
                    <td><strong>{entry.get('to_broadcaster_login')}</strong></td>
                    <td>{entry.get('viewer_count', 0)}</td>
                    <td>{entry.get('stream_duration_sec', 0) // 60} min</td>
                    <td>{entry.get('candidates_count', 0)}</td>
                    <td style="color: red; font-size: 0.85em;">{entry.get('error_message', '')}</td>
                </tr>
            """

        return web.Response(
            text=f"""
            <html>
            <head>
                <title>Raid History</title>
                <style>
                    body {{ font-family: sans-serif; margin: 32px; }}
                    table {{ border-collapse: collapse; width: 100%; }}
                    th, td {{ border: 1px solid #ddd; padding: 12px 10px; text-align: left; }}
                    th {{ background-color: #9146FF; color: white; }}
                    tr:nth-child(even) {{ background-color: #f2f2f2; }}
                </style>
            </head>
            <body>
                <h1>Raid History</h1>
                <p><a href="/twitch">Zurueck zum Dashboard</a></p>
                <table>
                    <thead>
                        <tr>
                            <th>Status</th>
                            <th>Zeitpunkt</th>
                            <th>Von</th>
                            <th>Nach</th>
                            <th>Viewer</th>
                            <th>Stream-Dauer</th>
                            <th>Kandidaten</th>
                            <th>Fehler</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html if rows_html else '<tr><td colspan="8">Keine Raids gefunden</td></tr>'}
                    </tbody>
                </table>
            </body>
            </html>
            """,
            content_type="text/html",
        )
