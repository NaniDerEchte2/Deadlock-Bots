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

        total_count = sum(1 for st in items if not bool(st.get("manual_partner_opt_out")))

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
        non_partner_entries: List[dict] = []
        filtered_count = 0
        for st in items:
            login = st.get("twitch_login", "")
            login_html = html.escape(login)
            permanent = bool(st.get("manual_verified_permanent"))
            until_raw = st.get("manual_verified_until")
            until_dt = _parse_dt(until_raw)
            verified_at_dt = _parse_dt(st.get("manual_verified_at"))
            partner_opt_out = bool(st.get("manual_partner_opt_out"))

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

            status_badge = "<span class='badge badge-neutral'>Nicht verifiziert</span>"
            status_text = "Nicht verifiziert"
            meta_parts: List[str] = []
            countdown_label = "—"
            countdown_classes: List[str] = []

            if partner_opt_out:
                status_badge = "<span class='badge badge-neutral'>Kein Partner</span>"
                status_text = "Kein Partner"
                meta_parts.append("Nicht als Partner gelistet")
            elif permanent:
                status_badge = "<span class='badge badge-ok'>Dauerhaft verifiziert</span>"
                status_text = "Dauerhaft verifiziert"
            elif until_dt:
                day_diff = (until_dt.date() - now.date()).days
                if day_diff >= 0:
                    status_badge = "<span class='badge badge-ok'>Verifiziert (30 Tage)</span>"
                    status_text = "Verifiziert (30 Tage)"
                    countdown_label = f"{day_diff} Tage"
                    countdown_classes.append("countdown-ok")
                    meta_parts.append(f"Bis {until_dt.date().isoformat()}")
                else:
                    status_badge = "<span class='badge badge-warn'>Verifizierung überfällig</span>"
                    status_text = "Verifizierung überfällig"
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

            discord_html_parts = [
                "<div class='discord-status'>",
                f"  <div class='discord-icon'>{discord_icon} {html.escape(discord_label)}</div>",
            ]
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
                else "Discord-Markierung entfernen"
            )
            toggle_classes = "btn btn-small" if not is_on_discord else "btn btn-small btn-secondary"

            is_current_partner = False
            if not partner_opt_out:
                is_current_partner = bool(permanent)
                if not is_current_partner and until_dt:
                    is_current_partner = until_dt >= now

            should_list_as_non_partner = partner_opt_out
            if should_list_as_non_partner:
                non_partner_entries.append(
                    {
                        "login": login,
                        "status": status_text,
                        "status_badge": status_badge,
                        "countdown": countdown_label,
                        "meta": list(meta_parts),
                        "discord_label": discord_label,
                        "discord_display_name": discord_display_name,
                        "discord_user_id": discord_user_id,
                        "warning": discord_warning,
                        "is_on_discord": is_on_discord,
                        "escaped_login": escaped_login,
                        "escaped_user_id": escaped_user_id,
                        "escaped_display": escaped_display,
                        "member_checked": member_checked,
                        "toggle_mode": toggle_mode,
                        "toggle_label": toggle_label,
                        "toggle_classes": toggle_classes,
                    }
                )
                continue

            discord_preview_rows: List[str] = []
            if discord_display_name:
                discord_preview_rows.append(
                    f"<span class='preview-label'>Name</span><span>{html.escape(discord_display_name)}</span>"
                )
            if discord_user_id:
                discord_preview_rows.append(
                    f"<span class='preview-label'>ID</span><span>{html.escape(discord_user_id)}</span>"
                )
            if not discord_preview_rows:
                discord_preview_rows.append(
                    "<span class='preview-empty'>Keine zusätzlichen Discord-Angaben hinterlegt.</span>"
                )

            discord_preview_html = "".join(
                f"<div class='discord-preview-row'>{row}</div>" for row in discord_preview_rows
            )

            advanced_html = (
                "  <details class='advanced-details'>"
                "    <summary>Discord verwalten</summary>"
                "    <div class='advanced-content'>"
                f"      <div class='discord-preview'>{discord_preview_html}</div>"
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
                "          <option value='temp'>30 Tage</option>"
                "          <option value='failed'>Verifizierung fehlgeschlagen</option>"
                "          <option value='clear'>Kein Partner</option>"
                "        </select>"
                "        <button class='btn btn-small'>Anwenden</button>"
                "      </form>"
                "      <form method='post' action='/twitch/discord_flag' class='inline'>"
                f"        <input type='hidden' name='login' value='{escaped_login}'>"
                f"        <input type='hidden' name='mode' value='{toggle_mode}'>"
                f"        <button class='{toggle_classes}'>{html.escape(toggle_label)}</button>"
                "      </form>"
                "      <form method='post' action='/twitch/remove' class='inline'>"
                f"        <input type='hidden' name='login' value='{escaped_login}'>"
                "        <button class='btn btn-small btn-danger'>Streamer entfernen</button>"
                "      </form>"
                "    </div>"
                "  </td>"
                "</tr>"
            )
            filtered_count += 1

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

        add_streamer_card_html = (
            "<div class='card add-streamer-card'>"
            "  <h2>Twitch Streamer hinzufügen</h2>"
            "  <form method='post' action='/twitch/add_streamer'>"
            "    <div class='form-grid'>"
            "      <label>"
            "        Twitch Login oder URL"
            "        <input type='text' name='login' placeholder='earlysalty  |  https://twitch.tv/earlysalty' required>"
            "      </label>"
            "      <label>"
            "        Discord User ID"
            "        <input type='text' name='discord_user_id' placeholder='123456789012345678'>"
            "      </label>"
            "      <label>"
            "        Discord Anzeigename"
            "        <input type='text' name='discord_display_name' placeholder='Discord-Name'>"
            "      </label>"
            "    </div>"
            "    <div class='form-actions'>"
            "      <label class='checkbox-label'>"
            "        <input type='checkbox' name='member_flag' value='1'>"
            "        <span>Als Discord-Mitglied markieren</span>"
            "      </label>"
            "      <button class='btn'>Speichern</button>"
            "    </div>"
            "    <div class='hint'>"
            "      Akzeptiert: @login, login, twitch.tv/login, auch URL-encoded. Discord-Angaben sind optional, können aber direkt mitgespeichert werden."
            "    </div>"
            "    <div class='hint'>"
            "      Ohne Haken bleibt der Streamer ohne Partner-Markierung im Live-Panel, die Discord-Daten werden dennoch gespeichert."
            "    </div>"
            "  </form>"
            "</div>"
        )

        if non_partner_entries:
            non_partner_rows: List[str] = []
            for entry in non_partner_entries:
                countdown_badge = ""
                countdown_label = entry.get("countdown") or ""
                if countdown_label and countdown_label != "—":
                    countdown_badge = (
                        f"<span class='badge badge-neutral'>{html.escape(countdown_label)}</span>"
                    )

                discord_details: List[str] = []
                if entry.get("discord_label"):
                    discord_details.append(entry["discord_label"])
                if entry.get("discord_display_name"):
                    discord_details.append(entry["discord_display_name"])
                if entry.get("discord_user_id"):
                    discord_details.append(f"ID: {entry['discord_user_id']}")

                discord_line = ""
                if discord_details:
                    discord_line = (
                        "    <span><span class='meta-label'>Discord</span><span>"
                        + " • ".join(html.escape(part) for part in discord_details)
                        + "</span></span>"
                    )

                info_lines = "".join(
                    f"    <span><span class='meta-label'>Info</span><span>{html.escape(meta)}</span></span>"
                    for meta in entry.get("meta") or []
                )

                warning_line = ""
                if entry.get("warning"):
                    warning_line = (
                        f"    <span class='non-partner-warning'>{html.escape(entry['warning'])}</span>"
                    )

                preview_rows: List[str] = []
                if entry.get("discord_display_name"):
                    preview_rows.append(
                        f"<span class='preview-label'>Name</span><span>{html.escape(entry['discord_display_name'])}</span>"
                    )
                if entry.get("discord_user_id"):
                    preview_rows.append(
                        f"<span class='preview-label'>ID</span><span>{html.escape(entry['discord_user_id'])}</span>"
                    )
                if not preview_rows:
                    preview_rows.append(
                        "<span class='preview-empty'>Keine zusätzlichen Discord-Angaben hinterlegt.</span>"
                    )
                preview_html = "".join(
                    f"<div class='discord-preview-row'>{row}</div>" for row in preview_rows
                )

                non_partner_rows.append(
                    "<li class='non-partner-item'>"
                    "  <div class='non-partner-header'>"
                    f"    <strong>{html.escape(entry['login'])}</strong>"
                    "    <div class='non-partner-badges'>"
                    f"      {entry.get('status_badge', '')}"
                    f"      {countdown_badge}"
                    "    </div>"
                    "  </div>"
                    "  <div class='non-partner-meta'>"
                    f"    <span><span class='meta-label'>Status</span><span>{html.escape(entry['status'])}</span></span>"
                    f"{discord_line}"
                    f"{info_lines}"
                    f"{warning_line}"
                    "  </div>"
                    "  <details class='non-partner-manage'>"
                    "    <summary>Verwaltung</summary>"
                    "    <div class='manage-body'>"
                    f"      <div class='discord-preview'>{preview_html}</div>"
                    "      <form method='post' action='/twitch/verify' class='inline'>"
                    f"        <input type='hidden' name='login' value='{entry['escaped_login']}'>"
                    "        <select name='mode'>"
                    "          <option value='permanent'>Permanent</option>"
                    "          <option value='temp'>30 Tage</option>"
                    "          <option value='failed'>Verifizierung fehlgeschlagen</option>"
                    "          <option value='clear'>Kein Partner</option>"
                    "        </select>"
                    "        <button class='btn btn-small'>Anwenden</button>"
                    "      </form>"
                    "      <form method='post' action='/twitch/discord_link'>"
                    f"        <input type='hidden' name='login' value='{entry['escaped_login']}' />"
                    "        <div class='form-row'>"
                    f"          <label>Discord User ID<input type='text' name='discord_user_id' value='{entry['escaped_user_id']}' placeholder='123456789012345678'></label>"
                    f"          <label>Discord Anzeigename<input type='text' name='discord_display_name' value='{entry['escaped_display']}' placeholder='Discord-Name'></label>"
                    "        </div>"
                    "        <div class='checkbox-label'>"
                    f"          <input type='checkbox' name='member_flag' value='1'{entry['member_checked']}>"
                    "          <span>Als Discord-Mitglied markieren</span>"
                    "        </div>"
                    "        <div class='hint'>Speichern aktualisiert die Discord-Angaben.</div>"
                    "        <div class='non-partner-actions'>"
                    "          <button class='btn btn-small'>Speichern</button>"
                    "          <a class='btn btn-small btn-secondary' href='/twitch?discord=linked'>Nur verknüpfte anzeigen</a>"
                    "        </div>"
                    "      </form>"
                    "      <div class='non-partner-actions'>"
                    "        <form method='post' action='/twitch/discord_flag' class='inline'>"
                    f"          <input type='hidden' name='login' value='{entry['escaped_login']}'>"
                    f"          <input type='hidden' name='mode' value='{entry['toggle_mode']}'>"
                    f"          <button class='{entry['toggle_classes']}'>{html.escape(entry['toggle_label'])}</button>"
                    "        </form>"
                    "        <form method='post' action='/twitch/remove' class='inline'>"
                    f"          <input type='hidden' name='login' value='{entry['escaped_login']}'>"
                    "          <button class='btn btn-small btn-danger'>Streamer entfernen</button>"
                    "        </form>"
                    "      </div>"
                    "      <p class='non-partner-note'>Aktionen verschieben den Streamer bei Bedarf zurück in die Hauptliste.</p>"
                    "    </div>"
                    "  </details>"
                    "</li>"
                )
            non_partner_list_html = "".join(non_partner_rows)
        else:
            non_partner_list_html = (
                "<li class='non-partner-item'><span class='non-partner-meta'>Keine zusätzlichen Streamer ohne Partner-Status vorhanden.</span></li>"
            )

        non_partner_card_html = (
            "<div class='card non-partner-card'>"
            "  <h2>Keine Partner</h2>"
            "  <p>Streamer, die ausdrücklich als „Kein Partner“ markiert wurden. Sie tauchen nicht in der Hauptliste auf, können aber hier samt Discord-Verknüpfung weiterverwaltet werden.</p>"
            f"  <ul class='non-partner-list'>{non_partner_list_html}</ul>"
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

{add_streamer_card_html}

{filter_card_html}

<table>
  <thead>
    <tr><th>Login</th><th>Discord</th><th>Verifizierung</th><th>Countdown</th><th>Aktionen</th></tr>
  </thead>
  <tbody>
    {table_rows}
  </tbody>
</table>

{non_partner_card_html}
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

    async def add_streamer(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        raw_login = (data.get("login") or "").strip()
        discord_user_id = (data.get("discord_user_id") or "").strip()
        discord_display_name = (data.get("discord_display_name") or "").strip()
        member_raw = (data.get("member_flag") or "").strip().lower()
        mark_member = member_raw in {"1", "true", "on", "yes"}

        if not raw_login:
            location = self._redirect_location(request, err="Bitte einen Twitch-Login angeben")
            raise web.HTTPFound(location=location)

        try:
            add_message = await self._do_add(raw_login)
        except web.HTTPBadRequest as exc:
            err_text = exc.text or "Ungültiger Twitch-Login"
            location = self._redirect_location(request, err=err_text)
            raise web.HTTPFound(location=location)
        except Exception as exc:
            log.exception("dashboard add_streamer failed: %s", exc)
            location = self._redirect_location(
                request, err="Twitch-Streamer konnte nicht hinzugefügt werden"
            )
            raise web.HTTPFound(location=location)

        profile_message = ""
        should_update_discord = bool(discord_user_id or discord_display_name or mark_member)
        if should_update_discord:
            try:
                profile_message = await self._discord_profile(
                    raw_login,
                    discord_user_id=discord_user_id or None,
                    discord_display_name=discord_display_name or None,
                    mark_member=mark_member,
                )
            except ValueError as exc:
                location = self._redirect_location(request, err=str(exc))
                raise web.HTTPFound(location=location)
            except Exception as exc:
                log.exception("dashboard add_streamer discord save failed: %s", exc)
                location = self._redirect_location(
                    request, err="Discord-Daten konnten nicht gespeichert werden"
                )
                raise web.HTTPFound(location=location)

        messages = [m for m in (add_message, profile_message) if m]
        ok_message = " – ".join(dict.fromkeys(messages)) if messages else "Gespeichert"
        location = self._redirect_location(request, ok=ok_message)
        raise web.HTTPFound(location=location)

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
