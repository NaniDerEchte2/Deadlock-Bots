"""Live dashboard views and actions for managing Twitch streamers."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import quote_plus

from aiohttp import web

from .base import log


class DashboardLiveMixin:
    async def index(self, request: web.Request):
        self._require_token(request)
        items = await self._list()

        msg = request.query.get("ok", "")
        err = request.query.get("err", "")

        discord_filter = (request.query.get("discord") or "any").lower()
        if discord_filter not in {"any", "yes", "no", "linked"}:
            discord_filter = "any"

        total_count = len(items)

        now = datetime.now(timezone.utc)

        def _parse_dt(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            try:
                dt = datetime.fromisoformat(value)
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        rows: List[str] = []
        filtered_count = 0
        for st in items:
            login = st.get("twitch_login", "")
            login_html = html.escape(login)
            permanent = bool(st.get("manual_verified_permanent"))
            until_raw = st.get("manual_verified_until")
            until_dt = _parse_dt(until_raw)
            verified_at_dt = _parse_dt(st.get("manual_verified_at"))

            is_on_discord = bool(st.get("is_on_discord"))
            discord_user_id = str(st.get("discord_user_id") or "").strip()
            discord_display_name = str(st.get("discord_display_name") or "").strip()
            has_discord_data = bool(discord_user_id or discord_display_name)

            if discord_filter == "yes" and not is_on_discord:
                continue
            if discord_filter == "no" and is_on_discord:
                continue
            if discord_filter == "linked" and not has_discord_data:
                continue

            filtered_count += 1

            status_badge = "<span class='badge badge-neutral'>Nicht verifiziert</span>"
            meta_parts: List[str] = []
            countdown_label = "—"
            countdown_classes: List[str] = []

            if permanent:
                status_badge = "<span class='badge badge-ok'>Dauerhaft verifiziert</span>"
            elif until_dt:
                day_diff = (until_dt.date() - now.date()).days
                if day_diff >= 0:
                    status_badge = "<span class='badge badge-ok'>Verifiziert (30 Tage)</span>"
                    countdown_label = f"{day_diff} Tage"
                    countdown_classes.append("countdown-ok")
                    meta_parts.append(f"Bis {until_dt.date().isoformat()}")
                else:
                    status_badge = "<span class='badge badge-warn'>Verifizierung überfällig</span>"
                    countdown_label = f"Überfällig {abs(day_diff)} Tage"
                    countdown_classes.append("countdown-warn")
                    meta_parts.append(f"Abgelaufen am {until_dt.date().isoformat()}")

            if verified_at_dt:
                meta_parts.append(f"Bestätigt am {verified_at_dt.date().isoformat()}")

            meta_html = (
                f"<div class='status-meta'>{' • '.join(meta_parts)}</div>" if meta_parts else ""
            )

            countdown_html = html.escape(countdown_label)
            if countdown_classes:
                countdown_html = f"<span class='{' '.join(countdown_classes)}'>{countdown_html}</span>"

            missing_discord_id = not discord_user_id
            discord_warning = ""

            if missing_discord_id and (is_on_discord or has_discord_data):
                discord_icon = "⚠️"
                discord_label = "Discord nicht verknüpft"
                discord_warning = "Discord-ID fehlt – bitte verknüpfen."
            elif is_on_discord:
                discord_icon = "✅"
                discord_label = "Auf Discord"
            elif has_discord_data:
                discord_icon = "🟡"
                discord_label = "Discord-Daten vorhanden"
            else:
                discord_icon = "❌"
                discord_label = "Nicht verknüpft"

            discord_meta_parts: List[str] = []
            if discord_display_name:
                discord_meta_parts.append(html.escape(discord_display_name))
            if discord_user_id:
                discord_meta_parts.append(f"ID: {html.escape(discord_user_id)}")

            discord_html_parts = [
                "<div class='discord-status'>",
                f"  <div class='discord-icon'>{discord_icon} {html.escape(discord_label)}</div>",
            ]
            if discord_meta_parts:
                discord_html_parts.append(
                    f"  <div class='status-meta'>{' • '.join(discord_meta_parts)}</div>"
                )
            if discord_warning:
                discord_html_parts.append(
                    f"  <div class='discord-warning'>{html.escape(discord_warning)}</div>"
                )
            discord_html_parts.append("</div>")
            discord_html = "".join(discord_html_parts)

            escaped_login = html.escape(login, quote=True)
            escaped_user_id = html.escape(discord_user_id, quote=True)
            escaped_display = html.escape(discord_display_name, quote=True)
            member_checked = " checked" if is_on_discord else ""
            toggle_mode = "mark" if not is_on_discord else "unmark"
            toggle_label = (
                "Als Discord-Mitglied markieren"
                if not is_on_discord
                else "Markierung entfernen"
            )
            toggle_classes = "btn btn-small" if not is_on_discord else "btn btn-small btn-secondary"

            advanced_html = (
                "  <details class='advanced-details'>"
                "    <summary>Erweitert</summary>"
                "    <div class='advanced-content'>"
                "      <form method='post' action='/twitch/discord_link'>"
                f"        <input type='hidden' name='login' value='{escaped_login}' />"
                "        <div class='form-row'>"
                f"          <label>Discord User ID<input type='text' name='discord_user_id' value='{escaped_user_id}' placeholder='123456789012345678'></label>"
                f"          <label>Discord Anzeigename<input type='text' name='discord_display_name' value='{escaped_display}' placeholder='Discord-Name'></label>"
                "        </div>"
                "        <div class='checkbox-label'>"
                f"          <input type='checkbox' name='member_flag' value='1'{member_checked}>"
                "          <span>Auch als Discord-Mitglied markieren</span>"
                "        </div>"
                "        <div class='hint'>Discord-Mitglieder erhalten höhere Priorität beim Posten.</div>"
                "        <div class='action-stack'>"
                "          <button class='btn btn-small'>Speichern</button>"
                "          <a class='btn btn-small btn-secondary' href='/twitch?discord=linked'>Nur verknüpfte anzeigen</a>"
                "        </div>"
                "      </form>"
                "    </div>"
                "  </details>"
            )

            rows.append(
                "<tr>"
                f"  <td>{login_html}</td>"
                f"  <td>{discord_html}{advanced_html}</td>"
                f"  <td>{status_badge}{meta_html}</td>"
                f"  <td>{countdown_html}</td>"
                "  <td>"
                "    <div class='action-stack'>"
                "      <form method='post' action='/twitch/verify' class='inline'>"
                f"        <input type='hidden' name='login' value='{escaped_login}'>"
                "        <select name='mode'>"
                "          <option value='permanent'>Permanent</option>"
                "          <option value='30d'>30 Tage</option>"
                "          <option value='reset'>Reset</option>"
                "        </select>"
                "        <button class='btn btn-small'>Set</button>"
                "      </form>"
                "      <form method='post' action='/twitch/discord_flag' class='inline'>"
                f"        <input type='hidden' name='login' value='{escaped_login}'>"
                f"        <input type='hidden' name='mode' value='{toggle_mode}'>"
                f"        <button class='{toggle_classes}'>{html.escape(toggle_label)}</button>"
                "      </form>"
                "      <form method='post' action='/twitch/remove' class='inline'>"
                f"        <input type='hidden' name='login' value='{escaped_login}'>"
                "        <button class='btn btn-small btn-danger'>Remove</button>"
                "      </form>"
                "    </div>"
                "  </td>"
                "</tr>"
            )

        if not rows:
            rows.append("<tr><td colspan=5><i>Keine Streamer gefunden.</i></td></tr>")

        table_rows = "".join(rows)

        filter_options = [
            ("any", "Alle"),
            ("yes", "Nur Discord-Mitglieder"),
            ("no", "Nicht auf Discord"),
            ("linked", "Discord-Daten vorhanden"),
        ]

        filter_options_html = "".join(
            f"<option value='{html.escape(value, quote=True)}'{' selected' if discord_filter == value else ''}>{html.escape(label)}</option>"
            for value, label in filter_options
        )

        discord_link_card_html = (
            "<div class='card discord-link-card'>"
            "  <h2>Discord-Verknüpfung hinzufügen</h2>"
            "  <form method='post' action='/twitch/discord_link' class='discord-link-form'>"
            "    <div class='row'>"
            "      <label>"
            "        Twitch Login"
            "        <input type='text' name='login' placeholder='twitch_login' required>"
            "      </label>"
            "      <label>"
            "        Discord User ID"
            "        <input type='text' name='discord_user_id' placeholder='123456789012345678'>"
            "      </label>"
            "      <label>"
            "        Discord Anzeigename"
            "        <input type='text' name='discord_display_name' placeholder='Discord-Name'>"
            "      </label>"
            "      <label class='checkbox-label'>"
            "        <input type='checkbox' name='member_flag' value='1'>"
            "        <span>Als Discord-Mitglied markieren</span>"
            "      </label>"
            "      <button class='btn'>Speichern</button>"
            "    </div>"
            "    <div class='hint'>"
            "      Ohne Haken bleibt der Streamer ohne Partner-Markierung im Live-Panel, die Discord-Daten werden dennoch gespeichert."
            "    </div>"
            "  </form>"
            "</div>"
        )

        filter_card_html = (
            '<div class="card filter-card">'
            '  <form method="get" action="/twitch" class="row filter-row">'
            '    <label class="filter-label">Discord Status'
            f'      <select name="discord">{filter_options_html}</select>'
            '    </label>'
            '    <button class="btn btn-small btn-secondary">Filter anwenden</button>'
            '    <a class="btn btn-small btn-secondary" href="/twitch">Zurücksetzen</a>'
            '  </form>'
            f'  <div class="status-meta">Treffer: {filtered_count} / {total_count}</div>'
            '</div>'
        )

        body = f"""
