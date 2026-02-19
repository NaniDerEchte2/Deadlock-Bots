"""Live dashboard views and actions for managing Twitch streamers."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import quote_plus

from aiohttp import web

from ..constants import log
from .. import storage as _storage

# Alle Scopes die ein vollständig autorisierter Streamer haben sollte
_REQUIRED_SCOPES: list[str] = [
    "channel:manage:raids",
    "moderator:read:followers",
    "moderator:manage:banned_users",
    "moderator:manage:chat_messages",
    "channel:read:subscriptions",
    "analytics:read:games",
    "channel:manage:moderators",
    "channel:bot",
    "chat:read",
    "chat:edit",
    "clips:edit",
    "channel:read:ads",
    "bits:read",
    "channel:read:hype_train",
    "moderator:read:chatters",
    "moderator:manage:shoutouts",
    "channel:read:redemptions",
]

_SCOPE_COLUMN_LABELS: dict[str, str] = {
    "channel:manage:raids": "Raids",
    "moderator:read:followers": "Follower",
    "moderator:manage:banned_users": "Bans",
    "moderator:manage:chat_messages": "Chat Mod",
    "channel:read:subscriptions": "Subs",
    "analytics:read:games": "Analytics",
    "channel:manage:moderators": "Mods",
    "channel:bot": "Bot",
    "chat:read": "Chat Read",
    "chat:edit": "Chat Edit",
    "clips:edit": "Clips",
    "channel:read:ads": "Ads",
    "bits:read": "Bits",
    "channel:read:hype_train": "Hype",
    "moderator:read:chatters": "Chatters",
    "moderator:manage:shoutouts": "Shoutouts",
    "channel:read:redemptions": "Points",
}

# Scopes die besonders wichtig für Analytics/Lurker-Tracking sind
_CRITICAL_SCOPES: set[str] = {
    "moderator:read:chatters",   # Lurker-Tracking via Chatters API
    "channel:read:redemptions",  # Channel Point Redemptions
    "bits:read",                 # Bits Events
    "channel:read:hype_train",   # Hype Train Events
    "channel:read:subscriptions", # Sub Events
}


class DashboardLiveMixin:
    async def index(self, request: web.Request):
        self._require_token(request)
        items = await self._list()

        msg = request.query.get("ok", "")
        err = request.query.get("err", "")

        discord_filter = (request.query.get("discord") or "any").lower()
        if discord_filter not in {"any", "yes", "no", "linked"}:
            discord_filter = "any"

        total_count = sum(
            1
            for st in items
            if not bool(st.get("manual_partner_opt_out")) and not bool(st.get("archived_at"))
        )
        raid_bot_available = bool(getattr(self, "_raid_bot", None))
        token_value = ""
        if not self._noauth and self._token:
            token_value = self._token
        token_query = f"&token={quote_plus(token_value)}" if token_value else ""

        def raid_auth_link(login: str) -> str:
            return f"/twitch/raid/auth?login={quote_plus(login)}{token_query}"

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
        archived_entries: List[dict] = []
        filtered_count = 0
        raid_authorized_count = 0
        raid_ready_count = 0
        raid_missing_logins: List[str] = []
        for st in items:
            login = st.get("twitch_login", "")
            login_html = html.escape(login)
            permanent = bool(st.get("manual_verified_permanent"))
            until_raw = st.get("manual_verified_until")
            until_dt = _parse_dt(until_raw)
            verified_at_dt = _parse_dt(st.get("manual_verified_at"))
            archived_at_raw = st.get("archived_at")
            archived_dt = _parse_dt(archived_at_raw)
            is_archived = archived_dt is not None
            last_deadlock_dt = _parse_dt(st.get("last_deadlock_stream_at"))
            inactive_days: Optional[int] = None
            if last_deadlock_dt:
                inactive_days = (now.date() - last_deadlock_dt.date()).days
            partner_opt_out = bool(st.get("manual_partner_opt_out"))
            raid_auth_enabled = st.get("raid_auth_enabled")
            raid_authorized_at = st.get("raid_authorized_at")
            raid_bot_enabled = bool(st.get("raid_bot_enabled"))
            raid_is_authorized = raid_auth_enabled is not None or bool(raid_authorized_at)
            raid_auto_active = bool(raid_auth_enabled) and raid_bot_enabled

            if not partner_opt_out and not is_archived:
                if raid_is_authorized:
                    raid_authorized_count += 1
                else:
                    if login:
                        raid_missing_logins.append(login)
                if raid_auto_active:
                    raid_ready_count += 1

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

            if last_deadlock_dt:
                days_since = inactive_days or 0
                meta_parts.append(
                    f"Letzter Deadlock-Stream: {last_deadlock_dt.date().isoformat()} ({days_since} Tage her)"
                )
            else:
                meta_parts.append("Noch kein Deadlock-Stream erfasst")

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

            archived_at_label = archived_dt.date().isoformat() if archived_dt else (archived_at_raw or "—")
            last_stream_label = last_deadlock_dt.date().isoformat() if last_deadlock_dt else "—"

            if is_archived:
                raid_status = (
                    "OAuth fehlt"
                    if not raid_is_authorized
                    else ("Auto-Raid aktiv" if raid_auto_active else "Autorisiert")
                )
                archived_entries.append(
                    {
                        "login": login,
                        "status": status_text,
                        "status_badge": status_badge,
                        "archived_at": archived_at_label,
                        "last_stream": last_stream_label,
                        "inactive_days": inactive_days,
                        "raid_status": raid_status,
                        "meta": list(meta_parts),
                        "escaped_login": escaped_login,
                    }
                )
                continue

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

            raid_cell_parts: List[str] = []
            if raid_bot_available:
                if raid_is_authorized:
                    badge_class = "badge-ok" if raid_auto_active else "badge-neutral"
                    badge_label = "Bereit" if raid_auto_active else "Autorisiert"
                    raid_cell_parts.append(f"<span class='badge {badge_class}'>{badge_label}</span>")
                    if raid_auto_active:
                        raid_cell_parts.append("<div class='status-meta'>Auto-Raid aktiv</div>")
                    else:
                        raid_cell_parts.append("<div class='status-meta'>Auto-Raid aus</div>")
                else:
                    raid_cell_parts.append("<span class='badge badge-warn'>Nicht autorisiert</span>")
                    raid_cell_parts.append("<div class='status-meta'>OAuth fehlt</div>")
                    raid_link = html.escape(raid_auth_link(login), quote=True)
                    requirements_link = html.escape(
                        f"/twitch/raid/requirements?login={escaped_login}{token_query}", quote=True
                    )
                    raid_cell_parts.append(
                        "<a class='btn btn-small btn-secondary' href='"
                        + raid_link
                        + "'>Autorisieren</a>"
                    )
                    raid_cell_parts.append(
                        "<a class='btn btn-small' href='"
                        + requirements_link
                        + "' data-same-tab='1'>Anforderungen senden</a>"
                    )
            else:
                raid_cell_parts.append("<span class='badge badge-warn'>Bot offline</span>")
                raid_cell_parts.append("<div class='status-meta'>Raids nicht verfügbar</div>")

            raid_cell_html = "<div class='raid-cell'>" + "".join(raid_cell_parts) + "</div>"

            rows.append(
                "<tr>"
                f"  <td>{login_html}</td>"
                f"  <td>{discord_html}{advanced_html}</td>"
                f"  <td>{status_badge}{meta_html}</td>"
                f"  <td>{countdown_html}</td>"
                f"  <td>{raid_cell_html}</td>"
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
                "      <form method='post' action='/twitch/archive' class='inline'>"
                f"        <input type='hidden' name='login' value='{escaped_login}'>"
                "        <input type='hidden' name='mode' value='archive'>"
                "        <button class='btn btn-small btn-secondary'>Archivieren</button>"
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
            rows.append("<tr><td colspan=6><i>Keine Streamer gefunden.</i></td></tr>")

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

        if archived_entries:
            archived_rows: List[str] = []
            for entry in archived_entries:
                inactive_badge = ""
                if entry.get("inactive_days") is not None:
                    inactive_badge = (
                        f"<span class='badge badge-neutral'>{entry['inactive_days']} Tage inaktiv</span>"
                    )
                meta_line = ""
                if entry.get("meta"):
                    meta_line = (
                        "    <span><span class='meta-label'>Info</span><span>"
                        + " • ".join(html.escape(m) for m in entry["meta"])
                        + "</span></span>"
                    )
                raid_line = ""
                if entry.get("raid_status"):
                    raid_line = (
                        "    <span><span class='meta-label'>Raid</span><span>"
                        + html.escape(entry["raid_status"])
                        + "</span></span>"
                    )

                archived_rows.append(
                    "<li class='non-partner-item archived-item'>"
                    "  <div class='non-partner-header'>"
                    f"    <strong>{html.escape(entry['login'])}</strong>"
                    "    <div class='non-partner-badges'>"
                    f"      {entry.get('status_badge', '')}"
                    f"      {inactive_badge}"
                    "    </div>"
                    "  </div>"
                    "  <div class='non-partner-meta'>"
                    f"    <span><span class='meta-label'>Archiviert</span><span>{html.escape(entry.get('archived_at') or '—')}</span></span>"
                    f"    <span><span class='meta-label'>Letzter Stream</span><span>{html.escape(entry.get('last_stream') or '—')}</span></span>"
                    f"{raid_line}"
                    f"{meta_line}"
                    "  </div>"
                    "  <div class='non-partner-actions'>"
                    "    <form method='post' action='/twitch/archive' class='inline'>"
                    f"      <input type='hidden' name='login' value='{entry['escaped_login']}'>"
                    "      <input type='hidden' name='mode' value='unarchive'>"
                    "      <button class='btn btn-small'>Reaktivieren</button>"
                    "    </form>"
                    "    <form method='post' action='/twitch/remove' class='inline'>"
                    f"      <input type='hidden' name='login' value='{entry['escaped_login']}'>"
                    "      <button class='btn btn-small btn-danger'>Streamer entfernen</button>"
                    "    </form>"
                    "  </div>"
                    "</li>"
                )
            archived_list_html = "".join(archived_rows)
        else:
            archived_list_html = (
                "<li class='non-partner-item archived-item'><span class='non-partner-meta'>Keine archivierten Streamer.</span></li>"
            )

        non_partner_card_html = (
            "<div class='card non-partner-card'>"
            "  <h2>Keine Partner</h2>"
            "  <p>Streamer, die ausdrücklich als „Kein Partner“ markiert wurden. Sie tauchen nicht in der Hauptliste auf, können aber hier samt Discord-Verknüpfung weiterverwaltet werden.</p>"
            f"  <ul class='non-partner-list'>{non_partner_list_html}</ul>"
            "</div>"
        )

        archived_card_html = (
            "<div class='card non-partner-card archived-card'>"
            "  <h2>Archivierte Streamer</h2>"
            "  <p>Automatisch nach 10+ Tagen Inaktivität. Bei neuem Stream werden sie automatisch reaktiviert.</p>"
            f"  <ul class='non-partner-list'>{archived_list_html}</ul>"
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

        unique_logins = sorted(
            {
                (st.get("twitch_login") or "").strip()
                for st in items
                if (st.get("twitch_login") or "").strip()
            }
        )
        login_options_html = "".join(
            f"<option value='{html.escape(login, quote=True)}'></option>" for login in unique_logins
        )
        token_input = ""
        if token_value:
            token_input = (
                f"<input type='hidden' name='token' value='{html.escape(token_value, quote=True)}'>"
            )
        missing_unique = sorted({login for login in raid_missing_logins if login})
        missing_preview = missing_unique[:6]
        missing_count = max(0, total_count - raid_authorized_count)
        if missing_preview:
            missing_chips = "".join(
                f"<span class='chip'>{html.escape(login)}</span>" for login in missing_preview
            )
            raid_missing_html = (
                "<div class='raid-meta'>"
                f"<span class='pill warn'>Fehlende Autorisierung: {missing_count}</span>"
                f"{missing_chips}"
                "</div>"
            )
        else:
            raid_missing_html = "<div class='raid-meta'><span class='pill ok'>Alle Partner autorisiert</span></div>"

        raid_bot_state_label = "Verfügbar" if raid_bot_available else "Nicht aktiv"
        raid_bot_state_class = "ok" if raid_bot_available else "warn"
        redirect_label = html.escape(self._redirect_uri or "—")
        raid_form_disabled_attr = " disabled" if not raid_bot_available else ""
        raid_form_note = ""
        if not raid_bot_available:
            raid_form_note = (
                "<div class='status-meta'>Raid Bot ist nicht aktiv. Autorisierung ist derzeit nicht möglich.</div>"
            )

        raid_auth_card_html = (
            "<div class='card raid-auth-card'>"
            "  <div class='card-header'>"
            "    <div>"
            "      <p class='eyebrow'>Raid Bot</p>"
            "      <h2>Twitch Bot Autorisierung</h2>"
            "      <p class='lead'>Einmal autorisieren: Auto-Raid, Chat-Schutz und Discord Auto-Post (mit Cooldowns).</p>"
            "    </div>"
            "    <div class='raid-metrics'>"
            f"      <div class='mini-stat'><strong>{raid_authorized_count}</strong><span>Autorisiert</span></div>"
            f"      <div class='mini-stat'><strong>{missing_count}</strong><span>Fehlt</span></div>"
            f"      <div class='mini-stat'><strong>{raid_ready_count}</strong><span>Auto-Raid aktiv</span></div>"
            "    </div>"
            "  </div>"
            "  <form class='raid-form' method='get' action='/twitch/raid/auth' target='_blank'>"
            "    <label>Streamer Login"
            f"      <input type='text' name='login' list='raid-login-list' placeholder='earlysalty' required{raid_form_disabled_attr}>"
            "    </label>"
            f"    {token_input}"
            f"    <button class='btn'{raid_form_disabled_attr}>OAuth Link erzeugen</button>"
            "  </form>"
            f"  {raid_form_note}"
            f"  <datalist id='raid-login-list'>{login_options_html}</datalist>"
            "  <div class='raid-meta'>"
            f"    <span class='pill'>Redirect: {redirect_label}</span>"
            f"    <span class='pill {raid_bot_state_class}'>Raid Bot: {raid_bot_state_label}</span>"
            "    <span class='pill'>Aktivieren im Chat: !raid_enable</span>"
            "  </div>"
            f"  {raid_missing_html}"
            "</div>"
        )

        # --- Scope Status Card ---
        scope_rows: List[str] = []
        scope_headers_html = "".join(
            (
                f"<th class='scope-header' title='{html.escape(scope, quote=True)}'>"
                f"{html.escape(_SCOPE_COLUMN_LABELS.get(scope, scope))}"
                "</th>"
            )
            for scope in _REQUIRED_SCOPES
        )
        scope_table_colspan = 2 + len(_REQUIRED_SCOPES)
        total_authorized = 0
        full_scope_count = 0
        try:
            with _storage.get_conn() as _sc:
                auth_rows = _sc.execute(
                    "SELECT twitch_login, scopes, needs_reauth FROM twitch_raid_auth ORDER BY twitch_login"
                ).fetchall()
            for auth_row in auth_rows:
                total_authorized += 1
                _login = str(auth_row[0] if not hasattr(auth_row, "keys") else auth_row["twitch_login"])
                _scopes_raw = str(auth_row[1] if not hasattr(auth_row, "keys") else auth_row["scopes"] or "")
                _needs_reauth = bool(auth_row[2] if not hasattr(auth_row, "keys") else auth_row["needs_reauth"])
                _token_scopes = set(_scopes_raw.split()) if _scopes_raw else set()
                _missing = [s for s in _REQUIRED_SCOPES if s not in _token_scopes]
                _missing_critical = [s for s in _missing if s in _CRITICAL_SCOPES]

                if _needs_reauth:
                    row_class = "scope-row scope-reauth"
                    status_pill = "<span class='pill err'>Re-Auth nötig</span>"
                elif _missing_critical:
                    row_class = "scope-row scope-critical"
                    status_pill = "<span class='pill warn'>Kritisch unvollständig</span>"
                elif _missing:
                    row_class = "scope-row scope-partial"
                    status_pill = "<span class='pill neutral'>Unvollständig</span>"
                else:
                    row_class = "scope-row scope-full"
                    status_pill = "<span class='pill ok'>Vollständig</span>"
                    full_scope_count += 1

                scope_cells_html = "".join(
                    (
                        f"<td class='scope-check {'yes' if scope in _token_scopes else 'no'}' "
                        f"title='{html.escape(scope, quote=True)}'>"
                        f"{'☑' if scope in _token_scopes else '☐'}"
                        "</td>"
                    )
                    for scope in _REQUIRED_SCOPES
                )

                scope_rows.append(
                    f"<tr class='{row_class}'>"
                    f"  <td><strong>{html.escape(_login)}</strong></td>"
                    f"  <td>{status_pill}</td>"
                    f"  {scope_cells_html}"
                    f"</tr>"
                )
        except Exception:
            log.debug("Scope-Status Card: DB-Fehler", exc_info=True)
            scope_rows = [f"<tr><td colspan='{scope_table_colspan}'>Fehler beim Laden der Scope-Daten</td></tr>"]

        missing_scope_count = total_authorized - full_scope_count
        scope_summary_pills = (
            f"<span class='pill ok'>{full_scope_count} vollständig</span>"
            f"<span class='pill warn'>{missing_scope_count} unvollständig</span>"
        )
        scope_empty_row_html = (
            f"<tr><td colspan='{scope_table_colspan}'>Kein Streamer mit OAuth autorisiert</td></tr>"
        )
        scope_body_html = "".join(scope_rows) if scope_rows else scope_empty_row_html

        scope_card_html = (
            "<div class='card scope-card'>"
            "  <div class='card-header'>"
            "    <div>"
            "      <p class='eyebrow'>OAuth Token Scopes</p>"
            "      <h2>Scope-Status pro Streamer</h2>"
            "      <p class='lead'>Zeigt welche Streamer alle erforderlichen Scopes autorisiert haben – besonders wichtig für Lurker-Tracking (<code>moderator:read:chatters</code>) und Channel Points (<code>channel:read:redemptions</code>).</p>"
            "    </div>"
            "    <div class='raid-metrics'>"
            f"      <div class='mini-stat'><strong>{total_authorized}</strong><span>Mit OAuth</span></div>"
            f"      <div class='mini-stat'><strong>{full_scope_count}</strong><span>Vollständig</span></div>"
            f"      <div class='mini-stat'><strong>{missing_scope_count}</strong><span>Unvollständig</span></div>"
            "    </div>"
            "  </div>"
            "  <div class='status-meta' style='margin-bottom:.8rem;'>"
            f"    {scope_summary_pills}"
            "    <span class='pill neutral'>Re-Auth Link → Raid Bot Autorisierung</span>"
            "  </div>"
            "  <div class='table-wrap'>"
            "  <table>"
            "    <thead><tr>"
            "      <th>Streamer</th>"
            "      <th>Status</th>"
            f"      {scope_headers_html}"
            "    </tr></thead>"
            f"    <tbody>{scope_body_html}</tbody>"
            "  </table>"
            "  </div>"
            "</div>"
        )

        raid_history_link = ""
        if raid_bot_available:
            history_href = "/twitch/raid/history"
            if token_value:
                history_href = history_href + "?token=" + quote_plus(token_value)
            raid_history_link = (
                "<a class='btn btn-secondary' href='"
                + html.escape(history_href, quote=True)
                    + "' target='_blank' rel='noopener'>Raid History</a>"
            )

        reload_token_input = ""
        if token_value:
            reload_token_input = (
                f"<input type='hidden' name='token' value='{html.escape(token_value, quote=True)}'>"
            )

        hero_actions = (
            (raid_history_link + " " if raid_history_link else "")
            + "<form method='post' action='/twitch/reload'>"
            + reload_token_input
            + "<button class='btn btn-warn'>Reload Twitch Cog</button>"
            + "</form>"
        )

        hero_html = (
            "<header class='hero'>"
            "  <div>"
            "    <p class='eyebrow'>Twitch Admin</p>"
            "    <h1>Deadlock Twitch Ops</h1>"
            "    <p class='lead'>Live-Partner verwalten, Discord-Verknüpfungen prüfen und Raid-Bot autorisieren.</p>"
            "  </div>"
            f"  <div class='hero-actions'>{hero_actions}</div>"
            "</header>"
        )

        table_html = f"""
<div class="card table-card" style="margin-top: 1.4rem;">
  <div class="table-wrap">
    <table>
      <thead>
        <tr><th>Login</th><th>Discord</th><th>Verifizierung</th><th>Countdown</th><th>Raid Bot</th><th>Aktionen</th></tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
  </div>
</div>
"""

        body = f"""
{hero_html}

<div class="panel-grid">
  {add_streamer_card_html}
  {raid_auth_card_html}
  {filter_card_html}
</div>

{table_html}

{scope_card_html}

{archived_card_html}
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

    async def archive(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        mode = (data.get("mode") or "").strip().lower() or "toggle"
        try:
            msg = await self._archive(login, mode)
            location = self._redirect_location(request, ok=msg)
        except ValueError as exc:
            location = self._redirect_location(request, err=str(exc))
        except Exception as exc:
            log.exception("dashboard archive failed: %s", exc)
            location = self._redirect_location(
                request, err="Archivierung fehlgeschlagen"
            )
        raise web.HTTPFound(location=location)


__all__ = ["DashboardLiveMixin"]
