"""Dashboard helpers for the Twitch cog."""

from __future__ import annotations

from typing import List

from aiohttp import web

from . import storage
from .dashboard import Dashboard
from .logger import log


class TwitchDashboardMixin:
    """Expose the aiohttp dashboard endpoints."""

    async def _dashboard_add(self, login: str, require_link: bool) -> str:
        return await self._cmd_add(login, require_link)

    async def _dashboard_remove(self, login: str) -> None:
        await self._cmd_remove(login)

    async def _dashboard_list(self):
        with storage.get_conn() as c:
            rows = c.execute(
                """
                SELECT twitch_login,
                       manual_verified_permanent,
                       manual_verified_until,
                       manual_verified_at
                  FROM twitch_streamers
                 ORDER BY twitch_login
                """
            ).fetchall()
        return [dict(row) for row in rows]

    async def _dashboard_stats(self) -> dict:
        stats = await self._compute_stats()
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

    async def _dashboard_export(self) -> dict:
        return await self._dashboard_stats()

    async def _dashboard_export_csv(self) -> str:
        stats = await self._compute_stats()
        items = stats.get("tracked", {}).get("top", []) or []
        lines = ["streamer,samples,avg_viewers,max_viewers,is_partner"]
        for data in items:
            streamer = str(data.get("streamer") or "")
            samples = int(data.get("samples") or 0)
            avgv = float(data.get("avg_viewers") or 0.0)
            peak = int(data.get("max_viewers") or 0)
            isp = 1 if data.get("is_partner") else 0
            lines.append(f"{streamer},{samples},{avgv:.3f},{peak},{isp}")
        return "\n".join(lines)

    async def _dashboard_verify(self, login: str, mode: str) -> str:
        login = self._normalize_login(login)
        if not login:
            return "Ungültiger Login"

        with storage.get_conn() as c:
            if mode == "permanent":
                c.execute(
                    "UPDATE twitch_streamers "
                    "SET manual_verified_permanent=1, manual_verified_until=NULL, manual_verified_at=datetime('now') "
                    "WHERE twitch_login=?",
                    (login,),
                )
                return f"{login} dauerhaft verifiziert"
            if mode == "temp":
                c.execute(
                    "UPDATE twitch_streamers "
                    "SET manual_verified_permanent=0, manual_verified_until=datetime('now','+30 days'), "
                    "    manual_verified_at=datetime('now') "
                    "WHERE twitch_login=?",
                    (login,),
                )
                return f"{login} für 30 Tage verifiziert"
            if mode == "clear":
                c.execute(
                    "UPDATE twitch_streamers "
                    "SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL "
                    "WHERE twitch_login=?",
                    (login,),
                )
                return f"Verifizierung für {login} zurückgesetzt"
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
                export_cb=self._dashboard_export,
                export_csv_cb=self._dashboard_export_csv,
                verify_cb=self._dashboard_verify,
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
