"""Dashboard helpers for the Twitch cog."""

from __future__ import annotations

import sqlite3
from typing import List, Optional

import discord

from aiohttp import web

from . import storage
from .dashboard import Dashboard
from .logger import log


VERIFICATION_SUCCESS_DM_MESSAGE = (
    "🎉 Glückwunsch! Du wurdest erfolgreich als **Streamer-Partner** verifiziert und bist jetzt offiziell Teil des "
    "Streamer-Teams. Wir melden uns, falls wir noch Fragen haben – ansonsten schauen wir uns deine Angaben kurz an. "
    "Bei Fragen kannst du dich gerne hier melden: https://discord.com/channels/1289721245281292288/1428062025145385111"
)


class TwitchDashboardMixin:
    """Expose the aiohttp dashboard endpoints."""

    async def _dashboard_add(self, login: str, require_link: bool) -> str:
        return await self._cmd_add(login, require_link)

    async def _dashboard_remove(self, login: str) -> str:
        return await self._cmd_remove(login)

    async def _dashboard_list(self):
        with storage.get_conn() as c:
            c.execute(
                """
                UPDATE twitch_streamers
                   SET is_on_discord=1
                 WHERE is_on_discord=0
                   AND (
                        manual_verified_permanent=1
                     OR manual_verified_until IS NOT NULL
                     OR manual_verified_at IS NOT NULL
                   )
                """
            )
            rows = c.execute(
                """
                SELECT twitch_login,
                       manual_verified_permanent,
                       manual_verified_until,
                       manual_verified_at,
                       manual_partner_opt_out,
                       is_on_discord,
                       discord_user_id,
                       discord_display_name
                  FROM twitch_streamers
                 ORDER BY twitch_login
                """
            ).fetchall()
        return [dict(row) for row in rows]

    async def _dashboard_set_discord_flag(self, login: str, is_on_discord: bool) -> str:
        normalized = self._normalize_login(login)
        if not normalized:
            raise ValueError("Ungültiger Login")

        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT twitch_login FROM twitch_streamers WHERE twitch_login=?",
                (normalized,),
            ).fetchone()
            if not row:
                raise ValueError(f"{normalized} ist nicht gespeichert")

            conn.execute(
                "UPDATE twitch_streamers SET is_on_discord=? WHERE twitch_login=?",
                (1 if is_on_discord else 0, normalized),
            )

        if is_on_discord:
            return f"{normalized} als Discord-Mitglied markiert"
        return f"Discord-Markierung für {normalized} entfernt"

    async def _dashboard_save_discord_profile(
        self,
        login: str,
        *,
        discord_user_id: Optional[str],
        discord_display_name: Optional[str],
        mark_member: bool,
    ) -> str:
        normalized = self._normalize_login(login)
        if not normalized:
            raise ValueError("Ungültiger Login")

        discord_id_clean = (discord_user_id or "").strip()
        if discord_id_clean and not discord_id_clean.isdigit():
            raise ValueError("Discord-ID muss eine Zahl sein")

        display_name_clean = (discord_display_name or "").strip()
        if len(display_name_clean) > 120:
            display_name_clean = display_name_clean[:120]

        try:
            with storage.get_conn() as conn:
                row = conn.execute(
                    "SELECT twitch_login FROM twitch_streamers WHERE twitch_login=?",
                    (normalized,),
                ).fetchone()

                if row:
                    conn.execute(
                        "UPDATE twitch_streamers "
                        "SET discord_user_id=?, discord_display_name=?, is_on_discord=? "
                        "WHERE twitch_login=?",
                        (
                            discord_id_clean or None,
                            display_name_clean or None,
                            1 if mark_member else 0,
                            normalized,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO twitch_streamers "
                        "(twitch_login, discord_user_id, discord_display_name, is_on_discord) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            normalized,
                            discord_id_clean or None,
                            display_name_clean or None,
                            1 if mark_member else 0,
                        ),
                    )
        except sqlite3.IntegrityError:
            raise ValueError("Discord-ID wird bereits verwendet")

        return f"Discord-Daten für {normalized} aktualisiert"

    async def _dashboard_stats(
        self,
        *,
        hour_from: Optional[int] = None,
        hour_to: Optional[int] = None,
        streamer: Optional[str] = None,
    ) -> dict:
        stats = await self._compute_stats(
            hour_from=hour_from,
            hour_to=hour_to,
            streamer=streamer,
        )
        tracked_top = stats.get("tracked", {}).get("top", []) or []
        category_top = stats.get("category", {}).get("top", []) or []

        def _agg(items: List[dict]):
            samples = sum(int(d.get("samples") or 0) for d in items)
            uniq = len(items)
            avg_over_streamers = (
                sum(float(d.get("avg_viewers") or 0.0) for d in items) / float(uniq)
            ) if uniq else 0.0
            return samples, uniq, avg_over_streamers

        cat_samples, cat_uniq, cat_avg = _agg(category_top)
        tr_samples, tr_uniq, tr_avg = _agg(tracked_top)

        stats.setdefault("tracked", {})["samples"] = tr_samples
        stats["tracked"]["unique_streamers"] = tr_uniq
        stats.setdefault("category", {})["samples"] = cat_samples
        stats["category"]["unique_streamers"] = cat_uniq
        stats["avg_viewers_all"] = cat_avg
        stats["avg_viewers_tracked"] = tr_avg
        return stats

    async def _notify_verification_success(self, login: str, row_data: Optional[dict]) -> str:
        if not row_data:
            log.info("Keine Discord-Daten für %s zum Versenden der Erfolgsnachricht gefunden", login)
            return ""

        user_id_raw = row_data.get("discord_user_id")
        if not user_id_raw:
            log.info("Keine Discord-ID für %s hinterlegt – überspringe Erfolgsnachricht", login)
            return ""

        try:
            user_id_int = int(str(user_id_raw))
        except (TypeError, ValueError):
            log.warning("Ungültige Discord-ID %r für %s – keine Erfolgsnachricht", user_id_raw, login)
            return "(Discord-DM konnte nicht zugestellt werden)"

        user = self.bot.get_user(user_id_int)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id_int)
            except discord.NotFound:
                user = None
            except discord.HTTPException:
                log.exception("Konnte Discord-User %s nicht abrufen", user_id_int)
                user = None

        if user is None:
            log.warning("Discord-User %s (%s) konnte nicht gefunden werden", user_id_int, login)
            return "(Discord-DM konnte nicht zugestellt werden)"

        try:
            await user.send(VERIFICATION_SUCCESS_DM_MESSAGE)
        except discord.Forbidden:
            log.warning(
                "DM an %s (%s) wegen erfolgreicher Verifizierung blockiert", user_id_int, login
            )
            return "(Discord-DM konnte nicht zugestellt werden)"
        except discord.HTTPException:
            log.exception(
                "Konnte Erfolgsnachricht nach Verifizierung nicht an %s senden", user_id_int
            )
            return "(Discord-DM konnte nicht zugestellt werden)"

        log.info(
            "Verifizierungs-Erfolgsnachricht an %s (%s) gesendet", user_id_int, login
        )
        return ""

    async def _dashboard_verify(self, login: str, mode: str) -> str:
        login = self._normalize_login(login)
        if not login:
            return "Ungültiger Login"

        if mode in {"permanent", "temp"}:
            row_data = None
            should_notify = False
            with storage.get_conn() as c:
                row = c.execute(
                    (
                        "SELECT discord_user_id, discord_display_name, manual_verified_at "
                        "FROM twitch_streamers WHERE twitch_login=?"
                    ),
                    (login,),
                ).fetchone()
                if row:
                    row_data = dict(row)
                    should_notify = row_data.get("manual_verified_at") is None

                if mode == "permanent":
                    c.execute(
                        "UPDATE twitch_streamers "
                        "SET manual_verified_permanent=1, manual_verified_until=NULL, manual_verified_at=datetime('now'), "
                        "    manual_partner_opt_out=0, "
                        "    is_on_discord=1 "
                        "WHERE twitch_login=?",
                        (login,),
                    )
                    base_msg = f"{login} dauerhaft verifiziert"
                else:
                    c.execute(
                        "UPDATE twitch_streamers "
                        "SET manual_verified_permanent=0, manual_verified_until=datetime('now','+30 days'), "
                        "    manual_verified_at=datetime('now'), manual_partner_opt_out=0, is_on_discord=1 "
                        "WHERE twitch_login=?",
                        (login,),
                    )
                    base_msg = f"{login} für 30 Tage verifiziert"

            note = ""
            if should_notify:
                note = await self._notify_verification_success(login, row_data)
            return f"{base_msg} {note}".strip()

        if mode == "clear":
            with storage.get_conn() as c:
                c.execute(
                    "UPDATE twitch_streamers "
                    "SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL, "
                    "    manual_partner_opt_out=1 "
                    "WHERE twitch_login=?",
                    (login,),
                )

            # "Kein Partner" ist eine rein interne Markierung – es sollen hierbei keine DMs
            # ausgelöst werden. Wir geben daher eine entsprechend klare Rückmeldung aus,
            # damit Dashboard-Nutzer:innen wissen, dass keine Nachricht verschickt wurde.
            return f"Verifizierung für {login} zurückgesetzt (keine DM versendet)"

        if mode == "failed":
            row_data = None
            with storage.get_conn() as c:
                row = c.execute(
                    "SELECT discord_user_id, discord_display_name FROM twitch_streamers WHERE twitch_login=?",
                    (login,),
                ).fetchone()
                if row:
                    row_data = dict(row)
                    c.execute(
                        "UPDATE twitch_streamers "
                        "SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL, "
                        "    manual_partner_opt_out=0 "
                        "WHERE twitch_login=?",
                        (login,),
                    )

            if not row_data:
                return f"{login} ist nicht gespeichert"

            user_id_raw = row_data.get("discord_user_id")
            if not user_id_raw:
                return f"Keine Discord-ID für {login} hinterlegt"

            try:
                user_id_int = int(str(user_id_raw))
            except (TypeError, ValueError):
                return f"Ungültige Discord-ID für {login}"

            user = self.bot.get_user(user_id_int)
            if user is None:
                try:
                    user = await self.bot.fetch_user(user_id_int)
                except discord.NotFound:
                    user = None
                except discord.HTTPException:
                    log.exception("Konnte Discord-User %s nicht abrufen", user_id_int)
                    user = None

            if user is None:
                return f"Discord-User {user_id_int} konnte nicht gefunden werden"

            message = (
                "Hey! Deine Deadlock-Streamer-Verifizierung konnte leider nicht abgeschlossen werden. "
                "Du erfüllst aktuell nicht alle Voraussetzungen. Bitte prüfe die Anforderungen erneut "
                "und starte die Verifizierung anschließend mit /streamer noch einmal."
            )

            try:
                await user.send(message)
            except discord.Forbidden:
                log.warning("DM an %s (%s) wegen fehlgeschlagener Verifizierung blockiert", user_id_int, login)
                return (
                    f"Konnte {row_data.get('discord_display_name') or user.name} nicht per DM erreichen."
                )
            except discord.HTTPException:
                log.exception("Konnte Verifizierungsfehler-Nachricht nicht senden an %s", user_id_int)
                return "Nachricht konnte nicht gesendet werden"

            log.info("Verifizierungsfehler-Benachrichtigung an %s (%s) gesendet", user_id_int, login)
            return (
                f"{login}: Discord-User wurde über die fehlgeschlagene Verifizierung informiert"
            )
        return "Unbekannter Modus"

    async def _start_dashboard(self):
        try:
            app = Dashboard.build_app(
                noauth=self._dashboard_noauth,
                token=self._dashboard_token,
                partner_token=self._partner_dashboard_token,
                add_cb=self._dashboard_add,
                remove_cb=self._dashboard_remove,
                list_cb=self._dashboard_list,
                stats_cb=self._dashboard_stats,
                verify_cb=self._dashboard_verify,
                discord_flag_cb=self._dashboard_set_discord_flag,
                discord_profile_cb=self._dashboard_save_discord_profile,
            )
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host=self._dashboard_host, port=self._dashboard_port)
            await site.start()
            self._web = runner
            self._web_app = app
            log.info("Twitch dashboard running on http://%s:%s/twitch", self._dashboard_host, self._dashboard_port)
        except Exception:
            log.exception("Konnte Dashboard nicht starten")

    async def _stop_dashboard(self):
        if self._web:
            await self._web.cleanup()
            self._web = None
            self._web_app = None
