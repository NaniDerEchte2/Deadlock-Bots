"""Raid Bot Dashboard Mixin."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Optional

import discord
from aiohttp import web

from ..storage import get_conn
from ..raid_views import build_raid_requirements_embed, RaidAuthGenerateView

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
        Sendet die Anforderungen per Discord DM an den Streamer.
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

        try:
            with get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT discord_user_id, discord_display_name
                    FROM twitch_streamers
                    WHERE lower(twitch_login) = lower(?)
                    """,
                    (login,),
                ).fetchone()
        except Exception:
            log.exception("Failed to load Discord link for raid requirements (%s)", login)
            return web.Response(text="Failed to load Discord link", status=500)

        if not row:
            return web.Response(text="Streamer not found", status=404)

        discord_user_id = ""
        if row:
            raw_discord_id = row["discord_user_id"] if hasattr(row, "keys") else row[0]
            if raw_discord_id:
                discord_user_id = str(raw_discord_id).strip()
        if not discord_user_id:
            return web.Response(text="No Discord user linked for this streamer", status=404)

        try:
            user_id_int = int(discord_user_id)
        except (TypeError, ValueError):
            return web.Response(text="Invalid Discord user id", status=400)

        discord_bot = getattr(self._raid_bot.auth_manager, "_discord_bot", None)
        if not discord_bot:
            return web.Response(text="Discord bot not available", status=503)

        user = discord_bot.get_user(user_id_int)
        if user is None:
            try:
                user = await discord_bot.fetch_user(user_id_int)
            except discord.NotFound:
                user = None
            except discord.HTTPException:
                log.exception("Failed to fetch Discord user %s for %s", user_id_int, login)
                user = None

        if user is None:
            return web.Response(text="Discord user not found", status=404)

        embed = build_raid_requirements_embed(login)
        view = RaidAuthGenerateView(
            auth_manager=self._raid_bot.auth_manager,
            twitch_login=login,
        )

        try:
            await user.send(embed=embed, view=view)
        except discord.Forbidden:
            log.warning("Discord DM blocked for %s (%s)", login, user_id_int)
            return web.Response(text="Discord DM blocked", status=403)
        except discord.HTTPException:
            log.exception("Failed to send raid requirements DM to %s (%s)", login, user_id_int)
            return web.Response(text="Failed to send Discord DM", status=502)

        log.info("Sent raid requirements DM to %s (discord_id=%s)", login, discord_user_id)
        ok_message = f"Anforderungen per Discord an @{login} gesendet"
        raise web.HTTPFound(location=self._redirect_location(request, ok=ok_message))

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