<h1 style="margin:.2rem 0 1rem 0;">Deadlock Twitch Posting – Admin</h1>

<div class="card">
  <form method="get" action="/twitch/add_any" class="row">
    <div>
      <div>Twitch Login <i>oder</i> URL:</div>
      <input name="q" placeholder="earlysalty  |  https://twitch.tv/earlysalty" required>
      <div><small>Akzeptiert: @login, login, twitch.tv/login, auch URL-encoded.</small></div>
    </div>
    <div><button class="btn">Add</button></div>
  </form>

</div>

{discord_link_card_html}

{filter_card_html}

<table>
  <thead>
    <tr><th>Login</th><th>Discord</th><th>Verifizierung</th><th>Countdown</th><th>Aktionen</th></tr>
  </thead>
  <tbody>
    {table_rows}
  </tbody>
</table>
"""

        return web.Response(text=self._html(body, active="live", msg=msg, err=err), content_type="text/html")

    async def add_any(self, request: web.Request):
        """Flexible Variante: nimmt ?q= … oder ?login= … oder ?url= …"""
        self._require_token(request)
        raw = request.query.get("q") or request.query.get("login") or request.query.get("url") or ""
        try:
            msg = await self._do_add(raw)
            raise web.HTTPFound(location="/twitch?ok=" + quote_plus(msg))
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("dashboard add_any failed: %s", e)
            raise web.HTTPFound(location="/twitch?err=" + quote_plus("could not add (twitch api)"))

    async def add_url(self, request: web.Request):
        """Backward-compatible: nimmt ?url=… (kann jetzt auch Login enthalten)."""
        self._require_token(request)
        raw = request.query.get("url") or ""
        try:
            msg = await self._do_add(raw)
            raise web.HTTPFound(location="/twitch?ok=" + quote_plus(msg))
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("dashboard add_url failed: %s", e)
            raise web.HTTPFound(location="/twitch?err=" + quote_plus("could not add (twitch api)"))

    async def add_login(self, request: web.Request):
        """Pfad-Shortcut: /twitch/add_login/<login>"""
        self._require_token(request)
        raw = request.match_info.get("login", "")
        try:
            msg = await self._do_add(raw)
            raise web.HTTPFound(location="/twitch?ok=" + quote_plus(msg))
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("dashboard add_login failed: %s", e)
            raise web.HTTPFound(location="/twitch?err=" + quote_plus("could not add (twitch api)"))

    async def discord_flag(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        mode = (data.get("mode") or "").strip().lower()
        desired: Optional[bool]
        if mode in {"mark", "on", "enable", "1"}:
            desired = True
        elif mode in {"unmark", "off", "disable", "0"}:
            desired = False
        else:
            desired = None

        try:
            if desired is None:
                raise ValueError("Ungültiger Modus für Discord-Markierung")
            message = await self._discord_flag(login, desired)
            location = self._redirect_location(request, ok=message)
        except ValueError as exc:
            location = self._redirect_location(request, err=str(exc))
        except Exception as exc:
            log.exception("dashboard discord_flag failed: %s", exc)
            location = self._redirect_location(
                request, err="Discord-Markierung konnte nicht aktualisiert werden"
            )
        raise web.HTTPFound(location=location)

    async def discord_link(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        discord_user_id = (data.get("discord_user_id") or "").strip()
        discord_display_name = (data.get("discord_display_name") or "").strip()
        member_raw = (data.get("member_flag") or "").strip().lower()
        mark_member = member_raw in {"1", "true", "on", "yes"}

        try:
            message = await self._discord_profile(
                login,
                discord_user_id=discord_user_id or None,
                discord_display_name=discord_display_name or None,
                mark_member=mark_member,
            )
            location = self._redirect_location(request, ok=message)
        except ValueError as exc:
            location = self._redirect_location(request, err=str(exc))
        except Exception as exc:
            log.exception("dashboard discord_link failed: %s", exc)
            location = self._redirect_location(
                request, err="Discord-Daten konnten nicht gespeichert werden"
            )
        raise web.HTTPFound(location=location)

    async def remove(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        try:
            msg = await self._remove(login)
            message = msg or f"{login} removed"
            location = self._redirect_location(request, ok=message)
            raise web.HTTPFound(location=location)
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("dashboard remove failed: %s", e)
            location = self._redirect_location(request, err="could not remove")
            raise web.HTTPFound(location=location)

    async def verify(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        mode = (data.get("mode") or "").strip().lower()
        try:
            msg = await self._verify(login, mode)
            message = msg or f"verify {mode} for {login}"
            location = self._redirect_location(request, ok=message)
            raise web.HTTPFound(location=location)
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("dashboard verify failed: %s", e)
            location = self._redirect_location(
                request, err="Verifizierung fehlgeschlagen"
            )
            raise web.HTTPFound(location=location)


__all__ = ["DashboardLiveMixin"]
